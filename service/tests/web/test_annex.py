"""Filers and annexes in the portal and the partner API (portal-api.md 5.20;
annex.md phase A1, Copy mode only)."""

from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from .conftest import APP_OWNER_SID, World, seed


def _dataset(sql, w: World, *, shared: bool = False) -> int:
    tag = uuid.uuid4().hex[:8]
    (ds,) = sql.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING dataset_id",
        (f"annex-ds-{tag}", w.backend_id, w.edge_id),
    ).fetchone()
    (root,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, NULL, '', 'dir')"
        " RETURNING file_id",
        (ds,),
    ).fetchone()
    if shared:
        sql.execute(
            "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, nfs_sec,"
            " owners_group_sid) VALUES (%s, %s, %s, ARRAY['nfs4'], ARRAY['krb5'], %s)",
            (ds, w.edge_id, f"s-{tag}", w.g_owners),
        )
    return ds


def _filer(b, w: World, **kw) -> dict:
    body = {
        "name": f"ontap-{uuid.uuid4().hex[:8]}",
        "host": f"nas-{uuid.uuid4().hex[:6]}",
        "protocol": "smb",
        "export": "geo",
        "edge_id": str(w.edge_id),
        "vendor": "ontap",
        **kw,
    }
    r = b.post("/v1/filers", body)
    assert r.status_code == 201, r.text
    return r.json()


def _annex(b, w: World, filer: dict, ds: int, **kw) -> dict:
    body = {
        "name": f"lts-{uuid.uuid4().hex[:8]}",
        "filer_id": filer["filer_id"],
        "path": "long-term-storage",
        "dataset_id": str(ds),
        "owners_sid": w.g_owners,
        **kw,
    }
    r = b.post("/v1/annexes", body)
    assert r.status_code == 201, r.text
    return r.json()


def test_register_a_filer_and_create_a_copy_annex(web, sql) -> None:
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    assert f["vendor"] == "ontap" and f["event_source"] == "smb_notify" and not f["host_trusted"]
    assert sql.execute(
        "SELECT kind FROM catalog.backend b JOIN catalog.filer f USING (backend_id)"
        " WHERE f.filer_id = %s",
        (int(f["filer_id"]),),
    ).fetchone() == ("nas",)
    # sec=sys is NFS only, and such a filer is host-trusted.
    r = admin.post(
        "/v1/filers",
        {
            "name": "bad",
            "host": "h",
            "protocol": "smb",
            "export": "x",
            "edge_id": str(w.edge_id),
            "credential_mode": "sec_sys",
        },
    )
    assert r.status_code == 400
    nfs = _filer(admin, w, protocol="nfs3", export="/export/geo", credential_mode="sec_sys")
    assert nfs["host_trusted"] and nfs["event_source"] == "scan"
    # Only an ArchiveAdmin registers one; a DataSteward reads them.
    steward = web.browser(w.steward)
    assert (
        steward.post(
            "/v1/filers",
            {"name": "x", "host": "h", "protocol": "smb", "export": "y", "edge_id": str(w.edge_id)},
        ).status_code
        == 403
    )
    listed = steward.get("/v1/filers")
    assert listed.status_code == 200
    assert f["filer_id"] in {x["filer_id"] for x in listed.json()["items"]}
    assert steward.get("/v1/filers/{filer_id}", filer_id=f["filer_id"]).status_code == 200
    h = steward.get("/v1/filers/{filer_id}/health", filer_id=f["filer_id"])
    assert h.status_code == 200 and h.json()["ok"] is None and h.json()["annexes"] == 0

    ds = _dataset(sql, w)
    # A mode a later phase brings is refused (open enum, closed list here).
    r = admin.call(
        "POST",
        "/v1/annexes",
        json={
            "name": "move",
            "filer_id": f["filer_id"],
            "path": "drop",
            "dataset_id": str(ds),
            "owners_sid": w.g_owners,
            "mode": "sync_both_ways",
        },
        check=False,
    )
    assert r.status_code == 400
    a = _annex(admin, w, f, ds, settings={"quiet_minutes": 15})
    assert a["mode"] == "copy" and a["state"] == "registered" and a["ruleset_version"] == 1
    assert a["unc"] == f"\\\\{f['host']}\\geo\\long-term-storage"
    assert a["settings"]["quiet_minutes"] == 15 and a["settings"]["quick_scan_minutes"] == 60
    body, state = sql.execute(
        "SELECT body, state FROM catalog.annex_ruleset WHERE annex_id = %s",
        (int(a["annex_id"]),),
    ).fetchone()
    assert body["template"] == "copy" and state == "active"
    # One directory, one annex: none inside another.
    r = admin.post(
        "/v1/annexes",
        {
            "name": "inner",
            "filer_id": f["filer_id"],
            "path": "long-term-storage/2026",
            "dataset_id": str(ds),
            "owners_sid": w.g_owners,
        },
    )
    assert r.status_code == 409 and r.json()["code"] == "source_conflict"
    # A shared dataset can't take an annex's copies, nor an annexed one a share.
    r = admin.post(
        "/v1/annexes",
        {
            "name": "s",
            "filer_id": f["filer_id"],
            "path": "other",
            "dataset_id": str(_dataset(sql, w, shared=True)),
            "owners_sid": w.g_owners,
        },
    )
    assert r.status_code == 409 and r.json()["code"] == "dataset_shared"
    r = admin.post(
        "/v1/shares",
        {
            "dataset_id": str(ds),
            "name": f"s{uuid.uuid4().hex[:6]}",
            "protocols": ["smb"],
            "owners_group_sid": w.g_owners,
        },
    )
    assert r.status_code == 409 and r.json()["code"] == "dataset_annexed"
    # The annex's binding isn't a migration source.
    srcs = admin.get("/v1/sources").json()["items"]
    assert all(s["kind"] != "annex" for s in srcs)
    # A DataSteward outside the Owners group can't create one.
    r = steward.post(
        "/v1/annexes",
        {
            "name": "nope",
            "filer_id": f["filer_id"],
            "path": "p",
            "dataset_id": str(ds),
            "owners_sid": w.g_owners,
        },
    )
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"


def test_an_app_names_a_person_who_owns_the_annex(web, sql) -> None:
    w = seed(sql)
    f = _filer(web.browser(w.admin), w)
    ds = _dataset(sql, w)
    token = web.app_token(["Archive.Adopt"])
    body = {
        "name": f"app-{uuid.uuid4().hex[:6]}",
        "filer_id": f["filer_id"],
        "path": "a",
        "dataset_id": str(ds),
        "owners_sid": w.g_owners,
    }
    r = web.call("POST", "/v1/annexes", token=token, json=body)
    assert r.status_code == 422 and r.json()["code"] == "owner_required"
    person = f"{w.domain}-4444"
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)",
        (person, [w.g_readers]),
    )
    r = web.call("POST", "/v1/annexes", token=token, json={**body, "owner": person})
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
    sql.execute(
        "UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([w.g_owners], person)
    )
    # In the Owners group, but unable to see the dataset (no fresh role, and
    # an annex dataset has no shares): refused.
    r = web.call("POST", "/v1/annexes", token=token, json={**body, "owner": person})
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
    sql.execute(
        "UPDATE catalog.identity SET app_roles = '{DataSteward}', app_roles_seen_at = now()"
        " WHERE sid = %s",
        (person,),
    )
    # The Owners group is a group, not the owner alone.
    r = web.call(
        "POST", "/v1/annexes", token=token, json={**body, "owner": person, "owners_sid": person}
    )
    assert r.status_code == 400
    r = web.call("POST", "/v1/annexes", token=token, json={**body, "owner": person})
    assert r.status_code == 201, r.text
    created = r.json()
    aid = created["annex_id"]
    # Proposed until a person approves it; the edge doesn't start it.
    assert created["ruleset_state"] == "proposed" and created["ruleset_version"] is None
    proposed_via, app, state = sql.execute(
        "SELECT proposed_via, proposed_by_app, state FROM catalog.annex_ruleset"
        " WHERE annex_id = %s",
        (int(aid),),
    ).fetchone()
    assert proposed_via == "api" and app.startswith("app:") and state == "proposed"
    r = web.call("GET", "/v1/annexes", token=web.app_token(["Archive.Read"]))
    assert r.status_code == 200 and r.json()["items"]
    # An app can't approve it; one of the Owners can, signed in.
    url = "/v1/annexes/{annex_id}/rulesets/{version}/approve"
    r = web.call("POST", url, token=token, json={}, annex_id=aid, version=1, check=False)
    assert r.status_code in (401, 403)  # not a partner route at all
    # Neither can the owner the app named (nor the client's owner).
    named = web.browser(w.user("named", roles=["DataSteward"], groups=[w.g_owners], sid_rid=4444))
    got = named.get("/v1/annexes/{annex_id}", annex_id=aid)
    r = named.call(
        "POST", url, json={}, headers={"If-Match": got.headers["ETag"]}, annex_id=aid, version=1
    )
    assert r.status_code == 403 and r.json()["code"] == "self_approval", r.text
    client_owner = web.browser(
        w.user("owen", roles=["DataSteward"], groups=[w.g_owners], sid=APP_OWNER_SID)
    )
    r = client_owner.call(
        "POST", url, json={}, headers={"If-Match": got.headers["ETag"]}, annex_id=aid, version=1
    )
    assert r.status_code == 403 and r.json()["code"] == "self_approval", r.text
    owner = web.browser(w.owner)
    got = owner.get("/v1/annexes/{annex_id}", annex_id=aid)
    r = owner.call(
        "POST", url, json={}, headers={"If-Match": got.headers["ETag"]}, annex_id=aid, version=1
    )
    assert r.status_code == 200, r.text
    assert r.json()["ruleset_state"] == "active" and r.json()["ruleset_version"] == 1
    decided = sql.execute(
        "SELECT decided_by_identity_id IS NOT NULL FROM catalog.annex_ruleset WHERE annex_id = %s",
        (int(aid),),
    ).fetchone()[0]
    assert decided
    # An app's change, retiring included, needs its client owner's access.
    sql.execute(
        "UPDATE catalog.identity SET app_roles_seen_at = now() - interval '60 days' WHERE sid = %s",
        (APP_OWNER_SID,),
    )
    try:
        r = web.call(
            "PATCH",
            "/v1/annexes/{annex_id}",
            token=token,
            json={"state": "retired"},
            headers={"If-Match": r.headers["ETag"]},
            annex_id=aid,
        )
        assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
        # Nor does it read the annex's events, files or problems.
        read = web.app_token(["Archive.Read"])
        for part in ("events", "files", "problems"):
            r = web.call("GET", f"/v1/annexes/{{annex_id}}/{part}", token=read, annex_id=aid)
            assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access", part
    finally:
        sql.execute(
            "UPDATE catalog.identity SET app_roles_seen_at = now() WHERE sid = %s",
            (APP_OWNER_SID,),
        )


def _copied(sql, w: World, a: dict, ds: int, path: str, acl: int, *, gone: bool = False) -> int:
    (fid,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, size, acl_id, state,"
        " origin, deleted_at) VALUES (%s, (SELECT file_id FROM catalog.file WHERE dataset_id = %s"
        " AND parent_id IS NULL), %s, 'file', 10, %s, 'remote', 'annex',"
        " CASE WHEN %s THEN now() END) RETURNING file_id",
        (ds, ds, path, acl, gone),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size, mtime_ns,"
        " first_seen, last_seen, gone_since, nas_state) VALUES (%s, %s, %s, %s, 10, 1, now(),"
        " now(), CASE WHEN %s THEN now() END, %s)",
        (int(a["annex_id"]), fid, path, str(fid), gone, "gone" if gone else "present"),
    )
    for kind in ("arrived",) + (("left",) if gone else ()):
        sql.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, size)"
            " VALUES (%s, %s, %s, %s, 10)",
            (int(a["annex_id"]), kind, path, fid),
        )
    return fid


def test_the_annex_page_shows_what_the_nas_lets_you_read(web, sql) -> None:
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    aid = a["annex_id"]
    # The owners' ACL copies the groups' read sets: open = readers too.
    open_acl = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first) VALUES (%s, %s, 420, '[]', %s, %s, true) RETURNING acl_id",
        (ds, uuid.uuid4().bytes * 2, [w.g_owners, w.g_readers], [w.g_other]),
    ).fetchone()[0]
    secret_acl = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first) VALUES (%s, %s, 420, '[]', %s, %s, true) RETURNING acl_id",
        (ds, uuid.uuid4().bytes * 2, [w.g_owners, w.g_readers], [w.g_readers]),
    ).fetchone()[0]
    open_f = _copied(sql, w, a, ds, "open.segy", open_acl)
    _copied(sql, w, a, ds, "secret.segy", secret_acl)
    gone_f = _copied(sql, w, a, ds, "gone.segy", open_acl, gone=True)
    sql.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, path, details)"
        " VALUES (%s, 'skipped', 'unreadable.bin', %s)",
        (int(aid), Jsonb({"reason": "unreadable"})),
    )
    sql.execute(
        "INSERT INTO catalog.reconcile_drift (source, backend_id, container, object_key, kind,"
        " detected_at, last_seen_at) VALUES ('annex', (SELECT backend_id FROM catalog.filer"
        " WHERE filer_id = %s), 'geo', 'long-term-storage/open.segy', 'changed_no_event',"
        " now(), now())",
        (int(f["filer_id"]),),
    )

    # A DataSteward in the readers' group: the deny wins for the secret file.
    steward = web.browser(w.steward)
    files = steward.get("/v1/annexes/{annex_id}/files", annex_id=aid)
    assert files.status_code == 200, files.text
    assert {x["path"] for x in files.json()["items"]} == {"open.segy", "gone.segy"}
    present = steward.get(
        "/v1/annexes/{annex_id}/files", params={"state": "present"}, annex_id=aid
    ).json()["items"]
    assert [x["path"] for x in present] == ["open.segy"]
    assert present[0]["copy_state"] == "pending" and present[0]["nas_state"] == "present"
    ev = steward.get("/v1/annexes/{annex_id}/events", annex_id=aid).json()["items"]
    paths = {e["path"] for e in ev}
    assert "secret.segy" not in paths and "unreadable.bin" not in paths and "open.segy" in paths
    # A path-only event (no catalog file to check an ACL against): its kind
    # and reason, never its NAS path (access-control.md section 5).
    [skipped] = [e for e in ev if e["kind"] == "skipped"]
    assert skipped["path"] is None and skipped["details"] == {"reason": "unreadable"}
    # The review of #83: a problem with no path whose details name a NAS folder,
    # above the annex root too (decision 63's folder_unreadable).
    sql.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, details) VALUES (%s, 'problem', %s)",
        (int(aid), Jsonb({"reason": "folder_unreadable", "folder": "hr/salaries",
                          "detail": "The permissions of folder 'hr/salaries' can't be read"})),
    )  # fmt: skip
    for who in (steward, web.browser(w.owner)):
        for url in ("/v1/annexes/{annex_id}/events", "/v1/annexes/{annex_id}/problems"):
            body = who.get(url, annex_id=aid)
            assert body.status_code == 200, body.text
            assert "hr/salaries" not in body.text, url
    sql.execute("DELETE FROM catalog.annex_event WHERE details->>'folder' = 'hr/salaries'")
    owner = web.browser(w.owner)
    ev = owner.get(
        "/v1/annexes/{annex_id}/events", params={"kind": "skipped,left"}, annex_id=aid
    ).json()["items"]
    assert {e["kind"] for e in ev} == {"skipped", "left"}
    assert all(e["path"] is None for e in ev if e["kind"] == "skipped")
    # An ArchiveAdmin sees the path.
    ev = admin.get(
        "/v1/annexes/{annex_id}/events", params={"kind": "skipped"}, annex_id=aid
    ).json()["items"]
    assert [e["path"] for e in ev] == ["unreadable.bin"]
    # Someone outside the Owners group, with no role over annexes, doesn't see it.
    assert web.browser(w.reader).get("/v1/annexes/{annex_id}", annex_id=aid).status_code == 404

    detail = owner.get("/v1/annexes/{annex_id}", annex_id=aid)
    assert detail.status_code == 200, detail.text
    c = detail.json()["counts"]
    assert (c["present"], c["gone"], c["pending"]) == (2, 1, 2)
    assert detail.json()["can_manage"] and not detail.json()["can_purge"]
    t = owner.get("/v1/annexes/{annex_id}/timeline", params={"days": 7}, annex_id=aid).json()
    assert len(t["days"]) == 7 and t["days"][-1]["arrived"] == 3 and t["days"][-1]["left"] == 1
    p = owner.get("/v1/annexes/{annex_id}/problems", annex_id=aid).json()
    # Drift names NAS paths: counts and kinds for the Owners, rows for admins.
    assert p["drift"] == [] and p["drift_counts"] == {"changed_no_event": 1}
    assert [e["kind"] for e in p["events"]] == ["skipped"]
    assert p["events"][0]["path"] is None
    p = admin.get("/v1/annexes/{annex_id}/problems", annex_id=aid).json()
    assert [d["kind"] for d in p["drift"]] == ["changed_no_event"]
    assert p["drift"][0]["path"] == "open.segy"

    # Settings: the Owners, with If-Match; a retention purge is ArchiveAdmin's.
    etag = detail.headers["ETag"]
    r = owner.call(
        "PATCH",
        "/v1/annexes/{annex_id}",
        json={"state": "paused", "settings": {"full_scan_minutes": 10080}},
        headers={"If-Match": etag},
        annex_id=aid,
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "paused" and r.json()["settings"]["full_scan_minutes"] == 10080
    r = owner.call(
        "PATCH",
        "/v1/annexes/{annex_id}",
        json={"deleted_retention_days": 30},
        headers={"If-Match": r.headers["ETag"]},
        annex_id=aid,
    )
    assert r.status_code == 403
    r = owner.call(
        "PATCH",
        "/v1/annexes/{annex_id}",
        json={"state": "active"},
        headers={"If-Match": etag},
        annex_id=aid,
    )
    assert r.status_code == 412

    # A retention, and a purge, need an ArchiveAdmin's fresh sign-in.
    sql.execute(
        "UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'"
        " WHERE entra_object_id = %s",
        (w.admin["oid"],),
    )
    got = admin.get("/v1/annexes/{annex_id}", annex_id=aid)
    r = admin.call(
        "PATCH",
        "/v1/annexes/{annex_id}",
        json={"deleted_retention_days": 30},
        headers={"If-Match": got.headers["ETag"]},
        annex_id=aid,
    )
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    url = "/v1/annexes/{annex_id}/files/{file_id}/purge"
    r = admin.post(url, annex_id=aid, file_id=gone_f)
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    admin.sign_in(w.admin, reauth=True)
    r = admin.call(
        "PATCH",
        "/v1/annexes/{annex_id}",
        json={"deleted_retention_days": 30},
        headers={"If-Match": got.headers["ETag"]},
        annex_id=aid,
    )
    assert r.status_code == 200, r.text

    # Purge: ArchiveAdmin only, and only a file gone from the NAS.
    assert owner.post(url, annex_id=aid, file_id=gone_f).status_code == 403
    r = admin.post(url, annex_id=aid, file_id=open_f)
    assert r.status_code == 409 and r.json()["code"] == "file_present"
    r = admin.post(url, annex_id=aid, file_id=gone_f)
    assert r.status_code == 202, r.text
    assert r.json()["purge_requested_at"] is not None
    queued = sql.execute(
        "SELECT count(*) FROM pgmq.q_retention WHERE message->>'op' = 'annex_purge'"
        " AND message->>'file_id' = %s",
        (str(gone_f),),
    ).fetchone()[0]
    assert queued == 1


def test_annex_defaults(web, sql) -> None:
    w = seed(sql)
    admin = web.browser(w.admin)
    r = admin.get("/v1/settings/annex-defaults")
    assert r.status_code == 200, r.text
    etag = r.headers["ETag"]
    r = admin.call(
        "PUT",
        "/v1/settings/annex-defaults",
        json={
            "quiet_minutes": 20,
            "quick_scan_minutes": 30,
            "full_scan_minutes": 720,
            "reconcile_days": 7,
        },
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    assert r.json()["quiet_minutes"] == 20
    try:
        f = _filer(admin, w)
        a = _annex(admin, w, f, _dataset(sql, w))
        assert a["settings"]["quiet_minutes"] == 20 and a["settings"]["full_scan_minutes"] == 720
        r = admin.call(
            "PUT",
            "/v1/settings/annex-defaults",
            json={"quick_scan_minutes": 90, "full_scan_minutes": 60},
            headers={"If-Match": admin.get("/v1/settings/annex-defaults").headers["ETag"]},
        )
        assert r.status_code == 400  # a full scan less often than a quick one
        assert (
            web.browser(w.steward)
            .call(
                "PUT",
                "/v1/settings/annex-defaults",
                json={"quiet_minutes": 5},
                headers={"If-Match": '"1"'},
            )
            .status_code
            == 403
        )
    finally:
        sql.execute(
            "UPDATE catalog.annex_defaults SET quiet_minutes = 10, quick_scan_minutes = 60,"
            " full_scan_minutes = 1440, reconcile_days = 7"
        )
