"""Catalog rows and sim objects for the retention and reconcile tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pooled
import psycopg
from psycopg.types.json import Jsonb

from omnifile_service.backends.base import ObjectRef
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import ConsumerConfig
from omnifile_service.worker.runner import Worker

T = Any


def run[R](coro: Coroutine[Any, Any, R]) -> R:
    return pooled.run(coro)  # asyncio.run, closing the worker pools it made (tests/pooled.py)


def with_conn[R](
    connect: Callable[[], Awaitable[psycopg.AsyncConnection]],
    fn: Callable[[psycopg.AsyncConnection], Awaitable[R]],
) -> R:
    async def go() -> R:
        conn = await connect()
        try:
            return await fn(conn)
        finally:
            await conn.close()

    return run(go())


def real_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def clock_days_ago(days: float) -> VirtualClock:
    """A virtual clock that starts `days` before the real now."""
    return VirtualClock(real_now() - timedelta(days=days))


@dataclass
class World:
    backend_id: int
    dataset_id: int
    root_id: int
    edge_id: int
    share_id: int
    share_name: str
    admin: int  # identity id
    container: str
    sim: SimBackend
    registry: BackendRegistry


def seed(
    c: psycopg.Connection,
    clock: VirtualClock,
    *,
    layout: str = "azure_two_account",
    depth: int = 0,
    depth_max: int = 0,
    feed_lag: timedelta = timedelta(0),
) -> World:
    tag = uuid.uuid4().hex[:8]
    container = "worm/data" if layout == "azure_worm" else "live/data"
    storage = "single_account" if layout == "azure_worm" else "two_account"
    (backend_id,) = c.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'sim', %s, true) RETURNING backend_id",
        (f"sim-{tag}", Jsonb({"layout": layout, "container": container})),
    ).fetchone()
    make = SimBackend.azure_worm if layout == "azure_worm" else SimBackend.azure_two_account
    sim = make(clock, backend_id=backend_id, feed_lag=feed_lag)
    (edge_id,) = c.execute(
        "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
        " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
        (f"edge-{tag}", uuid.uuid4().bytes * 2),
    ).fetchone()
    (dataset_id,) = c.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
        " VALUES (%s, %s, %s, %s) RETURNING dataset_id",
        (f"ds-{tag}", backend_id, edge_id, storage),
    ).fetchone()
    (root_id,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
        (dataset_id,),
    ).fetchone()
    (admin,) = c.execute(
        "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
        (f"S-1-5-21-{int(tag, 16) % 10**9}-500",),
    ).fetchone()
    name = f"share-{tag}"
    (share_id,) = c.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, owners_group_sid,"
        " rewind_depth_days, rewind_depth_max_days)"
        " VALUES (%s, %s, %s, '{smb}', 'S-1-5-21-1-2-3-512', %s, %s) RETURNING share_id",
        (dataset_id, edge_id, name, depth, depth_max),
    ).fetchone()
    registry = BackendRegistry.from_mapping({backend_id: sim})
    return World(backend_id, dataset_id, root_id, edge_id, share_id, name, admin, container,
                 sim, registry)  # fmt: skip


def add_dir(c: psycopg.Connection, w: World, parent: int, name: str) -> int:
    (fid,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, %s, %s, 'dir') RETURNING file_id",
        (w.dataset_id, parent, name),
    ).fetchone()
    return fid


def add_file(c: psycopg.Connection, w: World, name: str, parent: int | None = None) -> int:
    (fid,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state)"
        " VALUES (%s, %s, %s, 'file', 'resident') RETURNING file_id",
        (w.dataset_id, parent or w.root_id, name),
    ).fetchone()
    return fid


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


@dataclass
class Stored:
    file_id: int
    version_id: int
    location_id: int
    ref: ObjectRef


def commit_content(
    c: psycopg.Connection,
    w: World,
    file_id: int,
    data: bytes,
    committed_at: datetime,
    *,
    catalog_size: int | None = None,
    share_blob_of: Stored | None = None,
    key: str | None = None,
) -> Stored:
    """A content version of `file_id`, uploaded to sim (or referencing
    another version's blob, as whole-file dedupe does) and committed at
    `committed_at`."""
    (parent, name) = c.execute(
        "SELECT parent_id, name FROM catalog.file WHERE file_id = %s", (file_id,)
    ).fetchone()
    size = len(data) if catalog_size is None else catalog_size
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', %s, true)", (f"edge:{w.edge_id}",))
        (vid,) = c.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
            " state, size) VALUES (%s, %s, %s, %s, 'content', 'pending', %s) RETURNING version_id",
            (file_id, w.dataset_id, parent, name, size),
        ).fetchone()
        if share_blob_of is not None:
            ref = share_blob_of.ref
        else:
            # `key`: write to a fixed key, so versions share one blob name.
            blob = key or f"{file_id}/{vid}"
            ref = run(w.sim.put(w.container, blob, _one(data), None, {}))
        cls = "Smart" if w.container.startswith("live/") else "Hot"
        tier = "smart" if cls == "Smart" else "hot"
        (lid,) = c.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, crc64nvme,"
            " verified, verified_at, verified_by, committed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s, true,"
            " %s, 'upload_block_crc', %s) RETURNING location_id",
            (file_id, vid, w.backend_id, ref.container, ref.key, ref.version_id, tier, cls,
             size, b"\0" * 8, committed_at, committed_at),
        ).fetchone()  # fmt: skip
        c.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
            " WHERE version_id = %s",
            (committed_at, vid),
        )
        c.execute(
            "UPDATE catalog.file SET size = %s, resident_bytes = %s WHERE file_id = %s",
            (size, size, file_id),
        )
    return Stored(file_id, vid, lid, ObjectRef(w.backend_id, ref.container, ref.key,
                                               ref.version_id))  # fmt: skip


def commit_metadata(c: psycopg.Connection, w: World, file_id: int, at: datetime) -> int:
    (parent, name) = c.execute(
        "SELECT parent_id, name FROM catalog.file WHERE file_id = %s", (file_id,)
    ).fetchone()
    (vid,) = c.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at) VALUES (%s, %s, %s, %s, 'metadata', 'committed', %s) RETURNING version_id",
        (file_id, w.dataset_id, parent, name, at),
    ).fetchone()
    return vid


def version(c: psycopg.Connection, vid: int) -> dict:
    cur = c.execute("SELECT * FROM catalog.file_version WHERE version_id = %s", (vid,))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def location(c: psycopg.Connection, lid: int) -> dict:
    cur = c.execute("SELECT * FROM catalog.location WHERE location_id = %s", (lid,))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def share(c: psycopg.Connection, share_id: int) -> dict:
    cur = c.execute("SELECT * FROM catalog.share WHERE share_id = %s", (share_id,))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def audits(c: psycopg.Connection, dataset_id: int, prefix: str = "") -> list[str]:
    return [
        r[0]
        for r in c.execute(
            "SELECT action FROM catalog.audit_log WHERE dataset_id = %s AND action LIKE %s"
            " ORDER BY at, audit_id",
            (dataset_id, prefix + "%"),
        )
    ]


def worker(connect, w: World, clock: VirtualClock) -> Worker:
    return Worker(
        connect,
        w.registry,
        clock=clock,
        consumer_config=ConsumerConfig(backoff_base=0),
    )


def run_sweep(wk: Worker, name: str) -> int:
    return run(wk.run_sweep(wk.sweeps[name]))


def drain(wk: Worker, queue: str) -> int:
    async def go() -> int:
        try:
            return await wk.consumers[queue].drain()
        finally:
            await wk.consumers[queue].close()

    return run(go())


def expect_sqlstate(c: psycopg.Connection, sqlstate: str, sql: str, params: Any = ()) -> None:
    """`sql` fails with `sqlstate` (the catalog's own guards), as some actor."""
    try:
        with c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'user:test', true)")
            c.execute(sql, params)
    except psycopg.Error as e:
        assert e.sqlstate == sqlstate, e
    else:
        raise AssertionError(f"expected {sqlstate} from {sql}")
