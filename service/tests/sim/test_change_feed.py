"""The change feed: ordered, cursor-paged, delivered with a lag."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    BackendError,
    ChangeKind,
    ChangeRecord,
    LogicalTier,
    RestoreSpeed,
)
from omnifile_service.backends.sim import SimBackend, VirtualClock

from .helpers import current, put, run


def _drain(sim: SimBackend, container: str, cursor: str) -> tuple[list[ChangeRecord], str]:
    records: list[ChangeRecord] = []
    while True:
        page = run(sim.read_changes(container, cursor))
        records += page.records
        cursor = page.cursor
        if page.caught_up:
            return records, cursor


def test_records_arrive_after_the_lag_and_in_order(clock: VirtualClock) -> None:
    sim = SimBackend.azure_worm(clock, feed_lag=timedelta(minutes=3), feed_page_size=2)
    start = run(sim.read_changes("data", None)).cursor
    ref = put(sim, "data", "a", b"1")
    clock.advance(timedelta(minutes=1))
    put(sim, "data", "a", b"2")
    run(sim.set_class(ref, LogicalTier.COLD))
    run(sim.delete(current(ref)))
    empty = run(sim.read_changes("data", start))
    assert empty.records == [] and empty.cursor == start and empty.caught_up
    clock.advance(timedelta(minutes=2))  # only the first record is old enough
    first = run(sim.read_changes("data", start))
    assert [r.kind for r in first.records] == [ChangeKind.CREATED]
    clock.advance(timedelta(minutes=5))
    rest, end = _drain(sim, "data", first.cursor)
    assert [r.kind for r in rest] == [
        ChangeKind.CREATED,
        ChangeKind.TIER_CHANGED,
        ChangeKind.DELETED,
    ]
    assert rest[0].detail.get("overwrite") == "true"  # Azure: BlobCreated for overwrites
    times = [r.at for r in first.records + rest]
    assert times == sorted(times)
    again = run(sim.read_changes("data", end))
    assert again.records == [] and again.cursor == end


def test_pages_and_caught_up(azure: SimBackend) -> None:
    sim = azure
    sim.feed_page_size = 2
    start = run(sim.read_changes("data", None)).cursor
    for i in range(5):
        put(sim, "data", f"k{i}", b"x")
    page = run(sim.read_changes("data", start))
    assert len(page.records) == 2 and not page.caught_up
    records, _ = _drain(sim, "data", page.cursor)
    assert [r.ref.key for r in page.records + records] == [f"k{i}" for i in range(5)]


def test_none_cursor_means_from_now_and_containers_are_separate(azure: SimBackend) -> None:
    put(azure, "data", "before", b"x")
    start = run(azure.read_changes("data", None)).cursor
    put(azure, "other", "elsewhere", b"x")
    put(azure, "data", "after", b"x")
    records, _ = _drain(azure, "data", start)
    assert [r.ref.key for r in records] == ["after"]


def test_restore_completion_carries_the_time_it_happened(
    azure: SimBackend, clock: VirtualClock
) -> None:
    archived = run(azure.set_class(put(azure, "data", "f", b"x"), LogicalTier.ARCHIVE))
    start = run(azure.read_changes("archive/data", None)).cursor
    t0 = clock.now()
    run(azure.restore(archived, RestoreSpeed.STANDARD))
    clock.advance(timedelta(days=2))  # nobody polled in between
    records, _ = _drain(azure, "archive/data", start)
    assert [(r.kind, r.at) for r in records] == [
        (ChangeKind.RESTORE_STARTED, t0),
        (ChangeKind.RESTORE_COMPLETED, t0 + timedelta(hours=15)),
    ]
    recall, _ = _drain(azure, "live/recall", "0")
    assert [r.kind for r in recall] == [ChangeKind.CREATED, ChangeKind.TIER_CHANGED]


def test_bad_cursor(azure: SimBackend) -> None:
    for bad in ("nope", "-1", "999999"):
        with pytest.raises(BackendError):
            run(azure.read_changes("data", bad))
