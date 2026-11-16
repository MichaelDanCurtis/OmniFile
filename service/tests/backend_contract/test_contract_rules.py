"""Provider rules every backend must obey when it advertises the feature.

Each test is gated on `capabilities()`, so a backend that lacks the feature
skips it. They run against sim today and against the real Azure and S3
backends later; a difference is a bug in sim (BUILD-PLAN "Test strategy").

Rules that need very large objects (Azure's 10 GiB/h High-priority cap, no
promises above 10 GB) live in tests/sim, which can fake object sizes.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    ChangeKind,
    Immutable,
    LogicalTier,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    RestoreHandle,
    RestoreSpeed,
    RestoreState,
    RestoreStatus,
    StorageBackend,
    UnsupportedOperation,
)
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes

from .clock import ManualClock
from .test_contract import C, _put, _read, _stream, run


def _current(ref: ObjectRef) -> ObjectRef:
    return ObjectRef(ref.backend_id, ref.container, ref.key)


def _archive(b: StorageBackend, key: str, data: bytes) -> ObjectRef:
    caps = b.capabilities()
    if LogicalTier.ARCHIVE not in caps.tiers or not caps.restore_options:
        pytest.skip("no archive tier")
    return run(b.set_class(_put(b, key, data), LogicalTier.ARCHIVE))


def _speed(b: StorageBackend) -> RestoreSpeed:
    speeds = {o.speed for o in b.capabilities().restore_options}
    return RestoreSpeed.STANDARD if RestoreSpeed.STANDARD in speeds else sorted(speeds)[0]


def _wait(b: StorageBackend, clock: ManualClock, handle: RestoreHandle) -> RestoreStatus:
    status = run(b.restore_status(handle))
    for _ in range(100):
        if status.state is not RestoreState.IN_PROGRESS:
            break
        clock.advance(timedelta(hours=1))
        status = run(b.restore_status(handle))
    return status


# --- integrity --------------------------------------------------------------


def test_head_checksums_match_the_bytes(backend: StorageBackend) -> None:
    data = b"seg-y header" * 50
    head = run(backend.head(_put(backend, "sum", data)))
    if head.crc64nvme is not None:
        assert head.crc64nvme == to_bytes(crc64nvme(data))
    if head.md5 is not None:
        assert head.md5 == hashlib.md5(data).digest()


def test_posix_metadata_survives_the_round_trip(backend: StorageBackend) -> None:
    meta = {"file-owner": "1001", "file-group": "2001", "file-permissions": "0640"}
    ref = run(backend.put(C, "meta", _stream(b"x"), None, meta))
    head = run(backend.head(ref))
    if not head.metadata:
        pytest.skip("backend does not return user metadata")
    assert {k: head.metadata.get(k) for k in meta} == meta


# --- Smart (Azure) -----------------------------------------------------------


def test_explicit_tier_leaves_smart_for_good(backend: StorageBackend) -> None:
    caps = backend.capabilities()
    if not caps.explicit_tier_leaves_smart:
        pytest.skip("no Smart one-way rule")
    ref = _put(backend, "smart/a", b"x")
    head = run(backend.head(ref))
    if head.logical_tier is not LogicalTier.SMART:
        pytest.skip("the contract container's default tier is not Smart")
    moved = run(backend.set_class(ref, LogicalTier.HOT))
    assert run(backend.head(moved)).logical_tier is LogicalTier.HOT
    with pytest.raises(UnsupportedOperation):
        run(backend.set_class(moved, LogicalTier.SMART))
    # An upload with an explicit tier never enters Smart either.
    explicit = run(backend.put(C, "smart/b", _stream(b"x"), LogicalTier.HOT, {}))
    with pytest.raises(UnsupportedOperation):
        run(backend.set_class(explicit, LogicalTier.SMART))


def test_archive_from_smart_moves_to_another_account(backend: StorageBackend) -> None:
    caps = backend.capabilities()
    if not caps.archive_needs_separate_account:
        pytest.skip("archive is in place")
    ref = _put(backend, "smart/arch", b"bytes")
    archived = _archive_ref(backend, ref)
    assert archived.container != ref.container
    with pytest.raises(ObjectNotFound):
        run(_read(backend, ref, 0, 5))


def _archive_ref(b: StorageBackend, ref: ObjectRef) -> ObjectRef:
    return run(b.set_class(ref, LogicalTier.ARCHIVE))


# --- archive and restore -----------------------------------------------------


def test_archived_object_reports_archived(backend: StorageBackend) -> None:
    archived = _archive(backend, "arch/state", b"x")
    assert run(backend.head(archived)).restore_state is RestoreState.ARCHIVED


def test_unsupported_restore_speed_is_refused(backend: StorageBackend) -> None:
    archived = _archive(backend, "arch/speed", b"x")
    offered = {o.speed for o in backend.capabilities().restore_options}
    missing = [s for s in RestoreSpeed if s not in offered]
    if not missing:
        pytest.skip("every speed offered")
    with pytest.raises(UnsupportedOperation):
        run(backend.restore(archived, missing[0], days=1))


def test_restore_events_are_ordered_in_the_change_feed(
    backend: StorageBackend, clock: ManualClock
) -> None:
    archived = _archive(backend, "arch/feed", b"abc")
    container = archived.container
    cursor = run(backend.read_changes(container, None)).cursor
    handle = run(backend.restore(archived, _speed(backend), days=1))
    status = _wait(backend, clock, handle)
    assert status.state is RestoreState.COMPLETED
    clock.advance(timedelta(minutes=10))
    records = []
    page = run(backend.read_changes(container, cursor))
    records += page.records
    while not page.caught_up:
        page = run(backend.read_changes(container, page.cursor))
        records += page.records
    times = [r.at for r in records]
    assert times == sorted(times)
    kinds = [r.kind for r in records if r.ref.key == archived.key]
    assert kinds.index(ChangeKind.RESTORE_STARTED) < kinds.index(ChangeKind.RESTORE_COMPLETED)


def test_temporary_restored_copy_expires(backend: StorageBackend, clock: ManualClock) -> None:
    archived = _archive(backend, "arch/expiry", b"temp")
    handle = run(backend.restore(archived, _speed(backend), days=1))
    status = _wait(backend, clock, handle)
    assert status.state is RestoreState.COMPLETED
    if status.expires_at is None:
        pytest.skip("restores here make a permanent copy")
    readable = status.target or archived
    assert run(_read(backend, readable, 0, 4)) == b"temp"
    # S3: Days counts from completion and rounds up to the next midnight UTC.
    assert status.expires_at - clock.now() <= timedelta(days=2)
    clock.advance(status.expires_at - clock.now())
    assert run(backend.restore_status(handle)).state is RestoreState.EXPIRED
    assert run(backend.head(archived)).restore_state is RestoreState.EXPIRED
    with pytest.raises(NotRestored):
        run(_read(backend, archived, 0, 4))


# --- versioning and WORM -----------------------------------------------------


def test_tombstone_keeps_previous_versions(backend: StorageBackend) -> None:
    if not backend.capabilities().versioning:
        pytest.skip("no versioning")
    first = _put(backend, "tomb", b"first")
    run(backend.delete(_current(first)))
    assert run(_read(backend, first, 0, 10)) == b"first"
    versions = run(backend.list_versions(_current(first)))
    assert not any(v.is_current and not v.is_delete_marker for v in versions)


def test_worm_overwrite_makes_a_new_version_and_keeps_the_old(
    backend: StorageBackend, clock: ManualClock
) -> None:
    caps = backend.capabilities()
    if not caps.worm:
        pytest.skip("no WORM")
    first = _put(backend, "worm/f", b"v1")
    run(backend.set_retention(first, clock.now() + timedelta(days=7)))
    second = _put(backend, "worm/f", b"v2")
    assert second.version_id != first.version_id
    with pytest.raises(Immutable):
        run(backend.delete(first))
    run(backend.delete(_current(first)))  # a tombstone destroys nothing
    assert run(_read(backend, first, 0, 2)) == b"v1"
    head = run(backend.head(first))
    assert head.retention_until == clock.now() + timedelta(days=7)


def test_set_tier_is_allowed_on_immutable_versions(
    backend: StorageBackend, clock: ManualClock
) -> None:
    caps = backend.capabilities()
    if not (caps.worm and caps.in_place_archive and LogicalTier.ARCHIVE in caps.tiers):
        pytest.skip("needs WORM with in-place archive")
    ref = _put(backend, "worm/tier", b"x")
    run(backend.set_retention(ref, clock.now() + timedelta(days=30)))
    run(backend.set_legal_hold(ref, True))
    archived = run(backend.set_class(ref, LogicalTier.ARCHIVE))
    assert archived == ref
    assert run(backend.head(ref)).logical_tier is LogicalTier.ARCHIVE
