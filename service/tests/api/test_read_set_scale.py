"""Decision 63 at scale: what the folders cost a listing.

Off by default. `OMNIFILE_READ_SET_BENCH=1000000` runs it at that many annexed files,
spread over `OMNIFILE_READ_SET_BENCH_DIRS` NAS folders (default 100), each with an ACL
of its own, as per-user home folders have. `OMNIFILE_READ_SET_BENCH_DEPTH` (default
1) puts them that deep: the first depth - 1 levels are one shared chain of folders, so
a depth of 30 is a deep tree and a wide one at once. The export's root and the
chain let Everyone through; each home folder lets through its owner and GEO. The
catalog mirrors the NAS (a catalog folder per home folder). It prints, best of three:

- for a caller who passes every folder (in GEO): a count of every file through
  `readable_sql` against the captured ACL alone, the share map's count
  as the share map made it before its roll-up, and the walk alone
  (`catalog.passable_dirs`);
- the same for a caller who passes few: one home folder's owner, in no group;
- how long filing the annexed files took, folders and all.

`OMNIFILE_READ_SET_BENCH_TIMEOUT_S` (default 600) stops a query that takes longer.
The results are in `docs/milestones/results/wp14-read-set-scale.md`.
"""

from __future__ import annotations

import os
import time

import pytest

from omnifile_service.catalog.read_set import (
    readable_in_folder_sql,
    readable_page_sql,
    readable_row_sql,
    readable_sql,
)

from .test_annex import (  # noqa: F401 - the fixture
    GEO,
    OWNERS,
    annex,
    arrived,
    events,
)

N = int(os.environ.get("OMNIFILE_READ_SET_BENCH") or 0)
DIRS = int(os.environ.get("OMNIFILE_READ_SET_BENCH_DIRS") or 100)
DEPTH = int(os.environ.get("OMNIFILE_READ_SET_BENCH_DEPTH") or 1)
TIMEOUT_S = int(os.environ.get("OMNIFILE_READ_SET_BENCH_TIMEOUT_S") or 600)
BOB = "S-1-5-21-1-2-3-1101"
HOME0 = "S-1-5-21-9-9-9-1000"  # the owner of h0
EVERYONE = "S-1-1-0"

pytestmark = pytest.mark.skipif(not N, reason="OMNIFILE_READ_SET_BENCH unset")


def _best(sql, q: str, params: dict, runs: int = 3) -> tuple[float | None, int]:
    import psycopg

    best, n = float("inf"), -1
    for _ in range(runs):
        t = time.perf_counter()
        try:
            (n,) = sql.execute(q, params).fetchone()
        except psycopg.errors.QueryCanceled:
            return None, -1
        best = min(best, time.perf_counter() - t)
        if best > 60:
            break  # one run is enough to tell
    return best, n


def _ms(t: float | None) -> str:
    return f"over {TIMEOUT_S} s" if t is None else f"{t * 1000:.0f} ms"


def test_a_listing_through_the_folders_costs_little_more_than_the_acl_alone(
    annex,  # noqa: F811 - the fixture
    sql,
    db_url,
) -> None:
    from web.test_share_map_rollup import _LIVE

    from omnifile_service.web.routes_visual import readable_dirs

    def _readable_files() -> str:
        """The live count the share map made before the roll-up (#88): its files."""
        return readable_dirs() + _LIVE.split("SELECT 'edge'")[0].format(
            readable=readable_sql("f.file_id", walked=True)
        )

    w, annex_id, ds, filer = annex
    sql.execute("INSERT INTO omnifile_test.unread_annex VALUES (%s)", (annex_id,))
    sql.execute(
        "UPDATE catalog.filer SET access_limited = false, access_confirmed_by = 'bench',"
        " access_confirmed_at = now() WHERE filer_id = %s",
        (filer,),
    )
    chain = "/".join(f"p{i}" for i in range(1, DEPTH))  # '' at depth 1
    home = f"{chain}/h" if chain else "h"
    [r] = events(w, annex_id, [arrived(f"{home}0/f0.bin", b"x")])
    tmpl = int(r["file_id"])
    t = time.perf_counter()
    # The export's root and the shared chain let Everyone through; a home folder its
    # owner and GEO.
    sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first, search_sids, search_deny_sids)"
        " SELECT %(ds)s, sha256(convert_to('bench folder ' || i, 'UTF8')), 493, '[]', '{}',"
        "        '{}', true,"
        "        CASE WHEN i < 0 THEN ARRAY[%(all)s]"
        "             ELSE ARRAY['S-1-5-21-9-9-9-' || (1000 + i), %(geo)s] END::catalog.sid[],"
        "        '{}'"
        "   FROM generate_series(-1, %(dirs)s - 1) i",
        {"ds": ds, "dirs": DIRS, "geo": GEO, "all": EVERYONE},
    )
    above = ["", "long-term-storage"] + [
        "long-term-storage/" + "/".join(f"p{i}" for i in range(1, k + 1)) for k in range(1, DEPTH)
    ]
    sql.execute(
        "INSERT INTO catalog.nas_folder_acl (filer_id, path, acl_id, annex_id, observed_at)"
        " SELECT %(filer)s, p.path, a.acl_id, %(annex)s, now()"
        "   FROM (SELECT unnest(%(above)s::text[]) AS path, -1 AS i UNION ALL"
        "         SELECT 'long-term-storage/' || %(home)s || i, i"
        "           FROM generate_series(0, %(dirs)s - 1) i) p"
        "   JOIN catalog.acl a ON a.dataset_id = %(ds)s"
        "    AND a.sha256 = sha256(convert_to('bench folder ' || p.i, 'UTF8'))"
        " ON CONFLICT (filer_id, path) DO UPDATE SET acl_id = EXCLUDED.acl_id",
        {"filer": filer, "annex": annex_id, "dirs": DIRS, "ds": ds, "above": above,
         "home": home},
    )  # fmt: skip
    (top, owners_acl) = sql.execute(
        "SELECT p.parent_id, p.acl_id FROM catalog.file f JOIN catalog.file p"
        " ON p.file_id = f.parent_id WHERE f.file_id = %s",
        (tmpl,),
    ).fetchone()
    # The catalog mirrors the NAS: a folder per home folder, as the annex makes them.
    sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id, uid, gid, mode,"
        " origin)"
        " SELECT %(ds)s, %(top)s, 'h' || i, 'dir', %(acl)s, 0, 0, 488, 'annex'"
        "   FROM generate_series(1, %(dirs)s - 1) i",
        {"ds": ds, "top": top, "acl": owners_acl, "dirs": DIRS},
    )
    sql.execute(
        "INSERT INTO catalog.annex_folder (file_id, annex_id, path)"
        " SELECT f.file_id, %(annex)s, %(home)s || substr(f.name, 2) FROM catalog.file f"
        "  WHERE f.parent_id = %(top)s AND f.kind = 'dir'"
        " ON CONFLICT (file_id) DO NOTHING",
        {"annex": annex_id, "top": top, "home": home},
    )
    sql.execute("SET session_replication_role = replica")
    (base,) = sql.execute("SELECT max(file_id) FROM catalog.file").fetchone()
    sql.execute(
        "INSERT INTO catalog.file (file_id, dataset_id, parent_id, name, kind, size, acl_id,"
        " state, origin) OVERRIDING SYSTEM VALUE"
        " SELECT %(base)s + i, f.dataset_id, d.file_id, 'b' || i, 'file', 1, f.acl_id,"
        "        f.state, f.origin"
        "   FROM catalog.file f, generate_series(1, %(n)s - 1) i"
        "   JOIN catalog.file d ON d.parent_id = %(top)s AND d.name = 'h' || (i %% %(dirs)s)"
        "  WHERE f.file_id = %(t)s",
        {"base": base, "n": N, "t": tmpl, "top": top, "dirs": DIRS},
    )
    sql.execute("SET session_replication_role = origin")
    t_files = time.perf_counter()
    sql.execute(
        "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size,"
        " first_seen, last_seen)"
        " SELECT a.annex_id, %(base)s + i, %(home)s || (i %% %(dirs)s) || '/b' || i || '.bin',"
        "        'n' || i, 1, a.first_seen, a.last_seen"
        "   FROM catalog.annex_file a, generate_series(1, %(n)s - 1) i WHERE a.file_id = %(t)s",
        {"base": base, "n": N, "t": tmpl, "dirs": DIRS, "home": home},
    )
    filed = time.perf_counter() - t_files
    for table in ("file", "annex_file", "annex_dir_gate", "acl", "annex_folder", "nas_folder_acl"):
        sql.execute(f"ANALYZE catalog.{table}")
    made = time.perf_counter() - t
    (root,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL", (ds,)
    ).fetchone()
    # The share map walks from the dataset's root through folders the caller reads.
    sql.execute(
        "UPDATE catalog.file SET acl_id = %s WHERE file_id = %s AND acl_id IS NULL",
        (owners_acl, root),
    )
    sql.execute(f"SET statement_timeout = '{TIMEOUT_S}s'")
    if os.environ.get("OMNIFILE_READ_SET_BENCH_ONLY") == "rollup":
        _rollup(sql, db_url, ds, root, top)
        return
    q = ("SELECT count(*) FROM catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
         " WHERE f.dataset_id = %(ds)s AND f.kind = 'file' AND {}")  # fmt: skip
    print(f"\n{N} annexed files in {DIRS} folders {DEPTH} deep: made in {made:.0f} s,"
          f" of which filing the files and their {DIRS} new folders {filed:.1f} s")  # fmt: skip
    for who, sid, groups in (("passes all", BOB, [GEO, OWNERS]),
                             ("passes few", HOME0, [OWNERS])):  # fmt: skip
        params = {"ds": ds, "sid": sid, "groups": groups, "root": root}
        alone, n0 = _best(sql, q.format(
            "catalog.acl_can_read(acl.read_sids, acl.deny_sids, %(sid)s::text,"
            " %(groups)s::text[], acl.deny_first)"), params)  # fmt: skip
        flat, n1 = _best(sql, q.format(readable_sql("f.file_id")), params)
        smap_q = _readable_files() + " SELECT count(*) FROM files"
        smap, n2 = _best(sql, smap_q, params)
        # The same walk of the catalog with the captured ACL alone: what the share
        # map costs without the NAS's folders.
        for fid in ("r.file_id", "f.file_id"):
            smap_q = smap_q.replace(
                readable_sql(fid, walked=True),
                "coalesce(catalog.acl_can_read(acl.read_sids, acl.deny_sids, %(sid)s::text,"
                " %(groups)s::text[], acl.deny_first), false)",
            )
        smap_alone, _ = _best(sql, smap_q, params)
        walk, nd = _best(
            sql,
            "SELECT (SELECT count(*) FROM jsonb_object_keys("
            "catalog.passable_dirs(%(sid)s::text, %(groups)s::text[])))",
            params,
        )
        per_row = (
            None if None in (flat, alone, walk) else (flat - alone - walk) / N * 1e6
        )  # fmt: skip
        print(f"  {who}: the ACL alone {_ms(alone)}, through the folders {_ms(flat)}"
              f" ({n1} files), the share map {_ms(smap)} ({n2} files; with the captured"
              f" ACL alone {_ms(smap_alone)}), the walk"
              f" {_ms(walk)} ({nd} folders), so the per-row test"
              f" {'?' if per_row is None else f'{per_row:.2f} us'}")  # fmt: skip
        assert n0 == N
    # The review of #88: one file's page (load_scope's chain of ancestors), a browse
    # page of 101 rows in one folder, and a page of 101 of an annex's files across
    # folders, each with the walk (as #88 had them) and with the form its route uses
    # now: the item's chain, the folder's chain once, and the bounded walk.
    from omnifile_service.web.access import SCOPE_CHAIN_SQL

    h0, h1 = (sql.execute(
        "SELECT file_id FROM catalog.file WHERE parent_id = %s AND name = %s", (top, n)
    ).fetchone()[0] for n in ("h0", "h1" if DIRS > 1 else "h0"))  # fmt: skip
    browse = ("SELECT count(*) FROM (SELECT f.file_id FROM catalog.file f JOIN catalog.acl acl"
              " ON acl.acl_id = f.acl_id WHERE f.parent_id = %(d)s AND f.deleted_at IS NULL"
              " AND {} ORDER BY f.name LIMIT 101) s")  # fmt: skip
    across = ("SELECT count(*) FROM (SELECT f.file_id FROM catalog.annex_file af"
              " JOIN catalog.file f ON f.file_id = af.file_id"
              " JOIN catalog.acl acl ON acl.acl_id = f.acl_id WHERE af.annex_id = %(a)s"
              " AND {} ORDER BY af.path LIMIT 101) s")  # fmt: skip
    walk_chain = SCOPE_CHAIN_SQL.replace(readable_row_sql("a.file_id"), readable_sql("a.file_id"))
    one = "SELECT count(*) FILTER (WHERE readable) FROM ({}) s"
    for who, sid, groups in (("passes all", BOB, [GEO, OWNERS]),
                             ("passes few", HOME0, [OWNERS])):  # fmt: skip
        line = []
        cases = (
            ("a file's page", one.format(walk_chain), one.format(SCOPE_CHAIN_SQL), None),
            ("a page of the home folders", browse.format(readable_sql("f.file_id")),
             browse.format(readable_in_folder_sql("f.file_id", "%(d)s::bigint")), top),
            ("a page of h1", browse.format(readable_sql("f.file_id")),
             browse.format(readable_in_folder_sql("f.file_id", "%(d)s::bigint")), h1),
            ("a page of the annex's files", across.format(readable_sql("f.file_id")),
             across.format(readable_page_sql("f.file_id")), None),
        )  # fmt: skip
        for what, before_q, after_q, d in cases:
            params = {"sid": sid, "groups": groups, "d": d, "f": tmpl, "a": annex_id}
            before_t, nb = _best(sql, before_q, params)
            after_t, na = _best(sql, after_q, params)
            assert nb == na, (who, what, nb, na)
            line.append(f"{what}: walk {_ms(before_t)}, now {_ms(after_t)} ({na} rows)")
        print(f"  {who}: " + "; ".join(line))
    if os.environ.get("OMNIFILE_READ_SET_EXPLAIN"):
        params = {"ds": ds, "sid": BOB, "groups": [GEO, OWNERS], "root": root}
        for q in (
            _readable_files() + " SELECT count(*) FROM files",
            "SELECT * FROM catalog.passable_dirs(%(sid)s::text, %(groups)s::text[])",
        ):
            for (line,) in sql.execute("EXPLAIN (ANALYZE, BUFFERS OFF) " + q, params):
                print(line)
        sql.execute("LOAD 'auto_explain'")
        sql.execute("SET auto_explain.log_min_duration = 0")
        sql.execute("SET auto_explain.log_nested_statements = on")
        sql.execute("SET auto_explain.log_analyze = on")
        sql.execute("SET client_min_messages = log")
        sql.execute("SET auto_explain.log_level = notice")
        sql.add_notice_handler(lambda d: print(d.message_primary))
        sql.execute(
            "SELECT count(*) FROM jsonb_object_keys("
            "catalog.passable_dirs(%(sid)s::text, %(groups)s::text[]))",
            params,
        )


def _rollup(sql, url: str, ds: int, root: int, top: int) -> None:
    """The share map from the worker's roll-up (usage/share_map.py, migration 0056):
    its first count, one folder's recount, and the map's query for each caller,
    against the live count it replaced where that finishes."""
    import asyncio
    from datetime import UTC, datetime

    import psycopg
    from web.test_share_map_rollup import _LIVE

    from omnifile_service.usage import share_map
    from omnifile_service.web import routes_visual

    async def refresh_all() -> int:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            total = 0
            while n := await share_map.refresh(conn, datetime.now(UTC)):
                total += n
            return total

    async def recount(folder: int) -> None:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await share_map.recount(conn, [folder])

    sql.execute("SET statement_timeout = 0")
    t = time.perf_counter()
    counted = asyncio.run(refresh_all())
    built = time.perf_counter() - t
    sql.execute("ANALYZE catalog.share_map_group")
    (groups,) = sql.execute("SELECT count(*) FROM catalog.share_map_group").fetchone()
    (one,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE parent_id = %s AND kind = 'dir' LIMIT 1", (top,)
    ).fetchone()
    t = time.perf_counter()
    asyncio.run(recount(one))
    refresh = time.perf_counter() - t
    sql.execute(f"SET statement_timeout = '{TIMEOUT_S}s'")
    whole_q = routes_visual._map_whole()
    readable_q = routes_visual._map_readable()
    live_q = routes_visual.readable_dirs() + _LIVE.format(
        readable=readable_sql("f.file_id", walked=True)
    )
    count = "SELECT sum(files) FILTER (WHERE part = 'edge') FROM ({}) m"
    for who, sid, grp in (("passes all", BOB, [GEO, OWNERS]), ("passes few", HOME0, [OWNERS])):
        params = {"sid": sid, "groups": grp, "root": root, "active": ["requested"]}
        rolled, n_roll = _best(sql, count.format(readable_q), params)
        if N <= 1_000_000:
            live, n_live = _best(sql, count.format(live_q), params)
            assert n_live == n_roll, (who, n_live, n_roll)
            extra = f", the live count {_ms(live)}"
        else:
            extra = ""
        print(f"  {who}: the map {_ms(rolled)} ({n_roll} files){extra}")
    params = {"root": root, "active": ["requested"]}
    whole, n_whole = _best(sql, count.format(whole_q), params)
    print(f"  the whole share (ArchiveAdmin, DataSteward): {_ms(whole)} ({n_whole} files)")
    print(f"  the roll-up: {groups} groups from {counted} folders, first count {built:.1f} s,"
          f" one folder recounted in {refresh * 1000:.0f} ms")  # fmt: skip
    # Folders with a change waiting, counted live for a reader (the review of #91),
    # whose files at their last count decide "counting" (QUEUED_LIVE_FILES); here
    # the cap is lifted, to time the live part itself.
    routes_visual.QUEUED_LIVE_FILES = 10**12
    readable_q = routes_visual._map_readable()
    for k in (10, 100, 200, 2000):
        sql.execute(
            "INSERT INTO catalog.share_map_dirty (folder_id) SELECT file_id FROM catalog.file"
            " WHERE dataset_id = %s AND kind = 'dir' AND parent_id IS NOT NULL"
            " ORDER BY random() LIMIT %s",
            (ds, k),
        )
        (waiting,) = sql.execute(
            "SELECT coalesce(sum(files), 0) FROM catalog.share_map_group WHERE folder_id IN"
            " (SELECT folder_id FROM catalog.share_map_dirty)"
        ).fetchone()
        for who, sid, grp in (("passes all", BOB, [GEO, OWNERS]), ("passes few", HOME0, [OWNERS])):
            params = {"sid": sid, "groups": grp, "root": root, "active": ["requested"]}
            took, n = _best(sql, count.format(readable_q), params)
            print(
                f"  {k} folders waiting ({waiting} files), {who}: the map {_ms(took)} ({n} files)"
            )
        sql.execute("DELETE FROM catalog.share_map_dirty")
