"""A small world for tiering tests: catalog rows, sim on a virtual clock, and
helpers that run the scheduler and the move consumer the way the worker does.
Every dataset is fresh, so tests share one database safely."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from omnifile_service.backends.base import LogicalTier, ObjectRef
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes
from omnifile_service.catalog.privileges import as_app
from omnifile_service.policy import scheduler
from omnifile_service.retention.worm_sync import delete_blob
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Message
from omnifile_service.worker.tiering import handle_move
from omnifile_service.worker.verify import handle_verify

DRAIN_LIMIT = 200
GiB = 1024**3
GB = 10**9


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@dataclass
class World:
    url: str
    clock: VirtualClock
    sim: SimBackend
    backend_id: int
    dataset_id: int
    root_id: int
    edge_id: int
    identity_id: int
    layout: str

    @property
    def registry(self) -> BackendRegistry:
        return BackendRegistry.from_mapping({self.backend_id: self.sim})

    def day(self, n: float) -> datetime:
        return self.t0 + timedelta(days=n)

    t0: datetime = None  # type: ignore[assignment]


@dataclass
class Stored:
    file_id: int
    version_id: int
    location_id: int
    ref: ObjectRef


def make_world(
    c: psycopg.Connection,
    url: str,
    *,
    layout: str = "azure_two_account",
    policy: dict | None = None,
    worm_days: int | None = None,
) -> World:
    tag = uuid.uuid4().hex[:8]
    clock = VirtualClock()
    storage_layout = "two_account" if layout == "azure_two_account" else "single_account"
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (backend_id,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES (%s, 'sim', %s, true) RETURNING backend_id",
            (f"sim-{tag}", Jsonb({"layout": layout})),
        ).fetchone()
        (edge_id,) = c.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
            (f"edge-{tag}", uuid.uuid4().bytes * 2),
        ).fetchone()
        (dataset_id,) = c.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
            " VALUES (%s, %s, %s, %s) RETURNING dataset_id",
            (f"ds-{tag}", backend_id, edge_id, storage_layout),
        ).fetchone()
        (root_id,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
            " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
            (dataset_id,),
        ).fetchone()
        (iid,) = c.execute(
            "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
            (f"S-1-5-21-{int(tag, 16) % 10**9}-1105",),
        ).fetchone()
        if worm_days is not None:
            c.execute(
                "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols,"
                " owners_group_sid, worm_enabled, worm_mode, worm_retention_days)"
                " VALUES (%s, %s, 'worm', ARRAY['smb'], %s, true, 'unlocked_test', %s)",
                (dataset_id, edge_id, f"S-1-5-21-{int(tag, 16) % 10**9}-513", worm_days),
            )
        if policy is not None:
            c.execute(
                "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
                " VALUES (%s, 1, %s, %s)",
                (dataset_id, Jsonb(policy), iid),
            )
    sim = getattr(SimBackend, layout)(clock, backend_id=backend_id)
    w = World(url, clock, sim, backend_id, dataset_id, root_id, edge_id, iid, layout)
    w.t0 = clock.now()
    return w


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def store(
    c: psycopg.Connection,
    w: World,
    name: str,
    *,
    size: int | None = None,
    data: bytes = b"x" * 4096,
    parent: int | None = None,
    key: str | None = None,
) -> Stored:
    """A committed file whose content version is uploaded (with the account's
    default tier, as the edge does: set_tier false) and verified."""
    now = w.clock.now()
    if size is None:
        ref = run(w.sim.put("data", key or name, _one(data), None, {}))
        size = len(data)
        crc = to_bytes(crc64nvme(data))
    else:
        ref = w.sim.put_synthetic("data", name, size)
        crc = b"\0" * 8
    md5 = run(w.sim.head(ref)).md5  # what the catalog knows from the upload
    tier = "smart" if w.layout == "azure_two_account" else "hot"
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (fid,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes, written_at) VALUES (%s, %s, %s, 'file', 'resident', %s, %s, %s)"
            " RETURNING file_id",
            (w.dataset_id, parent or w.root_id, name, size, size, now),
        ).fetchone()
        (vid,) = c.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, size) VALUES (%s, %s, 1, %s, %s, 'content', 'pending', %s)"
            " RETURNING version_id",
            (fid, w.dataset_id, parent or w.root_id, name, size),
        ).fetchone()
        (lid,) = c.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, crc64nvme,"
            " md5, verified, verified_at, verified_by, committed_at, tier_changed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s, %s, true,"
            " %s, 'upload_block_crc', %s, %s) RETURNING location_id",
            (
                fid,
                vid,
                w.backend_id,
                ref.container,
                ref.key,
                ref.version_id,
                tier,
                tier.capitalize(),
                size,
                crc,
                md5,
                now,
                now,
                now,
            ),
        ).fetchone()
        c.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
            " WHERE version_id = %s",
            (now, vid),
        )
    return Stored(fid, vid, lid, ObjectRef(w.backend_id, ref.container, ref.key, ref.version_id))


async def _sweep_and_move(w: World) -> scheduler.SweepResult:
    now = w.clock.now()
    conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
    try:
        total = scheduler.SweepResult()
        vmoves: list[tuple[int, str]] = []
        for _ in range(8):  # sweep -> moves (and read-backs) land -> sweep again, until quiet
            res = await scheduler.sweep(conn, now, dataset_ids=[w.dataset_id])
            total.evaluated += res.evaluated
            total.moves += res.moves
            vmoves += getattr(res, "version_moves", [])
            total.stubs += res.stubs
            total.recalls += res.recalls
            total.flags.update(res.flags)
            total.skipped.update(res.skipped)
            moved = await drain_work(conn, w, now)
            if not moved and not res.moves and not getattr(res, "version_moves", []):
                break
        total.version_moves = vmoves
        return total
    finally:
        await conn.close()


def tick(w: World) -> scheduler.SweepResult:
    """One scheduler pass at the virtual now, with its moves executed."""
    return run(_sweep_and_move(w))


async def drain_tiering(conn: psycopg.AsyncConnection, w: World, now: datetime) -> int:
    """Run the ``tiering`` queue the way the worker does (a real Consumer:
    one transaction per message; Retry commits and re-sends the next phase)."""

    async def handler(msg: Message, c: psycopg.AsyncConnection) -> None:
        await handle_move(msg, c, registry=w.registry, now=now)

    async def connect() -> psycopg.AsyncConnection:
        # A lock wrongly taken fails the test rather than waiting for its timeout.
        return await psycopg.AsyncConnection.connect(
            as_app(w.url, "worker"), autocommit=True, options="-c lock_timeout=20000"
        )

    consumer = Consumer(connect, "tiering", handler, ConsumerConfig(backoff_base=0))
    try:
        n = await consumer.drain(limit=DRAIN_LIMIT)
    finally:
        await consumer.close()
    # A message that keeps coming back fails now, not at the test's timeout.
    assert n < DRAIN_LIMIT, f"the tiering queue is still busy after {n} messages"
    return n


async def drain_verify(w: World) -> int:
    """Run the ``verify`` queue (the upload / archive-copy read-back)."""

    async def handler(msg: Message, c: psycopg.AsyncConnection) -> None:
        await handle_verify(msg, c, registry=w.registry)

    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)

    consumer = Consumer(connect, "verify", handler, ConsumerConfig(backoff_base=0))
    try:
        return await consumer.drain(limit=200)
    finally:
        await consumer.close()


async def drain_work(
    conn: psycopg.AsyncConnection, w: World, now: datetime, *, verify: bool = True
) -> int:
    """tiering, then verify, then the tiering messages that were waiting on
    it (made visible at once: the poll delay is real time), until quiet."""
    total = 0
    for _ in range(10):
        n = await drain_tiering(conn, w, now)
        if verify:
            n += await drain_verify(w)
        async with conn.transaction():
            await conn.execute("UPDATE pgmq.q_tiering SET vt = now() WHERE vt > now()")
        n += await drain_tiering(conn, w, now)
        total += n
        if n == 0:
            break
    return total


async def _drain_retention(w: World) -> list[str]:
    conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
    out: list[str] = []
    try:
        while True:
            # Read first, as the consumer does: a read takes a transaction id.
            cur = await conn.execute(
                "SELECT msg_id, message FROM pgmq.read('retention'::text, 30, 1)"
            )
            r = await cur.fetchone()
            if r is None:
                return out
            async with conn.transaction():
                if r[1].get("op") == "delete_blob":
                    loc = int(r[1]["location_id"])
                    out.append(await delete_blob(conn, w.registry, loc, w.clock.now()))
                await conn.execute("SELECT pgmq.delete('retention'::text, %s::bigint)", (r[0],))
    finally:
        await conn.close()


def drain_retention(w: World) -> list[str]:
    """Run queued ``delete_blob`` work (m3-retention's handler)."""
    return run(_drain_retention(w))


def row(c: psycopg.Connection, sql: str, *args: Any) -> dict | None:
    cur = c.execute(sql, args)
    r = cur.fetchone()
    return None if r is None else dict(zip([d.name for d in cur.description], r, strict=True))


def primary(c: psycopg.Connection, file_id: int) -> dict:
    r = row(
        c,
        "SELECT l.* FROM catalog.file f JOIN catalog.location l"
        " ON l.location_id = f.primary_location_id WHERE f.file_id = %s",
        file_id,
    )
    assert r is not None
    return r


def queue(c: psycopg.Connection, name: str) -> list[dict]:
    return [r[0] for r in c.execute(f"SELECT message FROM pgmq.q_{name} ORDER BY msg_id")]


def set_tier(c: psycopg.Connection, w: World, s: Stored, tier: LogicalTier) -> ObjectRef:
    """Put a file straight into a tier (test setup), catalog included. Archive
    on the two-account layout goes as the tiering worker moves it: a copy into
    the archive account, Archive in place there, then the original's delete
    (the backend's own move won't delete an original it can't check)."""
    if tier is LogicalTier.ARCHIVE and w.layout == "azure_two_account":
        short = s.ref.container.split("/", 1)[-1]
        copy = run(w.sim.copy_object(s.ref, f"archive/{short}", s.ref.key, LogicalTier.HOT))
        ref = run(w.sim.set_class(copy, tier))
        run(w.sim.delete(s.ref))
    else:
        ref = run(w.sim.set_class(s.ref, tier))
    c.execute(
        "UPDATE catalog.location SET container = %s, object_key = %s, backend_version_id = %s,"
        " logical_tier = %s, storage_class = %s, explicit_tier_set = true WHERE location_id = %s",
        (ref.container, ref.key, ref.version_id, str(tier), tier.capitalize(), s.location_id),
    )
    return ref
