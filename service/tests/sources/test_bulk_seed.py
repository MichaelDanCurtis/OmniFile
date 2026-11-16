"""The bulk seed path (COPY into catalog.seed_stage, then catalog.seed_merge)
leaves the same catalog as the row-by-row path (docs/design/catalog-seeding.md
section 6), and its trigger bypass can't be used by anyone else.

The equivalence test seeds the same objects into two datasets, one through
each path, and compares every table the seeder touches by path: files,
versions, locations, ACLs, the feed (in its logical form: each entity's last
event, and that it carries the entity's version), the audit log and the drift
rows. The input covers nested folders, folder markers, unmappable keys, path
conflicts inside the batch and with existing entries, objects already
present, a WORM share (retention and its audit rows) and a legal hold.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from omnifile_service.backends.base import LogicalTier
from omnifile_service.catalog.privileges import as_app
from omnifile_service.sources.model import SeedObject
from omnifile_service.sources.seed import Source, Written, write_batch

from .support import BUCKET, GROUPS, World, world

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def obj(key: str, size: int = 10, *, version: str | None = "v1", minutes: int = 0,
        cls: str = "STANDARD", tier: LogicalTier = LogicalTier.HOT,
        md5: bool = True) -> SeedObject:  # fmt: skip
    etag = hashlib.sha256(key.encode()).hexdigest()[:16] * 2
    return SeedObject(
        key=key, version_id=version, size=size, last_modified=T0 + timedelta(minutes=minutes),
        native_class=cls, logical_tier=tier, etag=etag, etag_is_md5=md5,
    )  # fmt: skip


#: Every case the seeder distinguishes, in one batch.
OBJECTS = [
    obj("raw/a/one.segy", 100),
    obj("raw/a/two.segy", 200, minutes=1),
    obj("raw/a/tomb.segy", 150, minutes=1),  # where a deleted file was
    obj("raw/a/b/c/deep.segy", 300, minutes=2),
    obj("raw/empty/", 0),  # a folder marker
    obj("raw/a/b/", 0),  # a marker for a folder files also make
    obj("raw/bad//name", 5),  # unmappable
    obj("raw/" + "n" * 256, 5),  # unmappable: too long
    obj("raw/clash", 7),  # a file where ...
    obj("raw/clash/inner.segy", 8),  # ... a folder is needed: the folder wins
    obj("raw/taken.segy", 9),  # an existing entry has this path
    obj("raw/unversioned.segy", 11, version=None),
    obj("raw/glacier.segy", 12, cls="GLACIER", tier=LogicalTier.ARCHIVE),
    obj("raw/it.segy", 13, cls="INTELLIGENT_TIERING", tier=LogicalTier.SMART, md5=False),
    obj("raw/worm/kept.segy", 14, minutes=3),  # under a WORM share
    obj("raw/worm/sub/kept2.segy", 15, minutes=4),
    obj("raw/held/evidence.segy", 16, minutes=5),  # under a legal hold
    obj("raw/held/sub/more.segy", 17, minutes=6),
] + [obj(f"raw/many/f{i:04d}.segy", i, minutes=i % 50) for i in range(1200)]


def _edge(db: psycopg.Connection, w: World) -> int:
    return db.execute(
        "SELECT edge_id FROM catalog.dataset WHERE dataset_id = %s", (w.dataset_id,)
    ).fetchone()[0]


def _dir(db: psycopg.Connection, w: World, parent: int, name: str) -> int:
    return db.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, %s, 'dir')"
        " RETURNING file_id",
        (w.dataset_id, parent, name),
    ).fetchone()[0]


def _prepare(db: psycopg.Connection, w: World) -> Source:
    """The dataset as both paths find it: folders imported/raw-tree pieces,
    a file already at one path, a WORM share and a legal hold."""
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        # WORM shares live on single-account datasets (decision 31).
        db.execute(
            "UPDATE catalog.dataset SET storage_layout = 'single_account' WHERE dataset_id = %s",
            (w.dataset_id,),
        )
        imported = _dir(db, w, w.root_id, "imported")
        worm = _dir(db, w, imported, "worm")
        held = _dir(db, w, imported, "held")
        db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state)"
            " VALUES (%s, %s, 'taken.segy', 'file', 'resident')",
            (w.dataset_id, imported),
        )
        # A deleted file at a path the seed writes: a tombstone doesn't take
        # the name (review on #35).
        a = _dir(db, w, imported, "a")
        db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, deleted_at)"
            " VALUES (%s, %s, 'tomb.segy', 'file', 'resident', now())",
            (w.dataset_id, a),
        )
        db.execute(
            "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, owners_group_sid,"
            " root_dir_id, worm_enabled, worm_mode, worm_retention_days)"
            " VALUES (%s, %s, 'worm', ARRAY['smb'], %s, %s, true, 'unlocked_test', 30)",
            (w.dataset_id, _edge(db, w), GROUPS.owners, worm),
        )
        db.execute(
            "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
            " VALUES (%s, %s, 'legal', 'case 7', 'test')",
            (w.dataset_id, held),
        )
    return Source(
        source_id=-1, dataset_id=w.dataset_id, path="imported", backend_id=w.s3_id,
        container=BUCKET, prefix="raw/", groups=GROUPS, snapshot_at=T0 + timedelta(hours=1),
    )  # fmt: skip


async def _write(connect, src: Source, objects: list[SeedObject], *, bulk: bool) -> Written:  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        async with conn.transaction():
            return await write_batch(conn, src, objects, T0 + timedelta(days=1), bulk=bulk)
    finally:
        await conn.close()


# --- the catalog's logical form ------------------------------------------------

_PATHS = """
WITH RECURSIVE t(file_id, path) AS (
    SELECT file_id, ''::text COLLATE "C" FROM catalog.file
     WHERE dataset_id = %s AND parent_id IS NULL
    UNION ALL
    SELECT f.file_id, t.path || '/' || f.name FROM catalog.file f JOIN t ON f.parent_id = t.file_id
)
"""


def _rows(db: psycopg.Connection, sql: str, *args: Any) -> list[dict]:
    cur = db.execute(sql, args)
    names = [d.name for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def snapshot(db: psycopg.Connection, w: World, *, entity_version: bool = True) -> dict[str, Any]:
    """Everything the seeder wrote for a dataset, keyed by path, without
    surrogate ids or the times rows were written."""
    ds = w.dataset_id
    acl = "(SELECT a.sha256 FROM catalog.acl a WHERE a.acl_id = {}.acl_id)"
    files = _rows(db, _PATHS + f"""
        SELECT t.path, f.kind, f.state, f.size, f.resident_bytes, f.mtime_ns, f.uid, f.gid,
               f.mode, f.owner_sid, {acl.format('f')} AS acl, f.origin, f.remote_tier,
               f.restore_state, f.restore_expires_at, f.deleted_at IS NOT NULL AS deleted,
               f.crc64nvme, f.blake3,
               f.next_action, f.archive_at, f.written_at, f.entity_version,
               cv.seq AS current_seq, kv.seq AS content_seq, pl.object_key AS primary_key,
               pl.role AS primary_role
          FROM t JOIN catalog.file f USING (file_id)
          LEFT JOIN catalog.file_version cv ON cv.version_id = f.current_version_id
          LEFT JOIN catalog.file_version kv ON kv.version_id = f.content_version_id
          LEFT JOIN catalog.location pl ON pl.location_id = f.primary_location_id
         ORDER BY t.path, f.deleted_at IS NOT NULL""", ds)  # fmt: skip
    versions = _rows(db, _PATHS + f"""
        SELECT t.path, v.seq, v.kind, v.state, v.committed_at, v.superseded_at, v.size,
               v.mtime_ns, v.uid, v.gid, v.mode, {acl.format('v')} AS acl, v.retention_until,
               v.legal_hold, v.content_version_id, v.edge_id, v.edge_op_id, v.crc64nvme,
               v.actor_via, v.name, (v.parent_id = f.parent_id) AS same_parent
          FROM t JOIN catalog.file f USING (file_id)
          JOIN catalog.file_version v ON v.file_id = f.file_id
         ORDER BY t.path, v.seq""", ds)  # fmt: skip
    locations = _rows(db, _PATHS + """
        SELECT t.path, v.seq, l.container, l.object_key, l.backend_version_id, l.role, l.state,
               l.logical_tier, l.storage_class, l.tier_changed_at, l.explicit_tier_set, l.size,
               l.crc64nvme, l.md5, l.etag, l.verified, l.verified_by, l.committed_at,
               l.feed_seen_at, l.worm_sync_needed, l.worm_stamped_until, l.worm_stamped_locked,
               l.worm_stamped_hold
          FROM t JOIN catalog.location l USING (file_id)
          JOIN catalog.file_version v ON v.version_id = l.version_id
         ORDER BY t.path, v.seq, l.role""", ds)  # fmt: skip
    acls = _rows(
        db,
        "SELECT sha256, owner_sid, group_sid, mode, entries, read_sids, deny_sids FROM catalog.acl"
        " WHERE dataset_id = %s ORDER BY sha256",
        ds,
    )
    # The feed in its logical form: what an edge ends up applying.
    feed = _rows(db, _PATHS + """
        SELECT DISTINCT ON (e.entity, coalesce(t.path, e.entity_id::text))
               e.entity, coalesce(t.path, '') AS path, e.op, e.edge_id, e.origin_edge_id,
               e.origin_op_id, e.entity <> 'file' OR e.entity_version = f.entity_version
                   AS carries_version
          FROM catalog.feed_event e
          LEFT JOIN t ON e.entity = 'file' AND t.file_id = e.entity_id
          LEFT JOIN catalog.file f ON e.entity = 'file' AND f.file_id = e.entity_id
         WHERE e.dataset_id = %s AND e.entity IN ('file', 'acl')
         ORDER BY e.entity, coalesce(t.path, e.entity_id::text), e.event_id DESC""",
        ds, ds)  # fmt: skip
    for e in feed:
        if e["entity"] == "acl":
            e["path"] = "(acl)"  # ids differ; the count and ops must match
    audit = _rows(db, _PATHS + """
        SELECT a.actor, a.action, a.decision, t.path, v.seq, a.details,
               a.share_id IS NULL AS no_share
          FROM catalog.audit_log a
          LEFT JOIN t ON t.file_id = a.file_id
          LEFT JOIN catalog.file_version v ON v.version_id = a.version_id
         WHERE a.dataset_id = %s AND a.action LIKE 'version.%%'
         ORDER BY t.path, a.action""", ds, ds)  # fmt: skip
    drift = _rows(
        db,
        "SELECT object_key, backend_version_id, kind, observed->>'reason' AS reason,"
        " observed->>'size' AS size FROM catalog.reconcile_drift"
        " WHERE backend_id = %s AND source = 'seed' ORDER BY object_key, kind",
        w.s3_id,
    )
    # A row's entity_version counts its updates, which the paths make
    # differently (row by row: the insert, then two trigger updates; bulk:
    # the insert). What an edge relies on is that each entity's last event
    # carries the version its row has now.
    for f in files:
        f.pop("entity_version")
    if not entity_version:
        for e in feed:
            e.pop("carries_version")
    return {
        "files": files,
        "versions": versions,
        "locations": locations,
        "acls": acls,
        "feed": sorted(feed, key=lambda e: (e["entity"], e["path"])),
        "feed_ops": Counter((e["entity"], e["op"]) for e in feed),
        "audit": audit,
        "drift": drift,
    }


def _written(w: Written) -> tuple:
    return (w.files, w.dirs, w.present, w.skipped,
            sorted((o.key, k, why) for o, k, why in w.drift))  # fmt: skip


# --- equivalence ------------------------------------------------------------------


def test_a_bulk_seed_leaves_the_same_catalog_as_a_row_by_row_seed(db, connect) -> None:
    rows, bulk = world(db), world(db)
    src_rows, src_bulk = _prepare(db, rows), _prepare(db, bulk)

    by_rows = run(_write(connect, src_rows, OBJECTS, bulk=False))
    by_bulk = run(_write(connect, src_bulk, OBJECTS, bulk=True))
    assert _written(by_rows) == _written(by_bulk)
    assert by_bulk.files == 1200 + 12 and by_bulk.skipped == 4, _written(by_bulk)

    a, b = snapshot(db, rows), snapshot(db, bulk)
    for table in a:
        assert a[table] == b[table], table
    assert [e for e in b["feed"] if not e["carries_version"]] == []
    # The cases the input covers did happen.
    versions = {v["path"]: v for v in b["versions"]}
    assert versions["/imported/worm/kept.segy"]["retention_until"] == T0 + timedelta(
        minutes=3, days=30
    )
    assert versions["/imported/worm/sub/kept2.segy"]["retention_until"] is not None
    assert versions["/imported/held/sub/more.segy"]["legal_hold"] is True
    assert versions["/imported/a/one.segy"]["retention_until"] is None
    assert not versions["/imported/a/one.segy"]["legal_hold"]
    assert [x["path"] for x in b["audit"]] == [
        "/imported/worm/kept.segy", "/imported/worm/sub/kept2.segy"
    ]  # fmt: skip
    worm_flags = {x["path"]: x["worm_sync_needed"] for x in b["locations"]}
    assert worm_flags["/imported/held/evidence.segy"] and worm_flags["/imported/worm/kept.segy"]
    assert not worm_flags["/imported/a/one.segy"]
    assert {d["kind"] for d in b["drift"]} == {"unmappable", "path_conflict"}

    # A replayed batch changes nothing, through either path.
    again_rows = run(_write(connect, src_rows, OBJECTS, bulk=False))
    again_bulk = run(_write(connect, src_bulk, OBJECTS, bulk=True))
    assert _written(again_rows) == _written(again_bulk)
    assert again_bulk.files == 0 and again_bulk.present == by_bulk.files
    a2, b2 = snapshot(db, rows), snapshot(db, bulk)
    for table in a2:
        assert a2[table] == b2[table], table
        if table != "drift":  # the drift rows are seen again (and may say why anew)
            assert b2[table] == b[table], table


def test_the_bulk_path_is_one_feed_event_per_file_in_one_statement(db, connect) -> None:
    w = world(db)
    src = _prepare(db, w)
    before = db.execute("SELECT coalesce(max(event_id), 0) FROM catalog.feed_event").fetchone()[0]
    run(_write(connect, src, OBJECTS, bulk=True))
    rows = db.execute(
        "SELECT entity, op, entity_version, txid FROM catalog.feed_event"
        " WHERE dataset_id = %s AND event_id > %s",
        (w.dataset_id, before),
    ).fetchall()
    files = [r for r in rows if r[0] == "file"]
    (count,) = db.execute(
        "SELECT count(*) FROM catalog.file WHERE dataset_id = %s AND kind = 'file'"
        " AND origin = 's3_inventory'",
        (w.dataset_id,),
    ).fetchone()
    # One upsert per new folder and file, each at its row's first version.
    assert {(r[1], r[2]) for r in files} == {("upsert", 1)}
    assert (
        len(files)
        == count
        + db.execute(
            "SELECT count(*) FROM catalog.file WHERE dataset_id = %s AND origin = 's3_inventory'"
            " AND kind = 'dir'",
            (w.dataset_id,),
        ).fetchone()[0]
    )
    assert len({r[3] for r in rows}) == 1  # one transaction
    # Nothing is left in the staging table.
    assert db.execute("SELECT count(*) FROM catalog.seed_stage").fetchone()[0] == 0


def test_without_the_trigger_bypass_the_merge_is_already_what_the_triggers_make(
    db, connect
) -> None:
    """The bypass only saves time: with it off, every per-row trigger fires on
    the merged rows and changes nothing but each file's entity_version."""
    with_bypass, without = world(db), world(db)
    src_with, src_without = _prepare(db, with_bypass), _prepare(db, without)
    run(_write(connect, src_with, OBJECTS, bulk=True))
    original = db.execute(
        "SELECT pg_get_functiondef('catalog.seed_bypassed()'::regprocedure)"
    ).fetchone()[0]
    db.execute(
        "CREATE OR REPLACE FUNCTION catalog.seed_bypassed() RETURNS boolean"
        " LANGUAGE sql STABLE AS $$ SELECT false $$"
    )
    try:
        run(_write(connect, src_without, OBJECTS, bulk=True))
    finally:
        db.execute(original)
    a = snapshot(db, with_bypass, entity_version=False)
    b = snapshot(db, without, entity_version=False)
    for table in a:
        assert a[table] == b[table], table
    # The triggers did fire (the file rows were touched again).
    (bumped,) = db.execute(
        "SELECT count(*) FROM catalog.file WHERE dataset_id = %s AND kind = 'file'"
        " AND origin = 's3_inventory' AND entity_version > 1",
        (without.dataset_id,),
    ).fetchone()
    assert bumped == 1212


# --- the bypass is the merge's alone -------------------------------------------------


@pytest.mark.parametrize("app", ["api", "web", "worker"])
def test_an_app_that_sets_the_bypass_variable_still_fires_every_trigger(
    db, retention_db_url, app
) -> None:
    """omnifile.seed_bypass is an ordinary setting anyone can set; outside
    seed_merge an app still runs as itself, so a version it commits on a WORM
    share gets its retention and a held folder's hold, as always."""
    w = world(db)
    src = _prepare(db, w)
    worm = db.execute(
        "SELECT root_dir_id FROM catalog.share WHERE dataset_id = %s", (w.dataset_id,)
    ).fetchone()[0]
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (fid,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state)"
            " VALUES (%s, %s, 'x.segy', 'file', 'resident') RETURNING file_id",
            (src.dataset_id, worm),
        ).fetchone()
    with psycopg.connect(as_app(retention_db_url, app)) as c:
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute("SET omnifile.seed_bypass = 'on'")
        assert c.execute("SELECT catalog.seed_bypassed()").fetchone()[0] is False
        if app == "web":
            # web may not insert versions at all (privileges.py).
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(
                    "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name,"
                    " kind, state, committed_at) VALUES (%s, %s, 1, %s, 'x.segy', 'content',"
                    " 'committed', now())",
                    (fid, src.dataset_id, worm),
                )
            return
        (vid, until) = c.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, committed_at) VALUES (%s, %s, 1, %s, 'x.segy', 'content', 'committed',"
            " now()) RETURNING version_id, retention_until",
            (fid, src.dataset_id, worm),
        ).fetchone()
        c.commit()
    assert until is not None  # file_version_before fired
    (current,) = db.execute(
        "SELECT current_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert current == vid  # file_version_after_commit fired


@pytest.mark.parametrize("app", ["api", "web", "enrol"])
def test_only_the_worker_may_stage_and_merge(db, retention_db_url, app) -> None:
    with psycopg.connect(as_app(retention_db_url, app), autocommit=True) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT count(*) FROM catalog.seed_stage")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "SELECT * FROM catalog.seed_merge(0, 0, 0, '', 0, 0, 0, 0, 's3_inventory', now())"
            )


def test_the_merge_writes_only_under_live_folders_of_the_dataset(db, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A staged row whose parent is a file, a deleted folder or another
    dataset's folder is not written (the seeder never stages one; the merge
    doesn't trust it to)."""
    w, other = world(db), world(db)
    src = _prepare(db, w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (a_file,) = db.execute(
            "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND name = 'taken.segy'",
            (w.dataset_id,),
        ).fetchone()
        gone = _dir(db, w, w.root_id, "gone")
        db.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (gone,))
        (imported,) = db.execute(
            "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND name = 'imported'",
            (w.dataset_id,),
        ).fetchone()
    parents = {"file": a_file, "deleted": gone, "elsewhere": other.root_id, "good": imported}
    (acl,) = db.execute(
        "SELECT acl_id FROM catalog.acl WHERE dataset_id = %s LIMIT 1", (w.dataset_id,)
    ).fetchone() or (None,)
    with psycopg.connect(as_app(retention_db_url, "worker")) as c:
        (stage,) = c.execute("SELECT nextval('catalog.seed_stage_id_seq')").fetchone()
        for i, (name, parent) in enumerate(parents.items()):
            c.execute(
                "INSERT INTO catalog.seed_stage (stage_id, ord, parent_id, name, object_key, size,"
                " mtime_ns, last_modified, logical_tier, storage_class)"
                " VALUES (%s, %s, %s, %s, %s, 1, 0, %s, 'hot', 'STANDARD')",
                (stage, i, parent, f"{name}.segy", f"raw/{name}.segy", T0),
            )
        written = c.execute(
            "SELECT ord FROM catalog.seed_merge(%s, %s, %s, %s, %s, 0, 0, 432, 's3_inventory', %s)",
            (stage, src.dataset_id, src.backend_id, src.container, acl, T0),
        ).fetchall()
        c.commit()
    assert written == [(3,)]  # only under the dataset's own live folder
    assert db.execute(
        "SELECT count(*) FROM catalog.file WHERE name IN ('file.segy', 'deleted.segy',"
        " 'elsewhere.segy')"
    ).fetchone() == (0,)


@pytest.mark.parametrize("fails", [False, True])
def test_after_the_merge_the_rest_of_its_transaction_fires_every_trigger(
    db, retention_db_url, fails
) -> None:  # type: ignore[no-untyped-def]
    """seed_merge turns the bypass on with set_config, which lasts to the end
    of the transaction (fix of 0033 for a non-superuser migrator). It turns it
    off before it returns, or when it fails and the caller carries on from a
    savepoint: an insert later in the same transaction, even as the catalog's
    owner (for whom the check would otherwise be true), fires every trigger."""
    w = world(db)
    src = _prepare(db, w)
    (worm, owner) = db.execute(
        "SELECT s.root_dir_id, pg_get_userbyid(p.proowner) FROM catalog.share s, pg_proc p"
        " WHERE s.dataset_id = %s AND p.oid = 'catalog.seed_merge'::regproc",
        (w.dataset_id,),
    ).fetchone()
    names = ["a.segy", "a.segy"] if fails else ["a.segy", "b.segy"]  # a duplicate fails it
    with psycopg.connect(retention_db_url) as c:  # one transaction, rolled back
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute("SET LOCAL ROLE omnifile_worker")
        (stage,) = c.execute("SELECT nextval('catalog.seed_stage_id_seq')").fetchone()
        for i, name in enumerate(names):
            c.execute(
                "INSERT INTO catalog.seed_stage (stage_id, ord, parent_id, name, object_key, size,"
                " mtime_ns, last_modified, logical_tier, storage_class)"
                " VALUES (%s, %s, %s, %s, %s, 1, 0, %s, 'hot', 'STANDARD')",
                (stage, i, worm, name, f"raw/worm/{name}{i}", T0),
            )
        merge = (
            "SELECT count(*) FROM catalog.seed_merge(%s, %s, %s, %s, NULL, 0, 0, 432,"
            " 's3_inventory', %s)"
        )
        args = (stage, src.dataset_id, src.backend_id, src.container, T0)
        if fails:
            with pytest.raises(psycopg.errors.UniqueViolation):
                with c.transaction():  # a savepoint the caller carries on from
                    c.execute(merge, args)
        else:
            assert c.execute(merge, args).fetchone() == (2,)
        assert c.execute("SELECT current_setting('omnifile.seed_bypass', true)").fetchone()[0] in (
            None,
            "",
            "off",
        )
        c.execute("RESET ROLE")
        c.execute(f'SET LOCAL ROLE "{owner}"')
        assert c.execute("SELECT catalog.seed_bypassed()").fetchone() == (False,)
        (fid,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state)"
            " VALUES (%s, %s, 'later.segy', 'file', 'resident') RETURNING file_id",
            (src.dataset_id, worm),
        ).fetchone()
        (vid, until) = c.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, committed_at) VALUES (%s, %s, 1, %s, 'later.segy', 'content', 'committed',"
            " now()) RETURNING version_id, retention_until",
            (fid, src.dataset_id, worm),
        ).fetchone()
        assert until is not None  # file_version_before fired
        assert c.execute(
            "SELECT current_version_id FROM catalog.file WHERE file_id = %s", (fid,)
        ).fetchone() == (vid,)  # file_version_after_commit fired
        c.rollback()


def test_the_bypass_check_ignores_the_callers_search_path(retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """seed_bypassed() runs under its caller's search_path (review on #35):
    a schema ahead of pg_catalog with its own current_setting and `=` for
    names must not turn it on. Everything in it is schema-qualified."""
    with psycopg.connect(retention_db_url) as c:  # one transaction, rolled back
        c.execute("CREATE SCHEMA wp4_shadow")
        c.execute(
            "CREATE FUNCTION wp4_shadow.current_setting(text, boolean) RETURNS text"
            " LANGUAGE sql AS $$ SELECT 'on'::text $$"
        )
        c.execute(
            "CREATE FUNCTION wp4_shadow.name_eq(name, name) RETURNS boolean"
            " LANGUAGE sql AS $$ SELECT true $$"
        )
        c.execute(
            "CREATE OPERATOR wp4_shadow.= (LEFTARG = name, RIGHTARG = name,"
            " FUNCTION = wp4_shadow.name_eq)"
        )
        c.execute("GRANT USAGE ON SCHEMA wp4_shadow TO omnifile_api")
        c.execute("SET LOCAL ROLE omnifile_api")
        c.execute("SET LOCAL search_path = wp4_shadow, pg_catalog")
        assert c.execute("SELECT wp4_shadow.current_setting('x', true)").fetchone() == ("on",)
        assert c.execute("SELECT catalog.seed_bypassed()").fetchone() == (False,)
        c.rollback()


def test_the_worker_can_stage_but_not_change_the_bypass_or_its_triggers(
    db, retention_db_url
) -> None:
    with psycopg.connect(as_app(retention_db_url, "worker"), autocommit=True) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "CREATE OR REPLACE FUNCTION catalog.seed_bypassed() RETURNS boolean"
                " LANGUAGE sql AS $$ SELECT true $$"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("ALTER TABLE catalog.file_version DISABLE TRIGGER file_version_before_ins")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE catalog.seed_stage SET size = 0")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM catalog.seed_stage")


def test_no_other_catalog_function_inserts_the_rows_the_bypass_covers(db) -> None:
    """The bypass is safe because only seed_merge runs as the owner while
    inserting files, versions or locations. A new owner-run function that
    did would need its own look (migration 0033)."""
    rows = db.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p"
        " WHERE p.pronamespace = 'catalog'::regnamespace"
        "   AND p.prosrc ~* 'insert\\s+into\\s+catalog\\.(file|file_version|location)\\M'"
    ).fetchall()
    assert [r[0] for r in rows] == [
        "catalog.seed_merge(bigint,bigint,bigint,text,bigint,bigint,bigint,integer,text,"
        "timestamp with time zone)"
    ]


def test_the_split_triggers_fire_in_the_old_order(db) -> None:
    """Splitting INSERT from UPDATE triggers kept each table's order (they
    fire alphabetically within a timing)."""

    def order(table: str, timing: str, event: str) -> list[str]:
        bits = {"BEFORE": 2, "INSERT": 4, "DELETE": 8, "UPDATE": 16}
        rows = db.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal"
            " AND (tgtype & 1) = 1 AND ((tgtype & 2) = 2) = %s AND (tgtype & %s) <> 0"
            " ORDER BY tgname",
            (table, timing == "BEFORE", bits[event]),
        ).fetchall()
        return [r[0] for r in rows]

    assert order("catalog.file_version", "BEFORE", "INSERT") == [
        "file_version_before_ins", "file_version_commit_floor_ins",
        "file_version_hold_on_commit_ins",
    ]  # fmt: skip
    assert order("catalog.file_version", "BEFORE", "UPDATE") == [
        "file_version_before_upd", "file_version_commit_floor_upd",
        "file_version_hold_on_commit_upd",
    ]  # fmt: skip
    assert order("catalog.location", "BEFORE", "UPDATE") == [
        "location_guard", "location_source_guard", "location_worm_mark_upd",
    ]  # fmt: skip
