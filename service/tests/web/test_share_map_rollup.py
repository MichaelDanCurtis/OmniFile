"""The share map's roll-up (usage/share_map.py, migration 0056): what marks a folder
for recount, the refresh's batches and cursor, "counting" and "as of", and that
the map's sums equal a live count of the same files on random worlds."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from omnifile_service.catalog.read_set import readable_sql
from omnifile_service.usage import share_map
from omnifile_service.web import routes_visual

from .conftest import _acl, add_dir, add_file, seed

#: The live count the map made before the roll-up (#88), kept as the reference.
_LIVE = """
, files AS MATERIALIZED (
    SELECT f.file_id, f.state, f.size, f.resident_bytes, f.primary_location_id,
           f.content_version_id
      FROM catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id
     WHERE f.parent_id IN (SELECT file_id FROM t) AND f.kind = 'file'
       AND f.deleted_at IS NULL AND {readable}
)
SELECT 'edge' AS part, f.state AS k1, NULL::text AS k2, NULL::bigint AS backend_id,
       count(*) AS files, coalesce(sum(f.size), 0) AS bytes,
       coalesce(sum(f.resident_bytes), 0) AS local_bytes
  FROM files f GROUP BY f.state
UNION ALL
SELECT 'store', l.logical_tier::text, l.storage_class, l.backend_id,
       count(*), coalesce(sum(f.size), 0), 0
  FROM files f JOIN catalog.location l ON l.location_id = f.primary_location_id
 WHERE l.state = 'committed'
 GROUP BY l.logical_tier, l.storage_class, l.backend_id
UNION ALL
SELECT 'recall', r.state, NULL, NULL, count(*), coalesce(sum(f.size), 0), 0
  FROM files f JOIN catalog.recall r
    ON r.file_id = f.file_id AND r.version_id = f.content_version_id
 WHERE r.state = ANY(%(active)s)
 GROUP BY r.state
"""


def _refresh(db_url: str, *, until_empty: bool = True) -> int:
    async def run() -> int:
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            total = 0
            while n := await share_map.refresh(conn, datetime.now(UTC)):
                total += n
                if not until_empty:
                    break
            return total

    return asyncio.run(run())


def _queued(sql) -> set[int]:  # type: ignore[no-untyped-def]
    return {r[0] for r in sql.execute("SELECT folder_id FROM catalog.share_map_dirty")}


def _sums(rows) -> dict:  # type: ignore[no-untyped-def]
    out: dict = {}
    for r in rows:
        if r[0] in ("edge", "store", "recall"):
            key = (r[0], r[1], r[2], r[3])
            a = out.setdefault(key, [0, 0, 0])
            a[0] += int(r[4])
            a[1] += int(r[5])
            a[2] += int(r[6])
    return {k: tuple(v) for k, v in out.items() if v[0]}


def _map(sql, q: str, root: int, sid: str, groups: list[str]) -> dict:  # type: ignore[no-untyped-def]
    rows = sql.execute(q, {"root": root, "sid": sid, "groups": groups,
                           "active": list(routes_visual.RECALL_STEP)}).fetchall()  # fmt: skip
    return _sums(rows)


def _random_world(sql, rng: random.Random):  # type: ignore[no-untyped-def]
    w = seed(sql)
    deny = _acl(sql, w.dataset_id, [w.g_owners, w.g_readers], deny=[f"{w.domain}-1101"])
    acls = [w.acl_open, w.acl_secret, deny]
    folders = [w.root_id]
    for i in range(rng.randint(2, 5)):
        folders.append(add_dir(sql, w, f"d{i}", parent=rng.choice(folders), acl=rng.choice(acls)))
    files = []
    for i in range(rng.randint(5, 14)):
        files.append(add_file(
            sql, w, f"f{i}", parent=rng.choice(folders), acl=rng.choice(acls),
            tier=rng.choice(["hot", "warm", "cold", "archive"]), size=rng.randint(1, 10**6),
            state=rng.choice(["resident", "stub", "landing", "remote"])))  # fmt: skip
    for f in rng.sample(files, k=min(3, len(files))):
        vid = sql.execute(
            "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (f,)
        ).fetchone()[0]
        sql.execute(
            "INSERT INTO catalog.recall (file_id, version_id, state) VALUES (%s, %s, %s)",
            (f, vid, rng.choice(["requested", "rehydrating", "copying", "downloading"])),
        )
    return w, folders, files


def _change(sql, w, rng: random.Random, folders: list[int], files: list[int]) -> None:  # type: ignore[no-untyped-def]
    """A few changes of the kinds that mark folders for recount."""
    for _ in range(rng.randint(1, 5)):
        kind = rng.choice(["move", "state", "tier", "delete", "acl", "rename"])
        f = rng.choice(files)
        if kind == "move":
            sql.execute("UPDATE catalog.file SET parent_id = %s WHERE file_id = %s",
                        (rng.choice(folders), f))  # fmt: skip
        elif kind == "state":
            sql.execute("UPDATE catalog.file SET state = 'stub', resident_bytes = 0"
                        " WHERE file_id = %s", (f,))  # fmt: skip
        elif kind == "tier":
            sql.execute("UPDATE catalog.location SET logical_tier = 'archive',"
                        " storage_class = 'Archive' WHERE file_id = %s", (f,))  # fmt: skip
        elif kind == "delete":
            sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (f,))
        elif kind == "acl":
            sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s",
                        (w.acl_secret, f))  # fmt: skip
        elif len(folders) > 1:
            d = rng.choice(folders[1:])
            sql.execute("UPDATE catalog.file SET name = name || 'x' WHERE file_id = %s", (d,))


def test_the_maps_sums_equal_a_live_count_on_random_worlds(sql, db_url) -> None:  # type: ignore[no-untyped-def]
    """For a reader, an owner, someone in no group, and the whole share: after the
    first count, and again after changes and a recount; and for the three callers,
    after changes with no recount (a reader's map counts a changed folder live)."""
    whole = routes_visual._map_whole()
    readable = routes_visual._map_readable()
    live_whole = routes_visual._ALL_DIRS + _LIVE.format(readable="true")
    live_readable = routes_visual.readable_dirs() + _LIVE.format(
        readable=readable_sql("f.file_id", walked=True)
    )
    for seed_ in range(1, 26):
        rng = random.Random(seed_)
        w, folders, files = _random_world(sql, rng)
        viewers = [(f"{w.domain}-1101", [w.g_readers]), (f"{w.domain}-1102", [w.g_owners]),
                   (f"{w.domain}-1103", [w.g_other])]  # fmt: skip
        for round_ in range(2):
            _refresh(db_url)
            assert _map(sql, whole, w.root_id, "S-1-0-0", []) == _map(
                sql, live_whole, w.root_id, "S-1-0-0", []
            ), (seed_, round_, "whole")
            for sid, groups in viewers:
                got = _map(sql, readable, w.root_id, sid, groups)
                want = _map(sql, live_readable, w.root_id, sid, groups)
                assert got == want, (seed_, round_, sid)
            _change(sql, w, rng, folders, files)
            for sid, groups in viewers:
                got = _map(sql, readable, w.root_id, sid, groups)
                want = _map(sql, live_readable, w.root_id, sid, groups)
                assert got == want, (seed_, round_, sid, "no recount")


def test_a_new_share_says_counting_until_its_first_count(sql, db_url, web) -> None:  # type: ignore[no-untyped-def]
    """Never a total of zero: its folders are queued, and not counted yet."""
    w = seed(sql)
    add_file(sql, w, "a.segy", tier="hot", size=10)
    admin = web.browser(w.user("admin", roles=["ArchiveAdmin"]))
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert (m["basis"], m["edge"], m["computed_at"]) == ("counting", None, None)
    _refresh(db_url)
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["basis"] == "rollup" and m["edge"]["resident"]["files"] == 1


def test_a_folder_from_before_the_roll_up_says_counting_until_the_backfill_counts_it(
    sql, db_url, web
) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    add_file(sql, w, "a.segy", tier="hot", size=10)
    _refresh(db_url)
    d = add_dir(sql, w, "old")
    add_file(sql, w, "b.segy", parent=d, tier="hot", size=20)
    # As if the folder were made before migration 0056, and not counted yet.
    sql.execute("DELETE FROM catalog.share_map_dirty WHERE folder_id = %s", (d,))
    sql.execute(
        "UPDATE catalog.file SET created_at = (SELECT value::timestamptz FROM"
        " catalog.share_map_state WHERE key = 'since') - interval '1 day' WHERE file_id = %s",
        (d,),
    )
    sql.execute("UPDATE catalog.share_map_state SET value = '0' WHERE key = 'backfill'")
    admin = web.browser(w.user("admin", roles=["ArchiveAdmin"]))
    assert admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()["basis"] == "counting"
    _refresh(db_url)
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["basis"] == "rollup" and m["edge"]["resident"]["files"] == 2


def test_the_map_is_as_of_the_oldest_count_with_a_change_waiting(sql, db_url, web) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    d1, d2 = add_dir(sql, w, "one"), add_dir(sql, w, "two")
    add_file(sql, w, "a", parent=d1)
    add_file(sql, w, "b", parent=d2)
    _refresh(db_url)
    admin = web.browser(w.user("admin", roles=["ArchiveAdmin"]))
    sql.execute(
        "UPDATE catalog.share_map_folder SET counted_at = now() - interval '2 hours'"
        " WHERE folder_id = %s",
        (d1,),
    )
    # Nothing waiting: true now, however long ago it was counted.
    now_ = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()["computed_at"]
    assert now_ > (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    # A change waiting in d1: as of d1's count.
    add_file(sql, w, "c", parent=d1)
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["computed_at"] < (datetime.now(UTC) - timedelta(minutes=100)).isoformat()
    assert m["edge"]["resident"]["files"] == 2


def test_each_change_marks_the_folders_it_touches(sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    d1, d2 = add_dir(sql, w, "one"), add_dir(sql, w, "two")
    f = add_file(sql, w, "a", parent=d1)
    sql.execute("DELETE FROM catalog.share_map_dirty")
    # A file inserted, with nothing else: its folder.
    sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size, acl_id)"
        " VALUES (%s, %s, 'new', 'file', 'landing', 1, %s)",
        (w.dataset_id, d2, w.acl_open),
    )
    assert _queued(sql) == {d2}
    sql.execute("DELETE FROM catalog.share_map_dirty")
    # A file moving between folders: both.
    sql.execute("UPDATE catalog.file SET parent_id = %s WHERE file_id = %s", (d2, f))
    assert _queued(sql) == {d1, d2}
    sql.execute("DELETE FROM catalog.share_map_dirty")
    # A tier change on the location, not on catalog.file: the file's folder.
    sql.execute("UPDATE catalog.location SET logical_tier = 'archive', storage_class = 'Archive'"
                " WHERE file_id = %s", (f,))  # fmt: skip
    assert _queued(sql) == {d2}
    sql.execute("DELETE FROM catalog.share_map_dirty")
    # A folder renamed, moved or deleted: itself.
    sql.execute("UPDATE catalog.file SET name = 'renamed' WHERE file_id = %s", (d1,))
    assert _queued(sql) == {d1}
    sql.execute("DELETE FROM catalog.share_map_dirty")
    sql.execute("UPDATE catalog.file SET parent_id = %s WHERE file_id = %s", (d2, d1))
    assert _queued(sql) == {d1}
    sql.execute("DELETE FROM catalog.share_map_dirty")
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (d1,))
    assert _queued(sql) == {d1}
    sql.execute("DELETE FROM catalog.share_map_dirty")
    # What the map doesn't count changes nothing.
    sql.execute("UPDATE catalog.file SET last_read_at = now() WHERE file_id = %s", (f,))
    sql.execute("UPDATE catalog.location SET tier_changed_at = now() WHERE file_id = %s", (f,))
    assert _queued(sql) == set()


def test_a_folder_renamed_or_deleted_shows_at_once(sql, db_url, web) -> None:  # type: ignore[no-untyped-def]
    """The map walks the tree at query time: a folder moved out of the share, or
    deleted, leaves its counts at once, before any recount."""
    w = seed(sql)
    d = add_dir(sql, w, "one")
    a = add_file(sql, w, "a", parent=d)
    add_file(sql, w, "b")
    _refresh(db_url)
    admin = web.browser(w.user("admin", roles=["ArchiveAdmin"]))
    path = "/v1/shares/{share_id}/map"
    assert admin.get(path, share_id=w.share_id).json()["edge"]["resident"]["files"] == 2
    sql.execute("UPDATE catalog.file SET name = 'renamed' WHERE file_id = %s", (d,))
    assert admin.get(path, share_id=w.share_id).json()["edge"]["resident"]["files"] == 2
    # A folder is deleted once its files are; before any recount, it is gone from the map.
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (a,))
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (d,))
    assert d in _queued(sql)
    assert admin.get(path, share_id=w.share_id).json()["edge"]["resident"]["files"] == 1


def test_the_refresh_is_bounded_and_resumes(sql, db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    folders = [add_dir(sql, w, f"d{i}") for i in range(7)]
    for d in folders:
        add_file(sql, w, f"f{d}", parent=d)
    _refresh(db_url)  # the backfill and everything queued so far
    for d in folders:
        add_file(sql, w, f"g{d}", parent=d)
    monkeypatch.setattr(share_map, "BATCH", 2)
    monkeypatch.setattr(share_map, "MAX_BATCHES", 2)
    assert _refresh(db_url, until_empty=False) == 4, "two batches of two, then it stops"
    assert len(_queued(sql) & set(folders)) == 3
    assert _refresh(db_url, until_empty=False) == 3, "the next run takes the rest"
    assert _queued(sql) == set()


def test_a_change_committed_during_a_recount_stays_queued(sql, db_url) -> None:  # type: ignore[no-untyped-def]
    """The recount's snapshot didn't see it, so its entry isn't taken off."""
    w = seed(sql)
    d = add_dir(sql, w, "one")
    add_file(sql, w, "a", parent=d)
    _refresh(db_url)

    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:

            async def during() -> None:
                add_file(sql, w, "late", parent=d)  # another connection, committed now

            await share_map.recount(conn, [d], before_take=during)

    add_file(sql, w, "b", parent=d)
    asyncio.run(run())
    assert d in _queued(sql), "the late file's entry"
    (files,) = sql.execute(
        "SELECT sum(files) FROM catalog.share_map_group WHERE folder_id = %s", (d,)
    ).fetchone()
    assert files == 2, "a and b, not late"
    _refresh(db_url)
    (files,) = sql.execute(
        "SELECT sum(files) FROM catalog.share_map_group WHERE folder_id = %s", (d,)
    ).fetchone()
    assert files == 3


def test_the_roll_ups_age(sql, db_url) -> None:  # type: ignore[no-untyped-def]
    seed(sql)
    _refresh(db_url)

    async def age() -> float:
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            return await share_map.age_s(conn, datetime.now(UTC))

    assert asyncio.run(age()) == pytest.approx(0, abs=1)
    sql.execute("INSERT INTO catalog.share_map_dirty (folder_id, at)"
                " VALUES (1, now() - interval '3 hours')")  # fmt: skip
    assert asyncio.run(age()) == pytest.approx(3 * 3600, abs=60)


def _files(m: dict) -> int:
    return sum(e["files"] for e in (m["edge"] or {}).values())


def test_a_files_acl_narrowed_leaves_a_readers_map_at_once(sql, db_url, web) -> None:  # type: ignore[no-untyped-def]
    """No recount needed: the folder has a change waiting, so it is counted live."""
    w = seed(sql)
    d = add_dir(sql, w, "one")
    f = add_file(sql, w, "a", parent=d, tier="hot", size=10)
    add_file(sql, w, "b", parent=d, tier="hot", size=20)
    _refresh(db_url)
    reader = web.browser(w.reader)
    path = "/v1/shares/{share_id}/map"
    assert _files(reader.get(path, share_id=w.share_id).json()) == 2
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (w.acl_secret, f))
    m = reader.get(path, share_id=w.share_id).json()
    assert (m["basis"], _files(m)) == ("rollup", 1)
    assert m["computed_at"] > (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    # The worker down for good changes nothing.
    assert _files(reader.get(path, share_id=w.share_id).json()) == 1
    _refresh(db_url)
    assert _files(reader.get(path, share_id=w.share_id).json()) == 1


def test_a_file_moved_into_a_closed_folder_leaves_a_readers_map_at_once(sql, db_url, web) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    open_ = add_dir(sql, w, "open")
    closed = add_dir(sql, w, "closed", acl=w.acl_secret)
    f = add_file(sql, w, "a", parent=open_, tier="hot", size=10)
    _refresh(db_url)
    reader = web.browser(w.reader)
    path = "/v1/shares/{share_id}/map"
    assert _files(reader.get(path, share_id=w.share_id).json()) == 1
    sql.execute("UPDATE catalog.file SET parent_id = %s WHERE file_id = %s", (closed, f))
    m = reader.get(path, share_id=w.share_id).json()
    assert (m["basis"], _files(m)) == ("rollup", 0)
    owner = web.browser(w.user("owner", groups=[w.g_owners]))
    assert _files(owner.get(path, share_id=w.share_id).json()) == 1


def test_too_many_folders_waiting_make_a_readers_map_say_counting(
    sql, db_url, web, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Past QUEUED_LIVE_FILES files in folders with a change waiting, "counting"
    rather than a stale total; the whole share's map sums the roll-up as before."""
    w = seed(sql)
    d1, d2 = add_dir(sql, w, "one"), add_dir(sql, w, "two")
    a = add_file(sql, w, "a", parent=d1)
    b = add_file(sql, w, "b", parent=d2)
    _refresh(db_url)
    monkeypatch.setattr(routes_visual, "QUEUED_LIVE_FILES", 1)
    reader = web.browser(w.reader)
    admin = web.browser(w.user("admin", roles=["ArchiveAdmin"]))
    path = "/v1/shares/{share_id}/map"
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (w.acl_secret, a))
    assert _files(reader.get(path, share_id=w.share_id).json()) == 1, "one waiting: live"
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (w.acl_secret, b))
    m = reader.get(path, share_id=w.share_id).json()
    assert (m["basis"], m["edge"]) == ("counting", None)
    assert admin.get(path, share_id=w.share_id).json()["basis"] == "rollup"
    _refresh(db_url)
    m = reader.get(path, share_id=w.share_id).json()
    assert (m["basis"], _files(m)) == ("rollup", 0)


def test_a_batch_reads_the_oldest_entries_not_the_whole_queue(sql, db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    _refresh(db_url)
    d = add_dir(sql, w, "one")
    sql.execute("INSERT INTO catalog.share_map_dirty (folder_id)"
                " SELECT %s FROM generate_series(1, 5000)", (d,))  # fmt: skip
    sql.execute("ANALYZE catalog.share_map_dirty")
    (plan,) = sql.execute(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + share_map._NEXT,
        {"skip": [], "scan": 20, "batch": 10},
    ).fetchone()

    def read(node: dict) -> int:
        own = node["Actual Rows"] if node.get("Relation Name") == "share_map_dirty" else 0
        return own + sum(read(c) for c in node.get("Plans", []))

    assert read(plan[0]["Plan"]) <= 20, "at most SCAN entries, by seq"


def test_a_recount_that_meets_a_concurrent_change_moves_on_to_other_folders(
    sql, db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    _refresh(db_url)
    d1, d2 = add_dir(sql, w, "one"), add_dir(sql, w, "two")
    add_file(sql, w, "a", parent=d1)
    add_file(sql, w, "b", parent=d2)
    sql.execute("DELETE FROM catalog.share_map_dirty WHERE folder_id NOT IN (%s, %s)", (d1, d2))
    seen: list[list[int]] = []
    real = share_map.recount

    async def recount(conn, folders, **kw):  # type: ignore[no-untyped-def]
        seen.append(sorted(folders))
        if d1 in folders:
            raise psycopg.errors.SerializationFailure("a concurrent change")
        await real(conn, folders, **kw)

    monkeypatch.setattr(share_map, "recount", recount)
    monkeypatch.setattr(share_map, "BATCH", 1)
    assert _refresh(db_url, until_empty=False) == 1
    assert seen.count([d1]) == 1, "not retried in the same run"
    assert [d2] in seen
    assert _queued(sql) == {d1}


def test_the_cap_counts_the_files_of_the_folders_waiting_not_the_folders(
    sql, db_url, web, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """One folder of three files, waiting, is past a cap of two (at 10^7 files,
    2,000 folders held 200,000 files and took 9 s)."""
    w = seed(sql)
    d = add_dir(sql, w, "one")
    a = add_file(sql, w, "a", parent=d)
    add_file(sql, w, "b", parent=d)
    add_file(sql, w, "c", parent=d)
    _refresh(db_url)
    monkeypatch.setattr(routes_visual, "QUEUED_LIVE_FILES", 2)
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (w.acl_secret, a))
    reader = web.browser(w.reader)
    m = reader.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert (m["basis"], m["edge"]) == ("counting", None)
