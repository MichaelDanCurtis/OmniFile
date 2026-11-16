"""The restores consumer: start or join a rehydrate in sim, poll it on the
virtual clock, and move the recall through its states."""

from __future__ import annotations

from datetime import timedelta

import psycopg

from omnifile_service.backends.base import LogicalTier
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import ConsumerConfig, send
from omnifile_service.worker.recalls import RecallConfig
from omnifile_service.worker.runner import Worker

from .conftest import run
from .support import StoredFile, World, queue, seed, store

NOW_POLL = RecallConfig(poll_min=timedelta(0), poll_max=timedelta(0), poll_no_eta=timedelta(0))


def _worker(connect, sim: SimBackend, clock: VirtualClock, w: World) -> Worker:
    return Worker(
        connect,
        BackendRegistry.from_mapping({w.backend_id: sim}),
        clock=clock,
        recall_config=NOW_POLL,
        consumer_config=ConsumerConfig(backoff_base=0),
    )


def _archive(db: psycopg.Connection, sim: SimBackend, f: StoredFile) -> None:
    archived = run(sim.set_class(f.ref, LogicalTier.ARCHIVE))
    db.execute(
        "UPDATE catalog.location SET container = %s, object_key = %s, backend_version_id = %s,"
        " logical_tier = 'archive', storage_class = 'Archive', explicit_tier_set = true"
        " WHERE location_id = %s",
        (archived.container, archived.key, archived.version_id, f.location_id),
    )


def _recall(db: psycopg.Connection, f: StoredFile, priority: str = "standard") -> int:
    """What POST /recalls does: the recall and its message, one transaction."""
    with db.transaction():
        (rid,) = db.execute(
            "INSERT INTO catalog.recall (file_id, version_id, priority) VALUES (%s, %s, %s)"
            " RETURNING recall_id",
            (f.file_id, f.version_id, priority),
        ).fetchone()
        db.execute(
            "SELECT pgmq.send('restores'::text, %s::jsonb)",
            (psycopg.types.json.Jsonb({"recall_id": str(rid)}),),
        )
    return rid


def _recall_row(db: psycopg.Connection, rid: int) -> dict:
    cur = db.execute("SELECT * FROM catalog.recall WHERE recall_id = %s", (rid,))
    return dict(zip([d.name for d in cur.description], cur.fetchone(), strict=True))


def _drain(worker: Worker) -> None:
    async def go() -> None:
        await worker.restores.run_once()
        await worker.restores.close()

    run(go())


def test_archived_file_rehydrates_then_downloads(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "a.segy", b"seismic" * 100, clock.now()))
    _archive(db, sim, f)
    rid = _recall(db, f, priority="high")
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    rec = _recall_row(db, rid)
    assert rec["state"] == "rehydrating" and rec["restore_id"] is not None
    assert rec["eta"] == clock.now() + timedelta(hours=1)  # High: 1 h floor in sim
    restore = db.execute(
        "SELECT mechanism, speed, state, target_location_id FROM catalog.restore"
        " WHERE restore_id = %s",
        (rec["restore_id"],),
    ).fetchone()
    assert restore[:3] == ("azure_copy_rehydrate", "high", "in_progress")
    target = db.execute(
        "SELECT role, state, container FROM catalog.location WHERE location_id = %s",
        (restore[3],),
    ).fetchone()
    assert target == ("recall_copy", "pending", "live/recall")  # sim's choice; see backend.py
    file = db.execute(
        "SELECT restore_state FROM catalog.file WHERE file_id = %s", (f.file_id,)
    ).fetchone()
    assert file == ("in_progress",)  # the edge sees it through the feed
    assert queue(db, "notify") == [{"recall_id": str(rid)}]
    # Not done yet: polling keeps it rehydrating.
    _drain(worker)
    assert _recall_row(db, rid)["state"] == "rehydrating"
    clock.advance(timedelta(hours=1, minutes=1))
    _drain(worker)
    rec = _recall_row(db, rid)
    assert rec["state"] == "downloading"
    assert db.execute(
        "SELECT state FROM catalog.restore WHERE restore_id = %s", (rec["restore_id"],)
    ).fetchone() == ("completed",)
    assert db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (restore[3],)
    ).fetchone() == ("committed",)
    assert db.execute("SELECT count(*) FROM pgmq.q_restores").fetchone() == (0,)
    assert len(queue(db, "notify")) == 2


def test_online_file_goes_straight_to_downloading(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "hot.segy", b"x" * 10, clock.now()))
    rid = _recall(db, f)
    _drain(_worker(connect, sim, clock, w))
    assert _recall_row(db, rid)["state"] == "downloading"
    assert db.execute(
        "SELECT count(*) FROM catalog.restore r JOIN catalog.location l"
        " USING (location_id) WHERE l.file_id = %s",
        (f.file_id,),
    ).fetchone() == (0,)


def test_a_failed_rehydrate_fails_the_recall(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "f.segy", b"x" * 10, clock.now()))
    _archive(db, sim, f)
    sim.faults.fail_next_restore("disk on fire")
    rid = _recall(db, f)
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    clock.advance(timedelta(hours=16))
    _drain(worker)
    rec = _recall_row(db, rid)
    assert rec["state"] == "failed" and "disk on fire" in rec["failure"]


def test_a_second_recall_joins_the_running_restore(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "j.segy", b"x" * 10, clock.now()))
    _archive(db, sim, f)
    first = _recall(db, f)
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    # The first recall is cancelled by its requester; a new one must join.
    db.execute("UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s", (first,))
    second = _recall(db, f)
    _drain(worker)
    assert _recall_row(db, second)["restore_id"] == _recall_row(db, first)["restore_id"]
    (n,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE location_id = %s", (f.location_id,)
    ).fetchone()
    assert n == 1


def test_finished_recalls_and_bad_messages(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "done.segy", b"x", clock.now()))
    rid = _recall(db, f)
    db.execute("UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s", (rid,))
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    assert worker.restores.stats.acked == 1

    async def bad() -> None:
        conn = await connect()
        async with conn.transaction():
            await send(conn, "restores", {"recall_id": "999999999"})
        await conn.close()

    run(bad())
    _drain(worker)
    assert worker.restores.stats.dead == 1


def _placement(db: psycopg.Connection, w: World, f: StoredFile, rid: int,
               kind: str = "nas") -> int:  # fmt: skip
    """An annex placement waiting on recall `rid` (annex.md section 4.6)."""
    import uuid

    tag = uuid.uuid4().hex[:8]
    (nas,) = db.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'nas', '{}', false) RETURNING backend_id",
        (f"nas-{tag}",),
    ).fetchone()
    (filer,) = db.execute(
        "INSERT INTO catalog.filer (name, host, protocol, export, edge_id, backend_id, created_by)"
        " VALUES (%s, %s, 'smb', 'geo', %s, %s, 't') RETURNING filer_id",
        (f"f-{tag}", f"h-{tag}", w.edge_id, nas),
    ).fetchone()
    (src,) = db.execute(
        "INSERT INTO catalog.source (dataset_id, path, backend_id, container, prefix, owners_sid,"
        " kind) VALUES (%s, %s, %s, 'geo', 'lts/', 'S-1-5-21-1-2-3-512', 'annex')"
        " RETURNING source_id",
        (w.dataset_id, f"from-{tag}", nas),
    ).fetchone()
    (annex,) = db.execute(
        "INSERT INTO catalog.annex (name, source_id, filer_id, dataset_id, edge_id, path,"
        " owners_sid, created_by) VALUES (%s, %s, %s, %s, %s, 'lts', 'S-1-5-21-1-2-3-512', 't')"
        " RETURNING annex_id",
        (f"a-{tag}", src, filer, w.dataset_id, w.edge_id),
    ).fetchone()
    (ident,) = db.execute(
        "INSERT INTO catalog.identity (sid, kind) VALUES (%s, 'user') RETURNING identity_id",
        (f"S-1-5-21-9-9-{int(tag, 16) % 10**8}-1105",),
    ).fetchone()
    (pid,) = db.execute(
        "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id, recall_id,"
        " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict, state)"
        " VALUES (%s, %s, %s, %s, %s, 'portal', 'back', 1, %s, 'restored_name', 'rehydrating')"
        " RETURNING placement_id",
        (annex, f.file_id, f.version_id, rid, ident,
         psycopg.types.json.Jsonb({"kind": kind, "folder": "lts", "name": "a.segy"})),
    ).fetchone()  # fmt: skip
    return pid


def _placement_state(db: psycopg.Connection, pid: int) -> tuple:
    return db.execute(
        "SELECT state, fail_reason FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone()


def test_a_placement_is_ready_once_its_recall_has_the_bytes(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "a.segy", b"seismic" * 100, clock.now()))
    _archive(db, sim, f)
    rid = _recall(db, f)
    pid = _placement(db, w, f, rid)
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    assert _placement_state(db, pid) == ("rehydrating", None)
    clock.advance(timedelta(hours=16))
    _drain(worker)
    assert _recall_row(db, rid)["state"] == "downloading"
    assert _placement_state(db, pid) == ("ready", None)


def test_a_placement_fails_with_its_recall(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "f.segy", b"x" * 10, clock.now()))
    _archive(db, sim, f)
    sim.faults.fail_next_restore("disk on fire")
    rid = _recall(db, f)
    pid = _placement(db, w, f, rid)
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    clock.advance(timedelta(hours=16))
    _drain(worker)
    assert _placement_state(db, pid) == ("failed", "recall_failed")


def test_a_share_placement_is_handed_to_the_worker_when_its_bytes_are_back(
    db, connect, sim, clock
) -> None:
    db.execute("DELETE FROM pgmq.q_portal")
    w = seed(db)
    f = run(store(db, w, sim, "s.segy", b"x" * 10, clock.now()))
    _archive(db, sim, f)
    rid = _recall(db, f)
    nas = _placement(db, w, f, rid)
    share = _placement(db, w, f, rid, kind="share")
    worker = _worker(connect, sim, clock, w)
    _drain(worker)
    clock.advance(timedelta(hours=16))
    _drain(worker)
    assert _placement_state(db, nas)[0] == _placement_state(db, share)[0] == "ready"
    # The share's is the worker's to place (A3.2); the NAS's waits for its edge.
    assert queue(db, "portal") == [{"kind": "annex_placement", "placement_id": str(share)}]


# --- a recall whose file was replaced or deleted (migration 0059) ---------------------


def _with(connect, fn):
    async def go():
        conn = await connect()
        try:
            return await fn(conn)
        finally:
            await conn.close()

    return run(go())


def _replace(db: psycopg.Connection, w: World, f: StoredFile, name: str) -> int:
    """A new content version of the file, as an edge's commit makes one."""
    (vid,) = db.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size) VALUES (%s, %s, %s, %s, 'content', 'committed', now(), 9)"
        " RETURNING version_id",
        (f.file_id, w.dataset_id, w.root_id, name),
    ).fetchone()
    return vid


def test_a_superseded_recalls_rehydrate_runs_out_and_its_copy_expires(
    db, connect, sim, clock
) -> None:
    """The file is replaced while its rehydrate runs. The recall ends `superseded` and
    the worker stops polling for it, but the provider's copy is not aborted: the
    restore stays on record, the change feed closes it and commits the copy (so a
    read of that version in the next days needs no second rehydrate), and the recall
    copy expiry removes the copy after its window. Nothing is left behind."""
    from omnifile_service.retention.worm_sync import expire_recall_copies
    from omnifile_service.worker.changefeed_sync import register_container, sync_container

    w = seed(db)
    sim._id = w.backend_id  # the change feed sync finds a restore by its backend's own id
    f = run(store(db, w, sim, "r.segy", b"seismic" * 100, clock.now()))
    _archive(db, sim, f)
    rid = _recall(db, f)
    worker = _worker(connect, sim, clock, w)
    registry = BackendRegistry.from_mapping({w.backend_id: sim})
    _drain(worker)
    rec = _recall_row(db, rid)
    assert rec["state"] == "rehydrating"
    restore_id = rec["restore_id"]
    (target,) = db.execute(
        "SELECT target_location_id FROM catalog.restore WHERE restore_id = %s", (restore_id,)
    ).fetchone()
    containers = [
        c
        for (c,) in db.execute(
            "SELECT DISTINCT container FROM catalog.location WHERE file_id = %s", (f.file_id,)
        )
    ]
    for c in containers:
        _with(connect, lambda conn, c=c: register_container(conn, w.backend_id, c))
        _with(connect, lambda conn, c=c: sync_container(conn, registry, w.backend_id, c,
                                                        clock.now()))  # fmt: skip

    _replace(db, w, f, "r.segy")
    rec = _recall_row(db, rid)
    assert (rec["state"], rec["superseded_reason"]) == ("superseded", "replaced")
    # Its poll comes back once more and ends: the recall is not revived, not failed,
    # and the restore is left running.
    db.execute("UPDATE pgmq.q_restores SET vt = now()")
    _drain(worker)
    assert db.execute("SELECT count(*) FROM pgmq.q_restores").fetchone() == (0,)
    rec = _recall_row(db, rid)
    assert (rec["state"], rec["failure"]) == ("superseded", None)
    assert db.execute(
        "SELECT r.state, l.state FROM catalog.restore r"
        " JOIN catalog.location l ON l.location_id = r.target_location_id"
        " WHERE r.restore_id = %s",
        (restore_id,),
    ).fetchone() == ("in_progress", "pending")

    # The provider finishes; the change feed closes the restore, with no recall polling.
    clock.advance(timedelta(hours=16))
    for c in containers:
        _with(connect, lambda conn, c=c: sync_container(conn, registry, w.backend_id, c,
                                                        clock.now()))  # fmt: skip
    assert db.execute(
        "SELECT r.state, l.state FROM catalog.restore r"
        " JOIN catalog.location l ON l.location_id = r.target_location_id"
        " WHERE r.restore_id = %s",
        (restore_id,),
    ).fetchone() == ("completed", "committed")
    assert _recall_row(db, rid)["state"] == "superseded"

    # Within the window the copy stays (a rewind read of that version would use it).
    assert _with(connect, lambda c: expire_recall_copies(c, clock.now())) == 0
    clock.advance(timedelta(days=8))
    assert _with(connect, lambda c: expire_recall_copies(c, clock.now())) == 1
    assert db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (target,)
    ).fetchone() == ("deleting",)
    assert db.execute(
        "SELECT count(*) FROM pgmq.q_retention WHERE message->>'op' = 'delete_blob'"
        " AND message->>'location_id' = %s",
        (str(target),),
    ).fetchone() == (1,)


def test_no_rehydrate_is_started_for_a_recall_that_has_already_ended(
    db, connect, sim, clock
) -> None:
    """The replace lands before the worker has started the rehydrate: nothing is
    started for a recall that has ended."""
    w = seed(db)
    f = run(store(db, w, sim, "early.segy", b"x" * 10, clock.now()))
    _archive(db, sim, f)
    rid = _recall(db, f)
    _replace(db, w, f, "early.segy")
    _drain(_worker(connect, sim, clock, w))
    assert _recall_row(db, rid)["state"] == "superseded"
    assert db.execute(
        "SELECT count(*) FROM catalog.restore WHERE location_id = %s", (f.location_id,)
    ).fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM pgmq.q_restores").fetchone() == (0,)
