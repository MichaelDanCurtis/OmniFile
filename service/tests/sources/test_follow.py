"""A seeded source kept current from its bucket's events
(docs/design/catalog-seeding.md section 8; sources/follow.py)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from watched import FakeWorker, Watched, drain_job

from omnifile_service.backends.base import BackendError, ObjectRef
from omnifile_service.sources import register_source, seed_step
from omnifile_service.sources.run import create_source
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.changefeed_sync import SyncStats, sync_container
from omnifile_service.worker.seed_jobs import seed_jobs

from .support import BUCKET, GROUPS, INV, inventory, put, row, world


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _with(connect, fn):  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


def _seeded(connect, w, prefix: str = "f/") -> int:  # type: ignore[no-untyped-def]
    """Register and seed a listing source, then drain the events so far."""

    async def go(conn) -> int:  # type: ignore[no-untyped-def]
        sid = await register_source(
            conn, w.s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, prefix=prefix
        )
        while True:
            async with conn.transaction():
                state = await seed_step(conn, w.registry.get, sid, w.clock.now())
            if state == "seeded":
                return sid

    sid = run(_with(connect, go))
    _sync(connect, w)
    return sid


def _sync(connect, w) -> SyncStats:  # type: ignore[no-untyped-def]
    w.clock.advance(timedelta(seconds=5))
    return run(
        _with(connect, lambda c: sync_container(c, w.registry, w.s3_id, BUCKET, w.clock.now()))
    )


def _file(db: psycopg.Connection, w, name: str, *, live: bool = True) -> dict | None:  # type: ignore[no-untyped-def]
    cur = db.cursor(row_factory=dict_row)
    cur.execute(
        f"""
        SELECT f.*, l.object_key, l.backend_version_id, l.etag, l.state AS lstate,
               l.feed_seen_at, v.kind AS vkind, v.committed_at AS vcommitted
          FROM catalog.file f
          LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
          LEFT JOIN catalog.file_version v ON v.version_id = f.current_version_id
         WHERE f.dataset_id = %s AND f.name = %s
           AND f.deleted_at IS {"" if live else "NOT"} NULL
         ORDER BY f.file_id DESC LIMIT 1
        """,
        (w.dataset_id, name),
    )
    return cur.fetchone()


def _versions(db: psycopg.Connection, file_id: int) -> list[tuple[str, str | None]]:
    return [
        (k, s)
        for k, s in db.execute(
            "SELECT v.kind, l.state FROM catalog.file_version v"
            "  LEFT JOIN catalog.location l ON l.version_id = v.version_id AND l.role = 'primary'"
            " WHERE v.file_id = %s ORDER BY v.seq",
            (file_id,),
        ).fetchall()
    ]


def _drift(db: psycopg.Connection, w) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return dict(
        db.execute(
            "SELECT object_key, kind FROM catalog.reconcile_drift"
            " WHERE backend_id = %s AND source = 'changefeed' AND resolved_at IS NULL",
            (w.s3_id,),
        ).fetchall()
    )


def test_a_new_object_becomes_a_remote_file(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/old"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    ref = run(put(w, "f/sub/new.segy", b"12345"))
    assert _sync(connect, w).applied >= 1
    f = _file(db, w, "new.segy")
    assert f is not None
    assert (f["state"], f["size"], f["origin"]) == ("remote", 5, "s3_inventory")
    assert (f["object_key"], f["backend_version_id"]) == ("f/sub/new.segy", ref.version_id)
    assert f["feed_seen_at"] is not None and f["acl_id"] is not None
    # Replaying the events changes nothing.
    _sync(connect, w)
    assert _versions(db, f["file_id"]) == [("content", "committed")]


def test_an_overwrite_is_a_new_version_and_rewind_keeps_the_old(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    first = run(put(w, "f/a.txt", b"one"))
    _seeded(connect, w)
    before = _file(db, w, "a.txt")
    assert before is not None and before["backend_version_id"] == first.version_id
    w.clock.advance(timedelta(minutes=1))
    second = run(put(w, "f/a.txt", b"second"))
    _sync(connect, w)
    after = _file(db, w, "a.txt")
    assert after is not None and after["file_id"] == before["file_id"]
    assert (after["backend_version_id"], after["size"]) == (second.version_id, 6)
    # A versioned bucket keeps the old version: its location stays committed.
    assert _versions(db, after["file_id"]) == [("content", "committed"), ("content", "committed")]
    _sync(connect, w)
    assert len(_versions(db, after["file_id"])) == 2


def test_an_unversioned_overwrite_retires_the_old_location(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db, versioning=False)
    run(put(w, "f/a.txt", b"one"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/a.txt", b"other"))
    _sync(connect, w)
    f = _file(db, w, "a.txt")
    assert f is not None and f["size"] == 5 and f["backend_version_id"] is None
    assert _versions(db, f["file_id"]) == [("content", "deleted"), ("content", "committed")]
    # The same bytes again (a replay, or a re-upload): nothing new.
    _sync(connect, w)
    assert len(_versions(db, f["file_id"])) == 2


def test_a_delete_marker_tombstones_the_file(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    ref = run(put(w, "f/gone.txt"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(w.client.delete(ObjectRef(w.s3_id, BUCKET, ref.key)))  # a delete marker
    _sync(connect, w)
    assert _file(db, w, "gone.txt") is None
    dead = _file(db, w, "gone.txt", live=False)
    assert dead is not None and dead["vkind"] == "tombstone"
    # The version is still in S3, so its location stays: rewind reaches it.
    assert _versions(db, dead["file_id"]) == [("content", "committed"), ("tombstone", None)]
    # Put back: a new file at the path.
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/gone.txt", b"back"))
    _sync(connect, w)
    back = _file(db, w, "gone.txt")
    assert back is not None and back["file_id"] != dead["file_id"] and back["size"] == 4


def test_an_unversioned_delete_tombstones_and_retires(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db, versioning=False)
    run(put(w, "f/x.bin"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(w.client.delete(ObjectRef(w.s3_id, BUCKET, "f/x.bin")))
    _sync(connect, w)
    dead = _file(db, w, "x.bin", live=False)
    assert dead is not None and _file(db, w, "x.bin") is None
    assert _versions(db, dead["file_id"]) == [("content", "deleted"), ("tombstone", None)]
    assert _drift(db, w) == {}  # expected while S3 is live, not drift


def test_deleting_the_current_version_brings_back_the_previous(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    one = run(put(w, "f/v.txt", b"one"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    two = run(put(w, "f/v.txt", b"two!"))
    _sync(connect, w)
    mid = _file(db, w, "v.txt")
    assert mid is not None and mid["backend_version_id"] == two.version_id
    w.clock.advance(timedelta(minutes=1))
    run(w.client.delete(two))  # that version, permanently: v1 is current again
    _sync(connect, w)
    f = _file(db, w, "v.txt")
    assert f is not None and (f["backend_version_id"], f["size"]) == (one.version_id, 3)


def test_conflicts_are_drift_not_overwrites(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/file"))
    run(put(w, "f/dir/child"))
    run(put(w, "f/mine.txt"))
    _seeded(connect, w)
    # mine.txt was since written through OmniFile: its bytes are elsewhere.
    mine = _file(db, w, "mine.txt")
    assert mine is not None
    db.execute(
        "UPDATE catalog.location SET object_key = 'elsewhere' WHERE location_id = %s",
        (mine["primary_location_id"],),
    )
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/file/under"))  # a folder where a file is
    run(put(w, "f/dir"))  # a file where a folder is
    run(put(w, "f/mine.txt", b"from s3"))
    _sync(connect, w)
    assert _drift(db, w) == {
        "f/file/under": "path_conflict",
        "f/dir": "path_conflict",
        "f/mine.txt": "path_conflict",
    }
    m = _file(db, w, "mine.txt")
    assert m is not None and m["size"] == mine["size"]  # untouched


def test_a_file_busy_on_the_edge_is_not_overwritten(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/busy"))
    _seeded(connect, w)
    f = _file(db, w, "busy")
    assert f is not None
    db.execute("UPDATE catalog.file SET state = 'recalling' WHERE file_id = %s", (f["file_id"],))
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/busy", b"newer bytes"))
    _sync(connect, w)
    assert _drift(db, w) == {"f/busy": "path_conflict"}
    assert len(_versions(db, f["file_id"])) == 1


def test_keys_outside_any_source_are_left_alone(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/in"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "other/out"))
    stats = _sync(connect, w)
    assert stats.unmatched >= 1
    assert _file(db, w, "out") is None and _drift(db, w) == {}


def test_removing_a_delete_marker_brings_the_file_back(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    ref = run(put(w, "f/undo.txt", b"keep"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(w.client.delete(ObjectRef(w.s3_id, BUCKET, ref.key)))
    _sync(connect, w)
    assert _file(db, w, "undo.txt") is None
    versions = run(w.s3.list_versions(ObjectRef(w.s3_id, BUCKET, ref.key)))
    (marker,) = [v.ref for v in versions if v.is_delete_marker]
    w.clock.advance(timedelta(minutes=1))
    run(w.client.delete(marker))  # the old version is current again
    _sync(connect, w)
    back = _file(db, w, "undo.txt")
    assert back is not None and back["backend_version_id"] == ref.version_id


def test_a_new_folder_marker_makes_its_folder(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/a"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/empty/deeper/", b""))
    _sync(connect, w)
    d = _file(db, w, "deeper")
    assert d is not None and d["kind"] == "dir" and d["acl_id"] is not None
    assert _file(db, w, "empty") is not None


def test_a_followed_object_is_read_before_anything_is_written(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """A CREATED record's HEAD ran inside the page's transaction, with its writes and row
    locks. Every edge's feed waited for each of them."""
    w = world(db)
    run(put(w, "f/a.txt"))
    _seeded(connect, w)
    for i in range(3):
        run(put(w, f"f/n{i}.txt"))
    provider = Watched(w.s3, retention_db_url)
    registry = BackendRegistry.from_mapping({w.s3_id: provider})  # type: ignore[dict-item]
    w.clock.advance(timedelta(seconds=5))
    run(_with(connect, lambda c: sync_container(c, registry, w.s3_id, BUCKET, w.clock.now())))
    assert _file(db, w, "n2.txt") is not None
    assert provider.calls().count("head") == 3 and "read_changes" in provider.calls()
    assert provider.with_ids() == []


def test_a_followed_objects_failed_head_still_fails_its_own_record(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """The HEAD moved, and what its failure means didn't: the page isn't applied."""
    w = world(db)
    run(put(w, "f/a.txt"))
    _seeded(connect, w)
    run(put(w, "f/n.txt"))

    class Failing:
        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(w.s3, name)

        async def head(self, ref):  # type: ignore[no-untyped-def]
            raise BackendError("503")

    registry = BackendRegistry.from_mapping({w.s3_id: Failing()})  # type: ignore[dict-item]
    w.clock.advance(timedelta(seconds=5))
    run(_with(connect, lambda c: sync_container(c, registry, w.s3_id, BUCKET, w.clock.now())))
    assert _file(db, w, "n.txt") is None
    (failures, error) = db.execute(
        "SELECT consecutive_failures, last_error FROM catalog.changefeed_cursor"
        " WHERE backend_id = %s AND container = %s",
        (w.s3_id, BUCKET),
    ).fetchone()
    assert failures == 1 and "503" in error


def test_a_seed_step_holds_nothing_across_its_reads(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/a.txt"))

    async def create(conn) -> int:  # type: ignore[no-untyped-def]
        async with conn.transaction():
            await conn.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            return await create_source(
                conn, dataset_id=w.dataset_id, backend_id=w.s3_id, container=BUCKET, groups=GROUPS,
                prefix="f/",
            )  # fmt: skip

    db.execute("DELETE FROM pgmq.q_seed")
    sid = run(_with(connect, create))
    provider = Watched(w.s3, retention_db_url)
    registry = BackendRegistry.from_mapping({w.s3_id: provider})  # type: ignore[dict-item]
    jobs = seed_jobs(FakeWorker(registry, w.clock))
    stats = drain_job(retention_db_url, jobs, "seed")
    assert stats.dead == 0, stats.errors
    (state,) = db.execute(
        "SELECT state FROM catalog.source WHERE source_id = %s", (sid,)
    ).fetchone()
    assert state == "seeded"
    assert provider.seen, "it read the bucket"
    assert provider.with_ids() == [] and provider.with_locks() == []


def test_an_inventory_seed_holds_nothing_across_its_manifest(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """The first step reads the manifest for its snapshot time, then each batch's files."""
    w = world(db)
    mkey = run(inventory(w, [[row("f/a.txt", 1)]]))

    async def create(conn) -> int:  # type: ignore[no-untyped-def]
        async with conn.transaction():
            await conn.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            return await create_source(
                conn, dataset_id=w.dataset_id, backend_id=w.s3_id, container=BUCKET, groups=GROUPS,
                prefix="f/",
                inventory={"kind": "s3_inventory", "bucket": INV, "manifest_key": mkey},
            )  # fmt: skip

    db.execute("DELETE FROM pgmq.q_seed")
    sid = run(_with(connect, create))
    provider = Watched(w.s3, retention_db_url)
    registry = BackendRegistry.from_mapping({w.s3_id: provider})  # type: ignore[dict-item]
    stats = drain_job(retention_db_url, seed_jobs(FakeWorker(registry, w.clock)), "seed")
    assert stats.dead == 0, stats.errors
    (state,) = db.execute(
        "SELECT state FROM catalog.source WHERE source_id = %s", (sid,)
    ).fetchone()
    assert state == "seeded"
    assert "get_range" in provider.calls()
    assert provider.with_ids() == [] and provider.with_locks() == []
