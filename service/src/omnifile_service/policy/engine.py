"""Evaluate one file against its policy, tier pin and current state.

Pure and deterministic: everything comes in as arguments, including `now`,
so the scheduler can run on a virtual clock. The scheduler
(`policy.scheduler`) reads the facts from the catalog and turns the `Plan`
into queued work.

Rules (ARCHITECTURE.md section 5, decision 29, AGENTS.md):

- **Schedule:** days count from the policy clock. Scheduled: Hot until the
  warm day, Cool until the cold day, Cold until the archive day. Smart: Smart
  until the archive day. The schedule only moves files colder; a read under
  `clock = last_read` postpones the next step, it doesn't warm the file.
  Warmer moves come from pins only.
- **Pins** (decision 29) override the schedule while active (no `until`, or
  `until` in the future). The archive date stays respected: a pin to a
  warmer tier doesn't hold a file past its archive day unless the pin says
  so (`override_archive`). A pin to Archive archives at once. A pin warmer
  than Archive on an archived file starts a recall (the archived copy stays).
- **Smart is one-way:** a blob given an explicit tier can never return to
  Smart. The plan never asks for it; it flags `smart_one_way` instead.
- **Stubs** follow `stub_at`: `archive` once the file is archived,
  `leaves_hot` once it is warm, cold or archived, `never` not at all. A
  keep_online hold or an active pin warmer than the stub point blocks it,
  and a recalled file read in the last `restub_after` is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..backends.base import LogicalTier
from .model import TIER_RANK, Mode, Policy, PolicyClock, StubAt

#: next_action labels (catalog.file.next_action CHECK, 0002).
ACTION_FOR = {
    LogicalTier.WARM: "tier_warm",
    LogicalTier.COLD: "tier_cold",
    LogicalTier.ARCHIVE: "archive",
}

#: When nothing else is due, look again after this (e.g. a stub blocked by
#: an unverified location).
RECHECK = timedelta(hours=1)

#: A file may share a colder, still-online step with another file this much ahead
#: of its own schedule, so a small difference in age isn't worth a copy
#: (`same_tier_wanted`, decision 66).
SHARED_BLOB_SAME_PLAN_WINDOW = timedelta(days=7)
#: A file that needs its own copy of a shared blob and can't get one tries again this
#: much later. Only a move to Archive may move a blob over a file that differs, so in
#: the meantime nothing moves and nothing is billed (`worker.tiering._wait_a_day`).
OWN_COPY_RETRY_AFTER = timedelta(hours=24)
#: `tier.move.refused`'s reason for such a wait. Reported once a day per blob.
HELD_BACK = "held_back_shared_blob"


@dataclass(frozen=True)
class Pin:
    tier: LogicalTier
    until: datetime | None = None
    override_archive: bool = False
    pin_id: int | None = None
    set_by_identity_id: int | None = None

    def active(self, now: datetime) -> bool:
        return self.until is None or self.until > now


@dataclass(frozen=True)
class FileFacts:
    written_at: datetime
    last_read_at: datetime | None = None
    #: The content version's primary location tier; None means hot.
    tier: LogicalTier | None = None
    explicit_tier_set: bool = False
    #: The layout offers Smart (Azure two-account). False on WORM / single account.
    smart_available: bool = True
    #: Edge state: resident, stub, recalling, remote, ...
    state: str = "resident"
    stub_requested: bool = False
    #: The primary location is committed and verified (a stub is safe).
    verified: bool = True
    keep_online_until: datetime | None = None
    keep_online: bool = False


@dataclass(frozen=True)
class Plan:
    #: Move the primary copy to this tier (None: no move).
    move_to: LogicalTier | None = None
    #: Start a recall (pin warmer than Archive on an archived, non-local file).
    recall: bool = False
    #: Emit a stub instruction for the content version.
    stub: bool = False
    #: When to evaluate this file again, and why (None: nothing more to do).
    next_at: datetime | None = None
    next_action: str | None = None
    #: The file's archive date under the policy (for the portal and the edge).
    archive_at: datetime | None = None
    #: The tier the file should be in now, after pins and the archive date.
    desired: LogicalTier | None = None
    flags: tuple[str, ...] = field(default_factory=tuple)


def anchor(policy: Policy, facts: FileFacts) -> datetime:
    """The policy clock's zero for this file.

    `clock` applies to scheduled mode. In smart mode the archive day always
    counts from the commit (`written`): reads never postpone it, because the
    archive date is always respected (M3 coordinator decision).
    """
    if policy.mode is Mode.SMART:
        return facts.written_at
    if policy.clock is PolicyClock.LAST_READ and facts.last_read_at is not None:
        return max(facts.written_at, facts.last_read_at)
    return facts.written_at


def scheduled_tier(policy: Policy, start: datetime, at: datetime) -> LogicalTier:
    tier = policy.start_tier
    for day, step in policy.steps():
        if at >= start + timedelta(days=day):
            tier = step
    return tier


def next_step(
    policy: Policy, start: datetime, after: datetime, current: LogicalTier | None = None
) -> tuple[datetime, LogicalTier] | None:
    """The next scheduled step after `after` that is colder than `current`."""
    for day, step in policy.steps():
        when = start + timedelta(days=day)
        if when > after and (current is None or TIER_RANK[step] > _rank(current)):
            return when, step
    return None


def _rank(t: LogicalTier | None) -> int:
    return TIER_RANK[t or LogicalTier.HOT]


def same_tier_wanted(
    policy: Policy, facts: FileFacts, pin: Pin | None, now: datetime, target: LogicalTier
) -> bool:
    """Decision 66: does this file want to go along when another file moves the
    blob they share to `target`? If not, the file that is moving gets its own copy.

    Yes when its own plan (schedule, pin and archive date) has it in `target` now.
    Also yes for a colder move, when it has no pin in force and:

    - `target` is Cool or Cold, and its schedule gets there within
      `SHARED_BLOB_SAME_PLAN_WINDOW`. It goes up to that much early, which costs a
      dearer read on a file that is still online, and nothing else;
    - `target` is Archive, and its archive date is today (UTC). A file is never
      archived on a date before its own, because that takes it offline.

    A pin says what it wants now, however soon it ends. `facts.tier` is the blob's
    tier."""
    plan = evaluate(policy, facts, pin, now)
    return plan.desired is target or (
        "pin" not in plan.flags
        and _colder_soon(plan.desired, plan.archive_at, now, target)
        and _rank(evaluate(policy, facts, pin, now + SHARED_BLOB_SAME_PLAN_WINDOW).desired)
        >= _rank(target)
    )


def old_version_wants_same_tier(
    policy: Policy, committed_at: datetime, current: LogicalTier, now: datetime, target: LogicalTier
) -> bool:
    """`same_tier_wanted` for a superseded version, or a deleted file's last one:
    `scheduler.sweep_versions` moves those colder only, from their commit, with no pin."""

    def wants(at: datetime) -> LogicalTier:
        sched = scheduled_tier(policy, committed_at, at)
        return sched if _rank(sched) > _rank(current) else current

    desired = wants(now)
    archive_at = committed_at + timedelta(days=policy.archive_day)
    return desired is target or (
        _colder_soon(desired, archive_at, now, target)
        and _rank(wants(now + SHARED_BLOB_SAME_PLAN_WINDOW)) >= _rank(target)
    )


def _colder_soon(
    desired: LogicalTier | None,
    archive_at: datetime | None,
    now: datetime,
    target: LogicalTier,
) -> bool:
    """The part of the rule both kinds of reference share. Only a move colder than what
    the file wants now has a window: a warmer one can't, because a plan never wants
    anything warmer than the blob without a pin. And a file that wants a tier colder
    than `target` right now differs: it moves on its own, from the warmer tier, with
    no retrieval."""
    if _rank(desired) >= _rank(target):
        return False
    if target is LogicalTier.ARCHIVE:
        return archive_at is not None and archive_at.date() == now.date()
    return True


def evaluate(policy: Policy, facts: FileFacts, pin: Pin | None, now: datetime) -> Plan:
    flags: list[str] = []
    current = facts.tier or LogicalTier.HOT
    start = anchor(policy, facts)
    archive_at = start + timedelta(days=policy.archive_day)
    sched = scheduled_tier(policy, start, now)
    # The schedule only moves colder.
    desired = sched if _rank(sched) >= _rank(current) else current
    if current is LogicalTier.SMART and sched is LogicalTier.HOT and policy.mode is Mode.SCHEDULED:
        # Scheduled mode: the client chose when files cool, so Smart (the
        # upload default on a Smart account) must not move them on its own.
        desired = LogicalTier.HOT
    nxt = next_step(policy, start, now, desired)
    next_at, next_action = (nxt[0], ACTION_FOR[nxt[1]]) if nxt else (None, None)

    pinned = pin is not None and pin.active(now)
    pin_rules = False  # the pin decides the tier (not overridden by the archive date)
    if pinned:
        assert pin is not None
        if pin.tier is LogicalTier.ARCHIVE:
            desired, pin_rules = LogicalTier.ARCHIVE, True
            flags.append("pin")
        elif now >= archive_at and not pin.override_archive:
            desired = LogicalTier.ARCHIVE
            flags.append("archive_date_respected")
        else:
            desired, pin_rules = pin.tier, True
            flags.append("pin")
            wake = [t for t in (pin.until,) if t is not None]
            if not pin.override_archive:
                wake.append(archive_at)
            next_at = min(wake) if wake else None
            # At wake-up the schedule (or the archive date) takes over again.
            next_action = (
                ACTION_FOR.get(scheduled_tier(policy, start, next_at), "archive")
                if next_at is not None
                else None
            )

    # Smart is one-way: never ask for it once a blob has an explicit tier.
    if (
        policy.mode is Mode.SMART
        and not pinned
        and current not in (LogicalTier.SMART, LogicalTier.ARCHIVE)
    ):
        flags.append("smart_one_way")  # it keeps its explicit tier until the archive date
    if desired is LogicalTier.SMART:
        if not facts.smart_available:
            desired = LogicalTier.HOT
            flags.append("smart_unavailable")
        elif current is not LogicalTier.SMART or facts.explicit_tier_set:
            desired = current
            flags.append("smart_one_way")

    move_to: LogicalTier | None = None
    recall = False
    if current is LogicalTier.ARCHIVE and desired is not LogicalTier.ARCHIVE:
        # Leaving Archive is a rehydrate, never a policy step. A warmer pin
        # brings the bytes back to the edge; the archived copy stays.
        if pin_rules and facts.state in ("stub", "remote"):
            recall = True
            flags.append("recall_for_pin")
    elif desired is not current:
        move_to = desired

    # Stubbing, judged on the tier the file is in now (a move first lands,
    # then the next evaluation stubs).
    stub = False
    if (
        policy.stub_at is not StubAt.NEVER
        and facts.state == "resident"
        and not facts.stub_requested
    ):
        reached = (
            current is LogicalTier.ARCHIVE
            if policy.stub_at is StubAt.ARCHIVE
            else _rank(current) >= TIER_RANK[LogicalTier.WARM]
        )
        threshold = LogicalTier.ARCHIVE if policy.stub_at is StubAt.ARCHIVE else LogicalTier.WARM
        blocked_by_pin = pin_rules and pin is not None and _rank(pin.tier) < TIER_RANK[threshold]
        held = facts.keep_online and (
            facts.keep_online_until is None or facts.keep_online_until > now
        )
        if reached and move_to is None and not blocked_by_pin and not held:
            recent = (
                facts.last_read_at is not None
                and facts.last_read_at > facts.written_at
                and facts.last_read_at + policy.restub_after > now
            )
            if recent:
                flags.append("recently_read")
                wake = facts.last_read_at + policy.restub_after  # type: ignore[operator]
                if next_at is None or wake < next_at:
                    next_at, next_action = wake, "stub"
            elif not facts.verified:
                flags.append("not_verified")
                if next_at is None or now + RECHECK < next_at:
                    next_at, next_action = now + RECHECK, "stub"
            else:
                stub = True
        elif reached and held:
            flags.append("keep_online")
            if facts.keep_online_until is not None and (
                next_at is None or facts.keep_online_until < next_at
            ):
                next_at, next_action = facts.keep_online_until, "stub"

    if move_to is not None:
        # Look again once the move has landed (the move handler wakes the
        # file; the sweep skips files with a move queued).
        next_at, next_action = now, ACTION_FOR.get(move_to, next_action or "archive")
    elif recall:
        next_at, next_action = now + RECHECK, next_action or "archive"

    if next_at is not None and next_action is None:
        next_action = "archive"
    return Plan(
        move_to=move_to,
        recall=recall,
        stub=stub,
        next_at=next_at,
        next_action=next_action,
        archive_at=archive_at,
        desired=desired,
        flags=tuple(flags),
    )
