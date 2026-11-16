"""Azure rehydrate: copy into the recall container, priorities, the 10 GiB/h cap."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    ChangeKind,
    LogicalTier,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    RestoreState,
    UnsupportedOperation,
)
from omnifile_service.backends.sim import (
    AccountConfig,
    Flavour,
    Redundancy,
    SimBackend,
    SimTimings,
    VirtualClock,
)

from .helpers import put, read, run

GiB = 1024**3


def _archived(sim: SimBackend, key: str, data: bytes = b"seismic") -> ObjectRef:
    return run(sim.set_class(put(sim, "data", key, data), LogicalTier.ARCHIVE))


def _archived_big(sim: SimBackend, key: str, size: int) -> ObjectRef:
    """A large file in the archive account. Synthetic bytes carry no
    Content-MD5, so the backend's own archive move (which can't read its copy
    back) won't delete the original (`copy_check`); put it there directly."""
    return sim.put_synthetic("archive/data", key, size, tier=LogicalTier.ARCHIVE)


def test_standard_rehydrate_copies_into_the_recall_container(
    azure: SimBackend, clock: VirtualClock
) -> None:
    archived = _archived(azure, "f.segy")
    handle = run(azure.restore(archived, RestoreSpeed.STANDARD))
    assert handle.target is not None and handle.target.container == "live/recall"
    status = run(azure.restore_status(handle))
    assert status.state is RestoreState.IN_PROGRESS
    assert status.eta == clock.now() + timedelta(hours=15)
    # The copy exists but is not readable until the rehydrate finishes.
    assert run(azure.head(handle.target)).restore_state is RestoreState.IN_PROGRESS
    with pytest.raises(NotRestored):
        read(azure, handle.target)
    clock.advance(timedelta(hours=15))
    status = run(azure.restore_status(handle))
    assert status.state is RestoreState.COMPLETED and status.target == handle.target
    assert read(azure, handle.target) == b"seismic"
    # The archived original is untouched: no 180-day early-deletion fee [A].
    assert run(azure.head(archived)).restore_state is RestoreState.ARCHIVED
    assert azure.early_deletion_fees == []


def test_worm_recall_goes_to_a_deletable_container(worm: SimBackend, clock: VirtualClock) -> None:
    ref = put(worm, "data", "f", b"x")
    run(worm.set_retention(ref, clock.now() + timedelta(days=3650)))
    run(worm.set_class(ref, LogicalTier.ARCHIVE))
    handle = run(worm.restore(ref, RestoreSpeed.HIGH))
    clock.advance(timedelta(hours=1))
    assert run(worm.restore_status(handle)).state is RestoreState.COMPLETED
    assert handle.target is not None and handle.target.container == "worm/recall"
    run(worm.delete(handle.target))  # the recall container has no policy [W]
    with pytest.raises(ObjectNotFound):
        run(worm.head(handle.target))


def test_high_priority_shares_10_gib_per_hour_per_account(
    azure: SimBackend, clock: VirtualClock
) -> None:
    start = clock.now()
    handles = [
        run(azure.restore(_archived_big(azure, f"big{i}", 5 * GiB), RestoreSpeed.HIGH))
        for i in range(4)
    ]
    etas = [run(azure.restore_status(h)).eta for h in handles]
    # 4 x 5 GiB = 20 GiB through a 10 GiB/h pipe: the last cannot finish before 2 h.
    assert etas == [
        start + timedelta(hours=1),  # the 1 h floor for a small High rehydrate
        start + timedelta(hours=1.5),
        start + timedelta(hours=2),
        start + timedelta(hours=2.5),
    ]
    assert azure.priority_queue_clear_at("archive/data") == start + timedelta(hours=2)
    clock.advance(timedelta(hours=2))
    states = [run(azure.restore_status(h)).state for h in handles]
    assert states == [RestoreState.COMPLETED] * 3 + [RestoreState.IN_PROGRESS]


def test_priority_cap_is_per_account(clock: VirtualClock) -> None:
    sim = SimBackend(
        clock,
        Flavour.AZURE,
        accounts=[AccountConfig("a", Redundancy.LRS), AccountConfig("b", Redundancy.LRS)],
    )
    for acct in ("a", "b"):
        ref = sim.put_synthetic(f"{acct}/data", "big", 10 * GiB, tier=LogicalTier.ARCHIVE)
        run(sim.restore(ref, RestoreSpeed.HIGH))
    assert sim.priority_queue_clear_at("a/data") == clock.now() + timedelta(hours=1)
    assert sim.priority_queue_clear_at("b/data") == clock.now() + timedelta(hours=1)


def test_no_eta_above_10_gb(azure: SimBackend, clock: VirtualClock) -> None:
    small = run(azure.restore(_archived_big(azure, "s", 10 * 10**9), RestoreSpeed.STANDARD))
    big = run(azure.restore(_archived_big(azure, "b", 10 * 10**9 + 1), RestoreSpeed.STANDARD))
    high = run(azure.restore(_archived_big(azure, "h", 20 * GiB), RestoreSpeed.HIGH))
    assert run(azure.restore_status(small)).eta is not None
    assert run(azure.restore_status(big)).eta is None
    assert run(azure.restore_status(high)).eta is None
    clock.advance(timedelta(hours=16))
    assert run(azure.restore_status(big)).state is RestoreState.COMPLETED


def test_s3_speeds_and_non_archived_sources_are_refused(azure: SimBackend) -> None:
    archived = _archived(azure, "f")
    for speed in (RestoreSpeed.EXPEDITED, RestoreSpeed.BULK):
        with pytest.raises(UnsupportedOperation):
            run(azure.restore(archived, speed))
    with pytest.raises(UnsupportedOperation):
        run(azure.restore(put(azure, "data", "online", b"x"), RestoreSpeed.STANDARD))


def test_injected_restore_failure(azure: SimBackend, clock: VirtualClock) -> None:
    archived = _archived(azure, "f")
    start = run(azure.read_changes("archive/data", None)).cursor
    azure.faults.fail_next_restore("rehydrate failed: injected")
    handle = run(azure.restore(archived, RestoreSpeed.STANDARD))
    assert run(azure.restore_status(handle)).state is RestoreState.IN_PROGRESS
    clock.advance(timedelta(hours=15))
    status = run(azure.restore_status(handle))
    assert (status.state, status.detail) == (RestoreState.FAILED, "rehydrate failed: injected")
    assert handle.target is not None
    with pytest.raises(ObjectNotFound):
        run(azure.head(handle.target))
    kinds = [r.kind for r in run(azure.read_changes("archive/data", start)).records]
    assert kinds == [ChangeKind.RESTORE_STARTED, ChangeKind.RESTORE_FAILED]
    # A retry works.
    retry = run(azure.restore(archived, RestoreSpeed.STANDARD))
    clock.advance(timedelta(hours=15))
    assert run(azure.restore_status(retry)).state is RestoreState.COMPLETED


def test_timings_are_configurable(clock: VirtualClock) -> None:
    sim = SimBackend.azure_two_account(clock, timings=SimTimings(azure_standard=timedelta(hours=2)))
    archived = run(sim.set_class(put(sim, "data", "f", b"x"), LogicalTier.ARCHIVE))
    handle = run(sim.restore(archived, RestoreSpeed.STANDARD))
    clock.advance(timedelta(hours=2))
    assert run(sim.restore_status(handle)).state is RestoreState.COMPLETED
