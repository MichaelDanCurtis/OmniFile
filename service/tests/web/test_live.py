"""The live Activity feed (portal-api.md section 5.13, WP18)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

from .conftest import add_dir, add_file, seed
from .test_annex import _annex, _copied, _dataset, _filer


def _upload(sql, w, fid: int) -> int:  # type: ignore[no-untyped-def]
    """Mark the file's version as committed by the world's edge, now."""
    (vid,) = sql.execute(
        "UPDATE catalog.file_version SET edge_id = %s, committed_at = now()"
        " WHERE file_id = %s RETURNING version_id",
        (w.edge_id, fid),
    ).fetchone()
    return vid


def _live(caller, **params):  # type: ignore[no-untyped-def]
    r = caller.get("/v1/activity/live", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _mine(body, w):  # type: ignore[no-untyped-def]
    return [e for e in body["events"] if e["edge_id"] == str(w.edge_id)]


def test_the_feed_names_only_what_the_viewer_may_read(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    secret = add_dir(sql, w, "secret", acl=w.acl_secret)
    open_ = add_file(sql, w, "line_1042.segy", size=184)
    hidden = add_file(sql, w, "plan.segy", parent=secret, acl=w.acl_secret, size=7)
    vo, vh = _upload(sql, w, open_), _upload(sql, w, hidden)
    # A reader who is an ArchiveAdmin: roles never grant read (decision 28).
    admin = web.browser(w.user("admin-r", roles=["ArchiveAdmin"], groups=[w.g_readers]))
    body = _live(admin, edge_id=str(w.edge_id))
    by = {e["id"]: e for e in _mine(body, w)}
    seen = by[f"up:{vo}"]
    assert seen["kind"] == "upload" and seen["name"] == "line_1042.segy"
    assert seen["path"] == "line_1042.segy" and seen["file_id"] == str(open_)
    assert seen["share_name"] == w.share_name and seen["bytes"] == 184
    assert seen["hidden"] is False
    blind = by[f"up:{vh}"]
    assert blind["hidden"] is True
    assert (blind["name"], blind["path"], blind["file_id"]) == (None, None, None)
    assert blind["share_name"] == w.share_name  # which share, not which file
    assert "plan.segy" not in str(body) and "secret" not in str(body)
    # The Owners read the secret folder: the name, and the path through it.
    owner_admin = web.browser(w.user("admin-o", roles=["ArchiveAdmin"], groups=[w.g_owners]))
    by = {e["id"]: e for e in _mine(_live(owner_admin, edge_id=str(w.edge_id)), w)}
    assert by[f"up:{vh}"]["name"] == "plan.segy"
    assert by[f"up:{vh}"]["path"] == "secret/plan.segy"
    # An app reads at dataset level, with no SID: it sees no names at all.
    r = web.call("GET", "/v1/activity/live", token=web.app_token(["Archive.Read"]),
                 params={"edge_id": str(w.edge_id)})  # fmt: skip
    assert r.status_code == 200, r.text
    app_events = [e for e in r.json()["events"] if e["kind"] == "upload"]
    assert app_events and all(e["name"] is None and e["hidden"] for e in app_events)


def test_the_feed_is_for_admins_and_archive_readers(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    for user in (w.owner, w.steward, w.reader):
        r = web.browser(user).get("/v1/activity/live")
        assert r.status_code == 403 and r.json()["code"] == "role_required", user["name"]
    r = web.call("GET", "/v1/activity/live", token=web.app_token([]))
    assert r.status_code == 403
    admin = web.browser(w.admin)
    for bad in ({"wait": "26"}, {"limit": "0"}, {"kind": "upload,nope"}, {"since": "yesterday"}):
        r = admin.get("/v1/activity/live", params=bad)
        assert r.status_code == 400, bad


def test_six_kinds_counters_series_and_the_edge_strip(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    sql.execute(
        "UPDATE catalog.edge SET state = 'active', enrolled_at = now(), cert_sha256 = %s,"
        " last_heartbeat_at = now(), guard_state = 'armed', software_version = '0.3.0',"
        " health = '{\"outbox_depth\": 4}' WHERE edge_id = %s",
        (os.urandom(32), w.edge_id),
    )
    a = add_file(sql, w, "a.segy", size=100)
    _upload(sql, w, a)
    b = add_file(sql, w, "b.segy", size=50, tier="archive", state="stub")
    (vb,) = sql.execute("SELECT content_version_id FROM catalog.file WHERE file_id = %s",
                        (b,)).fetchone()  # fmt: skip
    (rid,) = sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, state) VALUES (%s, %s, 'requested')"
        " RETURNING recall_id",
        (b, vb),
    ).fetchone()
    sql.execute("UPDATE catalog.recall SET state = 'rehydrating' WHERE recall_id = %s", (rid,))
    sql.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id, details)"
        " VALUES ('system:tiering', 'tier.move', 'allow', %s, %s,"
        ' \'{"from": "hot", "to": "cool"}\')',
        (w.dataset_id, a),
    )
    admin = web.browser(w.user("admin-r", roles=["ArchiveAdmin"], groups=[w.g_readers]))
    ads = _dataset(sql, w)
    annex = _annex(admin, w, _filer(admin, w), ads)
    _copied(sql, w, annex, ads, "nas.segy", w.acl_open)
    body = _live(admin, edge_id=str(w.edge_id))
    kinds = {e["kind"] for e in _mine(body, w)}
    assert {"upload", "recall", "tier", "heartbeat", "annex"} <= kinds, kinds
    recall_states = [e["state"] for e in _mine(body, w) if e["kind"] == "recall"]
    assert recall_states[:2] == ["rehydrating", "requested"]  # newest first
    tier = next(e for e in _mine(body, w) if e["kind"] == "tier")
    assert tier["state"] == "moved" and tier["detail"] == {"from": "hot", "to": "cool"}
    sql.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id, details)"
        " VALUES ('system:tiering', 'tier.archive.late', 'allow', %s, %s,"
        ' \'{"archive_at": "2026-11-01", "late_by_hours": 30}\')',
        (w.dataset_id, a),
    )
    late = next(e for e in _mine(_live(admin, edge_id=str(w.edge_id)), w) if e["kind"] == "tier")
    assert late["state"] == "late" and late["detail"]["archive_at"] == "2026-11-01"
    sql.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id, details)"
        " VALUES ('system:tiering', 'tier.move.waiting', 'allow', %s, %s,"
        ' \'{"to": "cold", "why": "own_copy_slow"}\')',
        (w.dataset_id, a),
    )
    waits = next(e for e in _mine(_live(admin, edge_id=str(w.edge_id)), w) if e["kind"] == "tier")
    assert waits["state"] == "waiting" and waits["detail"]["to"] == "cold"
    hb = next(e for e in _mine(body, w) if e["kind"] == "heartbeat")
    assert hb["detail"] == {"software_version": "0.3.0", "outbox_depth": 4}
    an = next(e for e in _mine(body, w) if e["kind"] == "annex")
    assert an["state"] == "arrived" and an["name"] == "nas.segy" and an["path"] == "nas.segy"
    assert an["share_id"] is None  # an annex's dataset has no share
    assert an["detail"]["annex_id"] == annex["annex_id"]
    c = body["counters"]
    assert c["uploads_15m"] >= 1 and c["recalls_in_flight"] >= 1
    assert c["edges_online"] >= 1 and c["edges_total"] >= c["edges_online"]
    assert len(body["series"]["uploads"]) == 30 and body["series"]["step_s"] == 60
    assert sum(body["series"]["uploads"]) >= 1
    strip = next(e for e in body["edges"] if e["edge_id"] == str(w.edge_id))
    assert strip["status"] == "ok" and strip["uploads_15m"] == 1 and strip["bytes_15m"] == 100
    assert strip["recalls_open"] == 1 and strip["outbox_depth"] == 4
    # Filters: one kind, and one share.
    only = _live(admin, edge_id=str(w.edge_id), kind="recall")
    assert {e["kind"] for e in only["events"]} == {"recall"}
    other = seed(sql)
    assert _live(admin, share_id=str(other.share_id))["events"] == []
    assert len(_live(admin, share_id=str(w.share_id))["events"]) >= 3


def test_the_cursor_returns_only_newer_and_late_commits(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    first = _live(admin, edge_id=str(w.edge_id))
    assert first["events"] == []
    a = add_file(sql, w, "late.segy")
    va = _upload(sql, w, a)
    nxt = _live(admin, edge_id=str(w.edge_id), since=first["cursor"])
    assert [e["id"] for e in nxt["events"]] == [f"up:{va}"]
    # A row committed with a time just before the cursor (a late transaction) still shows.
    b = add_file(sql, w, "older.segy")
    vb = _upload(sql, w, b)
    cut = datetime.fromisoformat(nxt["cursor"].replace("Z", "+00:00"))
    sql.execute("UPDATE catalog.file_version SET committed_at = %s WHERE version_id = %s",
                (cut - timedelta(seconds=10), vb))  # fmt: skip
    again = _live(admin, edge_id=str(w.edge_id), since=nxt["cursor"])
    assert f"up:{vb}" in {e["id"] for e in again["events"]}
    # A person filter: nobody did these.
    assert _live(admin, edge_id=str(w.edge_id), identity_id="999999999")["events"] == []
    # since older than 15 minutes reads 15 minutes back, no more.
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    assert len(_live(admin, edge_id=str(w.edge_id), since=old)["events"]) == 2


def test_a_long_poll_waits_for_news_and_the_cap_answers_at_once(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    web.services.settings.live_probe_s = 0.05
    admin = web.browser(w.admin)
    cur = _live(admin, edge_id=str(w.edge_id))["cursor"]
    t0 = time.monotonic()
    body = _live(admin, edge_id=str(w.edge_id), kind="upload", since=cur, wait="1")
    assert time.monotonic() - t0 >= 0.9
    assert body["live"] == {"waited": True, "capped": False, "next_poll_s": 2}
    # Something new ends the wait at once.
    a = add_file(sql, w, "news.segy")
    _upload(sql, w, a)
    t0 = time.monotonic()
    body = _live(admin, edge_id=str(w.edge_id), kind="upload", since=cur, wait="20")
    assert time.monotonic() - t0 < 5 and len(body["events"]) == 1
    # Past the replica's cap, a poll is answered at once and told to come back later.
    web.services.settings.live_max_waiting = 0
    t0 = time.monotonic()
    body = _live(admin, edge_id=str(w.edge_id), since=body["cursor"], wait="20")
    assert time.monotonic() - t0 < 5
    assert body["live"] == {"waited": False, "capped": True, "next_poll_s": 10}


def test_the_cap_follows_the_catalog_pool() -> None:
    """#63 review: the small profile's pool is 4 connections; 32 held polls, each
    probing every 2 s, would swamp it."""
    from types import SimpleNamespace

    from omnifile_service.web.live import _max_waiting

    def ctx(pool: int | None, cap: int = 32):  # type: ignore[no-untyped-def]
        catalog = SimpleNamespace(pool=SimpleNamespace(max_size=pool))
        settings = SimpleNamespace(live_max_waiting=cap)
        return SimpleNamespace(svc=SimpleNamespace(catalog=catalog, settings=settings))

    assert _max_waiting(ctx(4)) == 8
    assert _max_waiting(ctx(40)) == 32
    assert _max_waiting(ctx(None)) == 32


def _ago(sql, vid: int, seconds: float) -> None:  # type: ignore[no-untyped-def]
    sql.execute(
        "UPDATE catalog.file_version SET committed_at = now() - make_interval(secs => %s)"
        " WHERE version_id = %s",
        (seconds, vid),
    )


def test_an_app_sees_only_its_client_owners_datasets_and_no_people(web, sql) -> None:  # type: ignore[no-untyped-def]
    """#63 review: the owner rule of the annex reads applies here too, and an app
    is never told who did what."""
    from .conftest import APP_OWNER_SID

    w = seed(sql)
    fid = add_file(sql, w, "app.segy", size=11)
    vid = _upload(sql, w, fid)
    (iid,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, display_name) VALUES (%s, 'user',"
        " 'Una Uploader') RETURNING identity_id",
        (f"{w.domain}-4321",),
    ).fetchone()
    sql.execute(
        "UPDATE catalog.file_version SET actor_via = 'smb', actor_proof = 'proven',"
        " actor_identity_id = %s WHERE version_id = %s",
        (iid, vid),
    )
    token = web.app_token(["Archive.Read"])

    def app_live():  # type: ignore[no-untyped-def]
        r = web.call("GET", "/v1/activity/live", token=token, params={"edge_id": str(w.edge_id)})
        assert r.status_code == 200, r.text
        return r.json()

    body = app_live()
    ev = next(e for e in body["events"] if e["id"] == f"up:{vid}")
    assert ev["person"] is None and ev["name"] is None
    assert "Una Uploader" not in str(body)
    # Nor may it filter by person, which would say whose events these are.
    r = web.call("GET", "/v1/activity/live", token=token,
                 params={"identity_id": str(iid)}, check=False)  # fmt: skip
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "identity_id"
    # A person signed in is told who.
    admin = web.browser(w.user("admin-r", roles=["ArchiveAdmin"], groups=[w.g_readers]))
    mine = {e["id"]: e for e in _live(admin, edge_id=str(w.edge_id))["events"]}
    assert mine[f"up:{vid}"]["person"]["display_name"] == "Una Uploader"
    # The client's owner no longer holds a role: the app sees nothing of the dataset.
    sql.execute(
        "UPDATE catalog.identity SET app_roles_seen_at = now() - interval '60 days' WHERE sid = %s",
        (APP_OWNER_SID,),
    )
    try:
        body = app_live()
        assert [e for e in body["events"] if e["dataset_id"] == str(w.dataset_id)] == []
        strip = next(e for e in body["edges"] if e["edge_id"] == str(w.edge_id))
        assert strip["uploads_15m"] == 0
    finally:
        sql.execute(
            "UPDATE catalog.identity SET app_roles_seen_at = now() WHERE sid = %s",
            (APP_OWNER_SID,),
        )


def test_a_row_dated_ahead_does_not_end_every_wait(web, sql) -> None:  # type: ignore[no-untyped-def]
    """#63 review: 0031's commit floor dates versions ahead. The probe saw one at
    once while the answer, which stops at now, was empty: every page busy-looped."""
    w = seed(sql)
    web.services.settings.live_probe_s = 0.05
    admin = web.browser(w.admin)
    cur = _live(admin, edge_id=str(w.edge_id))["cursor"]
    vid = _upload(sql, w, add_file(sql, w, "ahead.segy"))
    _ago(sql, vid, -3600)  # an hour ahead
    try:
        t0 = time.monotonic()
        body = _live(admin, edge_id=str(w.edge_id), kind="upload", since=cur, wait="1")
        assert time.monotonic() - t0 >= 0.9
        assert body["events"] == [] and body["live"]["next_poll_s"] >= 2
    finally:
        _ago(sql, vid, 3600)


def test_the_share_filter_applies_before_the_limit_and_skips_nested_shares(web, sql) -> None:  # type: ignore[no-untyped-def]
    """#63 review: filtered after the limit, a quiet share's events were crowded out."""
    busy, quiet = seed(sql), seed(sql)
    old = _upload(sql, quiet, add_file(sql, quiet, "quiet.segy"))
    _ago(sql, old, 60)
    for i in range(5):
        _upload(sql, busy, add_file(sql, busy, f"busy-{i}.segy"))
    admin = web.browser(busy.admin)
    body = _live(admin, share_id=str(quiet.share_id), limit="2", kind="upload")
    assert [e["id"] for e in body["events"]] == [f"up:{old}"]
    # A file under a share nested in another belongs to the nested one only.
    sub = add_dir(sql, busy, "sub")
    (inner,) = sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, nfs_sec,"
        " owners_group_sid, root_dir_id, on_open) VALUES (%s, %s, %s, ARRAY['nfs4'],"
        " ARRAY['krb5'], %s, %s, 'wait') RETURNING share_id",
        (busy.dataset_id, busy.edge_id, f"inner-{busy.share_name}", busy.g_owners, sub),
    ).fetchone()
    deep = _upload(sql, busy, add_file(sql, busy, "deep.segy", parent=sub))
    outer = {e["id"] for e in _live(admin, share_id=str(busy.share_id), kind="upload")["events"]}
    assert f"up:{deep}" not in outer and len(outer) == 5
    got = _live(admin, share_id=str(inner), kind="upload")["events"]
    assert [e["id"] for e in got] == [f"up:{deep}"] and got[0]["share_id"] == str(inner)
    r = admin.get("/v1/activity/live", params={"share_id": "999999999"})
    assert r.status_code == 404
