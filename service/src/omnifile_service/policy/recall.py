"""Recall planning: priority, the per-account High budget, ETAs and the
wait/fail decision.

- **Restores copy** (Azure Copy Blob out of Archive into the recall
  container), at Standard or High priority. Never Set Blob Tier on the
  original, which would incur the 180-day early-deletion fee (AGENTS.md).
- **High priority is budgeted per storage account: 10 GiB/h** (AGENTS.md;
  `capabilities().throughput_caps`). The High pipeline is modelled as one
  queue per account draining at that rate, rebuilt from the catalog's open
  High restores. A request is admitted while the backlog plus its own bytes
  fit in one hour of pipeline; beyond that it waits in OmniFile's queue.
- **Don't offer High where it can't be faster** (anti-pattern 5): when the
  queued High completion is no earlier than a Standard rehydrate, the recall
  is downgraded to Standard at once.
- **ETAs come from `capabilities()`**: the restore option's stated upper
  bound, and never a time above `no_promise_above_bytes` (10 GB on Azure).
- **waitquick** (`on_open = auto`): wait only if the ETA is known and within
  the share's threshold; otherwise fail fast.

Everything is a pure function of its arguments, `now` included.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..backends.base import Capabilities, RestoreOption, RestoreSpeed

GiB = 1024**3


def account_of(container: str) -> str:
    """Azure containers are "account/container"; the account is the budget scope."""
    return container.split("/", 1)[0]


def _option(caps: Capabilities, speed: RestoreSpeed) -> RestoreOption | None:
    for o in caps.restore_options:
        if o.speed is speed:
            return o
    return None


def offers(caps: Capabilities, speed: RestoreSpeed) -> bool:
    return _option(caps, speed) is not None


def high_rate_bytes_per_hour(caps: Capabilities) -> float | None:
    """The per-account priority rehydrate cap, in bytes per hour."""
    for cap in caps.throughput_caps:
        if cap.name == "priority rehydrate":
            unit = cap.unit.lower()
            if unit == "gib/hour":
                return cap.limit * GiB
            if unit == "gb/hour":
                return cap.limit * 1e9
    return None


def no_promise(caps: Capabilities, speed: RestoreSpeed, size: int) -> bool:
    opt = _option(caps, speed)
    limit = opt.no_promise_above_bytes if opt else None
    return limit is not None and size > limit


def typical_upper(caps: Capabilities, speed: RestoreSpeed) -> timedelta | None:
    opt = _option(caps, speed)
    return None if opt is None else timedelta(hours=opt.typical_hours[1])


@dataclass(frozen=True)
class PipelineEntry:
    admitted_at: datetime
    size: int


def pipeline_free_at(entries: Iterable[PipelineEntry], rate: float) -> datetime | None:
    """When the account's High pipeline drains, given the admitted requests."""
    free: datetime | None = None
    for e in sorted(entries, key=lambda e: e.admitted_at):
        start = e.admitted_at if free is None else max(free, e.admitted_at)
        free = start + timedelta(hours=e.size / rate)
    return free


def backlog_bytes(free_at: datetime | None, rate: float, now: datetime) -> float:
    if free_at is None or free_at <= now:
        return 0.0
    return (free_at - now).total_seconds() / 3600 * rate


@dataclass(frozen=True)
class RecallPlan:
    speed: RestoreSpeed
    #: When the request may go to the provider (== now unless queued).
    submit_at: datetime
    #: Promised completion, or None (no promise above 10 GB, or unknown).
    eta: datetime | None
    queued: bool = False
    downgraded: bool = False
    reason: str | None = None


def plan_recall(
    caps: Capabilities,
    size: int,
    priority: str,
    now: datetime,
    *,
    high_backlog: Iterable[PipelineEntry] = (),
    waiting_ahead_bytes: int = 0,
) -> RecallPlan:
    """Choose the speed and submission time for one recall.

    `high_backlog`: High restores already admitted on the same account.
    `waiting_ahead_bytes`: High recalls queued (not yet admitted) ahead of
    this one on the same account, served first.
    """
    std = RestoreSpeed.STANDARD if offers(caps, RestoreSpeed.STANDARD) else None
    std_upper = typical_upper(caps, RestoreSpeed.STANDARD) if std else None
    std_eta = None
    if std is not None and std_upper is not None and not no_promise(caps, std, size):
        std_eta = now + std_upper

    rate = high_rate_bytes_per_hour(caps)
    if priority != "high" or not offers(caps, RestoreSpeed.HIGH) or rate is None:
        speed = std or sorted(o.speed for o in caps.restore_options)[0]
        if speed is not std:
            upper = typical_upper(caps, speed)
            eta = None if upper is None or no_promise(caps, speed, size) else now + upper
        else:
            eta = std_eta
        return RecallPlan(speed, now, eta)

    budget = rate  # one hour of pipeline
    free = pipeline_free_at(high_backlog, rate)
    backlog = backlog_bytes(free, rate, now) + waiting_ahead_bytes
    pipe_free = now + timedelta(hours=backlog / rate)
    transfer = timedelta(hours=size / rate)
    floor = typical_upper(caps, RestoreSpeed.HIGH) or timedelta(0)
    high_done = pipe_free + max(floor, transfer)
    # Standard's time is only known up to the no-promise size; above it,
    # High (a known 10 GiB/h) is never assumed slower.
    if std is not None and std_eta is not None and high_done >= std_eta:
        return RecallPlan(
            std,
            now,
            std_eta,
            downgraded=True,
            reason="High is no faster than Standard with the account's priority backlog",
        )
    high_eta = None if no_promise(caps, RestoreSpeed.HIGH, size) else high_done
    if backlog <= 0 or backlog + size <= budget:
        return RecallPlan(RestoreSpeed.HIGH, now, high_eta)
    # Over budget: hold it until the backlog has room for it.
    room_needed = backlog + size - budget
    submit_at = now + timedelta(hours=min(backlog, room_needed) / rate)
    return RecallPlan(
        RestoreSpeed.HIGH,
        submit_at,
        high_eta,
        queued=True,
        reason="over the account's 10 GiB/h priority budget",
    )


def decide_on_open(
    on_open: str, threshold_minutes: int | None, eta: datetime | None, now: datetime
) -> str:
    """The share's open policy against the ETA: ``wait`` or ``fail``.

    ``auto`` (the FSAL's ``waitquick``) waits only when a time is promised and
    it is within the threshold; files with no promise (over 10 GB) fail fast.
    """
    if on_open in ("wait", "fail"):
        return on_open
    if on_open != "auto":
        raise ValueError(f"on_open must be wait, fail or auto, not {on_open!r}")
    if eta is None or threshold_minutes is None:
        return "fail"
    return "wait" if eta - now <= timedelta(minutes=threshold_minutes) else "fail"


def fsal_open_policy(on_open: str, threshold_minutes: int | None) -> dict[str, object]:
    """The FSAL's terms (fsal-socket.md section 10): auto -> waitquick + quick_ms."""
    if on_open == "auto":
        if threshold_minutes is None:
            raise ValueError("auto needs wait_threshold_minutes")
        return {"policy": "waitquick", "quick_ms": threshold_minutes * 60_000}
    return {"policy": on_open}
