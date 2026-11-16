"""S3 restores: Days and expiry, Intelligent-Tiering, Deep Archive, the request rate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnifile_service.backends.base import (
    ChangeKind,
    LogicalTier,
    NotRestored,
    RestoreSpeed,
    RestoreState,
    Throttled,
    UnsupportedOperation,
)
from omnifile_service.backends.sim import SimBackend, VirtualClock

from .helpers import put, read, run

MB = 1024 * 1024


def test_restore_needs_days_and_the_copy_expires_at_midnight(
    s3: SimBackend, clock: VirtualClock
) -> None:
    ref = put(s3, "b", "k", b"glacier", tier=LogicalTier.ARCHIVE)
    with pytest.raises(UnsupportedOperation, match="Days"):
        run(s3.restore(ref, RestoreSpeed.STANDARD))
    start = run(s3.read_changes("b", None)).cursor
    handle = run(s3.restore(ref, RestoreSpeed.STANDARD, days=2))
    assert handle.days == 2 and handle.target is None
    clock.advance(timedelta(hours=5))  # Standard from Flexible Retrieval: 3-5 h [V]
    status = run(s3.restore_status(handle))
    assert status.state is RestoreState.COMPLETED
    # Completed 2026-10-05 17:00 + 2 days, rounded up to midnight UTC.
    assert status.expires_at == datetime(2026, 10, 8, tzinfo=UTC)
    head = run(s3.head(ref))
    assert (head.restore_state, head.restore_expires_at) == (
        RestoreState.COMPLETED,
        status.expires_at,
    )
    assert read(s3, ref) == b"glacier"
    clock.set(status.expires_at)
    assert run(s3.restore_status(handle)).state is RestoreState.EXPIRED
    assert run(s3.head(ref)).restore_state is RestoreState.EXPIRED
    with pytest.raises(NotRestored):
        read(s3, ref)
    kinds = [r.kind for r in run(s3.read_changes("b", start)).records]
    assert kinds == [
        ChangeKind.RESTORE_STARTED,
        ChangeKind.RESTORE_COMPLETED,
        ChangeKind.RESTORE_EXPIRED,
    ]


def test_restoring_again_extends_the_expiry(s3: SimBackend, clock: VirtualClock) -> None:
    ref = put(s3, "b", "k", b"x", tier=LogicalTier.ARCHIVE)
    first = run(s3.restore(ref, RestoreSpeed.EXPEDITED, days=1))
    clock.advance(timedelta(minutes=5))
    old = run(s3.restore_status(first)).expires_at
    assert old is not None
    again = run(s3.restore(ref, RestoreSpeed.STANDARD, days=5))
    new = run(s3.restore_status(again)).expires_at
    assert new is not None and new > old
    clock.set(old)
    assert read(s3, ref) == b"x"  # the old expiry no longer applies


def test_restore_in_progress_returns_the_same_restore(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"x", tier=LogicalTier.ARCHIVE)
    a = run(s3.restore(ref, RestoreSpeed.BULK, days=1))
    b = run(s3.restore(ref, RestoreSpeed.BULK, days=1))
    assert a.provider_ref == b.provider_ref


def test_intelligent_tiering_archive_restore_has_no_days_and_never_expires(
    s3: SimBackend, clock: VirtualClock
) -> None:
    ref = put(s3, "b", "k", b"i" * (128 * 1024), tier=LogicalTier.SMART)
    assert s3.it_archive(ref)
    assert run(s3.head(ref)).restore_state is RestoreState.ARCHIVED
    with pytest.raises(NotRestored):
        read(s3, ref, 0, 1)
    with pytest.raises(UnsupportedOperation, match="Days"):
        run(s3.restore(ref, RestoreSpeed.STANDARD, days=1))
    start = run(s3.read_changes("b", None)).cursor
    handle = run(s3.restore(ref, RestoreSpeed.STANDARD))
    clock.advance(timedelta(hours=5))
    status = run(s3.restore_status(handle))
    assert status.state is RestoreState.COMPLETED and status.expires_at is None
    clock.advance(timedelta(days=365))
    assert run(s3.head(ref)).restore_state is RestoreState.NOT_NEEDED
    assert read(s3, ref, 0, 1) == b"i"
    kinds = [r.kind for r in run(s3.read_changes("b", start)).records]
    assert ChangeKind.RESTORE_EXPIRED not in kinds


def test_intelligent_tiering_never_tiers_small_objects(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"x" * (128 * 1024 - 1), tier=LogicalTier.SMART)
    assert not s3.it_archive(ref)
    assert run(s3.head(ref)).restore_state is RestoreState.NOT_NEEDED


def test_deep_archive_has_no_expedited(s3: SimBackend, clock: VirtualClock) -> None:
    ref = put(s3, "b", "k", b"x")
    run(s3.set_class(ref, LogicalTier.ARCHIVE, native_class="DEEP_ARCHIVE"))
    with pytest.raises(UnsupportedOperation, match="Deep Archive"):
        run(s3.restore(ref, RestoreSpeed.EXPEDITED, days=1))
    handle = run(s3.restore(ref, RestoreSpeed.STANDARD, days=1))
    clock.advance(timedelta(hours=11))
    assert run(s3.restore_status(handle)).state is RestoreState.IN_PROGRESS
    clock.advance(timedelta(hours=1))  # "within 12 h" [V]
    assert run(s3.restore_status(handle)).state is RestoreState.COMPLETED
    it = put(s3, "b", "it", b"x" * (256 * 1024), tier=LogicalTier.SMART)
    s3.it_archive(it, deep=True)
    with pytest.raises(UnsupportedOperation):
        run(s3.restore(it, RestoreSpeed.EXPEDITED))


def test_expedited_only_under_250_mb(s3: SimBackend) -> None:
    big = s3.put_synthetic("b", "big", 250 * MB, tier=LogicalTier.ARCHIVE)
    with pytest.raises(UnsupportedOperation, match="250 MB"):
        run(s3.restore(big, RestoreSpeed.EXPEDITED, days=1))
    small = s3.put_synthetic("b", "small", 250 * MB - 1, tier=LogicalTier.ARCHIVE)
    run(s3.restore(small, RestoreSpeed.EXPEDITED, days=1))
    with pytest.raises(UnsupportedOperation):
        run(s3.restore(small, RestoreSpeed.HIGH, days=1))


def test_1000_restores_per_second_then_throttled(clock: VirtualClock) -> None:
    sim = SimBackend.s3(clock, restore_rate_limit=1000)
    refs = [sim.put_synthetic("b", f"k{i}", 1, tier=LogicalTier.ARCHIVE) for i in range(1001)]
    clock.advance(timedelta(milliseconds=250))
    for ref in refs[:1000]:
        run(sim.restore(ref, RestoreSpeed.BULK, days=1))
    with pytest.raises(Throttled) as info:
        run(sim.restore(refs[1000], RestoreSpeed.BULK, days=1))
    assert info.value.retry_after == timedelta(milliseconds=750)
    clock.advance(info.value.retry_after)
    run(sim.restore(refs[1000], RestoreSpeed.BULK, days=1))


def test_online_objects_need_no_restore(s3: SimBackend) -> None:
    for tier in (LogicalTier.HOT, LogicalTier.WARM, LogicalTier.COLD, LogicalTier.SMART):
        ref = put(s3, "b", str(tier), b"x", tier=tier)
        with pytest.raises(UnsupportedOperation):
            run(s3.restore(ref, RestoreSpeed.STANDARD, days=1))
