"""The worker reaches the sim Blob facade over HTTP when the catalog row says
``data_plane = "sim_http"`` (the end-to-end setup, where the facade is its
own process): reads through the Blob data plane, every control call (tier
moves, restores, the change feed, retention) through ``/_sim/backend``, on
the same sim the edge writes to."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from omnifile_service.api.dataplane import SimHttpDataPlane
from omnifile_service.backends.base import (
    ChangeKind,
    Immutable,
    LogicalTier,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    RestoreState,
    UnsupportedOperation,
)
from omnifile_service.backends.sim import OffsetClock, SimBackend, VirtualClock
from omnifile_service.backends.sim_http import BlobFacade, create_app
from omnifile_service.worker.backends import (
    BackendRegistry,
    BackendRow,
    SimHttpBackend,
    capabilities_for,
)

BASE = "http://sim.test"


async def _one(data: bytes):
    yield data


def _client(sim: SimBackend, *, layout: str = "azure_two_account") -> tuple[BlobFacade, Any]:
    facade = BlobFacade(sim)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(facade, admin=True)))
    plane = SimHttpDataPlane(BASE, facade.key, http=http, clock=sim.clock.now)
    return facade, SimHttpBackend(3, plane, layout=layout, clock=sim.clock)


def test_registry_picks_the_http_facade_for_sim_http_rows(tmp_path) -> None:
    key_file = tmp_path / "key.json"
    facade = BlobFacade(SimBackend.azure_two_account(VirtualClock()))
    key_file.write_text(json.dumps(facade.key.to_json()))
    config = {"data_plane": "sim_http", "url": BASE, "key_file": str(key_file),
              "layout": "azure_worm"}  # fmt: skip
    reg = BackendRegistry([BackendRow(3, "sim", config)])
    backend = reg.get(3)
    assert isinstance(backend, SimHttpBackend)
    assert backend.capabilities().worm and backend.capabilities().in_place_archive
    assert capabilities_for("sim", config) == backend.capabilities()
    # No staging bucket unless the row names one: urgent recalls are not staged.
    assert backend.config.staging_bucket is None and backend.config.batch is None


def test_an_s3_sim_row_names_its_staging_bucket_for_the_migration_engine(tmp_path) -> None:
    """The M5 e2e test stages a user's recall through the facade's S3 sim:
    `migrate/staging.py` reads the bucket from the backend's config, as from
    `S3Config.staging_bucket`, and there are no Batch Operations on sim."""
    from omnifile_service.migrate.staging import staging_bucket

    key_file = tmp_path / "key.json"
    facade = BlobFacade(SimBackend.azure_two_account(VirtualClock()))
    key_file.write_text(json.dumps(facade.key.to_json()))
    config = {"data_plane": "sim_http", "flavour": "s3", "url": BASE, "key_file": str(key_file),
              "staging_bucket": "ops"}  # fmt: skip
    backend = BackendRegistry([BackendRow(2, "sim", config)]).get(2)
    assert backend.config.staging_bucket == "ops" and backend.config.batch is None
    assert staging_bucket(backend, {"src_container": "src"}) == "ops"
    # Never into the dataset's own bucket.
    assert staging_bucket(backend, {"src_container": "ops"}) is None


def test_head_over_http_sees_the_facade_blob() -> None:
    async def run() -> None:
        sim = SimBackend.azure_two_account(VirtualClock())
        await sim.put("live/data", "5/6", _one(b"x" * 1234), None, {})
        _, backend = _client(sim)
        head = await backend.head(ObjectRef(3, "live/data", "5/6"))
        assert head.size == 1234 and head.logical_tier is LogicalTier.SMART
        with pytest.raises(ObjectNotFound):
            await backend.head(ObjectRef(3, "live/data", "nope"))
        await backend.close()

    asyncio.run(run())


def test_the_services_own_objects_are_put_over_rpc() -> None:
    """Snapshot export parts (`exports.py`): the worker writes into the
    facade's sim, and reads them back through the data plane."""

    async def run() -> None:
        sim = SimBackend.azure_two_account(VirtualClock())
        _, backend = _client(sim)

        async def parts():
            yield b"first "
            yield b"second"

        ref = await backend.put("live/omnifile-exports", "e/part-00000", parts(), None, {})
        assert (await sim.head(ref)).size == 12
        got = b"".join([c async for c in backend.get_range(ref, 0, 12)])
        assert got == b"first second"
        with pytest.raises(UnsupportedOperation):
            await backend.put("live/x", "k", _one(b"x"), None, {}, crc64nvme=b"\0" * 8)
        await backend.close()

    asyncio.run(run())


def test_archive_move_and_rehydrate_over_rpc_on_the_facade_clock() -> None:
    """Two-account layout: archive copies into the archive account and
    deletes the original; a Standard rehydrate copies into the recall
    container and completes 15 h later on the facade's clock."""

    async def run() -> None:
        clock = VirtualClock()
        sim = SimBackend.azure_two_account(clock)
        data = b"seismic" * 1000
        ref = await sim.put("live/data", "a", _one(data), None, {})
        _, backend = _client(sim)
        arch = await backend.set_class(ref, LogicalTier.ARCHIVE)
        assert arch.container == "archive/data"
        with pytest.raises(ObjectNotFound):
            await backend.head(ObjectRef(3, "live/data", "a"))
        head = await backend.head(arch)
        assert head.logical_tier is LogicalTier.ARCHIVE
        assert head.restore_state is RestoreState.ARCHIVED
        start = await backend.read_changes("archive/data", None)  # None: from now
        handle = await backend.restore(arch, RestoreSpeed.STANDARD)
        assert handle.target is not None and handle.target.container == "live/recall"
        status = await backend.restore_status(handle)
        assert status.state is RestoreState.IN_PROGRESS
        assert status.eta == clock.now() + timedelta(hours=15)
        clock.advance(timedelta(hours=15, minutes=1))
        assert (await backend.restore_status(handle)).state is RestoreState.COMPLETED
        got = b"".join([c async for c in backend.get_range(handle.target, 0, len(data))])
        assert got == data
        page = await backend.read_changes("archive/data", start.cursor)
        kinds = {r.kind for r in page.records}
        assert {ChangeKind.RESTORE_STARTED, ChangeKind.RESTORE_COMPLETED} <= kinds
        with pytest.raises(UnsupportedOperation):  # an explicit tier never returns to Smart
            await backend.set_class(arch, LogicalTier.SMART)
        await backend.close()

    asyncio.run(run())


def test_worm_errors_cross_the_wire() -> None:
    async def run() -> None:
        clock = VirtualClock()
        sim = SimBackend.azure_worm(clock)
        sim.create_container("worm/data", worm=True)
        ref = await sim.put("worm/data", "w", _one(b"v1"), None, {})
        _, backend = _client(sim, layout="azure_worm")
        await backend.set_container_policy("worm/data", 3, locked=False)
        await backend.set_retention(ref, clock.now() + timedelta(days=3), locked=True)
        with pytest.raises(Immutable):
            await backend.delete(ref)
        moved = await backend.set_class(ref, LogicalTier.ARCHIVE)  # in place [W]
        assert (moved.container, moved.key) == (ref.container, ref.key)
        versions = await backend.list_versions(ref)
        assert [v.ref.version_id for v in versions] == [ref.version_id]
        await backend.close()

    asyncio.run(run())


def test_offset_clock_is_shared_through_its_file(tmp_path) -> None:
    path = tmp_path / "clock"
    a, b = OffsetClock(path), OffsetClock(path)
    t0 = b.now()
    a.advance(timedelta(days=2, seconds=0.5))
    assert path.read_text() == "+172801\n"  # libfaketime's relative form, whole seconds
    assert timedelta(days=2) <= b.now() - t0 < timedelta(days=2, seconds=30)
    with pytest.raises(ValueError):
        a.advance(timedelta(seconds=-1))


def test_advance_rotates_and_publishes_a_lapsed_key(tmp_path) -> None:
    """After the clock jumps past the user delegation key's 7 days, the
    facade issues a new key and publishes it; the api's plane re-reads it."""
    clock_file, key_file = tmp_path / "clock", tmp_path / "key.json"
    clock = OffsetClock(clock_file)
    facade = BlobFacade(SimBackend.azure_two_account(clock), key_file=str(key_file))
    first = facade.key
    plane = SimHttpDataPlane.from_key_file(BASE, key_file, clock=clock.now)
    app = create_app(facade, admin=True)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as h:
            r = await h.post("/_sim/advance", json={"seconds": 8 * 86400})
            assert r.status_code == 200
        assert facade.key is not first and len(facade.keys) == 2
        plane.url(type("T", (), {"container": "live/data", "key": "k", "version_id": None})(),
                  "r", clock.now() + timedelta(minutes=5), version=False)  # fmt: skip
        assert plane.key.value == facade.key.value

    asyncio.run(run())


def test_dev_clock_is_refused_outside_dev_mode_and_over_real_backends(tmp_path) -> None:
    from omnifile_service.backends.base import SystemClock
    from omnifile_service.backends.sim.clock import DevClockRefused, dev_clock, require_sim_only

    path = str(tmp_path / "clock")
    assert isinstance(dev_clock({}), SystemClock)
    with pytest.raises(DevClockRefused):  # a dev setting in production fails, never ignored
        dev_clock({"OMNIFILE_DEV_CLOCK_FILE": path})
    clock = dev_clock({"OMNIFILE_DEV_CLOCK_FILE": path, "OMNIFILE_DEV_MODE": "1"})
    assert isinstance(clock, OffsetClock)
    require_sim_only(clock, {1: "sim", 2: "sim"})
    with pytest.raises(DevClockRefused, match="azure_blob"):
        require_sim_only(clock, {1: "sim", 2: "azure_blob"})
    require_sim_only(SystemClock(), {2: "azure_blob"})  # the real clock: anything goes


def test_worker_and_api_refuse_the_dev_clock_over_azure(monkeypatch, tmp_path) -> None:
    from omnifile_service.api import __main__ as api_main
    from omnifile_service.worker import __main__ as worker_main

    monkeypatch.setenv("OMNIFILE_DEV_CLOCK_FILE", str(tmp_path / "clock"))
    monkeypatch.delenv("OMNIFILE_DEV_MODE", raising=False)
    monkeypatch.setenv("OMNIFILE_DATABASE_URL", "postgresql://unused")
    with pytest.raises(SystemExit, match="OMNIFILE_DEV_MODE"):
        asyncio.run(worker_main.main())

    monkeypatch.setenv("OMNIFILE_DEV_MODE", "1")

    class Registry:
        @staticmethod
        async def load(conn, clock=None):
            return BackendRegistry([BackendRow(1, "sim"), BackendRow(2, "azure_blob")])

    class Conn:
        async def close(self) -> None: ...

    async def connect(*a, **kw):
        return Conn()

    monkeypatch.setattr(worker_main, "BackendRegistry", Registry)
    monkeypatch.setattr(worker_main.psycopg.AsyncConnection, "connect", connect)
    with pytest.raises(SystemExit, match="not sim"):
        asyncio.run(worker_main.main())

    monkeypatch.setattr(api_main, "backend_kinds", lambda conninfo: {7: "azure_blob"})
    with pytest.raises(SystemExit, match="not sim"):
        api_main.service_clock("postgresql://unused")
    monkeypatch.setattr(api_main, "backend_kinds", lambda conninfo: {7: "sim"})
    assert isinstance(api_main.service_clock("postgresql://unused"), OffsetClock)
    monkeypatch.delenv("OMNIFILE_DEV_MODE")
    with pytest.raises(SystemExit, match="OMNIFILE_DEV_MODE"):
        api_main.service_clock("postgresql://unused")
