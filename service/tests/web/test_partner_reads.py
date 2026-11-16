"""Partner reads (portal-api.md section 5.11): files by path, restores."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import psycopg

from omnifile_service.catalog.privileges import as_app
from omnifile_service.usage import snapshot

from .conftest import add_dir, add_file, seed


def _blake(sql, fid: int, digest: bytes) -> None:  # type: ignore[no-untyped-def]
    sql.execute("UPDATE catalog.file SET blake3 = %s WHERE file_id = %s", (digest, fid))


def test_files_by_path(web, sql):
    w = seed(sql)
    folder = add_dir(sql, w, "seismic")
    a = add_file(sql, w, "a.segy", parent=folder, tier="archive", state="stub", size=10)
    b = add_file(sql, w, "b.segy", parent=folder, size=10)
    add_file(sql, w, "c.segy", parent=folder, size=10)
    elsewhere = add_file(sql, w, "copy.segy", size=10)
    for fid in (a, elsewhere):
        _blake(sql, fid, b"\x11" * 32)
    sql.execute(
        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
        " VALUES (%s, %s, 'legal', 'case 7', 'test-app')",
        (w.dataset_id, folder),
    )
    sql.execute("UPDATE catalog.file SET archive_at = '2027-04-01' WHERE file_id = %s", (b,))
    read = web.app_token(["Archive.Read"])

    def get(path: str, **params):  # type: ignore[no-untyped-def]
        q = {"dataset_id": str(w.dataset_id), "path": path, **params}
        return web.call("GET", "/v1/files", token=read, params=q)

    one = get("/seismic/a.segy")
    assert one.status_code == 200, one.text
    body = one.json()
    entry = body["entry"]
    assert (entry["path"], entry["kind"], entry["state"], entry["tier"]) == (
        "/seismic/a.segy", "file", "stub", "archive")  # fmt: skip
    assert entry["stored"]["container"] == "archive/data"
    assert entry["stored"]["storage_class"] == "Archive"
    assert entry["hashes"]["blake3"] == "11" * 32
    assert entry["duplicates"] == {"count": 1, "file_ids": [str(elsewhere)]}
    assert [(h["kind"], h["on_file_id"]) for h in entry["holds"]] == [("legal", str(folder))]
    assert body["items"] == [] and body["next_cursor"] is None
    # A folder: its children by name, paged.
    first = get("/seismic", limit=2).json()
    assert first["entry"]["kind"] == "dir" and first["entry"]["stored"] is None
    assert [x["name"] for x in first["items"]] == ["a.segy", "b.segy"]
    assert first["items"][1]["archive_at"] == "2027-04-01"
    rest = get("/seismic", cursor=first["next_cursor"]).json()
    assert [x["name"] for x in rest["items"]] == ["c.segy"] and rest["next_cursor"] is None
    root = get("/").json()
    assert root["entry"]["path"] == "/" and "seismic" in [x["name"] for x in root["items"]]
    # Refusals.
    assert get("/seismic/missing").status_code == 404
    for bad in ("seismic", "/seismic/../x"):
        r = web.call("GET", "/v1/files", token=read, check=False,
                     params={"dataset_id": str(w.dataset_id), "path": bad})  # fmt: skip
        assert r.status_code == 400, bad
    r = web.call("GET", "/v1/files", token=web.app_token(["Policy.Propose"]),
                 params={"dataset_id": str(w.dataset_id), "path": "/"})  # fmt: skip
    assert r.status_code == 403
    # Rows that name files are for apps: a role never grants read (decision 28),
    # so not even an ArchiveAdmin reads them in the portal.
    params = {"dataset_id": str(w.dataset_id), "path": "/seismic/b.segy"}
    for who in (w.admin, w.steward):
        r = web.browser(who).get("/v1/files", params=params)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", r.text
    ds_only = {"dataset_id": str(w.dataset_id)}
    for path in ("/v1/restores", "/v1/events"):
        r = web.browser(w.admin).get(path, params=ds_only if path == "/v1/restores" else None)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", (path, r.text)


def test_restores(web, sql):
    w = seed(sql)
    folder = add_dir(sql, w, "raw")
    a = add_file(sql, w, "a.segy", parent=folder, tier="archive", state="stub")
    b = add_file(sql, w, "b.segy", parent=folder, tier="archive", state="stub")

    def restore(fid: int, state: str, trigger: str) -> str:
        (loc,) = sql.execute(
            "SELECT primary_location_id FROM catalog.file WHERE file_id = %s", (fid,)
        ).fetchone()
        (rid,) = sql.execute(
            "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, state,"
            " triggered_by, migrate_on_complete, bytes) VALUES (%s, %s, 's3_restore_object',"
            " 'bulk', %s, %s, true, 4096) RETURNING restore_id",
            (loc, w.backend_id, state, trigger),
        ).fetchone()
        return str(rid)

    first = restore(a, "completed", "user_recall")
    second = restore(b, "in_progress", "migration_wave")
    read = web.app_token(["Archive.Read"])
    ds = {"dataset_id": str(w.dataset_id)}
    r = web.call("GET", "/v1/restores", token=read, params=ds)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [x["restore_id"] for x in items] == [second, first]
    assert items[1]["path"] == "/raw/a.segy" and items[1]["migrate_on_complete"] is True
    only = web.call("GET", "/v1/restores", token=read,
                    params={**ds, "triggered_by": "user_recall"}).json()["items"]  # fmt: skip
    assert [x["restore_id"] for x in only] == [first]
    paged = web.call("GET", "/v1/restores", token=read, params={**ds, "limit": 1}).json()
    assert [x["restore_id"] for x in paged["items"]] == [second]
    rest = web.call("GET", "/v1/restores", token=read,
                    params={**ds, "cursor": paged["next_cursor"]}).json()  # fmt: skip
    assert [x["restore_id"] for x in rest["items"]] == [first]
    r = web.call("GET", "/v1/restores", token=read, check=False,
                 params={**ds, "state": "nonsense"})  # fmt: skip
    assert r.status_code == 400


def test_the_event_feed(web, sql):
    w = seed(sql)
    read = web.app_token(["Archive.Read"])
    ds = {"dataset_id": str(w.dataset_id)}

    def events(**params):  # type: ignore[no-untyped-def]
        r = web.call("GET", "/v1/events", token=read, params={**ds, **params})
        assert r.status_code == 200, r.text
        return r.json()

    start = events(after="now")
    assert start["items"] == []
    folder = add_dir(sql, w, "feed")
    f = add_file(sql, w, "x.bin", parent=folder)
    sql.execute("UPDATE catalog.file SET name = 'y.bin' WHERE file_id = %s", (f,))
    page1 = events(after=start["next"], limit=2)
    assert [e["kind"] for e in page1["items"]] == ["file.upsert", "file.upsert"]
    assert page1["items"][0]["entity_id"] == str(folder)
    assert page1["items"][1]["file"] == {"path": "/feed/y.bin", "kind": "file", "deleted": False}
    rest = events(after=page1["next"])
    kinds = [(e["kind"], e["entity_id"]) for e in rest["items"]]
    assert ("file.rename", str(f)) in kinds
    # Created and deleted within one read: the event still resolves.
    gone = add_file(sql, w, "gone.bin", parent=folder)
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (gone,))
    both = [e for e in events(after=rest["next"])["items"] if e["entity_id"] == str(gone)]
    assert both and all(e["file"]["deleted"] and e["file"]["path"] == "/feed/gone.bin"
                        for e in both)  # fmt: skip
    r = web.call("GET", "/v1/files", token=read, params={"file_id": str(gone)})
    assert r.status_code == 200 and r.json()["entry"]["deleted_at"] is not None
    rest = events(after=rest["next"])
    assert all(e["entity"] in ("file", "dataset", "share", "recall") for e in rest["items"])
    # Caught up: the next page is empty and its cursor doesn't go backwards.
    again = events(after=rest["next"])
    assert again["items"] == [] and again["next"] >= rest["next"]
    # Another dataset's events are filtered out.
    other = seed(sql)
    add_dir(sql, other, "elsewhere")
    assert events(after=again["next"])["items"] == []
    # Bad and expired cursors.
    r = web.call("GET", "/v1/events", token=read, check=False, params={"after": "x"})
    assert r.status_code == 400
    sql.execute(
        "UPDATE catalog.feed_retention SET dropped_through_txid = '1', dropped_through_event = 5"
    )
    try:
        r = web.call("GET", "/v1/events", token=read, params={"after": "0.1"})
        assert r.status_code == 410 and r.json()["code"] == "cursor_expired"
    finally:
        sql.execute(
            "UPDATE catalog.feed_retention SET dropped_through_txid = '0',"
            " dropped_through_event = 0"
        )
    # A restored catalog (migration 0019): cursors from before are refused,
    # and "now" starts a cursor in the new incarnation.
    sql.execute("SELECT catalog.feed_restored()")
    try:
        r = web.call("GET", "/v1/events", token=read, check=False,
                     params={**ds, "after": again["next"]})  # fmt: skip
        assert r.status_code == 410 and r.json()["code"] == "cursor_expired"
        now = events(after="now")["next"]
        assert now.endswith(".1")
        add_dir(sql, w, "after-restore")
        page = events(after=now)
        assert page["items"] and page["next"].endswith(".1")
        assert all(e["cursor"].endswith(".1") for e in page["items"])
        assert events(after="0.0")["items"] is not None
    finally:
        sql.execute("UPDATE catalog.feed_retention SET incarnation = 0")


def test_usage(web, sql, db_url):
    w = seed(sql)
    read = web.app_token(["Archive.Read"])
    ds = {"dataset_id": str(w.dataset_id)}
    folder = add_dir(sql, w, "usage")
    add_file(sql, w, "hot.bin", parent=folder, size=2 * 1024**3, versions=2)
    add_file(sql, w, "cold.bin", parent=folder, tier="archive", state="stub", size=1024**3)
    sql.execute("UPDATE catalog.file SET remote_tier = 'hot' WHERE name = 'hot.bin'"
                " AND dataset_id = %s", (w.dataset_id,))  # fmt: skip
    # A dedupe reference: another file's version naming hot.bin's current blob.
    ref = add_file(sql, w, "same.bin", parent=folder, size=2 * 1024**3)
    sql.execute(
        "UPDATE catalog.location SET object_key = h.object_key, container = h.container"
        "  FROM catalog.location h WHERE catalog.location.file_id = %s"
        "   AND h.location_id = (SELECT primary_location_id FROM catalog.file WHERE file_id = %s)",
        (ref, w.files["hot.bin"]),
    )

    async def count() -> None:
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            async with conn.transaction():
                await snapshot(conn, datetime.now(UTC))

    asyncio.run(count())
    r = web.call("GET", "/v1/usage", token=read, params=ds)
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["computed_at"] is not None
    (d,) = u["datasets"]
    # Two versions of hot.bin are both billed, the dedupe reference isn't;
    # one archived copy of cold.bin.
    stored = {(x["tier"], x["storage_class"]): x for x in d["stored"]}
    assert stored[("hot", "Hot")]["objects"] == 2
    assert stored[("hot", "Hot")]["bytes"] == 4 * 1024**3
    assert stored[("archive", "Archive")]["bytes"] == 1024**3
    assert stored[("hot", "Hot")]["monthly_cost"]["currency"] == "USD"
    assert d["monthly_cost"] and d["monthly_cost"][0]["currency"] == "USD"
    (share,) = [s for s in u["shares"] if s["share_id"] == str(w.share_id)]
    assert share["files"] == 3 and share["bytes"] == 5 * 1024**3  # what users see
    assert {t["tier"] for t in share["by_tier"]} == {"hot", "archive"}
    assert u["prices"] and u["prices"][0]["backend_kind"] == "sim"
    # Only an ArchiveAdmin in the portal.
    assert web.browser(w.steward).get("/v1/usage", params=ds).status_code == 403
