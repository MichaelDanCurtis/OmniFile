"""M3 review fixes for the policy and tiering track, each pinned by a test:

- blocker 1: the two-account archive move is phased (intent, copy, commit,
  delete on ``retention``) and survives a crash between any two phases;
- blocker 2: the archive copy is checked (length, Content-MD5) before the
  original goes, and is only ``verified`` when a checksum agreed;
- high: the 10 GiB/h High budget holds across workers (advisory lock, one
  read);
- medium: superseded versions are tiered too;
- decision: in smart mode the archive day counts from the commit;
- low: no hot loops on no-op moves, no duplicate moves, no masked errors.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.backends.base import ObjectNotFound, ObjectRef
from omnifile_service.backends.sim import CopyFault, InjectedError, Op
from omnifile_service.catalog.privileges import as_app
from omnifile_service.policy import FileFacts, Policy, evaluate, scheduler
from omnifile_service.worker.queue import Message, Retry
from omnifile_service.worker.recalls import RecallConfig
from omnifile_service.worker.tiering import handle_move, handle_restore_budgeted

from .support import (
    GiB,
    Stored,
    World,
    _one,
    drain_retention,
    drain_work,
    make_world,
    primary,
    queue,
    row,
    run,
    set_tier,
    store,
    tick,
)

NOW_POLL = RecallConfig(poll_min=timedelta(0), poll_max=timedelta(0), poll_no_eta=timedelta(0))


@pytest.fixture
def two(db: psycopg.Connection, policy_db_url: str) -> World:
    return make_world(db, policy_db_url, policy={"mode": "smart", "archive_day": 10})


def dlq(db: psycopg.Connection) -> list[dict]:
    db.execute("SELECT pgmq.create('tiering_dlq')")
    return queue(db, "tiering_dlq")


def stubs(db: psycopg.Connection) -> list[int]:
    return [int(m["file_id"]) for m in queue(db, "edge_instructions") if m["kind"] == "stub"]


def fail_once(db: psycopg.Connection, location_id: int, states: tuple[str, ...]) -> str:
    """A one-shot database failure: the first update that moves this location
    into one of `states` raises (a crash in the middle of a commit). The
    sequence isn't rolled back with the transaction, so it fires once."""
    name = f"t_fail_{location_id}"
    db.execute(f"CREATE SEQUENCE IF NOT EXISTS {name}")
    db.execute(
        f"""
        CREATE OR REPLACE FUNCTION catalog.{name}() RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER AS $$  -- as every catalog trigger (catalog/privileges.py)
        BEGIN
            IF NEW.location_id = {location_id} AND NEW.state = ANY (%s::text[])
               AND NEW.state IS DISTINCT FROM OLD.state AND nextval('{name}') = 1 THEN
                RAISE EXCEPTION 'injected crash in the commit of {location_id}';
            END IF;
            RETURN NEW;
        END $$""".replace("%s::text[]", "ARRAY[" + ",".join(f"'{s}'" for s in states) + "]")
    )
    db.execute(
        f"CREATE TRIGGER {name} BEFORE UPDATE ON catalog.location"
        f" FOR EACH ROW EXECUTE FUNCTION catalog.{name}()"
    )
    return name


def drop_fail(db: psycopg.Connection, name: str) -> None:
    db.execute(f"DROP TRIGGER IF EXISTS {name} ON catalog.location")
    db.execute(f"DROP FUNCTION IF EXISTS catalog.{name}()")
    db.execute(f"DROP SEQUENCE IF EXISTS {name}")


# --- blocker 1: crash safety -----------------------------------------------------


def test_crash_in_the_catalog_commit_after_the_copy(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "a.segy")
    name = fail_once(db, f.location_id, ("deleting", "deleted"))
    try:
        two.clock.set(two.day(10))
        tick(two)
    finally:
        drop_fail(db, name)
    assert dlq(db) == []
    loc = primary(db, f.file_id)
    assert loc["logical_tier"] == "archive" and loc["container"] == "archive/data"
    # The original outlived the crash; only the retention queue deletes it.
    assert run(two.sim.head(f.ref)).size == 4096
    assert drain_retention(two) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(f.ref))


def test_crash_after_the_provider_copy_never_copies_twice(
    db: psycopg.Connection, two: World
) -> None:
    f = store(db, two, "b.segy")
    two.clock.set(two.day(10))
    # Phase "copy": the HEAD after the copy fails, as if the worker died
    # there. The next attempt finds the copy and adopts it.
    two.sim.faults.fail_nth(Op.HEAD, 2, InjectedError("worker died after the copy"))
    tick(two)
    assert two.sim.faults.calls[Op.COPY_OBJECT] == 1
    assert dlq(db) == []
    loc = primary(db, f.file_id)
    assert loc["logical_tier"] == "archive" and loc["verified"]


def test_source_gone_after_copy_is_adopted(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "c.segy")
    two.clock.set(two.day(10))
    assert sweep_only(two).moves == [(f.file_id, "archive")]
    # An earlier attempt copied (Hot) and the original went (crash / old code).
    run(two.sim.copy_object(f.ref, "archive/data", "c.segy", T.HOT))
    run(two.sim.delete(f.ref))
    tick(two)
    assert dlq(db) == []
    loc = primary(db, f.file_id)
    # The leftover copy was read back like any other before it became the record.
    assert loc["container"] == "archive/data" and loc["verified"]
    assert loc["verified_by"] == "azure_read_crc" and loc["logical_tier"] == "archive"


def test_source_gone_and_copy_already_archived_is_kept_unverified(
    db: psycopg.Connection, two: World
) -> None:
    f = store(db, two, "c2.segy")
    two.clock.set(two.day(10))
    sweep_only(two)
    run(two.sim.copy_object(f.ref, "archive/data", "c2.segy", T.ARCHIVE))
    run(two.sim.delete(f.ref))
    tick(two)
    assert dlq(db) == []
    loc = primary(db, f.file_id)
    assert loc["container"] == "archive/data" and not loc["verified"]
    assert stubs(db) == []  # unverified: the edge keeps its bytes


def sweep_only(w: World) -> scheduler.SweepResult:
    """The scheduler alone: the move is queued, nothing runs it yet."""

    async def go() -> scheduler.SweepResult:
        conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
        try:
            return await scheduler.sweep(conn, w.clock.now(), dataset_ids=[w.dataset_id])
        finally:
            await conn.close()

    return run(go())


# --- blocker 2 (coordinator decision): CRC64 read-back before Archive -------------


def test_verified_path_lands_hot_reads_back_then_archives(
    db: psycopg.Connection, two: World
) -> None:
    f = store(db, two, "ok.segy")
    two.clock.set(two.day(10))
    sweep_only(two)

    async def without_verify() -> None:
        conn = await psycopg.AsyncConnection.connect(as_app(two.url, "worker"), autocommit=True)
        try:
            await drain_work(conn, two, two.clock.now(), verify=False)
        finally:
            await conn.close()

    run(without_verify())
    # Copied, landed Hot, not read back yet: nothing else has happened.
    copy = row(db, "SELECT * FROM catalog.location WHERE version_id = %s AND role = 'secondary'",
               f.version_id)  # fmt: skip
    assert (copy["state"], copy["logical_tier"], copy["verified"]) == ("committed", "hot", False)
    head = run(two.sim.head(ObjectRef(two.backend_id, "archive/data", "ok.segy",
                                      copy["backend_version_id"])))  # fmt: skip
    assert head.native_class == "Hot"
    assert primary(db, f.file_id)["location_id"] == f.location_id
    assert {"location_id": str(copy["location_id"])} in queue(db, "verify")

    tick(two)  # verify reads it back; the move goes on
    loc = primary(db, f.file_id)
    assert loc["location_id"] == copy["location_id"]
    assert (loc["logical_tier"], loc["storage_class"]) == ("archive", "Archive")
    assert loc["verified"] and loc["verified_by"] == "azure_read_crc"
    head = run(two.sim.head(ObjectRef(two.backend_id, "archive/data", "ok.segy",
                                      loc["backend_version_id"])))  # fmt: skip
    assert head.native_class == "Archive"
    assert two.sim.early_deletion_fees == []  # Hot has no minimum duration
    assert stubs(db) == [f.file_id]
    assert drain_retention(two) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(f.ref))


def test_failed_read_back_keeps_the_source(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "bad.segy")
    two.clock.set(two.day(10))
    two.sim.faults.corrupt_next_copy(CopyFault.CORRUPT)  # same length, wrong bytes
    tick(two)
    loc = primary(db, f.file_id)
    assert loc["location_id"] == f.location_id and loc["container"] == "live/data"
    assert stubs(db) == []
    assert run(two.sim.head(f.ref)).size == 4096
    refused = row(db, "SELECT details FROM catalog.audit_log WHERE file_id = %s"
                      " AND action = 'tier.move.refused'", f.file_id)  # fmt: skip
    assert refused["details"]["why"] == "verify_failed"
    nxt = row(db, "SELECT next_action_at FROM catalog.file WHERE file_id = %s", f.file_id)
    assert nxt["next_action_at"] == two.clock.now() + timedelta(days=1)
    assert drain_retention(two) == ["deleted"]  # the bad copy goes
    assert run(two.sim.head(f.ref)).size == 4096  # the source stays
    # A day later it tries again, and a good copy goes through.
    two.clock.set(two.day(11))
    tick(two)
    assert primary(db, f.file_id)["verified_by"] == "azure_read_crc"


def test_short_archive_copy_keeps_the_original(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "d.segy")
    two.clock.set(two.day(10))
    two.sim.faults.corrupt_next_copy(CopyFault.PARTIAL)
    tick(two)
    loc = primary(db, f.file_id)
    assert loc["location_id"] == f.location_id and loc["container"] == "live/data"
    assert stubs(db) == []
    refused = row(db, "SELECT details FROM catalog.audit_log WHERE file_id = %s"
                      " AND action = 'tier.move.refused'", f.file_id)  # fmt: skip
    assert refused["details"]["why"] in ("copy_length_mismatch", "verify_failed")


def test_crash_between_verify_and_tier_change(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "t.segy")
    two.clock.set(two.day(10))
    # The first Set Blob Tier (the copy -> Archive, after the read-back) fails.
    two.sim.faults.fail_nth(Op.SET_CLASS, 1, InjectedError("worker died before the tier change"))
    tick(two)
    assert dlq(db) == []
    assert two.sim.faults.calls[Op.COPY_OBJECT] == 1  # no second copy
    loc = primary(db, f.file_id)
    assert loc["logical_tier"] == "archive" and loc["verified_by"] == "azure_read_crc"
    assert run(two.sim.head(f.ref)).size == 4096  # deleted only by retention, after commit


# --- high: the High budget across workers --------------------------------------------


def test_high_budget_holds_across_two_workers(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(db, policy_db_url, policy={"archive_day": 365})
    a = store(db, w, "a", size=6 * GiB)
    b = store(db, w, "b", size=6 * GiB)
    for s in (a, b):
        set_tier(db, w, s, T.ARCHIVE)
    ra = _recall(db, w, a, w.clock.now())
    rb = _recall(db, w, b, w.clock.now() + timedelta(seconds=1))

    async def both() -> None:
        a_done = asyncio.Event()

        async def worker(rid: int, hook: bool) -> None:
            conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
            real = conn.execute
            seen = {"n": 0}

            async def execute(query, params=None, **kw):  # type: ignore[no-untyped-def]
                q = str(query)
                if hook and "r.priority = 'high'" in q:
                    # Worker B reads the queue only after A has committed (or
                    # after 1 s if A is held back by B's lock).
                    try:
                        await asyncio.wait_for(a_done.wait(), 1.0)
                    except TimeoutError:
                        pass
                seen["n"] += 1
                return await real(query, params, **kw)

            conn.execute = execute  # type: ignore[method-assign]
            msg = Message(1, 1, w.clock.now(), w.clock.now(), {"recall_id": str(rid)})
            try:
                async with conn.transaction():
                    try:
                        await handle_restore_budgeted(
                            msg, conn, registry=w.registry, now=w.clock.now(), config=NOW_POLL
                        )
                    except Retry:
                        pass
            finally:
                if not hook:
                    a_done.set()
                await conn.close()

        tb = asyncio.create_task(worker(rb, hook=True))
        await asyncio.sleep(0.2)  # B is inside its transaction first
        await worker(ra, hook=False)
        await tb

    run(both())
    admitted = db.execute(
        "SELECT r.recall_id FROM catalog.recall r JOIN catalog.restore s"
        " ON s.restore_id = r.restore_id WHERE r.recall_id = ANY (%s) AND s.speed = 'high'",
        ([ra, rb],),
    ).fetchall()
    assert len(admitted) == 1, "two 6 GiB High restores together exceed 10 GiB/h"


def _recall(db: psycopg.Connection, w: World, s: Stored, at) -> int:  # type: ignore[no-untyped-def]
    (rid,) = db.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority, requested_at)"
        " VALUES (%s, %s, %s, 'high', %s) RETURNING recall_id",
        (s.file_id, s.version_id, w.edge_id, at),
    ).fetchone()
    return rid


def test_restore_error_is_not_masked(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(db, policy_db_url, policy={"archive_day": 365})
    s = store(db, w, "huge", size=20 * 10**9)
    set_tier(db, w, s, T.ARCHIVE)
    rid = _recall(db, w, s, w.clock.now())
    db.execute("UPDATE catalog.recall SET priority = 'standard' WHERE recall_id = %s", (rid,))
    db.execute(
        "CREATE OR REPLACE FUNCTION catalog.t_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
        " BEGIN RAISE EXCEPTION 'boom from the restore insert'; END $$"
    )
    db.execute(
        "CREATE TRIGGER t_boom BEFORE INSERT ON catalog.restore"
        " FOR EACH ROW EXECUTE FUNCTION catalog.t_boom()"
    )

    async def go() -> None:
        conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
        try:
            msg = Message(1, 1, w.clock.now(), w.clock.now(), {"recall_id": str(rid)})
            async with conn.transaction():
                await handle_restore_budgeted(
                    msg, conn, registry=w.registry, now=w.clock.now(), config=NOW_POLL
                )
        finally:
            await conn.close()

    try:
        with pytest.raises(psycopg.Error, match="boom from the restore insert"):
            run(go())
    finally:
        db.execute("DROP TRIGGER t_boom ON catalog.restore")
        db.execute("DROP FUNCTION catalog.t_boom()")


# --- medium: superseded versions ---------------------------------------------------


def new_version(db: psycopg.Connection, w: World, s: Stored, data: bytes) -> tuple[int, int]:
    ref = run(w.sim.put("data", s.ref.key, _one(data), None, {}))
    now = w.clock.now()
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (vid,) = db.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, size) SELECT file_id, dataset_id, 2, parent_id, name, 'content', 'pending',"
            " %s FROM catalog.file WHERE file_id = %s RETURNING version_id",
            (len(data), s.file_id),
        ).fetchone()
        (lid,) = db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
            " committed_at) VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s,"
            " %s) RETURNING location_id",
            (s.file_id, vid, w.backend_id, ref.container, ref.key, ref.version_id,
             "smart" if w.layout == "azure_two_account" else "hot",
             "Smart" if w.layout == "azure_two_account" else "Hot", len(data), now),
        ).fetchone()  # fmt: skip
        db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
            " WHERE version_id = %s",
            (now, vid),
        )
        db.execute(  # as the API's upload commit does
            "UPDATE catalog.file SET written_at = %s, size = %s, resident_bytes = %s"
            " WHERE file_id = %s",
            (now, len(data), len(data), s.file_id),
        )
    return vid, lid


def test_superseded_versions_are_tiered(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "v.segy")
    two.clock.set(two.day(5))
    vid2, lid2 = new_version(db, two, f, b"y" * 2048)
    two.clock.set(two.day(10))  # version 1 is due; version 2 (day 5) is not
    res = tick(two)
    assert res.version_moves == [(f.version_id, "archive")]
    old = row(db, "SELECT container, logical_tier FROM catalog.location WHERE version_id = %s"
                  " AND role = 'primary' AND state = 'committed'", f.version_id)  # fmt: skip
    assert old == {"container": "archive/data", "logical_tier": "archive"}
    assert primary(db, f.file_id)["location_id"] == lid2  # the live version is untouched
    assert tick(two).version_moves == []  # settled


def test_superseded_worm_version_archives_in_place(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = make_world(db, policy_db_url, layout="azure_worm", worm_days=3650,
                   policy={"archive_day": 30})  # fmt: skip
    f = store(db, w, "w.segy")
    w.clock.set(w.day(1))
    new_version(db, w, f, b"z" * 100)
    w.clock.set(w.day(30))
    res = tick(w)
    assert (f.version_id, "archive") in res.version_moves
    old = row(db, "SELECT location_id, logical_tier FROM catalog.location WHERE"
                  " location_id = %s", f.location_id)  # fmt: skip
    assert old == {"location_id": f.location_id, "logical_tier": "archive"}
    assert run(w.sim.head(f.ref)).native_class == "Archive"


# --- decision: smart archive day from the commit --------------------------------------


def test_smart_archive_day_ignores_reads() -> None:
    from datetime import UTC, datetime

    t0 = datetime(2026, 10, 5, tzinfo=UTC)
    pol = Policy.from_body({"mode": "smart", "clock": "last_read", "archive_day": 365})
    facts = FileFacts(written_at=t0, last_read_at=t0 + timedelta(days=300), tier=T.SMART)
    plan = evaluate(pol, facts, None, t0 + timedelta(days=365))
    assert plan.move_to is T.ARCHIVE and plan.archive_at == t0 + timedelta(days=365)


# --- low ------------------------------------------------------------------------------


def test_move_to_a_tier_the_backend_lacks_waits_a_day(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = make_world(db, policy_db_url, layout="azure_worm", policy={"archive_day": 365})
    f = store(db, w, "n.segy")

    async def go() -> None:
        conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
        try:
            body = {"kind": "move", "file_id": str(f.file_id), "version_id": str(f.version_id),
                    "location_id": str(f.location_id), "tier": "smart"}  # fmt: skip
            async with conn.transaction():
                await handle_move(Message(1, 1, w.clock.now(), w.clock.now(), body), conn,
                                  registry=w.registry, now=w.clock.now())  # fmt: skip
        finally:
            await conn.close()

    run(go())
    nxt = row(db, "SELECT next_action_at FROM catalog.file WHERE file_id = %s", f.file_id)
    assert nxt["next_action_at"] == w.clock.now() + timedelta(days=1)


def test_no_second_move_when_one_is_queued(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "q.segy")
    two.clock.set(two.day(10))
    db.execute(
        "SELECT pgmq.send('tiering'::text, %s::jsonb)",
        (Jsonb({"kind": "move", "file_id": str(f.file_id), "version_id": str(f.version_id),
                "location_id": str(f.location_id), "tier": "archive"}),),
    )  # fmt: skip

    async def go() -> scheduler.SweepResult:
        conn = await psycopg.AsyncConnection.connect(as_app(two.url, "worker"), autocommit=True)
        res = scheduler.SweepResult()
        try:
            async with conn.transaction():
                await scheduler.evaluate_file(conn, f.file_id, two.clock.now(), res)
        finally:
            await conn.close()
        return res

    res = run(go())
    assert res.moves == []
    assert len([m for m in queue(db, "tiering") if m["file_id"] == str(f.file_id)]) == 1
