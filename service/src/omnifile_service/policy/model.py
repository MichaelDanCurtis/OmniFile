"""The policy body (ARCHITECTURE.md section 5) and the editor's checks.

A policy is the ``catalog.policy.body`` jsonb of a dataset's active policy:

    {"mode": "scheduled" | "smart",
     "clock": "last_read" | "written",
     "warm_day": 30, "cold_day": 120,        # scheduled only, both optional
     "archive_day": 365,                      # required, both modes
     "stub_at": "archive" | "leaves_hot" | "never",
     "resident_mb": 1.5,
     "restub_after_hours": 24}

`mode = scheduled` moves a file Hot -> Cool ("warm") -> Cold -> Archive on
the days given. `mode = smart` leaves it in Azure Smart tier, where Microsoft
moves it among Hot/Cool/Cold by use, until the archive day, which is always
respected (owner decision, AGENTS.md). Days count from the policy clock:
when the file was written, or when it was last read (`/reads`, migration
0005).

A body without ``archive_day`` is an M1 policy (``stub_after_minutes``) and
is left to the M1 stub sweep: the tiering engine only acts on M3 policies.

`problems()` is what the policy editor shows before saving. The database
enforces the hard layout rules too (catalog.policy_layout_problem, 0003);
here they come with the warnings in the backend's own terms, from
`capabilities()`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from ..backends.base import Capabilities, LogicalTier


class Mode(StrEnum):
    SCHEDULED = "scheduled"
    SMART = "smart"


class PolicyClock(StrEnum):
    LAST_READ = "last_read"
    WRITTEN = "written"


class StubAt(StrEnum):
    ARCHIVE = "archive"  # the file becomes a stub once it is archived
    LEAVES_HOT = "leaves_hot"  # ... once it is moved off hot (warm, cold or archive)
    NEVER = "never"


class PolicyError(ValueError):
    """The body can't be evaluated (missing or contradictory fields)."""


#: Coldness order. Smart counts as hot: OmniFile can't see where Microsoft
#: has moved a Smart blob, and a read never needs a restore.
TIER_RANK: Mapping[LogicalTier, int] = {
    LogicalTier.HOT: 0,
    LogicalTier.SMART: 0,
    LogicalTier.WARM: 1,
    LogicalTier.COLD: 2,
    LogicalTier.ARCHIVE: 3,
}

DEFAULT_RESTUB_AFTER = timedelta(hours=24)


def _days(body: Mapping[str, Any], key: str) -> int | None:
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise PolicyError(f"{key} must be a whole number of days >= 0, not {v!r}")
    return v


@dataclass(frozen=True)
class Policy:
    mode: Mode
    clock: PolicyClock
    archive_day: int
    warm_day: int | None = None
    cold_day: int | None = None
    stub_at: StubAt = StubAt.ARCHIVE
    resident_mb: float | None = None
    #: After a recalled file is read, wait this long before stubbing it again.
    restub_after: timedelta = DEFAULT_RESTUB_AFTER

    @staticmethod
    def is_tiering_body(body: Mapping[str, Any] | None) -> bool:
        """True for an M3 policy (it has an archive day)."""
        return bool(body) and "archive_day" in body  # type: ignore[operator]

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> Policy:
        try:
            mode = Mode(body.get("mode", Mode.SCHEDULED))
            clock = PolicyClock(body.get("clock", PolicyClock.WRITTEN))
            stub_at = StubAt(body.get("stub_at", StubAt.ARCHIVE))
        except ValueError as e:
            raise PolicyError(str(e)) from None
        archive_day = _days(body, "archive_day")
        if archive_day is None:
            raise PolicyError("archive_day is required (both modes)")
        warm, cold = _days(body, "warm_day"), _days(body, "cold_day")
        if mode is Mode.SMART and (warm is not None or cold is not None):
            raise PolicyError("smart mode has no warm or cold day: Microsoft moves Smart blobs")
        steps = [d for d in (warm, cold, archive_day) if d is not None]
        if steps != sorted(set(steps)) or len(steps) != len(set(steps)):
            raise PolicyError("days must increase: warm_day < cold_day < archive_day")
        resident = body.get("resident_mb")
        if resident is not None and (not isinstance(resident, int | float) or resident < 0):
            raise PolicyError("resident_mb must be a number >= 0")
        hours = body.get("restub_after_hours", 24)
        if not isinstance(hours, int | float) or hours < 0:
            raise PolicyError("restub_after_hours must be a number >= 0")
        return cls(
            mode=mode,
            clock=clock,
            archive_day=archive_day,
            warm_day=warm,
            cold_day=cold,
            stub_at=stub_at,
            resident_mb=None if resident is None else float(resident),
            restub_after=timedelta(hours=hours),
        )

    def steps(self) -> list[tuple[int, LogicalTier]]:
        """(day, tier) transitions in order."""
        out: list[tuple[int, LogicalTier]] = []
        if self.mode is Mode.SCHEDULED:
            if self.warm_day is not None:
                out.append((self.warm_day, LogicalTier.WARM))
            if self.cold_day is not None:
                out.append((self.cold_day, LogicalTier.COLD))
        out.append((self.archive_day, LogicalTier.ARCHIVE))
        return out

    @property
    def start_tier(self) -> LogicalTier:
        return LogicalTier.SMART if self.mode is Mode.SMART else LogicalTier.HOT


@dataclass(frozen=True)
class Problem:
    severity: str  # "error" (can't save) | "warning" (costs or surprises)
    code: str
    message: str


def problems(policy: Policy, caps: Capabilities, *, worm: bool = False) -> list[Problem]:
    """Editor checks, stated in the chosen backend's terms."""
    out: list[Problem] = []
    tiers = caps.tiers
    if policy.mode is Mode.SMART:
        if worm:
            out.append(
                Problem("error", "smart_on_worm", "Smart tier is not offered on WORM shares")
            )
        elif LogicalTier.SMART not in tiers:
            out.append(
                Problem("error", "smart_unavailable", "this storage layout has no Smart tier")
            )
        if policy.stub_at is StubAt.LEAVES_HOT:
            out.append(
                Problem(
                    "warning",
                    "leaves_hot_under_smart",
                    "Smart tier's own moves aren't visible, so files become stubs at the "
                    "archive day",
                )
            )
    # Minimum storage durations: leaving a tier early is charged as if it stayed.
    prev_day, prev_tier = 0, policy.start_tier
    for day, tier in policy.steps():
        info = (tiers.get(prev_tier) or [None])[0]
        if info is not None and info.minimum_days and day - prev_day < info.minimum_days:
            out.append(
                Problem(
                    "warning",
                    "minimum_duration",
                    f"{info.native_class} has a {info.minimum_days}-day minimum; moving on after "
                    f"{day - prev_day} days is charged an early-deletion fee",
                )
            )
        prev_day, prev_tier = day, tier
    arch = (tiers.get(LogicalTier.ARCHIVE) or [None])[0]
    if arch is not None:
        no_promise = [o.no_promise_above_bytes for o in caps.restore_options]
        limit = min((n for n in no_promise if n), default=None)
        note = f"; no recall time is promised above {limit / 1e9:g} GB" if limit else ""
        out.append(
            Problem(
                "warning",
                "archive_recall_time",
                f"archived files need a restore before they can be read{note}",
            )
        )
    return out
