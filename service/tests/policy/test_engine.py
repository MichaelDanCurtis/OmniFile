"""The policy engine on its own: no database, no backend, a fixed `now`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.policy import FileFacts, Pin, Policy, PolicyError, evaluate

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


SCHED = Policy.from_body({"mode": "scheduled", "warm_day": 30, "cold_day": 120, "archive_day": 365})
SMART = Policy.from_body({"mode": "smart", "archive_day": 365})


def facts(tier: T | None = T.HOT, **kw) -> FileFacts:
    return FileFacts(written_at=T0, tier=tier, **kw)


def test_scheduled_steps_in_order() -> None:
    assert evaluate(SCHED, facts(T.HOT), None, day(10)).move_to is None
    p = evaluate(SCHED, facts(T.HOT), None, day(10))
    assert (p.next_at, p.next_action) == (day(30), "tier_warm")
    assert evaluate(SCHED, facts(T.HOT), None, day(30)).move_to is T.WARM
    p = evaluate(SCHED, facts(T.WARM), None, day(30))
    assert p.move_to is None and (p.next_at, p.next_action) == (day(120), "tier_cold")
    assert evaluate(SCHED, facts(T.WARM), None, day(120)).move_to is T.COLD
    p = evaluate(SCHED, facts(T.COLD), None, day(365))
    assert p.move_to is T.ARCHIVE and p.archive_at == day(365)
    # A late sweep jumps straight to the right tier.
    assert evaluate(SCHED, facts(T.HOT), None, day(400)).move_to is T.ARCHIVE


def test_scheduled_mode_takes_files_out_of_smart_upload_default() -> None:
    p = evaluate(SCHED, facts(T.SMART), None, day(0))
    assert p.move_to is T.HOT


def test_smart_waits_for_the_archive_day() -> None:
    p = evaluate(SMART, facts(T.SMART), None, day(200))
    assert p.move_to is None and (p.next_at, p.next_action) == (day(365), "archive")
    assert evaluate(SMART, facts(T.SMART), None, day(365)).move_to is T.ARCHIVE


def test_last_read_clock_postpones_but_never_warms() -> None:
    pol = Policy.from_body(
        {"mode": "scheduled", "clock": "last_read", "warm_day": 30, "archive_day": 90}
    )
    f = facts(T.WARM, last_read_at=day(50))
    p = evaluate(pol, f, None, day(60))
    assert p.move_to is None  # read 10 days ago: still warm, not back to hot
    assert p.next_at == day(140) and p.next_action == "archive"
    written = Policy.from_body({**{"warm_day": 30, "archive_day": 90}, "clock": "written"})
    assert evaluate(written, f, None, day(90)).move_to is T.ARCHIVE


def test_pin_to_hot_holds_until_the_archive_date() -> None:
    pin = Pin(T.HOT, until=day(500))
    p = evaluate(SCHED, facts(T.HOT), pin, day(200))
    assert p.move_to is None and p.desired is T.HOT
    assert p.next_at == day(365)  # the archive date stays respected
    p = evaluate(SCHED, facts(T.HOT), pin, day(365))
    assert p.move_to is T.ARCHIVE and "archive_date_respected" in p.flags


def test_pin_with_override_blocks_archive_until_it_expires() -> None:
    pin = Pin(T.HOT, until=day(500), override_archive=True)
    p = evaluate(SCHED, facts(T.HOT), pin, day(400))
    assert p.move_to is None and p.next_at == day(500)
    assert evaluate(SCHED, facts(T.HOT), pin, day(500)).move_to is T.ARCHIVE


def test_pin_to_warmer_tier_moves_the_file_up() -> None:
    p = evaluate(SCHED, facts(T.COLD), Pin(T.HOT, until=day(200)), day(150))
    assert p.move_to is T.HOT and p.next_at == day(150)


def test_pin_to_archive_archives_now_and_stubs_after() -> None:
    assert evaluate(SCHED, facts(T.HOT), Pin(T.ARCHIVE), day(1)).move_to is T.ARCHIVE
    assert evaluate(SCHED, facts(T.ARCHIVE), Pin(T.ARCHIVE), day(1)).stub


def test_warmer_pin_on_an_archived_stub_recalls() -> None:
    pin = Pin(T.HOT, until=day(500), override_archive=True)
    p = evaluate(SCHED, facts(T.ARCHIVE, state="stub"), pin, day(400))
    assert p.recall and p.move_to is None
    # Resident already: nothing to fetch, and the archived copy stays put.
    p = evaluate(SCHED, facts(T.ARCHIVE, state="resident"), pin, day(400))
    assert not p.recall and p.move_to is None and not p.stub


def test_smart_is_one_way() -> None:
    p = evaluate(SMART, facts(T.WARM, explicit_tier_set=True), None, day(10))
    assert p.move_to is None and "smart_one_way" in p.flags
    p = evaluate(SMART, facts(T.HOT, explicit_tier_set=True), Pin(T.SMART), day(10))
    assert p.move_to is None and "smart_one_way" in p.flags
    # Still archived on its date.
    assert evaluate(SMART, facts(T.WARM, explicit_tier_set=True), None, day(365)).move_to is (
        T.ARCHIVE
    )


@pytest.mark.parametrize(
    ("stub_at", "tier", "stub"),
    [
        ("archive", T.COLD, False),
        ("archive", T.ARCHIVE, True),
        ("leaves_hot", T.HOT, False),
        ("leaves_hot", T.WARM, True),
        ("leaves_hot", T.ARCHIVE, True),
        ("never", T.ARCHIVE, False),
    ],
)
def test_stub_at(stub_at: str, tier: T, stub: bool) -> None:
    pol = Policy.from_body(
        {"warm_day": 30, "cold_day": 120, "archive_day": 365, "stub_at": stub_at}
    )
    now = {T.HOT: day(1), T.WARM: day(31), T.COLD: day(121), T.ARCHIVE: day(366)}[tier]
    assert evaluate(pol, facts(tier), None, now).stub is stub


def test_stub_blocked_by_hold_pin_and_recent_read() -> None:
    at = day(366)
    assert not evaluate(SCHED, facts(T.ARCHIVE, keep_online=True), None, at).stub
    held = facts(T.ARCHIVE, keep_online=True, keep_online_until=day(400))
    assert evaluate(SCHED, held, None, at).next_at == day(400)
    read = facts(T.ARCHIVE, last_read_at=day(366) - timedelta(hours=2))
    p = evaluate(SCHED, read, None, at)
    assert not p.stub and p.next_at == day(366) + timedelta(hours=22)
    assert not evaluate(SCHED, facts(T.ARCHIVE, stub_requested=True), None, at).stub
    assert not evaluate(SCHED, facts(T.ARCHIVE, state="stub"), None, at).stub
    p = evaluate(SCHED, facts(T.ARCHIVE, verified=False), None, at)
    assert not p.stub and p.next_action == "stub"


def test_fully_settled_file_has_nothing_next() -> None:
    p = evaluate(SCHED, facts(T.ARCHIVE, state="stub"), None, day(400))
    assert p.next_at is None and p.move_to is None


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "scheduled"},
        {"mode": "smart", "warm_day": 10, "archive_day": 100},
        {"warm_day": 100, "cold_day": 50, "archive_day": 365},
        {"archive_day": 30, "cold_day": 30},
        {"archive_day": -1},
        {"archive_day": 30, "stub_at": "sometimes"},
    ],
)
def test_bad_bodies(body: dict) -> None:
    with pytest.raises(PolicyError):
        Policy.from_body(body)
