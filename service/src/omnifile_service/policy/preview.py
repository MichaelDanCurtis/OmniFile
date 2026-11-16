"""The policy editor's live preview: timeline, first-year cost and checks.

`POST /v1/policies/preview` (portal-api.md section 5.7) calls `preview()`.
Pure: no database and no clock. The Portal API reads the dataset, its shares
and the price table, and passes them in a `PreviewContext`.

An invalid body is not an exception: the result has ``valid = false`` and
error checks, an empty timeline, and the cost of keeping the data Hot.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..backends.base import Capabilities, LogicalTier
from .checks import Check, policy_checks, sort_checks
from .cost import (
    CostEstimate,
    PriceTable,
    account_for,
    first_year_cost,
    keep_hot_estimate,
    tier_info,
)
from .model import TIER_RANK, Mode, Policy, PolicyClock, StubAt

MIN_HORIZON_DAYS = 400


@dataclass(frozen=True)
class ShareFacts:
    share_id: int
    name: str
    protocols: tuple[str, ...]  # nfs3, nfs4, smb
    nfs_sec: tuple[str, ...]  # sys, krb5, krb5i, krb5p
    trust_level: str  # proven | host_trusted
    on_open: str  # wait | fail | auto
    wait_threshold_minutes: int | None
    worm_enabled: bool


@dataclass(frozen=True)
class CostAssumptions:
    tb: float = 1.0
    mean_file_bytes: int = 1 << 30  # objects per TB, for per-operation prices
    recall_fraction_per_year: float = 0.0

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> CostAssumptions:
        """The request's ``assumptions``; raises ValueError when out of range."""
        if not d:
            return cls()
        tb = d.get("tb", 1.0)
        mfb = d.get("mean_file_bytes", 1 << 30)
        frac = d.get("recall_fraction_per_year", 0.0)
        if isinstance(tb, bool) or not isinstance(tb, int | float) or not 0 < tb <= 100_000:
            raise ValueError("assumptions.tb must be a number over 0 and at most 100000")
        if isinstance(mfb, bool) or not isinstance(mfb, int) or mfb < 1:
            raise ValueError("assumptions.mean_file_bytes must be a whole number, 1 or more")
        if isinstance(frac, bool) or not isinstance(frac, int | float) or not 0 <= frac <= 1:
            raise ValueError("assumptions.recall_fraction_per_year must be between 0 and 1")
        return cls(float(tb), mfb, float(frac))

    def to_json(self) -> dict[str, Any]:
        return {
            "tb": self.tb,
            "mean_file_bytes": self.mean_file_bytes,
            "recall_fraction_per_year": self.recall_fraction_per_year,
        }


@dataclass(frozen=True)
class PreviewContext:
    caps: Capabilities
    layout: str  # two_account | single_account
    shares: Sequence[ShareFacts]
    prices: PriceTable  # from cost.load_price_table
    assumptions: CostAssumptions = CostAssumptions()


def apply_share_overrides(
    shares: Iterable[ShareFacts], overrides: Iterable[Mapping[str, Any]] | None
) -> tuple[ShareFacts, ...]:
    """The request's ``share_overrides`` (on_open, wait_threshold_minutes)
    applied to the dataset's shares. Overrides for other shares are ignored."""
    by_id: dict[int, Mapping[str, Any]] = {}
    for o in overrides or ():
        by_id[int(o["share_id"])] = o
    out = []
    for s in shares:
        o = by_id.get(s.share_id)
        if o is not None:
            s = replace(
                s,
                on_open=o.get("on_open", s.on_open),
                wait_threshold_minutes=o.get("wait_threshold_minutes", s.wait_threshold_minutes),
            )
        out.append(s)
    return tuple(out)


@dataclass(frozen=True)
class Segment:
    from_day: int
    to_day: int | None  # None: open-ended
    logical_tier: LogicalTier
    native_class: str | None
    account: str | None  # live | archive | single
    local: str  # resident | stub
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "from_day": self.from_day,
            "to_day": self.to_day,
            "logical_tier": self.logical_tier.value,
            "native_class": self.native_class,
            "account": self.account,
            "local": self.local,
            "note": self.note,
        }


@dataclass(frozen=True)
class Timeline:
    segments: tuple[Segment, ...]
    archive_day: int | None
    horizon_days: int
    clock: str | None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "segments": [s.to_json() for s in self.segments],
            "archive_day": self.archive_day,
            "horizon_days": self.horizon_days,
        }
        if self.clock is not None:
            out["clock"] = self.clock
        return out


def horizon(archive_day: int | None) -> int:
    return max(MIN_HORIZON_DAYS, (archive_day or 0) + 30)


def _stub_day(policy: Policy) -> int | None:
    if policy.stub_at is StubAt.NEVER:
        return None
    if policy.stub_at is StubAt.ARCHIVE:
        return policy.archive_day
    # leaves_hot: the first step off hot. Smart counts as hot (model.TIER_RANK).
    return next(d for d, t in policy.steps() if TIER_RANK[t] >= TIER_RANK[LogicalTier.WARM])


def timeline(policy: Policy, caps: Capabilities, layout: str) -> Timeline:
    """One segment per (tier, native class, edge state), from day 0 on.

    Days count from the policy clock: last read or written (scheduled), and
    always from the write under Smart. Pins override this per file and are
    shown on their own screen, not here.
    """
    marks = [(0, policy.start_tier), *policy.steps()]
    stub_day = _stub_day(policy)
    cuts = sorted({d for d, _ in marks} | ({stub_day} if stub_day is not None else set()))
    two_account_archive = layout == "two_account" and caps.archive_needs_separate_account
    segments: list[Segment] = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else None
        tier = [t for d, t in marks if d <= start][-1]
        info = tier_info(caps, tier)
        local = "stub" if stub_day is not None and start >= stub_day else "resident"
        notes = []
        if tier is LogicalTier.SMART:
            notes.append("Microsoft moves it among Hot, Cool and Cold by use")
        if tier is LogicalTier.ARCHIVE:
            if two_account_archive:
                notes.append("copied to the archive account, read back, then archived")
            elif caps.in_place_archive:
                notes.append("archived in place")
        if local == "stub" and policy.resident_mb:
            notes.append(f"the first {policy.resident_mb:g} MB stay on the edge")
        seg = Segment(
            from_day=start,
            to_day=end,
            logical_tier=tier,
            native_class=info.native_class if info else None,
            account=account_for(layout, tier),
            local=local,
            note="; ".join(notes) or None,
        )
        prev = segments[-1] if segments else None
        if prev is not None and (prev.logical_tier, prev.local) == (seg.logical_tier, seg.local):
            segments[-1] = replace(prev, to_day=seg.to_day)
        elif end is None or end > start:
            segments.append(seg)
    clock = PolicyClock.WRITTEN if policy.mode is Mode.SMART else policy.clock
    return Timeline(tuple(segments), policy.archive_day, horizon(policy.archive_day), clock.value)


@dataclass(frozen=True)
class PreviewResult:
    valid: bool
    body: Mapping[str, Any]
    timeline: Timeline
    cost: CostEstimate
    checks: tuple[Check, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        """Exactly the ``PolicyPreview`` schema of portal-api.yaml."""
        return {
            "valid": self.valid,
            "body": dict(self.body),
            "timeline": self.timeline.to_json(),
            "cost": self.cost.to_json(),
            "checks": [c.to_json() for c in self.checks],
        }


def _price_checks(cost: CostEstimate) -> list[Check]:
    if not cost.missing:
        return []
    return [
        Check(
            "warning",
            "price_missing",
            f"The price table {cost.prices.id} has no price for "
            f"{'; '.join(cost.missing)}. The cost leaves it out.",
        )
    ]


def preview(body: Mapping[str, Any], ctx: PreviewContext) -> PreviewResult:
    """Timeline, first-year cost per TB and checks for a policy body."""
    checks = policy_checks(body, ctx)
    valid = not any(c.severity == "error" for c in checks)
    if valid:
        policy = Policy.from_body(body)
        line = timeline(policy, ctx.caps, ctx.layout)
        cost = first_year_cost(policy, ctx)
    else:
        archive_day = body.get("archive_day") if isinstance(body, Mapping) else None
        if isinstance(archive_day, bool) or not isinstance(archive_day, int) or archive_day < 0:
            archive_day = None
        line = Timeline((), archive_day, horizon(archive_day), None)
        cost = keep_hot_estimate(ctx)
    checks = sort_checks(checks + _price_checks(cost))
    return PreviewResult(
        valid=valid,
        body=body if isinstance(body, Mapping) else {},
        timeline=line,
        cost=cost,
        checks=tuple(checks),
    )
