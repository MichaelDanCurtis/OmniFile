"""Catalog seeding from S3: registration, the listing and inventory readers,
the catalog rows, idempotency, resume and drift
(docs/design/catalog-seeding.md)."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from omnifile_service.backends.base import LogicalTier, ObjectRef
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.sources import SourceError, register_source, seed_step
from omnifile_service.sources.__main__ import parse_inventory
from omnifile_service.sources.inventory_s3 import S3InventoryReader, close_streams
from omnifile_service.sources.listing import ListingReader
from omnifile_service.sources.paths import Unmappable, place
from omnifile_service.sources.run import retry_source
from omnifile_service.worker.changefeed_sync import SyncStats, sync_container
from omnifile_service.worker.queue import ConsumerConfig
from omnifile_service.worker.runner import Worker

from .support import BUCKET, GROUPS, INV, OWNERS, READERS, WRITERS, inventory, put, row, world


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _register(connect, w, **kw: Any) -> int:  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await register_source(
            conn, w.s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, **kw
        )
    finally:
        await conn.close()


async def _seed_all(connect, w, source_id: int, batch: int = 5000) -> int:  # type: ignore[no-untyped-def]
    """Run seed_step until the source is done, as the queue would. Returns
    the number of batches."""
    conn = await connect()
    try:
        steps = 0
        while True:
            async with conn.transaction():
                state = await seed_step(conn, w.registry.get, source_id, w.clock.now(), batch=batch)
            steps += 1
            if state in ("seeded", "failed"):
                return steps
    finally:
        await conn.close()


def _tree(db: psycopg.Connection, dataset_id: int) -> dict[str, dict]:
    rows = db.execute(
        """
        WITH RECURSIVE t(file_id, path) AS (
            SELECT file_id, ''::text COLLATE "C" FROM catalog.file
             WHERE dataset_id = %s AND parent_id IS NULL
            UNION ALL
            SELECT f.file_id, t.path || '/' || f.name FROM catalog.file f
              JOIN t ON f.parent_id = t.file_id WHERE f.deleted_at IS NULL)
        SELECT t.path, f.kind, f.state, f.size, f.uid, f.gid, f.mode, f.acl_id, f.origin,
               f.current_version_id, f.primary_location_id, f.remote_tier,
               l.object_key, l.backend_version_id, l.storage_class, l.logical_tier,
               l.state AS lstate,
               l.md5, l.etag, l.feed_seen_at, v.state AS vstate
          FROM t JOIN catalog.file f USING (file_id)
          LEFT JOIN catalog.file_version v ON v.version_id = f.current_version_id
          LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
         WHERE t.path <> ''
        """,
        (dataset_id,),
    )
    names = [d.name for d in rows.description]
    return {r[0]: dict(zip(names, r, strict=True)) for r in rows.fetchall()}


def _source(db: psycopg.Connection, source_id: int) -> dict:
    cur = db.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM catalog.source WHERE source_id = %s", (source_id,))
    return cur.fetchone()  # type: ignore[return-value]


def _drift(db: psycopg.Connection, backend_id: int) -> dict[str, str]:
    return dict(
        db.execute(
            "SELECT object_key, kind FROM catalog.reconcile_drift"
            " WHERE backend_id = %s AND source = 'seed' AND resolved_at IS NULL",
            (backend_id,),
        ).fetchall()
    )


# --- key -> path --------------------------------------------------------------


def test_keys_map_to_paths_under_the_source() -> None:
    p = place("raw/a/b/one.segy", "raw/", ("imported",), 5)
    assert p.folders == ("imported", "a", "b") and p.name == "one.segy"
    marker = place("raw/a/empty/", "raw/", (), 0)
    assert marker.folders == ("a", "empty") and marker.name is None
    for bad in ("raw/a//b", "raw/./x", "raw/../x", "raw/", "raw/" + "n" * 256, "other/x"):
        with pytest.raises(Unmappable):
            place(bad, "raw/", (), 1)
    with pytest.raises(Unmappable):
        place("raw/data/", "raw/", (), 5)  # a "folder" with bytes


# --- listing source -------------------------------------------------------------


def test_a_listing_takes_the_etag_as_the_md5_only_when_it_is_one() -> None:
    """Found by the M5 e2e test: the S3-flavoured sim knows each object's MD5
    but gives Azure-style ETags, and the seeder decoded that ETag as hex."""
    sim = SimBackend.s3(VirtualClock())
    sim.create_container("b")

    async def one(data: bytes):  # type: ignore[no-untyped-def]
        yield data

    run(sim.put("b", "k", one(b"bytes"), None, {}))
    (obj,) = run(ListingReader(sim, "b", "").read(None, 10)).objects
    assert obj.etag and obj.etag != hashlib.md5(b"bytes").hexdigest()
    assert obj.etag_is_md5 is False
    # A single-part S3 object: the ETag is the MD5's hex, and is kept as it.
    reader = ListingReader(sim, "b", "")
    md5 = hashlib.md5(b"x").digest()
    head = run(sim.head(ObjectRef(sim.backend_id, "b", "k")))
    assert reader._object(replace(head, md5=md5, etag=md5.hex())).etag_is_md5 is True
    assert reader._object(replace(head, md5=md5, etag=md5.hex() + "-2")).etag_is_md5 is False
    assert reader._object(replace(head, md5=None, etag=md5.hex())).etag_is_md5 is False


def test_a_listing_seeds_folders_files_versions_and_locations(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "raw/survey/line_1042.segy", b"seismic"))
    run(put(w, "raw/survey/notes.txt", b"n"))
    run(put(w, "raw/top.txt", b"t"))
    run(put(w, "raw/empty/", b""))
    run(put(w, "elsewhere/x", b"x"))  # outside the source prefix
    sid = run(_register(connect, w, prefix="raw/", path="imported/s3"))
    src = _source(db, sid)
    assert src["state"] == "registered" and src["snapshot_at"] == w.clock.now()
    assert run(_seed_all(connect, w, sid, batch=2)) >= 2  # resumed across batches
    tree = _tree(db, w.dataset_id)
    assert set(tree) == {
        "/imported",
        "/imported/s3",
        "/imported/s3/survey",
        "/imported/s3/empty",
        "/imported/s3/survey/line_1042.segy",
        "/imported/s3/survey/notes.txt",
        "/imported/s3/top.txt",
    }
    f = tree["/imported/s3/survey/line_1042.segy"]
    assert (f["kind"], f["state"], f["size"], f["origin"]) == ("file", "remote", 7, "s3_inventory")
    assert (f["uid"], f["gid"], f["mode"]) == (5000, 5001, 0o660)
    assert f["vstate"] == "committed" and f["lstate"] == "committed"
    assert f["object_key"] == "raw/survey/line_1042.segy"
    assert f["backend_version_id"]  # versioned bucket: the version seeded
    assert f["storage_class"] == "STANDARD" and f["logical_tier"] == "hot"
    assert f["feed_seen_at"] == src["snapshot_at"]
    assert f["primary_location_id"] is not None  # the commit trigger linked it
    folder = tree["/imported/s3/survey"]
    assert folder["kind"] == "dir" and folder["mode"] == 0o770 and folder["acl_id"]
    assert all(r["acl_id"] for r in tree.values())  # never unreadable, never world-readable
    done = _source(db, sid)
    assert done["state"] == "seeded" and done["seeded_at"] is not None
    assert done["files_created"] == 3 and done["dirs_created"] == 4
    assert done["skipped"] == 0 and done["resume"] is None


def test_the_acls_come_from_the_source_groups(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f.txt"))
    sid = run(_register(connect, w))
    run(_seed_all(connect, w, sid))
    rows = db.execute(
        "SELECT f.kind, a.mode, a.read_sids::text[], a.group_sid::text FROM catalog.file f"
        "  JOIN catalog.acl a USING (acl_id)"
        " WHERE f.dataset_id = %s AND f.parent_id IS NOT NULL",
        (w.dataset_id,),
    ).fetchall()
    assert {k for k, *_ in rows} == {"file"}
    for _, mode, read, group in rows:
        assert group == OWNERS and mode == 0o660
        assert set(read) == {OWNERS, WRITERS, READERS}
        assert "S-1-1-0" not in read


def test_seeding_twice_changes_nothing(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    for i in range(3):
        run(put(w, f"d/{i}.bin"))
    sid = run(_register(connect, w))
    run(_seed_all(connect, w, sid))
    before = _tree(db, w.dataset_id)
    db.execute(
        "UPDATE catalog.source SET state = 'seeding', resume = NULL WHERE source_id = %s", (sid,)
    )
    run(_seed_all(connect, w, sid))
    assert _tree(db, w.dataset_id) == before
    s = _source(db, sid)
    assert s["files_created"] == 3 and s["already_present"] == 3


def test_unmappable_keys_and_path_conflicts_are_drift(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "a//b"))
    run(put(w, "x"))
    run(put(w, "x/y"))  # needs a folder where file x is; in one batch the folder wins
    run(put(w, "ok.txt"))
    sid = run(_register(connect, w))
    run(_seed_all(connect, w, sid))
    assert _drift(db, w.s3_id) == {"a//b": "unmappable", "x": "path_conflict"}
    tree = _tree(db, w.dataset_id)
    assert tree["/x"]["kind"] == "dir" and {"/x/y", "/ok.txt"} <= set(tree)
    assert _source(db, sid)["skipped"] == 2


# --- inventory source ------------------------------------------------------------


def test_an_inventory_seeds_current_objects_only(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    mkey = run(
        inventory(
            w,
            [
                [
                    row("raw/a.segy", 100, cls="GLACIER", etag="a" * 32),
                    row("raw/a.segy", 90, version="v0", latest=False),  # an older version
                    row("raw/gone.txt", 0, marker=True),
                ],  # a delete marker
                [
                    row("raw/with+space%2Bplus.txt", 5, etag="b" * 32, enc="SSE-KMS"),
                    row(
                        "raw/smart.bin",
                        7,
                        cls="INTELLIGENT_TIERING",
                        it="DEEP_ARCHIVE_ACCESS",
                        version="null",
                    ),
                ],
            ],
        )
    )
    sid = run(
        _register(
            connect,
            w,
            prefix="raw/",
            inventory={"kind": "s3_inventory", "bucket": INV, "manifest_key": mkey},
        )
    )
    run(_seed_all(connect, w, sid, batch=1))
    tree = _tree(db, w.dataset_id)
    assert set(tree) == {"/a.segy", "/with space+plus.txt", "/smart.bin"}
    a = tree["/a.segy"]
    assert (a["size"], a["storage_class"], a["logical_tier"]) == (100, "GLACIER", "archive")
    assert a["md5"] == bytes.fromhex("a" * 32)  # SSE-S3 single part: the ETag is the MD5
    assert a["remote_tier"] == "archive"
    kms = tree["/with space+plus.txt"]
    assert kms["md5"] is None and kms["etag"] == "b" * 32
    smart = tree["/smart.bin"]
    assert smart["logical_tier"] == str(LogicalTier.ARCHIVE) and smart["backend_version_id"] is None
    s = _source(db, sid)
    assert s["state"] == "seeded" and s["files_created"] == 3


def test_a_bad_inventory_is_refused_or_fails_the_source(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)

    def inv(**kw: Any) -> dict:
        return {
            "kind": "s3_inventory",
            "bucket": INV,
            "manifest_key": run(inventory(w, [[]], **kw)),
        }

    with pytest.raises(SourceError, match="CSV"):
        run(_register(connect, w, inventory=inv(fmt="Parquet")))
    with pytest.raises(SourceError, match="Size"):
        run(_register(connect, w, inventory=inv(schema="Bucket, Key")))
    with pytest.raises(SourceError, match="manifest.checksum"):
        run(_register(connect, w, inventory=inv(corrupt_checksum=True)))
    with pytest.raises(SourceError, match="newer"):
        run(_register(connect, w, inventory=inv(created=w.clock.now() - timedelta(days=15))))
    # A data file that isn't gzip fails the source when it's read.
    good = inv()
    first = run(_first_data_key(w, good["manifest_key"]))
    run(put(w, first, b"not gzip", bucket=INV))
    sid = run(_register(connect, w, inventory=good))
    run(_seed_all(connect, w, sid))
    s = _source(db, sid)
    assert s["state"] == "failed" and "gzip" in s["last_error"]


async def _first_data_key(w, mkey: str) -> str:  # type: ignore[no-untyped-def]
    import json

    from omnifile_service.backends.base import ObjectRef

    raw = b"".join([c async for c in w.s3.get_range(ObjectRef(w.s3_id, INV, mkey), 0, 1 << 30)])
    return json.loads(raw)["files"][0]["key"]


# --- registration -----------------------------------------------------------------


def test_registering_starts_the_migration_and_the_feed_at_the_snapshot(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    created = w.clock.now() - timedelta(hours=20)
    mkey = run(inventory(w, [[row("p/a", 1)]], created=created))
    run(
        _register(
            connect,
            w,
            prefix="p/",
            inventory={"kind": "s3_inventory", "bucket": INV, "manifest_key": mkey},
        )
    )
    state, source_backend = db.execute(
        "SELECT migration_state, source_backend_id FROM catalog.dataset WHERE dataset_id = %s",
        (w.dataset_id,),
    ).fetchone()
    assert (state, source_backend) == ("migrating", w.s3_id)
    (cursor,) = db.execute(
        "SELECT cursor FROM catalog.changefeed_cursor WHERE backend_id = %s AND container = %s",
        (w.s3_id, BUCKET),
    ).fetchone()
    assert cursor == f"s3ev:{BUCKET}:{int(created.timestamp())}:0"
    (queued,) = db.execute(
        "SELECT count(*) FROM pgmq.q_seed WHERE message->>'source_id' IS NOT NULL"
    ).fetchone()
    assert queued >= 1
    # Overlapping prefixes and bad shapes are refused.
    for kw in ({"prefix": "p/q/"}, {"prefix": ""}):
        with pytest.raises(SourceError, match="overlaps"):
            run(_register(connect, w, **kw))
    for kw in (
        {"prefix": "noslash"},
        {"prefix": "z/", "path": "/abs"},
        {"prefix": "z/", "path": "a/../b"},
    ):
        with pytest.raises(SourceError):
            run(_register(connect, w, **kw))


def test_the_worker_seeds_a_source_through_its_queue(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    db.execute("DELETE FROM pgmq.q_seed")
    run(put(w, "q/one"))
    run(put(w, "q/two"))
    sid = run(_register(connect, w, prefix="q/"))
    wk = Worker(connect, w.registry, clock=w.clock, consumer_config=ConsumerConfig(backoff_base=0))

    async def drain() -> int:
        try:
            return await wk.consumers["seed"].drain()
        finally:
            await wk.consumers["seed"].close()

    assert run(drain()) >= 1
    assert _source(db, sid)["state"] == "seeded"
    assert {"/one", "/two"} <= set(_tree(db, w.dataset_id))


def test_parse_inventory_url() -> None:
    assert parse_inventory("s3://inv/b/daily/manifest.json") == {
        "kind": "s3_inventory",
        "bucket": "inv",
        "manifest_key": "b/daily/manifest.json",
    }
    assert parse_inventory(None) is None
    with pytest.raises(SystemExit):
        parse_inventory("https://x/y")


def test_across_batches_the_first_seeded_wins_a_path(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "a"))
    run(put(w, "x"))
    run(put(w, "x/y"))  # pages of two: x/y is on the second page, a later batch
    sid = run(_register(connect, w))
    run(_seed_all(connect, w, sid, batch=1))
    assert _drift(db, w.s3_id) == {"x/y": "path_conflict"}
    assert _tree(db, w.dataset_id)["/x"]["kind"] == "file"


def test_a_path_through_a_file_is_refused(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    db.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, 'f', 'file')",
        (w.dataset_id, w.root_id),
    )
    with pytest.raises(SourceError, match="is a file"):
        run(_register(connect, w, path="f/sub"))


def test_a_failed_source_resumes_where_it_stopped(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "r/a"))
    run(put(w, "r/b"))
    run(put(w, "r/c"))
    sid = run(_register(connect, w, prefix="r/"))
    db.execute(
        "UPDATE catalog.source SET state = 'failed', last_error = 'boom' WHERE source_id = %s",
        (sid,),
    )

    async def retry() -> None:
        conn = await connect()
        try:
            await retry_source(conn, sid)
            with pytest.raises(SourceError, match="not failed"):
                await retry_source(conn, sid)
        finally:
            await conn.close()

    run(retry())
    assert _source(db, sid)["state"] == "seeding"
    run(_seed_all(connect, w, sid))
    s = _source(db, sid)
    assert s["state"] == "seeded" and s["files_created"] == 3 and s["last_error"] is None


def test_a_seed_skips_analyse_while_another_holds_its_lock(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "lk/a"))
    sid = run(_register(connect, w, prefix="lk/"))
    with psycopg.connect(retention_db_url) as other:  # a transaction, not autocommit
        other.execute("ANALYZE catalog.file")  # holds the lock until it ends
        run(_seed_all(connect, w, sid))
        other.rollback()
    assert _source(db, sid)["state"] == "seeded"


# --- review fixes: kept streams, registration lock, held feed -----------------


def _inv(mkey: str) -> dict:
    return {"kind": "s3_inventory", "bucket": INV, "manifest_key": mkey}


def _count_gets(w) -> list[str]:  # type: ignore[no-untyped-def]
    gets: list[str] = []
    real = w.s3.get_range

    def counting(ref, offset, length):  # type: ignore[no-untyped-def]
        gets.append(ref.key)
        return real(ref, offset, length)

    w.s3.get_range = counting
    return gets


def test_an_inventory_file_is_read_once_across_batches(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    mkey = run(inventory(w, [[row(f"s/{i}") for i in range(7)], [row("s/z")]]))
    sid = run(_register(connect, w, prefix="s/", inventory=_inv(mkey)))
    gets = _count_gets(w)
    assert run(_seed_all(connect, w, sid, batch=2)) >= 4
    data = sorted(k for k in gets if k.endswith(".csv.gz"))
    assert [k.rsplit("/", 1)[1] for k in data] == ["part-0.csv.gz", "part-1.csv.gz"]
    assert _source(db, sid)["files_created"] == 8


def test_a_batch_read_again_reopens_its_file(db) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    mkey = run(inventory(w, [[row(f"t/{i}") for i in range(6)]]))
    reader = S3InventoryReader(w.s3, INV, mkey, source_bucket=BUCKET, source_prefix="t/")
    gets = _count_gets(w)

    async def go() -> list[list[str]]:
        try:
            a = await reader.read(None, 2)
            b = await reader.read(a.resume, 2)
            again = await reader.read(a.resume, 2)  # b's transaction rolled back
            c = await reader.read(again.resume, 2)
            assert c.resume is not None and (await reader.read(c.resume, 2)).resume is None
            return [[o.key for o in x.objects] for x in (a, b, again, c)]
        finally:
            await close_streams()

    a, b, again, c = run(go())
    assert (a, b, again, c) == (["t/0", "t/1"], ["t/2", "t/3"], ["t/2", "t/3"], ["t/4", "t/5"])
    assert len([k for k in gets if k.endswith(".csv.gz")]) == 2  # opened, then reopened once


def test_registrations_on_one_bucket_take_turns(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Two datasets, overlapping prefixes of one bucket: the second waits for
    the first to commit, then sees its prefix."""
    w = world(db)
    (other,) = db.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout, posix_defaults)"
        " SELECT name || '-2', backend_id, edge_id, storage_layout, posix_defaults"
        "   FROM catalog.dataset WHERE dataset_id = %s RETURNING dataset_id",
        (w.dataset_id,),
    ).fetchone()
    db.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, NULL, '', 'dir')",
        (other,),
    )

    async def go() -> None:
        first = await psycopg.AsyncConnection.connect(retention_db_url)  # a transaction
        try:
            await first.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"omnifile.source:{w.s3_id}:{BUCKET}",),
            )
            second = asyncio.ensure_future(register_source_on(other, prefix="o/p/"))
            await asyncio.sleep(0.3)
            assert not second.done()  # waiting for the bucket's lock
            await first.execute(
                "INSERT INTO catalog.source (dataset_id, backend_id, container, prefix,"
                " owners_sid) VALUES (%s, %s, %s, 'o/', %s)",
                (w.dataset_id, w.s3_id, BUCKET, OWNERS),
            )
            await first.commit()
            with pytest.raises(SourceError, match="overlaps"):
                await second
        finally:
            await first.close()

    async def register_source_on(dataset_id: int, **kw: Any) -> int:
        conn = await connect()
        try:
            return await register_source(
                conn, w.s3, dataset_id=dataset_id, container=BUCKET, groups=GROUPS, **kw
            )
        finally:
            await conn.close()

    run(go())


def test_the_change_feed_waits_until_the_bucket_is_seeded(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    a = run(put(w, "g/a"))
    b = run(put(w, "g/b"))
    rows = [row(r.key, 1, version=str(r.version_id)) for r in (a, b)]
    sid = run(_register(connect, w, prefix="g/", inventory=_inv(run(inventory(w, [rows])))))
    w.clock.advance(timedelta(minutes=1))
    run(w.client.delete(a))  # that version, after the inventory was taken

    async def sync() -> SyncStats:
        conn = await connect()
        try:
            return await sync_container(conn, w.registry, w.s3_id, BUCKET, w.clock.now())
        finally:
            await conn.close()

    def cursor() -> tuple:
        return db.execute(
            "SELECT last_error, last_record_at FROM catalog.changefeed_cursor"
            " WHERE backend_id = %s AND container = %s",
            (w.s3_id, BUCKET),
        ).fetchone()

    held = run(sync())
    assert held.records == 0 and held.caught_up[(w.s3_id, BUCKET)] is False
    assert cursor()[0] == f"held: source {sid} is registered"
    run(_seed_all(connect, w, sid, batch=1))
    assert run(sync()).records >= 1  # the events waited on the queue
    assert cursor()[0] is None
    assert "/a" not in _tree(db, w.dataset_id)  # the seeded row learnt of the delete
    assert "/b" in _tree(db, w.dataset_id)
    # Those events are gone now: an inventory from before them can't be
    # replayed for another prefix. A listing (it reads after now) can.
    h = run(inventory(w, [[row("h/a")]]))
    with pytest.raises(SourceError, match="can't be replayed"):
        run(_register(connect, w, prefix="h/", inventory=_inv(h)))
    run(_register(connect, w, prefix="h/"))


# --- registered through the API: the worker starts it ---------------------------------


def _created(connect, w, **kw: Any) -> int:  # type: ignore[no-untyped-def]
    from omnifile_service.sources.run import create_source

    async def go(conn) -> int:  # type: ignore[no-untyped-def]
        async with conn.transaction():
            return await create_source(
                conn, dataset_id=w.dataset_id, backend_id=w.s3_id, container=BUCKET,
                groups=GROUPS, **kw,
            )  # fmt: skip

    return run(_conn_call(connect, go))


async def _conn_call(connect, fn):  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


def test_the_first_seed_step_starts_an_api_source(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    created = w.clock.now() - timedelta(hours=3)
    mkey = run(inventory(w, [[row("api/a", 1)]], created=created))
    sid = _created(connect, w, prefix="api/", inventory=_inv(mkey))
    s = _source(db, sid)
    assert (s["state"], s["snapshot_at"]) == ("registered", None)
    # No storage call yet, so no feed either.
    assert (
        db.execute(
            "SELECT cursor FROM catalog.changefeed_cursor WHERE backend_id = %s", (w.s3_id,)
        ).fetchone()
        is None
    )
    run(_seed_all(connect, w, sid))
    s = _source(db, sid)
    assert (s["state"], s["snapshot_at"], s["files_created"]) == ("seeded", created, 1)
    (cursor,) = db.execute(
        "SELECT cursor FROM catalog.changefeed_cursor WHERE backend_id = %s", (w.s3_id,)
    ).fetchone()
    assert cursor == f"s3ev:{BUCKET}:{int(created.timestamp())}:0"


def test_an_old_inventory_fails_an_api_source(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    mkey = run(inventory(w, [[row("old/a", 1)]], created=w.clock.now() - timedelta(days=20)))
    sid = _created(connect, w, prefix="old/", inventory=_inv(mkey))
    run(_seed_all(connect, w, sid))
    s = _source(db, sid)
    assert s["state"] == "failed" and "Use a newer one" in s["last_error"]
    assert s["snapshot_at"] is None and s["objects_seen"] == 0


def test_adopted_rows_have_origin_adopt(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    db.execute(
        "UPDATE catalog.dataset SET backend_id = %s WHERE dataset_id = %s", (w.s3_id, w.dataset_id)
    )
    run(put(w, "moved/a.segy"))
    sid = _created(connect, w, prefix="moved/", kind="adopt")
    run(_seed_all(connect, w, sid))
    tree = _tree(db, w.dataset_id)
    assert tree["/a.segy"]["origin"] == "adopt"
    (state,) = db.execute(
        "SELECT migration_state FROM catalog.dataset WHERE dataset_id = %s", (w.dataset_id,)
    ).fetchone()
    assert state == "steady"
