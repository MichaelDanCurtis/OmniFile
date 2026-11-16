"""The checks the policy editor, share settings and pin screens show.

Pure functions, no database, stated in the backend's own terms from
`capabilities()` (portal-api.md section 8 lists the codes). `model.problems()`
is the base; this adds field placement, fees, layout, notify, per-share
open-policy and security checks, and pin warnings (decision 29).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..backends.base import Capabilities, LogicalTier, RestoreOption, RestoreSpeed
from .cost import (
    TB_BYTES,
    Money,
    account_for,
    archive_restore_options,
    early_deletion_fee,
    own_copy_read_fee,
    recall_cost,
    tier_info,
)
from .model import TIER_RANK, Mode, Policy, PolicyClock, PolicyError, StubAt, problems

if TYPE_CHECKING:
    from .preview import PreviewContext, ShareFacts

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
DAY_FIELDS = ("warm_day", "cold_day", "archive_day")


@dataclass(frozen=True)
class Check:
    severity: str  # error | warning | info
    code: str
    message: str
    field: str | None = None
    fee: Money | None = None
    share_id: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "share_id": None if self.share_id is None else str(self.share_id),
            "fee": None if self.fee is None else self.fee.to_json(),
        }


@dataclass(frozen=True)
class PinFacts:
    """One file as a pin preview sees it (catalog location + file)."""

    size: int
    logical_tier: LogicalTier
    native_class: str | None
    tier_changed_at: datetime | None
    explicit_tier_set: bool
    archived: bool
    #: The file's dataset uses the two-account (Smart) layout.
    smart_layout: bool
    #: The file's archive date under its policy (`file.archive_at`), if known.
    archive_at: datetime | None = None
    #: Another file's primary copy is the same blob (decision 66).
    shares_blob: bool = False


def sort_checks(checks: list[Check]) -> list[Check]:
    return sorted(checks, key=lambda c: SEVERITY_ORDER.get(c.severity, 3))


def _gb(n: int) -> str:
    return f"{n / 1e9:g} GB"


def _hours(h: float) -> str:
    if h < 1:
        return f"{round(h * 60)} minutes"
    return f"{h:g} hour" + ("" if h == 1 else "s")


def _no_promise_limit(options: list[RestoreOption]) -> int | None:
    return min(
        (o.no_promise_above_bytes for o in options if o.no_promise_above_bytes), default=None
    )


# --- the policy body -------------------------------------------------------


def _shape_checks(body: Any) -> list[Check]:
    """Type and ordering errors, each placed on its field."""
    if not isinstance(body, Mapping):
        return [Check("error", "invalid_body", "The policy must be a JSON object.")]
    out: list[Check] = []
    for key, enum in (("mode", Mode), ("clock", PolicyClock), ("stub_at", StubAt)):
        v = body.get(key)
        if v is not None and v not in {e.value for e in enum}:
            allowed = ", ".join(e.value for e in enum)
            out.append(Check("error", "invalid_body", f"{key} must be one of {allowed}.", key))
    days: dict[str, int] = {}
    for key in DAY_FIELDS:
        v = body.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            msg = f"{key} must be a whole number of days, 0 or more."
            out.append(Check("error", "invalid_body", msg, key))
        else:
            days[key] = v
    for key in ("resident_mb", "restub_after_hours"):
        v = body.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int | float) or v < 0):
            out.append(Check("error", "invalid_body", f"{key} must be a number, 0 or more.", key))
    notify = body.get("notify")
    if notify is not None:
        if not isinstance(notify, Mapping):
            out.append(Check("error", "invalid_body", "notify must be an object.", "notify"))
        else:
            for key in ("requester", "owners", "email_fallback"):
                if key in notify and not isinstance(notify[key], bool):
                    msg = f"notify.{key} must be true or false."
                    out.append(Check("error", "invalid_body", msg, f"notify.{key}"))
            w = notify.get("watchers_group_sid")
            if w is not None and not (isinstance(w, str) and w.startswith("S-")):
                msg = "notify.watchers_group_sid must be a group SID (S-1-5-…) or null."
                out.append(Check("error", "invalid_body", msg, "notify.watchers_group_sid"))

    if "archive_day" not in body or body.get("archive_day") is None:
        if "stub_after_minutes" in body:
            msg = (
                "This is an M1 policy (stub after a number of minutes). Convert it to a "
                "scheduled policy to edit it: every policy now needs an archive day."
            )
        else:
            msg = "Set an archive day: both modes need one."
        out.append(Check("error", "archive_day_required", msg, "archive_day"))

    smart = body.get("mode") == Mode.SMART.value
    if smart:
        for key in ("warm_day", "cold_day"):
            if body.get(key) is not None:
                msg = "Smart has no warm or cold day: Microsoft moves Smart files by use."
                out.append(Check("error", "smart_has_no_warm_cold", msg, key))
    else:
        prev: tuple[str, int] | None = None
        for key in DAY_FIELDS:
            if key not in days:
                continue
            if prev is not None and days[key] <= prev[1]:
                msg = (
                    f"{key} ({days[key]}) must come after {prev[0]} ({prev[1]}): "
                    "warm, then cold, then archive."
                )
                out.append(Check("error", "days_not_increasing", msg, key))
            prev = (key, days[key])
    return out


def _min_duration_steps(
    policy: Policy, caps: Capabilities
) -> list[tuple[str, LogicalTier, str, int, int]]:
    """(field, tier left, its native class, days held, minimum) for each
    early move, in `model.problems()` order."""
    field_of = {
        LogicalTier.WARM: "warm_day",
        LogicalTier.COLD: "cold_day",
        LogicalTier.ARCHIVE: "archive_day",
    }
    out = []
    prev_day, prev_tier = 0, policy.start_tier
    for day, tier in policy.steps():
        info = tier_info(caps, prev_tier)
        if info is not None and info.minimum_days and day - prev_day < info.minimum_days:
            out.append(
                (field_of[tier], prev_tier, info.native_class, day - prev_day, info.minimum_days)
            )
        prev_day, prev_tier = day, tier
    return out


def _sentence(message: str) -> str:
    return f"{message[0].upper()}{message[1:]}."


def _model_checks(policy: Policy, ctx: PreviewContext, worm: bool) -> list[Check]:
    """Everything `model.problems()` says, placed, with the fee per TB."""
    out: list[Check] = []
    steps = iter(_min_duration_steps(policy, ctx.caps))
    place = {
        "smart_on_worm": "mode",
        "smart_unavailable": "mode",
        "leaves_hot_under_smart": "stub_at",
        "archive_recall_time": "archive_day",
    }
    for p in problems(policy, ctx.caps, worm=worm):
        if p.code == "minimum_duration":
            field, left, native, held, minimum = next(steps)
            acct = account_for(ctx.layout, left)
            fee = early_deletion_fee(ctx.prices, native, acct, TB_BYTES, Decimal(minimum - held))
            msg = _sentence(p.message) + (" The fee is shown per TB." if fee is not None else "")
            out.append(Check(p.severity, p.code, msg, field, fee))
        else:
            out.append(Check(p.severity, p.code, _sentence(p.message), place.get(p.code)))
    return out


def _layout_checks(
    policy: Policy, ctx: PreviewContext, worm_shares: list[ShareFacts]
) -> list[Check]:
    caps = ctx.caps
    out: list[Check] = []
    smart_offered = LogicalTier.SMART in caps.tiers
    if policy.mode is Mode.SMART:
        if ctx.layout == "single_account" and not worm_shares and smart_offered:
            out.append(
                Check(
                    "error",
                    "smart_archive_needs_two_accounts",
                    "Smart plus Archive needs two storage accounts; this dataset keeps one "
                    "account and tiers in place.",
                    "mode",
                )
            )
        if smart_offered and not worm_shares:
            if caps.explicit_tier_leaves_smart:
                out.append(
                    Check(
                        "info",
                        "smart_one_way",
                        "Smart is one-way: a tier pin, or any tier set by hand, takes a file "
                        "out of Smart for good.",
                        "mode",
                    )
                )
            msg = "Under Smart, the archive day counts from when the file was written; reads "
            msg += "don't postpone it."
            if policy.clock is PolicyClock.LAST_READ:
                msg += " The last-read clock applies to scheduled policies only."
            out.append(Check("info", "smart_clock_written", msg, "clock"))
    if ctx.layout == "two_account" and caps.archive_needs_separate_account and not worm_shares:
        arch = tier_info(caps, LogicalTier.ARCHIVE)
        name = arch.native_class if arch else "Archive"
        out.append(
            Check(
                "info",
                "archive_copy_two_accounts",
                f"Smart needs zone-redundant storage (ZRS), where {name} isn't allowed. On the "
                f"archive day each file is copied to the archive account (LRS or GRS), read back "
                f"and checked, then set to {name}; the original is then deleted.",
                "archive_day",
            )
        )
    for s in worm_shares:
        out.append(
            Check(
                "info",
                "worm_tiers_in_place",
                f"{s.name} is WORM: its files tier in place in one LRS or GRS account (Set "
                "Blob Tier), nothing is copied and deleted, and Smart isn't offered.",
                None,
                share_id=s.share_id,
            )
        )
    return out


def _notify_checks(body: Mapping[str, Any]) -> list[Check]:
    notify = body.get("notify")
    if not isinstance(notify, Mapping):
        notify = {}
    out: list[Check] = []
    told = (
        notify.get("requester", True) is True
        or notify.get("owners", False) is True
        or bool(notify.get("watchers_group_sid"))
    )
    if not told:
        out.append(
            Check(
                "warning",
                "nobody_told",
                "Nobody is told when an archived file is opened. Tell the person who opened "
                "it, the share's owners, or a watchers group.",
                "notify",
            )
        )
    if notify.get("email_fallback", True) is False:
        out.append(
            Check(
                "info",
                "no_email_fallback",
                "Without the email fallback, people who can't get a Teams card are told nothing.",
                "notify.email_fallback",
            )
        )
    return out


def _open_checks(share: ShareFacts, caps: Capabilities) -> list[Check]:
    """The share's open policy against what the backend can promise."""
    options = archive_restore_options(caps)
    if not options:
        return []
    out: list[Check] = []
    limit = _no_promise_limit(options)
    if share.on_open == "wait" and limit:
        out.append(
            Check(
                "warning",
                "wait_without_promise",
                f"Reads of archived files on {share.name} wait for the recall. No recall time is "
                f"promised above {_gb(limit)}, so a program can wait many hours; the person is "
                "told in Teams and by the note file.",
                "on_open",
                share_id=share.share_id,
            )
        )
    if share.on_open == "auto":
        fastest = min(options, key=lambda o: o.typical_hours[1])
        upper_min = fastest.typical_hours[1] * 60
        t = share.wait_threshold_minutes
        if t is None or t < upper_min:
            got = "no threshold is set" if t is None else f"it waits only up to {t} minutes"
            out.append(
                Check(
                    "warning",
                    "auto_threshold_below_eta",
                    f"On {share.name}, {got}, but the fastest restore ({fastest.speed.value}) "
                    f"is promised only within {_hours(fastest.typical_hours[1])}, so reads of "
                    "archived files will always fail fast.",
                    "wait_threshold_minutes",
                    share_id=share.share_id,
                )
            )
    return out


def policy_checks(body: Mapping[str, Any], ctx: PreviewContext) -> list[Check]:
    """Every check for a policy body on a dataset, errors first.

    Share facts in `ctx.shares` should already carry the editor's
    `share_overrides` (`preview.apply_share_overrides`).
    """
    out = _shape_checks(body)
    if not isinstance(body, Mapping):
        return out
    worm_shares = [s for s in ctx.shares if s.worm_enabled]
    policy: Policy | None = None
    if not any(c.severity == "error" for c in out):
        try:
            policy = Policy.from_body(body)
        except PolicyError as e:
            out.append(Check("error", "invalid_body", f"{e}."))
    if policy is not None:
        out += _model_checks(policy, ctx, bool(worm_shares))
        out += _layout_checks(policy, ctx, worm_shares)
    out += _notify_checks(body)
    stubs = policy is None or policy.stub_at is not StubAt.NEVER
    for share in ctx.shares:
        if stubs:
            out += _open_checks(share, ctx.caps)
        out += share_checks(share, restricted_groups=False)
    return sort_checks(out)


# --- shares ----------------------------------------------------------------


def share_checks(share: ShareFacts, *, restricted_groups: bool) -> list[Check]:
    """Security notes for a share's settings.

    `restricted_groups`: folders on the share carry restricted AD groups and
    no host-trust override is recorded.
    """
    out: list[Check] = []
    nfs = any(p.startswith("nfs") for p in share.protocols)
    krb = [s for s in share.nfs_sec if s.startswith("krb5")]
    if nfs and krb:
        out.append(
            Check(
                "info",
                "kerberos_throughput",
                f"{share.name} uses {', '.join(krb)}. Kerberos costs NFS throughput: Microsoft "
                "measured about −53% IOPS with krb5 and about −77% with krb5p.",
                "nfs_sec",
                share_id=share.share_id,
            )
        )
    if share.trust_level == "host_trusted":
        out.append(
            Check(
                "warning",
                "host_trusted",
                f"{share.name} allows sec=sys: the uid is whatever the client says. Only the "
                "named clients can mount it, and recalls and audit record the user as claimed.",
                "nfs_sec",
                share_id=share.share_id,
            )
        )
        if restricted_groups:
            out.append(
                Check(
                    "error",
                    "host_trusted_restricted",
                    f"Folders on {share.name} are restricted to AD groups, which sec=sys can't "
                    "prove. Use Kerberos, or record a host-trust override with a reason.",
                    "host_trust_override",
                    share_id=share.share_id,
                )
            )
    return out


# --- pins (decision 29) ----------------------------------------------------


def pin_checks(
    target: LogicalTier,
    current: PinFacts,
    ctx: PreviewContext,
    now: datetime,
    *,
    until: datetime | None = None,
    override_archive: bool = False,
) -> list[Check]:
    """Warnings before pinning one file to `target`: early-deletion fee,
    leaving Smart for good, recall time, and the archive date."""
    caps = ctx.caps
    out: list[Check] = []
    layout = "two_account" if current.smart_layout else "single_account"
    warmer_than_archive = TIER_RANK[target] < TIER_RANK[LogicalTier.ARCHIVE]
    target_info = tier_info(caps, target)
    native_target = target_info.native_class if target_info else target.value

    if current.archived and warmer_than_archive:
        options = archive_restore_options(caps)
        limit = _no_promise_limit(options)
        if limit is not None and current.size > limit:
            eta = f"No recall time is promised above {_gb(limit)}."
        else:
            parts = [f"{o.speed.value} up to {_hours(o.typical_hours[1])}" for o in options]
            eta = f"Provider estimate: {'; '.join(parts)}." if parts else ""
        out.append(
            Check(
                "warning",
                "pin_recall_time",
                f"This file is archived: pinning it to {native_target} starts a recall, and the "
                f"archived copy stays. {eta}".rstrip(),
                "tier",
                recall_cost(caps, ctx.prices, layout, current.size, RestoreSpeed.STANDARD),
            )
        )
    elif (
        current.logical_tier is LogicalTier.SMART
        and caps.explicit_tier_leaves_smart
        and not current.explicit_tier_set
    ):
        out.append(
            Check(
                "warning",
                "pin_leaves_smart",
                f"This file is in Smart. Pinning it to {native_target} takes it out of Smart for "
                "good: it keeps a tier set by hand after the pin ends.",
                "tier",
            )
        )

    if not current.archived and target is not current.logical_tier and current.shares_blob:
        # Decision 66: this file leaves, and the blob stays where it is, so nothing
        # bills an early deletion. Unless the other file wants this tier too, and then
        # the blob moves for both. A preview doesn't look at the other file's plan.
        out.append(
            Check(
                "info",
                "pin_own_copy",
                f"Another file shares this file's storage. Unless it wants {native_target} too, "
                "this file gets its own copy: one read of its storage now, and one more blob "
                "to pay for from then on. The other file's storage doesn't change.",
                "tier",
                own_copy_read_fee(current, ctx),
            )
        )
    elif not current.archived and target is not current.logical_tier:
        native = current.native_class
        info = next(
            (i for i in caps.tiers.get(current.logical_tier, ()) if i.native_class == native),
            tier_info(caps, current.logical_tier),
        )
        if info is not None and info.minimum_days and current.tier_changed_at is not None:
            held = Decimal((now - current.tier_changed_at).total_seconds()) / 86_400
            if held < info.minimum_days:
                short = info.minimum_days - held
                acct = account_for(layout, current.logical_tier)
                fee = early_deletion_fee(ctx.prices, info.native_class, acct, current.size, short)
                out.append(
                    Check(
                        "warning",
                        "pin_early_deletion",
                        f"{info.native_class} has a {info.minimum_days}-day minimum and this file "
                        f"moved there {int(held)} days ago. Moving it now is charged as if it "
                        f"stayed {math.ceil(short)} more days.",
                        "tier",
                        fee,
                    )
                )

    if warmer_than_archive and not override_archive and current.archive_at is not None:
        day = current.archive_at.date().isoformat()
        if current.archive_at <= now:
            msg = (
                f"This file is past its archive date ({day}), so a warmer pin changes nothing "
                "unless an Owner overrides the archive date."
            )
            out.append(Check("info", "pin_past_archive_date", msg, "override_archive"))
        elif until is None or until > current.archive_at:
            msg = f"The pin ends on {day}, the file's archive date, unless an Owner overrides it."
            out.append(Check("info", "pin_past_archive_date", msg, "override_archive"))
    return sort_checks(out)
