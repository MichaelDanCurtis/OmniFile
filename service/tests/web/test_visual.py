"""The visual views' routes: file journey, share map and its file lists
(portal-api.md section 5.22)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from omnifile_service.api.validate import rfc3339
from omnifile_service.usage import share_map
from omnifile_service.web import routes_visual
from omnifile_service.web.app import ROUTES

from .conftest import _acl, add_dir, add_file, seed


def _identity(sql, user) -> int:  # type: ignore[no-untyped-def]
    return sql.execute(
        "SELECT identity_id FROM catalog.identity WHERE sid = %s", (user["sid"],)
    ).fetchone()[0]


def _recall(sql, fid: int, state: str) -> None:  # type: ignore[no-untyped-def]
    vid = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()[0]
    sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, state) VALUES (%s, %s, %s)",
        (fid, vid, state),
    )


def _s3_source(sql, w):  # type: ignore[no-untyped-def]
    """An S3 migration source for the world's dataset, now `migrating`."""
    (s3,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, true) RETURNING backend_id",
        (f"s3-{w.dataset_id}", Jsonb({"bucket": "acme-seismic", "region": "us-east-1"})),
    ).fetchone()
    sql.execute(
        "UPDATE catalog.dataset SET source_backend_id = %s, migration_state = 'migrating'"
        " WHERE dataset_id = %s",
        (s3, w.dataset_id),
    )
    return s3


def _move_to(sql, fid: int, backend: int, tier: str, native: str) -> None:  # type: ignore[no-untyped-def]
    sql.execute(
        "UPDATE catalog.location SET backend_id = %s, container = 'acme-seismic',"
        " logical_tier = %s, storage_class = %s WHERE file_id = %s",
        (backend, tier, native, fid),
    )
    sql.execute("UPDATE catalog.file SET state = 'remote', resident_bytes = 0 WHERE file_id = %s",
                (fid,))  # fmt: skip


# --- journey ---------------------------------------------------------------------


def test_journey_gives_the_backends_ladder_and_who_set_what(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    survey = add_dir(sql, w, "survey")
    seg = add_file(sql, w, "line.segy", parent=survey, tier="archive", state="stub", size=10**9)
    reader, owner = web.browser(w.reader), web.browser(w.owner)
    until = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    r = owner.post("/v1/pins", {"items": [str(survey)], "tier": "cold", "until": until,
                                "reason": "project paused"})  # fmt: skip
    assert r.status_code == 201 and r.json()["pins"], r.text
    sql.execute(
        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id, until)"
        " VALUES (%s, %s, 'keep_online', 'Kept online from Teams', %s, now() + interval '14 days')",
        (w.dataset_id, seg, _identity(sql, w.reader)),
    )

    r = reader.get("/v1/files/{file_id}/journey", file_id=seg)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["backend"] == {"kind": "sim", "role": "home", "storage_layout": "two_account",
                            "account": "archive", "migration_state": "steady"}  # fmt: skip
    ladder = {t["logical"]: t["classes"][0] for t in j["tiers"]}
    assert [t["logical"] for t in j["tiers"]] == ["hot", "warm", "cold", "archive", "smart"]
    assert ladder["warm"] == {"native_class": "Cool", "minimum_days": 30, "needs_restore": False}
    assert ladder["archive"]["needs_restore"] and ladder["archive"]["minimum_days"] == 180
    # A reader sees the pin and when, not who or why; they see their own hold's setter.
    pin = j["pin"]
    assert pin["tier"] == "cold" and pin["inherited_from"] == str(survey)
    assert pin["set_by"] is None and pin["reason"] is None and pin["set_at"]
    [hold] = j["holds"]
    assert hold["kind"] == "keep_online" and hold["until"] and hold["reason"] is None
    assert hold["set_by"]["display_name"] == "Reader"

    # The share's Owner (who may list pins and holds) sees who and why.
    j = owner.get("/v1/files/{file_id}/journey", file_id=seg).json()
    assert j["pin"]["set_by"]["display_name"] == "Owner"
    assert j["pin"]["reason"] == "project paused"
    assert j["holds"][0]["reason"] == "Kept online from Teams"
    assert j["holds"][0]["set_by"]["display_name"] == "Reader"


def test_journey_is_404_for_an_unreadable_file(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    secret = add_file(sql, w, "secret.segy", acl=w.acl_secret)
    for user in (w.reader, w.admin):  # roles never grant read
        r = web.browser(user).get("/v1/files/{file_id}/journey", file_id=secret)
        assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = web.browser(w.owner).get("/v1/files/{file_id}/journey", file_id=secret)
    assert r.status_code == 200


def test_journey_of_a_file_still_in_s3_uses_s3s_classes(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    s3 = _s3_source(sql, w)
    fid = add_file(sql, w, "raw.segy", tier="archive")
    _move_to(sql, fid, s3, "archive", "GLACIER")
    j = web.browser(w.reader).get("/v1/files/{file_id}/journey", file_id=fid).json()
    assert j["backend"]["kind"] == "s3" and j["backend"]["role"] == "source"
    assert j["backend"]["account"] is None and j["backend"]["migration_state"] == "migrating"
    classes = {t["logical"]: [c["native_class"] for c in t["classes"]] for t in j["tiers"]}
    assert classes["warm"] == ["STANDARD_IA"] and classes["cold"] == ["GLACIER_IR"]
    assert classes["archive"] == ["GLACIER", "DEEP_ARCHIVE"]
    assert j["pin"] is None and j["holds"] == []


# --- share map -----------------------------------------------------------------------


def _count(db_url: str) -> int:
    """The worker's share map roll-up (usage/share_map.py), run until nothing is
    queued, as its one-minute sweep would. Returns the folders recounted."""

    async def run() -> int:
        # Autocommit, as the worker's sweeps run: the refresh opens its own.
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            total = 0
            while n := await share_map.refresh(conn, datetime.now(UTC)):
                total += n
            return total

    return asyncio.run(run())


def _map_world(sql):  # type: ignore[no-untyped-def]
    w = seed(sql)
    d = add_dir(sql, w, "survey")
    sub = add_dir(sql, w, "deep", parent=d)
    add_file(sql, w, "hot.segy", parent=d, tier="hot", size=100)
    add_file(sql, w, "cool.segy", parent=sub, tier="warm", size=200)
    arch = add_file(sql, w, "arch.segy", parent=sub, tier="archive", size=1000, state="stub")
    sql.execute("UPDATE catalog.file SET resident_bytes = 10 WHERE file_id = %s", (arch,))
    add_file(sql, w, "secret.segy", acl=w.acl_secret, tier="archive", size=5000, state="stub")
    # Still landing on the edge: no cloud copy yet.
    sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size, acl_id)"
        " VALUES (%s, %s, 'new.segy', 'file', 'landing', 7, %s)",
        (w.dataset_id, w.root_id, w.acl_open),
    )
    _recall(sql, arch, "copying")
    _recall(sql, w.files["secret.segy"], "requested")
    return w


def _acl_world(sql, web):  # type: ignore[no-untyped-def]
    """Realistic ACLs (M7 review of WP13): a restricted folder holding an
    open-ACL file, a deny entry, a pin with a reason on the restricted folder,
    and a nested folder share."""
    w = _map_world(sql)
    restricted = add_dir(sql, w, "restricted", acl=w.acl_secret)
    add_file(sql, w, "inner.segy", parent=restricted, tier="hot", size=30000)
    deny = _acl(sql, w.dataset_id, [w.g_owners, w.g_readers], deny=[w.reader["sid"]])
    add_file(sql, w, "denied.segy", acl=deny, tier="warm", size=400000)
    owner = web.browser(w.owner)
    r = owner.post("/v1/pins", {"items": [str(restricted)], "tier": "cold",
                                "reason": "secret project codename"})  # fmt: skip
    assert r.status_code == 201 and r.json()["pins"], r.text
    (nested,) = sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, nfs_sec,"
        " owners_group_sid, readers_group_sid, on_open, root_dir_id)"
        " VALUES (%s, %s, %s, ARRAY['nfs4'], ARRAY['krb5'], %s, %s, 'wait', %s)"
        " RETURNING share_id",
        (w.dataset_id, w.edge_id, f"{w.share_name}-survey", w.g_owners, w.g_readers,
         w.files["survey"]),
    ).fetchone()  # fmt: skip
    w.files["nested-share"] = nested
    return w


def test_map_gives_admins_and_stewards_the_workers_whole_share_count(web, sql, db_url) -> None:  # type: ignore[no-untyped-def]
    w = _map_world(sql)
    admin = web.browser(w.admin)
    # Before the worker's first count: counting, never a total of zero; recalls are live.
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert (m["basis"], m["edge"], m["stores"], m["computed_at"]) == ("counting", None, [], None)
    assert m["recalls"]["open"] == 2
    _count(db_url)
    for user in (w.admin, w.steward):  # owner, 2026-11-04: DataStewards too, counts only
        r = web.browser(user).get("/v1/shares/{share_id}/map", share_id=w.share_id)
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["scope"] == "all" and m["basis"] == "rollup" and m["computed_at"]
        assert m["home"] == {"backend_kind": "sim", "storage_layout": "two_account"}
        assert m["source"] is None and m["migration_state"] == "steady"
        assert m["edge"]["resident"] == {"files": 2, "bytes": 300, "local_bytes": 300}
        assert m["edge"]["stub"] == {"files": 2, "bytes": 6000, "local_bytes": 10}
        assert m["edge"]["arriving"] == {"files": 1, "bytes": 7, "local_bytes": 0}
        assert m["edge"]["remote"] == {"files": 0, "bytes": 0, "local_bytes": 0}
        [home] = m["stores"]
        assert home["role"] == "home" and home["backend_kind"] == "sim"
        assert [(t["logical_tier"], t["account"], t["files"], t["bytes"])
                for t in home["tiers"]] == [
            ("hot", "live", 1, 100), ("warm", "live", 1, 200), ("archive", "archive", 2, 6000)
        ]  # fmt: skip
        assert m["recalls"] == {"open": 2, "by_step": {"requested": 1, "rehydrating": 1,
                                                        "downloading": 0}}  # fmt: skip
    assert admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()["migrations"] == []
    steward = web.browser(w.steward)
    assert (
        steward.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()["migrations"] is None
    )
    # A page view reads the roll-up. A new file waits for the next count, and the map
    # says as of when its folder was last counted.
    current = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()["computed_at"]
    (counted_at,) = sql.execute(
        "SELECT counted_at FROM catalog.share_map_folder WHERE folder_id = %s", (w.root_id,)
    ).fetchone()
    add_file(sql, w, "later.segy", tier="hot", size=1)
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["edge"]["resident"]["files"] == 2
    assert m["computed_at"] == rfc3339(counted_at) and m["computed_at"] < current
    _count(db_url)
    m = admin.get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["edge"]["resident"]["files"] == 3 and m["computed_at"] > current


def test_map_counts_only_what_a_reader_can_read(web, sql, db_url) -> None:  # type: ignore[no-untyped-def]
    w = _map_world(sql)
    _count(db_url)
    m = web.browser(w.reader).get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["scope"] == "readable" and m["basis"] == "rollup"
    assert m["edge"]["stub"] == {"files": 1, "bytes": 1000, "local_bytes": 10}
    arch = [t for t in m["stores"][0]["tiers"] if t["logical_tier"] == "archive"]
    assert arch[0]["files"] == 1 and arch[0]["bytes"] == 1000
    assert m["recalls"]["open"] == 1 and m["recalls"]["by_step"]["requested"] == 0
    assert m["migrations"] is None
    m = web.browser(w.owner).get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["scope"] == "readable" and m["edge"]["stub"]["files"] == 2


def test_readable_counts_skip_unreadable_folders_and_denied_files(web, sql, db_url) -> None:  # type: ignore[no-untyped-def]
    w = _acl_world(sql, web)
    _count(db_url)
    path = "/v1/shares/{share_id}/map"
    # inner.segy's own ACL is open, but it sits in a folder the reader can't
    # read (section 3.2); denied.segy denies the reader by SID.
    m = web.browser(w.reader).get(path, share_id=w.share_id).json()
    assert m["edge"]["resident"] == {"files": 2, "bytes": 300, "local_bytes": 300}
    hot = [t for t in m["stores"][0]["tiers"] if t["logical_tier"] == "hot"]
    assert hot[0]["files"] == 1 and hot[0]["bytes"] == 100
    # The owner reads all of it.
    m = web.browser(w.owner).get(path, share_id=w.share_id).json()
    assert m["edge"]["resident"]["files"] == 4
    # The whole-share count has everything, and a nested share counts its own subtree.
    m = web.browser(w.admin).get(path, share_id=w.share_id).json()
    assert m["edge"]["resident"]["files"] == 4 and m["edge"]["stub"]["files"] == 2
    m = web.browser(w.admin).get(path, share_id=w.files["nested-share"]).json()
    assert m["edge"]["resident"]["files"] == 2 and m["edge"]["stub"]["files"] == 1
    assert m["recalls"]["open"] == 1
    m = web.browser(w.reader).get(path, share_id=w.files["nested-share"]).json()
    assert (m["scope"], m["edge"]["resident"]["files"]) == ("readable", 2)


def test_the_live_count_puts_the_previous_statement_timeout_back(db_url) -> None:  # type: ignore[no-untyped-def]
    async def run() -> tuple[str, str]:
        async with await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '7s'")
                await routes_visual._with_timeout(conn, "SELECT 1 AS one", {})
                ok = (await (await conn.execute("SHOW statement_timeout")).fetchone())[
                    "statement_timeout"
                ]
                await routes_visual._with_timeout(conn, "SELECT pg_sleep(1)", {})
                timed_out = (await (await conn.execute("SHOW statement_timeout")).fetchone())[
                    "statement_timeout"
                ]
                return ok, timed_out

    old = routes_visual.LIVE_TIMEOUT_MS
    routes_visual.LIVE_TIMEOUT_MS = 50
    try:
        assert asyncio.run(run()) == ("7s", "7s")
    finally:
        routes_visual.LIVE_TIMEOUT_MS = old


def test_a_readable_count_that_runs_out_of_time_shows_no_totals(web, sql, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    w = _map_world(sql)
    monkeypatch.setattr(routes_visual, "LIVE_TIMEOUT_MS", 50)
    monkeypatch.setattr(
        routes_visual,
        "_GROUP_PARTS",
        routes_visual._GROUP_PARTS
        + " UNION ALL SELECT 'edge', 'resident', NULL, NULL, 0, 0, 0, NULL FROM pg_sleep(1)",
    )
    reader = web.browser(w.reader)
    r = reader.get("/v1/shares/{share_id}/map", share_id=w.share_id)
    assert r.status_code == 200, r.text
    m = r.json()
    assert (m["basis"], m["edge"], m["stores"], m["recalls"]) == ("none", None, [], None)
    # The connection is fine afterwards.
    assert reader.get("/v1/shares/{share_id}", share_id=w.share_id).status_code == 200


def test_the_map_routes_are_on_the_preview_limiter() -> None:
    opts = {r.path: r.opts for r in ROUTES}
    assert opts["/v1/shares/{share_id}/map"].get("preview") is True
    assert opts["/v1/shares/{share_id}/map/files"].get("preview") is True


def test_map_is_404_for_someone_who_cant_see_the_share(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = _map_world(sql)
    out = web.browser(w.outsider)
    assert out.get("/v1/shares/{share_id}/map", share_id=w.share_id).status_code == 404
    r = out.get("/v1/shares/{share_id}/map/files", share_id=w.share_id, params={"edge": "stub"})
    assert r.status_code == 404


def test_map_shows_the_s3_source_and_open_migrations(web, sql, db_url) -> None:  # type: ignore[no-untyped-def]
    w = _map_world(sql)
    s3 = _s3_source(sql, w)
    raw = add_file(sql, w, "raw.segy", tier="archive", size=40)
    _move_to(sql, raw, s3, "archive", "GLACIER")
    (src,) = sql.execute(
        "INSERT INTO catalog.source (dataset_id, backend_id, container, owners_sid)"
        " VALUES (%s, %s, 'acme-seismic', %s) RETURNING source_id",
        (w.dataset_id, s3, w.g_owners),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.migration_prefix (dataset_id, prefix, state, files_total,"
        " files_verified, source_id, to_backend_id, to_container, target_root)"
        " VALUES (%s, 'raw/', 'copying', 10, 3, %s, %s, 'live/data', 'migrated/')",
        (w.dataset_id, src, w.backend_id),
    )
    _count(db_url)
    m = web.browser(w.admin).get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["source"] == {"backend_kind": "s3"} and m["migration_state"] == "migrating"
    assert [s["role"] for s in m["stores"]] == ["home", "source"]
    assert m["stores"][1]["tiers"] == [{"logical_tier": "archive", "native_class": "GLACIER",
                                        "account": None, "files": 1, "bytes": 40}]  # fmt: skip
    assert m["edge"]["remote"]["files"] == 1
    [mig] = m["migrations"]
    assert mig["state"] == "copying" and mig["files"] == {"total": 10, "verified": 3}
    # Not for a reader: GET /v1/migrations is ArchiveAdmin-only.
    m = web.browser(w.reader).get("/v1/shares/{share_id}/map", share_id=w.share_id).json()
    assert m["migrations"] is None and m["stores"][1]["tiers"][0]["files"] == 1


# --- a node's files ------------------------------------------------------------------


def test_map_files_lists_only_readable_files_for_every_role(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = _map_world(sql)
    path = "/v1/shares/{share_id}/map/files"
    names = lambda r: [i["name"] for i in r.json()["items"]]  # noqa: E731
    reader = web.browser(w.reader)
    r = reader.get(path, share_id=w.share_id, params={"edge": "stub"})
    assert r.status_code == 200, r.text
    assert names(r) == ["arch.segy"]
    item = r.json()["items"][0]
    assert item["tier"]["logical"] == "archive" and item["tier"]["archived"] is True
    assert item["resident_bytes"] == 10 and item["parent_id"] == str(w.files["deep"])
    assert names(reader.get(path, share_id=w.share_id, params={"tier": "archive"})) == ["arch.segy"]
    assert names(reader.get(path, share_id=w.share_id,
                            params={"tier": "warm", "role": "home"})) == ["cool.segy"]  # fmt: skip
    assert names(reader.get(path, share_id=w.share_id, params={"recalling": "true"})) == [
        "arch.segy"
    ]
    assert reader.get(path, share_id=w.share_id,
                      params={"tier": "hot", "role": "source"}).json()["items"] == []  # fmt: skip
    # The owner reads secret.segy; an ArchiveAdmin or a DataSteward without the group
    # sees no names it couldn't browse to.
    owner = web.browser(w.owner)
    assert names(owner.get(path, share_id=w.share_id, params={"tier": "archive"})) == [
        "arch.segy",
        "secret.segy",
    ]
    admin = web.browser(w.admin)
    assert admin.get(path, share_id=w.share_id, params={"tier": "archive"}).json()["items"] == []
    steward = web.browser(w.steward)
    assert names(steward.get(path, share_id=w.share_id, params={"tier": "archive"})) == [
        "arch.segy"
    ]


def test_map_files_never_names_a_file_in_an_unreadable_folder(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = _acl_world(sql, web)
    path = "/v1/shares/{share_id}/map/files"
    names = lambda r: [i["name"] for i in r.json()["items"]]  # noqa: E731
    reader = web.browser(w.reader)
    # The folder is 404 to the reader, so the files in it are not listed either.
    r = reader.get("/v1/dirs/{dir_id}/children", dir_id=w.files["restricted"])
    assert r.status_code == 404
    assert names(reader.get(path, share_id=w.share_id, params={"tier": "hot"})) == ["hot.segy"]
    assert names(reader.get(path, share_id=w.share_id, params={"edge": "resident"})) == [
        "hot.segy", "cool.segy"
    ]  # fmt: skip
    # A deny entry wins over the readers group.
    assert names(reader.get(path, share_id=w.share_id, params={"tier": "warm"})) == ["cool.segy"]
    steward = web.browser(w.steward)  # in the readers group, not denied
    assert names(steward.get(path, share_id=w.share_id, params={"tier": "warm"})) == [
        "cool.segy", "denied.segy"
    ]  # fmt: skip
    assert "inner.segy" not in names(steward.get(path, share_id=w.share_id, params={"tier": "hot"}))
    # Every listed file's folder is one the caller can open.
    owner = web.browser(w.owner)
    items = owner.get(path, share_id=w.share_id, params={"tier": "hot"}).json()["items"]
    inner = next(i for i in items if i["name"] == "inner.segy")
    assert inner["parent_id"] == str(w.files["restricted"])
    # The nested share lists its own subtree.
    nested = names(reader.get(path, share_id=w.files["nested-share"], params={"edge": "resident"}))
    assert nested == ["hot.segy", "cool.segy"]


def test_journey_hides_who_and_why_on_a_folder_the_caller_cant_read(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = _acl_world(sql, web)
    inner = w.files["inner.segy"]
    # inner.segy itself is readable to the DataSteward (its own ACL), the folder above isn't.
    j = web.browser(w.steward).get("/v1/files/{file_id}/journey", file_id=inner).json()
    pin = j["pin"]
    assert pin["tier"] == "cold" and pin["inherited_from"] == str(w.files["restricted"])
    assert pin["set_by"] is None and pin["reason"] is None
    # The owner can read the folder: who and why.
    j = web.browser(w.owner).get("/v1/files/{file_id}/journey", file_id=inner).json()
    assert j["pin"]["reason"] == "secret project codename"
    assert j["pin"]["set_by"]["display_name"] == "Owner"


def test_map_files_pages_and_checks_its_node(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    for i in range(5):
        add_file(sql, w, f"f{i}.dat", tier="hot")
    reader = web.browser(w.reader)
    path = "/v1/shares/{share_id}/map/files"
    r = reader.get(path, share_id=w.share_id, params={"tier": "hot", "limit": "2"})
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["f0.dat", "f1.dat"] and body["next_cursor"]
    seen = [i["name"] for i in body["items"]]
    while body["next_cursor"]:
        more = {"tier": "hot", "limit": "2", "cursor": body["next_cursor"]}
        body = reader.get(path, share_id=w.share_id, params=more).json()
        seen += [i["name"] for i in body["items"]]
    assert seen == [f"f{i}.dat" for i in range(5)]
    for bad in ({}, {"edge": "nowhere"}, {"tier": "tepid"}, {"role": "home"},
                {"tier": "hot", "role": "elsewhere"}, {"recalling": "yes"},
                {"tier": "hot", "limit": "201"}):  # fmt: skip
        r = reader.get(path, share_id=w.share_id, params=bad)
        assert r.status_code == 400 and r.json()["code"] == "invalid_request", (bad, r.text)
