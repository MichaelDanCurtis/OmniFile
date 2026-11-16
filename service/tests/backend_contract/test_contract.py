"""The backend contract. Every registered backend runs every test here.

Async calls run through `run()`, so the suite needs no async plugin.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from typing import Any

import pytest

from omnifile_service.backends.base import (
    BackendError,
    ChangeKind,
    EdgeAccess,
    Immutable,
    LogicalTier,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    RestoreState,
    StorageBackend,
    UnsupportedOperation,
)

from .clock import ManualClock

C = "contract"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _read(b: StorageBackend, ref: ObjectRef, offset: int, length: int) -> bytes:
    return b"".join([chunk async for chunk in b.get_range(ref, offset, length)])


def _put(b: StorageBackend, key: str, data: bytes) -> ObjectRef:
    return run(b.put(C, key, _stream(data), None, {"file-owner": "0"}))


# --- shape -----------------------------------------------------------------


def test_implements_protocol(backend: StorageBackend) -> None:
    assert isinstance(backend, StorageBackend)


def test_capabilities_are_consistent(backend: StorageBackend) -> None:
    caps = backend.capabilities()
    assert caps.kind == backend.kind
    assert caps.checksum == "crc64nvme"
    assert caps.tiers, "a backend offers at least one tier"
    for logical, infos in caps.tiers.items():
        assert infos, f"{logical} maps to no native class"
        assert all(i.logical == logical for i in infos)
    for info in caps.tiers.get(LogicalTier.ARCHIVE, []):
        assert info.minimum_days, "archive classes have a minimum duration"
    for tier in (LogicalTier.HOT, LogicalTier.WARM, LogicalTier.SMART):
        assert not any(i.needs_restore for i in caps.tiers.get(tier, []))
    if caps.explicit_tier_leaves_smart:
        assert LogicalTier.SMART in caps.tiers
    if caps.worm:
        assert caps.versioning, "version-level WORM needs versioning"
    natives = {i.native_class for infos in caps.tiers.values() for i in infos}
    for opt in caps.restore_options:
        assert opt.from_classes <= natives
        low, high = opt.typical_hours
        assert 0 <= low <= high


# --- data path -------------------------------------------------------------


def test_put_head_and_ranged_read(backend: StorageBackend) -> None:
    data = bytes(range(256)) * 4
    ref = _put(backend, "a/one.segy", data)
    head = run(backend.head(ObjectRef(ref.backend_id, C, "a/one.segy")))
    assert head.size == len(data)
    assert head.restore_state == RestoreState.NOT_NEEDED
    assert run(_read(backend, ref, 0, len(data))) == data
    assert run(_read(backend, ref, 100, 50)) == data[100:150]
    assert run(_read(backend, ref, len(data) - 10, 100)) == data[-10:]


def test_committed_blocks_describe_the_version(backend: StorageBackend) -> None:
    """Upload `complete` relies on this (control-api.md section 4.2): the
    committed block list of a version, in order, covering its bytes. Backends
    without block lists (S3) say so with UnsupportedOperation."""
    v1 = _put(backend, "blocks.segy", bytes(range(200)))
    v2 = _put(backend, "blocks.segy", b"short")
    try:
        first = run(backend.committed_blocks(v1))
    except UnsupportedOperation:
        with pytest.raises(UnsupportedOperation):
            run(backend.committed_blocks(v2))
        return
    assert first and all(b.block_id for b in first)
    assert sum(b.size for b in first) == 200
    if backend.capabilities().versioning:
        assert sum(b.size for b in run(backend.committed_blocks(v1))) == 200
    assert sum(b.size for b in run(backend.committed_blocks(v2))) == len(b"short")
    for b in first:
        assert b.crc64nvme is None or len(b.crc64nvme) == 8
    with pytest.raises(ObjectNotFound):
        run(backend.committed_blocks(ObjectRef(v1.backend_id, C, "no-such-blob")))


def test_timestamps_come_from_the_backend_clock(
    backend: StorageBackend, clock: ManualClock
) -> None:
    assert backend.clock is clock
    ref = _put(backend, "t", b"x")
    head = run(backend.head(ref))
    assert head.last_modified is None or head.last_modified == clock.now()
    cred = run(backend.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=30)))
    if cred.expires_at is not None:
        assert cred.expires_at == clock.now() + timedelta(minutes=30)


def test_list_pages_through_a_prefix(backend: StorageBackend) -> None:
    for i in range(5):
        _put(backend, f"p/{i}", b"x")
    _put(backend, "other/0", b"x")
    keys: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = run(backend.list(C, "p/", cursor))
        keys += [h.ref.key for h in page.items]
        cursor = page.cursor
        if cursor is None:
            break
    assert sorted(keys) == [f"p/{i}" for i in range(5)]


def test_server_side_copy(backend: StorageBackend) -> None:
    src = _put(backend, "copy/src", b"payload")
    dst = run(backend.copy_object(src, C, "copy/dst"))
    assert dst.key == "copy/dst"
    assert run(_read(backend, dst, 0, 100)) == b"payload"


def test_delete_then_head_is_not_found(backend: StorageBackend) -> None:
    ref = _put(backend, "gone", b"x")
    current = ObjectRef(ref.backend_id, C, "gone")
    run(backend.delete(current))
    with pytest.raises(ObjectNotFound):
        run(backend.head(current))
    with pytest.raises(ObjectNotFound):
        run(backend.delete(current))


def test_change_feed_reports_creates_and_deletes(
    backend: StorageBackend, clock: ManualClock
) -> None:
    start = run(backend.read_changes(C, None)).cursor
    ref = _put(backend, "feed/x", b"x")
    run(backend.delete(ObjectRef(ref.backend_id, C, "feed/x")))
    clock.advance(timedelta(minutes=10))  # Azure delivers records "within minutes"
    page = run(backend.read_changes(C, start))
    kinds = [(r.kind, r.ref.key) for r in page.records]
    assert (ChangeKind.CREATED, "feed/x") in kinds
    assert (ChangeKind.DELETED, "feed/x") in kinds
    again = run(backend.read_changes(C, page.cursor))
    assert again.records == []
    assert again.cursor == page.cursor


# --- versioning (decision 30) ----------------------------------------------


def test_versions_keep_old_bytes(backend: StorageBackend) -> None:
    if not backend.capabilities().versioning:
        pytest.skip("no versioning")
    first = _put(backend, "v", b"first")
    second = _put(backend, "v", b"second")
    assert first.version_id and second.version_id and first.version_id != second.version_id
    versions = run(backend.list_versions(ObjectRef(first.backend_id, C, "v")))
    assert versions[0].is_current and versions[0].ref.version_id == second.version_id
    assert {v.ref.version_id for v in versions} >= {first.version_id, second.version_id}
    assert run(_read(backend, first, 0, 100)) == b"first"
    assert run(_read(backend, ObjectRef(first.backend_id, C, "v"), 0, 100)) == b"second"


# --- WORM (decision 31) ----------------------------------------------------


def test_worm_retention_blocks_delete_and_never_shortens(
    backend: StorageBackend, clock: ManualClock
) -> None:
    if not backend.capabilities().worm:
        with pytest.raises(UnsupportedOperation):
            run(backend.set_retention(_put(backend, "w", b"x"), clock.now()))
        return
    ref = _put(backend, "w", b"x")
    until = clock.now() + timedelta(days=30)
    run(backend.set_retention(ref, until))
    with pytest.raises(Immutable):
        run(backend.delete(ref))
    with pytest.raises(Immutable):
        run(backend.set_retention(ref, until - timedelta(days=1)))
    run(backend.set_retention(ref, until + timedelta(days=1)))
    run(backend.set_legal_hold(ref, True))
    clock.advance(timedelta(days=60))
    with pytest.raises(Immutable):
        run(backend.delete(ref))
    run(backend.set_legal_hold(ref, False))
    run(backend.delete(ref))


# --- archive and restore ---------------------------------------------------


def test_archive_needs_restore_then_reads(backend: StorageBackend, clock: ManualClock) -> None:
    caps = backend.capabilities()
    if LogicalTier.ARCHIVE not in caps.tiers or not caps.restore_options:
        pytest.skip("no archive tier")
    ref = _put(backend, "cold/f", b"seismic")
    archived = run(backend.set_class(ref, LogicalTier.ARCHIVE))
    head = run(backend.head(archived))
    assert head.logical_tier == LogicalTier.ARCHIVE
    with pytest.raises(NotRestored):
        run(_read(backend, archived, 0, 7))
    speed = RestoreSpeed.STANDARD
    handle = run(backend.restore(archived, speed, days=1))
    status = run(backend.restore_status(handle))
    assert status.state in (RestoreState.IN_PROGRESS, RestoreState.COMPLETED)
    if status.eta is not None:
        assert status.eta >= clock.now()
    for _ in range(100):
        if status.state == RestoreState.COMPLETED:
            break
        clock.advance(timedelta(hours=1))
        status = run(backend.restore_status(handle))
    assert status.state == RestoreState.COMPLETED
    readable = status.target or archived
    assert run(_read(backend, readable, 0, 7)) == b"seismic"


def test_unsupported_tier_is_an_error_not_a_silent_default(backend: StorageBackend) -> None:
    caps = backend.capabilities()
    missing = [t for t in LogicalTier if t not in caps.tiers]
    if not missing:
        pytest.skip("backend offers every tier")
    with pytest.raises(BackendError):
        run(backend.put(C, "nope", _stream(b"x"), missing[0], {}))


# --- tags and metadata (holds in tags; decision 24 stamps metadata) ----------


def test_tags_round_trip_and_are_limited(backend: StorageBackend) -> None:
    ref = _put(backend, "tags/a", b"x")
    run(backend.set_tags(ref, {"omnifile-state": "verified", "hold": "legal"}))
    assert run(backend.get_tags(ref)) == {"omnifile-state": "verified", "hold": "legal"}
    run(backend.set_tags(ref, {}))
    assert run(backend.get_tags(ref)) == {}
    too_many = {f"k{i}": "v" for i in range(backend.capabilities().max_tags + 1)}
    with pytest.raises(BackendError):
        run(backend.set_tags(ref, too_many))


def test_metadata_can_be_replaced(backend: StorageBackend) -> None:
    ref = _put(backend, "meta/a", b"x")
    new = run(backend.set_metadata(ref, {"file-owner": "1000", "file-permissions": "0640"}))
    head = run(backend.head(ObjectRef(new.backend_id, C, "meta/a")))
    assert head.metadata.get("file-owner") == "1000"
    assert run(_read(backend, new, 0, 10)) == b"x"


def test_archived_objects_take_tags_but_not_metadata(backend: StorageBackend) -> None:
    caps = backend.capabilities()
    if LogicalTier.ARCHIVE not in caps.tiers:
        pytest.skip("no archive tier")
    archived = run(backend.set_class(_put(backend, "arch/t", b"x"), LogicalTier.ARCHIVE))
    run(backend.set_tags(archived, {"omnifile-state": "archived"}))
    assert run(backend.get_tags(archived)) == {"omnifile-state": "archived"}
    if not caps.metadata_on_archive:
        with pytest.raises(UnsupportedOperation):
            run(backend.set_metadata(archived, {"file-owner": "0"}))
