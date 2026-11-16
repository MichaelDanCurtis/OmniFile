"""No transaction id across a provider call, on the worker's main paths.

The edge feed's horizon is the oldest open id, for every edge, so an id held during a
storage call keeps every edge's ACL and share events unread until the call ends. Each
test runs the real consumer over a provider that asks, at every call, who holds what
(`tests/watched.py`). `during_each_call` also commits a change to another file and reads
the feed, as an edge would, to show the event is readable at once. The horizon is worked
out from this database's sessions alone, as in `test_tiering_shared_blob.py`."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import provider_guard
import psycopg
import pytest
from psycopg.types.json import Jsonb
from watched import Interleaved, Watched

import omnifile_service
from omnifile_service.backends.base import LogicalTier
from omnifile_service.catalog.privileges import as_app
from omnifile_service.retention.worm_sync import delete_blob, expire_recall_copies
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.changefeed_sync import register_container, sync_container
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Message
from omnifile_service.worker.stubs import handle_edge_instruction
from omnifile_service.worker.tiering import handle_move, handle_restore_budgeted
from omnifile_service.worker.tiering_jobs import tiering_jobs
from omnifile_service.worker.verify import handle_verify

from .support import Stored, World, make_world, primary, run, set_tier, store
from .test_tiering_shared_blob import _feed_events


async def _connect(w: World) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)


def watched(db: psycopg.Connection, w: World, other: Stored) -> tuple[Watched, list[bool]]:
    """A provider that, at every call, commits a change to `other` and looks for it."""
    readable: list[bool] = []

    def during_each_call(_call: str) -> None:
        before = len(_feed_events(w, other.file_id))
        with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute(
                "UPDATE catalog.file SET mode = %s WHERE file_id = %s",
                (0o600 + len(readable), other.file_id),
            )
        readable.append(len(_feed_events(w, other.file_id)) > before)

    return Watched(w.sim, w.url, during_each_call), readable


def consume(w: World, queue: str, handler: Any) -> None:
    async def go() -> None:
        c = Consumer(lambda: _connect(w), queue, handler, ConsumerConfig(backoff_base=0))
        try:
            await c.drain(limit=20)
            assert c.stats.dead == 0, c.stats.errors
        finally:
            await c.close()

    run(go())


def recall(db: psycopg.Connection, w: World, s: Stored, priority: str = "standard") -> int:
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (rid,) = db.execute(
            "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority)"
            " VALUES (%s, %s, %s, %s) RETURNING recall_id",
            (s.file_id, s.version_id, w.edge_id, priority),
        ).fetchone()
        db.execute(
            "SELECT pgmq.send('restores'::text, %s::jsonb)", (Jsonb({"recall_id": str(rid)}),)
        )
    return rid


def _state(db: psycopg.Connection, rid: int) -> str:
    (st,) = db.execute("SELECT state FROM catalog.recall WHERE recall_id = %s", (rid,)).fetchone()
    return st


@pytest.fixture
def world(db: psycopg.Connection, policy_db_url: str) -> World:
    for q in ("restores", "retention", "verify"):
        db.execute(f"DELETE FROM pgmq.q_{q}")
    return make_world(db, policy_db_url, layout="azure_worm")


@pytest.mark.parametrize("priority", ["standard", "high"])
def test_a_recall_holds_no_id_across_its_calls(
    db: psycopg.Connection, world: World, priority: str
) -> None:
    w = world
    a, other = store(db, w, "a.segy"), store(db, w, "other.segy", data=b"o" * 4096)
    set_tier(db, w, a, LogicalTier.ARCHIVE)
    provider, readable = watched(db, w, other)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]
    rid = recall(db, w, a, priority)

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=registry, now=w.clock.now())

    consume(w, "restores", handler)
    assert _state(db, rid) == "rehydrating"
    assert {"head", "restore", "restore_status"} <= set(provider.calls())
    # And its polling.
    db.execute("UPDATE pgmq.q_restores SET vt = now()")
    consume(w, "restores", handler)
    assert provider.calls().count("restore_status") >= 2
    assert provider.with_ids() == []
    assert readable and all(readable)
    if priority == "standard":
        # Its transaction is closed before every call, so no read lock is held either.
        # A High recall keeps one open: the budget's lock must last until it is recorded.
        assert provider.with_locks() == []


def test_a_recall_cancelled_during_its_call_stays_cancelled_and_its_restore_is_kept(
    db: psycopg.Connection, world: World
) -> None:
    """The row isn't locked during the call any more, so it can move."""
    w = world
    a = store(db, w, "a.segy")
    set_tier(db, w, a, LogicalTier.ARCHIVE)
    rid = recall(db, w, a)

    def cancel(call: str) -> None:
        if call == "restore":
            with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
                c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                c.execute("UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s",
                          (rid,))  # fmt: skip

    provider = Watched(w.sim, w.url, cancel)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=registry, now=w.clock.now())

    consume(w, "restores", handler)
    assert _state(db, rid) == "cancelled"
    (open_restores,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE location_id = %s AND state = 'in_progress'",
        (a.location_id,),
    ).fetchone()
    assert open_restores == 1, "the provider is running it, so it is on record"
    # A later recall joins it, rather than starting another.
    rid2 = recall(db, w, a)
    consume(w, "restores", handler)
    assert _state(db, rid2) == "rehydrating"
    assert provider.calls().count("restore") == 1


def test_two_holders_of_one_recall_record_one_restore(db: psycopg.Connection, world: World) -> None:
    """A message whose visibility lapsed during a call: the other holder got there first."""
    w = world
    a = store(db, w, "a.segy")
    set_tier(db, w, a, LogicalTier.ARCHIVE)
    rid = recall(db, w, a)
    (msg_id,) = db.execute("SELECT msg_id FROM pgmq.q_restores").fetchone()
    first_holder_ran: list[bool] = []
    inner = BackendRegistry.from_mapping({w.backend_id: w.sim})

    def second_holder(call: str) -> None:
        if call != "restore" or first_holder_ran:
            return
        first_holder_ran.append(True)

        async def other() -> None:
            conn = await _connect(w)
            try:
                async with conn.transaction():
                    try:
                        await handle_restore_budgeted(
                            Message(msg_id, 1, None, None, {"recall_id": str(rid)}),  # type: ignore[arg-type]
                            conn, registry=inner, now=w.clock.now(),
                        )  # fmt: skip
                    except Exception:  # noqa: BLE001, S110 - its Retry; its writes commit below
                        pass
            finally:
                await conn.close()

        import asyncio
        import threading

        t = threading.Thread(target=lambda: asyncio.run(other()))
        t.start()
        t.join(20)
        assert not t.is_alive()

    provider = Watched(w.sim, w.url, second_holder)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=registry, now=w.clock.now())

    consume(w, "restores", handler)
    assert first_holder_ran == [True]
    assert _state(db, rid) == "rehydrating"
    (restores,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert restores == 1


def test_a_read_back_holds_nothing_across_its_read(db: psycopg.Connection, world: World) -> None:
    """A 184 GB file takes hours. No id, and no read lock for an `ALTER` to wait on."""
    w = world
    a, other = store(db, w, "a.segy"), store(db, w, "other.segy", data=b"o" * 4096)
    db.execute(
        "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
        " WHERE location_id = %s",
        (a.location_id,),
    )
    db.execute(
        "SELECT pgmq.send('verify'::text, %s::jsonb)", (Jsonb({"location_id": str(a.location_id)}),)
    )
    provider, readable = watched(db, w, other)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_verify(msg, conn, registry=registry)

    consume(w, "verify", handler)
    assert primary(db, a.file_id)["verified"] is True
    assert provider.calls() == ["get_range"]
    assert provider.with_ids() == [] and provider.with_locks() == []
    assert readable == [True]


def test_a_location_changed_during_its_read_back_isnt_verified(
    db: psycopg.Connection, world: World
) -> None:
    """The handler runs again after the read, and reads the row afresh."""
    w = world
    a = store(db, w, "a.segy")
    db.execute(
        "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
        " WHERE location_id = %s",
        (a.location_id,),
    )
    db.execute(
        "SELECT pgmq.send('verify'::text, %s::jsonb)", (Jsonb({"location_id": str(a.location_id)}),)
    )

    def change(_call: str) -> None:
        with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute("UPDATE catalog.location SET state = 'deleting' WHERE location_id = %s",
                      (a.location_id,))  # fmt: skip

    provider = Watched(w.sim, w.url, change)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_verify(msg, conn, registry=registry)

    consume(w, "verify", handler)
    (verified,) = db.execute(
        "SELECT verified FROM catalog.location WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert verified is False


def test_an_archive_copy_holds_nothing_across_its_calls(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Copy Blob has no time promise at any size, so it runs with nothing open."""
    w = make_world(db, policy_db_url, layout="azure_two_account")
    a, other = store(db, w, "a.segy"), store(db, w, "other.segy", data=b"o" * 4096)
    provider, readable = watched(db, w, other)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]
    db.execute(
        "SELECT pgmq.send('tiering'::text, %s::jsonb)",
        (Jsonb({"kind": "move", "file_id": str(a.file_id), "version_id": str(a.version_id),
                "location_id": str(a.location_id), "tier": "archive"}),),
    )  # fmt: skip

    async def moves(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_move(msg, conn, registry=registry, now=w.clock.now())

    async def verify(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_verify(msg, conn, registry=registry)

    for _ in range(4):
        consume(w, "tiering", moves)
        consume(w, "verify", verify)
        db.execute("UPDATE pgmq.q_tiering SET vt = now() WHERE vt > now()")
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert {"copy_object", "set_class", "get_range"} <= set(provider.calls())
    assert provider.with_ids() == []
    assert all(readable)
    locked = {s.call for s in provider.with_locks()}
    assert not locked & {"copy_object", "set_class", "get_range"}, provider.with_locks()


def test_the_change_feed_holds_no_id_across_a_read(db: psycopg.Connection, world: World) -> None:
    """Confirmed by running on main: `_sync_page` held one, from its row lock."""
    w = world
    other = store(db, w, "other.segy", data=b"o" * 4096)
    provider, readable = watched(db, w, other)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def go() -> None:
        conn = await _connect(w)
        try:
            await register_container(conn, w.backend_id, "data")
            await sync_container(conn, registry, w.backend_id, "data", w.clock.now(), max_pages=2)
        finally:
            await conn.close()

    run(go())
    assert "read_changes" in provider.calls()
    assert provider.with_ids() == []
    assert readable and all(readable)


def test_two_syncers_of_a_container_still_take_turns(db: psycopg.Connection, world: World) -> None:
    """The row lock did that before. The second finds the container busy and moves on."""
    w = world
    syncs: list[int] = []

    def second(call: str) -> None:
        if call != "read_changes" or syncs:
            return
        syncs.append(1)
        import asyncio
        import threading

        async def other() -> None:
            conn = await _connect(w)
            try:
                inner = BackendRegistry.from_mapping({w.backend_id: counting})  # type: ignore[dict-item]
                await sync_container(conn, inner, w.backend_id, "data", w.clock.now())
            finally:
                await conn.close()

        t = threading.Thread(target=lambda: asyncio.run(other()))
        t.start()
        t.join(20)
        assert not t.is_alive()

    counting = Watched(w.sim, w.url)
    provider = Watched(w.sim, w.url, second)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def go() -> None:
        conn = await _connect(w)
        try:
            await register_container(conn, w.backend_id, "data")
            await sync_container(conn, registry, w.backend_id, "data", w.clock.now(), max_pages=1)
        finally:
            await conn.close()

    run(go())
    assert syncs == [1]
    assert counting.calls() == [], "the second syncer read the provider while the first held it"


def test_a_feed_page_read_before_a_seed_moved_the_cursor_is_dropped(
    db: psycopg.Connection, world: World
) -> None:
    """A seed starts a container's feed at its snapshot, and its row lock used to keep any
    page out. A page read just before must not then overwrite that cursor."""
    w = world

    def move_cursor(call: str) -> None:
        if call == "read_changes":
            db.execute(
                "UPDATE catalog.changefeed_cursor SET cursor = 'from-the-seed'"
                " WHERE backend_id = %s AND container = 'data'",
                (w.backend_id,),
            )

    provider = Watched(w.sim, w.url, move_cursor)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def go() -> None:
        conn = await _connect(w)
        try:
            await register_container(conn, w.backend_id, "data")
            await sync_container(conn, registry, w.backend_id, "data", w.clock.now(), max_pages=1)
        finally:
            await conn.close()

    run(go())
    (cursor,) = db.execute(
        "SELECT cursor FROM catalog.changefeed_cursor WHERE backend_id = %s AND container = 'data'",
        (w.backend_id,),
    ).fetchone()
    assert cursor == "from-the-seed"


def _deleting(db: psycopg.Connection, s: Stored) -> None:
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = %s",
            (s.version_id,),
        )
        db.execute(
            "UPDATE catalog.location SET state = 'deleting', verified = false"
            " WHERE location_id = %s",
            (s.location_id,),
        )
        db.execute(
            "SELECT pgmq.send('retention'::text, %s::jsonb)",
            (Jsonb({"op": "delete_blob", "location_id": str(s.location_id)}),),
        )


def test_a_delete_holds_no_id_across_its_calls(db: psycopg.Connection, world: World) -> None:
    """By reading on main: its row lock did, across the provider's delete."""
    w = world
    a, other = store(db, w, "a.segy"), store(db, w, "other.segy", data=b"o" * 4096)
    _deleting(db, a)
    provider, readable = watched(db, w, other)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await delete_blob(conn, registry, int(msg.body["location_id"]), w.clock.now())

    consume(w, "retention", handler)
    assert "delete" in provider.calls()
    assert provider.with_ids() == []
    assert readable and all(readable)
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert st == "deleted"


def test_a_delete_defers_to_a_dedupe_in_flight_and_then_sees_its_reference(
    db: psycopg.Connection, world: World
) -> None:
    """`find_duplicate` holds the blob's lock shared until its transaction ends. The row
    lock never kept that out; the blob's lock does."""
    from omnifile_service.catalog.blob_lock import blob_lock_key

    w = world
    a = store(db, w, "a.segy")
    _deleting(db, a)
    key = blob_lock_key(w.backend_id, a.ref.container, a.ref.key)
    registry = BackendRegistry.from_mapping({w.backend_id: w.sim})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await delete_blob(conn, registry, int(msg.body["location_id"]), w.clock.now())

    dedupe = psycopg.connect(w.url)
    try:
        dedupe.execute("SELECT pg_advisory_xact_lock_shared(hashtext(%s))", (key,))
        consume(w, "retention", handler)
        (st,) = db.execute(
            "SELECT state FROM catalog.location WHERE location_id = %s", (a.location_id,)
        ).fetchone()
        assert st == "deleting"
        (n,) = db.execute("SELECT count(*) FROM pgmq.q_retention").fetchone()
        assert n == 1, "deferred, with its message"
    finally:
        dedupe.close()
    db.execute("UPDATE pgmq.q_retention SET vt = now()")
    consume(w, "retention", handler)
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert st == "deleted"


def test_an_expired_recall_copy_is_queued_not_deleted_in_the_sweep(
    db: psycopg.Connection, world: World
) -> None:
    """Marking it `deleting` takes an id, and the delete was in the same transaction."""
    w = world
    a = store(db, w, "a.segy")
    long_ago = w.clock.now() - timedelta(days=30)
    db.execute(
        "UPDATE catalog.location SET role = 'recall_copy' WHERE location_id = %s", (a.location_id,)
    )
    db.execute(
        "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed,"
        " target_location_id, state, triggered_by, requested_at, completed_at, updated_at)"
        " VALUES (%s, %s, 'azure_copy_rehydrate', 'standard', %s, 'completed', 'user_recall',"
        " %s, %s, now())",
        (a.location_id, w.backend_id, a.location_id, long_ago, long_ago),
    )

    async def go() -> int:
        conn = await _connect(w)
        try:
            return await expire_recall_copies(conn, w.clock.now())
        finally:
            await conn.close()

    assert run(go()) >= 1
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert st == "deleting"
    (n,) = db.execute(
        "SELECT count(*) FROM pgmq.q_retention WHERE message->>'location_id' = %s",
        (str(a.location_id),),
    ).fetchone()
    assert n == 1


def test_a_stub_check_holds_nothing_across_its_head(db: psycopg.Connection, world: World) -> None:
    w = world
    a, other = store(db, w, "a.segy"), store(db, w, "other.segy", data=b"o" * 4096)
    db.execute(
        "SELECT pgmq.send('edge_instructions'::text, %s::jsonb)",
        (Jsonb({"kind": "stub", "file_id": str(a.file_id), "version_id": str(a.version_id)}),),
    )
    provider, readable = watched(db, w, other)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_edge_instruction(msg, conn, registry=registry, now=w.clock.now())

    consume(w, "edge_instructions", handler)
    assert provider.calls() == ["head"]
    assert provider.with_ids() == [] and provider.with_locks() == []
    assert readable == [True]


def test_the_restores_consumer_outlasts_its_longest_call() -> None:
    from dataclasses import dataclass, field

    from omnifile_service.worker.recalls import CALL_TIMEOUT, VISIBILITY_SECONDS

    @dataclass
    class FakeWorker:
        registry: Any = None
        clock: Any = None
        consumer_config: ConsumerConfig = field(default_factory=ConsumerConfig)

    jobs = {getattr(j, "queue", None): j for j in tiering_jobs(FakeWorker())}  # type: ignore[arg-type]
    assert jobs["restores"].config.visibility_timeout == VISIBILITY_SECONDS > CALL_TIMEOUT


@pytest.mark.parametrize("priority", ["standard", "high"])
def test_a_recall_another_holder_finished_during_the_call_gets_no_second_restore(
    db: psycopg.Connection, world: World, priority: str
) -> None:
    """Looked at again with the row locked: the restore it started isn't a new one. A High
    recall reads the row once, before its call, so only this catches it."""
    w = world
    a = store(db, w, "a.segy")
    set_tier(db, w, a, LogicalTier.ARCHIVE)
    rid = recall(db, w, a, priority)

    def finish(call: str) -> None:
        if call != "restore":
            return
        with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            (rs,) = c.execute(
                "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, state,"
                " triggered_by, requested_at, completed_at, updated_at)"
                " VALUES (%s, %s, 'azure_copy_rehydrate', 'standard', 'completed', 'user_recall',"
                " now(), now(), now()) RETURNING restore_id",
                (a.location_id, w.backend_id),
            ).fetchone()
            c.execute(
                "UPDATE catalog.recall SET state = 'downloading', restore_id = %s"
                " WHERE recall_id = %s",
                (rs, rid),
            )

    provider = Watched(w.sim, w.url, finish)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=registry, now=w.clock.now())

    consume(w, "restores", handler)
    assert _state(db, rid) == "downloading"
    (restores,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert restores == 1


def test_a_recall_cancelled_during_a_poll_isnt_revived(
    db: psycopg.Connection, world: World
) -> None:
    """The row isn't locked across the status call, so it is looked at again after."""
    w = world
    a = store(db, w, "a.segy")
    set_tier(db, w, a, LogicalTier.ARCHIVE)
    rid = recall(db, w, a)
    inner = BackendRegistry.from_mapping({w.backend_id: w.sim})

    async def first(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=inner, now=w.clock.now())

    consume(w, "restores", first)
    assert _state(db, rid) == "rehydrating"

    def cancel(call: str) -> None:
        if call == "restore_status":
            with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
                c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                c.execute("UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s",
                          (rid,))  # fmt: skip

    provider = Watched(w.sim, w.url, cancel)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def polling(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=registry, now=w.clock.now())

    db.execute("UPDATE pgmq.q_restores SET vt = now()")
    consume(w, "restores", polling)
    assert provider.calls() == ["restore_status"]
    assert _state(db, rid) == "cancelled"
    (n,) = db.execute(
        "SELECT count(*) FROM pgmq.q_restores WHERE message->>'recall_id' = %s", (str(rid),)
    ).fetchone()
    assert n == 0


def test_a_row_that_moved_before_the_delete_got_the_blobs_lock_keeps_its_bytes(
    db: psycopg.Connection, world: World
) -> None:
    """Another holder refused this delete, and the original is the record again. Reading
    the row only before the lock would delete it."""
    w = world
    a = store(db, w, "a.segy")
    _deleting(db, a)
    provider = Watched(w.sim, w.url)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    def committed_again() -> None:
        with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute("UPDATE catalog.file_version SET state = 'committed' WHERE version_id = %s",
                      (a.version_id,))  # fmt: skip
            c.execute("UPDATE catalog.location SET state = 'committed' WHERE location_id = %s",
                      (a.location_id,))  # fmt: skip

    async def go() -> str:
        conn = await _connect(w)
        try:
            async with conn.transaction():
                racing = Interleaved(conn, "pg_try_advisory_xact_lock", committed_again)
                return await delete_blob(racing, registry, a.location_id, w.clock.now())  # type: ignore[arg-type]
        finally:
            await conn.close()

    assert run(go()) == "noop"
    assert "delete" not in provider.calls()
    assert run(w.sim.head(a.ref)).size == a_size(db, a)


def a_size(db: psycopg.Connection, s: Stored) -> int:
    (n,) = db.execute(
        "SELECT size FROM catalog.location WHERE location_id = %s", (s.location_id,)
    ).fetchone()
    return n


def test_a_row_that_moved_during_the_delete_isnt_overwritten(
    db: psycopg.Connection, world: World
) -> None:
    """The final `state = 'deleted'` is only for a row still `deleting`."""
    w = world
    a = store(db, w, "a.segy")
    _deleting(db, a)

    def moved(call: str) -> None:
        if call == "delete":
            with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
                c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                c.execute("UPDATE catalog.file_version SET state = 'committed'"
                          " WHERE version_id = %s", (a.version_id,))  # fmt: skip
                c.execute("UPDATE catalog.location SET state = 'committed'"
                          " WHERE location_id = %s", (a.location_id,))  # fmt: skip

    provider = Watched(w.sim, w.url, moved)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await delete_blob(conn, registry, int(msg.body["location_id"]), w.clock.now())

    consume(w, "retention", handler)
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (a.location_id,)
    ).fetchone()
    assert st == "committed"


# --- the guard itself (tests/provider_guard.py) ------------------------------------------------


def _delete_in_a_transaction(w: World, a: Stored, before: str) -> str:
    """`delete_blob`, as if called in a transaction that has already run `before`."""
    registry = BackendRegistry.from_mapping({w.backend_id: w.sim})

    async def go() -> str:
        conn = await _connect(w)
        try:
            async with conn.transaction():
                await conn.execute(before)
                return await delete_blob(conn, registry, a.location_id, w.clock.now())
        finally:
            await conn.close()

    return run(go())


def test_the_guard_catches_a_provider_call_with_an_id_held(
    db: psycopg.Connection, world: World
) -> None:
    w = world
    a = store(db, w, "a.segy")
    _deleting(db, a)
    provider_guard.take_violations()
    # A write, then the call: what the guard is for.
    assert _delete_in_a_transaction(w, a, "SELECT pg_current_xact_id()") == "deleted"
    found = provider_guard.take_violations()
    assert "retention/worm_sync.py:delete_blob" in found


def test_the_guard_ignores_a_transaction_with_no_id(db: psycopg.Connection, world: World) -> None:
    """A read lock isn't what it is for."""
    w = world
    a = store(db, w, "a.segy")
    _deleting(db, a)
    provider_guard.take_violations()
    assert _delete_in_a_transaction(w, a, "SELECT 1 FROM catalog.location LIMIT 1") == "deleted"
    assert provider_guard.take_violations() == {}


def test_the_guard_ignores_another_sessions_id(db: psycopg.Connection, world: World) -> None:
    """Another suite's open transaction, on a shared server, can't fail a test."""
    w = world
    a = store(db, w, "a.segy")
    _deleting(db, a)
    provider_guard.take_violations()
    with psycopg.connect(w.url) as elsewhere:
        elsewhere.execute("SELECT pg_current_xact_id()")
        assert _delete_in_a_transaction(w, a, "SELECT 1") == "deleted"
        elsewhere.rollback()
    assert provider_guard.take_violations() == {}


def test_the_guards_list_names_why_each_site_is_there() -> None:
    assert provider_guard.KNOWN
    assert all(why.strip() for why in provider_guard.KNOWN.values())
    # The worker's own paths stay off it: only what is routed elsewhere.
    assert {k.split("/")[0] for k in provider_guard.KNOWN} == {"notify"}


def test_a_recall_cancelled_during_a_status_call_in_place_isnt_revived(
    db: psycopg.Connection, world: World
) -> None:
    """The row was read before the call and isn't locked across it. A consumer's next run
    reads it afresh; the look under the lock is what covers everything after that read."""
    w = world
    a = store(db, w, "a.segy")
    set_tier(db, w, a, LogicalTier.ARCHIVE)
    rid = recall(db, w, a)
    inner = BackendRegistry.from_mapping({w.backend_id: w.sim})

    async def first(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=inner, now=w.clock.now())

    consume(w, "restores", first)
    assert _state(db, rid) == "rehydrating"

    def cancel(call: str) -> None:
        if call == "restore_status":
            with psycopg.connect(w.url, autocommit=True) as c, c.transaction():
                c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                c.execute("UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s",
                          (rid,))  # fmt: skip

    provider = Watched(w.sim, w.url, cancel)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]

    async def go() -> None:
        conn = await _connect(w)
        now = w.clock.now()
        try:
            async with conn.transaction():
                await handle_restore_budgeted(
                    Message(1, 1, now, now, {"recall_id": str(rid)}), conn,
                    registry=registry, now=now,
                )  # fmt: skip
        finally:
            await conn.close()

    run(go())
    assert provider.calls() == ["restore_status"]
    assert _state(db, rid) == "cancelled"


def _service_code(source: str) -> dict[str, Any]:
    """Functions the guard takes for product code, by the file they say they are in."""
    code = compile(source, "/x/omnifile_service/worker/made_up.py", "exec")
    names: dict[str, Any] = {}
    exec(code, names)  # noqa: S102 - this test's own source
    return names


def test_the_guard_isnt_fooled_by_a_nearer_idle_connection(
    db: psycopg.Connection, world: World
) -> None:
    w = world
    a = store(db, w, "a.segy")
    code = _service_code(
        """
async def outer(conn, inner):
    async with conn.transaction():
        await conn.execute("SELECT pg_current_xact_id()")
        return await inner(conn=None)

def make_inner(idle, call):
    async def inner(conn):
        conn = idle
        return await call()
    return inner
"""
    )

    async def go() -> None:
        holding, idle = await _connect(w), await _connect(w)
        try:
            await code["outer"](holding, code["make_inner"](idle, lambda: w.sim.head(a.ref)))
        finally:
            await holding.close()
            await idle.close()

    provider_guard.take_violations()
    run(go())
    assert "worker/made_up.py:inner" in provider_guard.take_violations()


def test_no_call_outside_passes_none_in_product_code() -> None:
    """The guard skips `outside` in place, which is how a test drives a handler. So product
    code passes the message it was given, or a name that says why not."""
    root = Path(omnifile_service.__file__).parent
    found = [
        f"{p.relative_to(root)}:{n}"
        for p in root.rglob("*.py")
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"outside\(\s*None\b", line)
    ]
    assert found == []
    # Nor on the next line, as ruff may format it.
    spread = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if re.search(r"outside\(\s*\n\s*None\b", p.read_text())
    ]
    assert spread == []


def test_the_guard_wraps_azures_reads_and_arm() -> None:
    from omnifile_service.backends.azure.backend import AzureBlobBackend
    from omnifile_service.settings.arm import HttpArm
    from omnifile_service.settings.fakes import FakeArm

    for fn in (AzureBlobBackend.get_range, HttpArm.get, HttpArm.put, FakeArm.get, FakeArm.put):
        assert hasattr(fn, "__wrapped__"), fn
