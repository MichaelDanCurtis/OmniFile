"""Adding datasets and shares (portal-api.md section 5.16; M6,
docs/design/share-provisioning.md)."""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.types.json import Jsonb

from .conftest import seed


def _s3_backend(sql) -> int:  # type: ignore[no-untyped-def]
    (b,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, true) RETURNING backend_id",
        (f"s3-{os.urandom(3).hex()}", Jsonb({"bucket": "b", "region": "eu-west-1"})),
    ).fetchone()
    return b


def _new_dataset(admin, w, **extra):  # type: ignore[no-untyped-def]
    body = {"name": f"ds-{os.urandom(3).hex()}", "backend_id": str(w.backend_id),
            "edge_id": str(w.edge_id), **extra}  # fmt: skip
    r = admin.post("/v1/datasets", body)
    assert r.status_code == 201, r.text
    return r.json()


def _share_body(w, ds: dict, **extra) -> dict:  # type: ignore[no-untyped-def]
    return {
        "dataset_id": ds["dataset_id"],
        "name": f"s-{os.urandom(3).hex()}",
        "protocols": ["nfs4", "smb"],
        "nfs_sec": ["krb5p"],
        "owners_group_sid": w.g_owners,
        "readers_group_sid": w.g_readers,
        **extra,
    }


def test_backends_are_listed_with_where_a_dataset_may_live(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    s3 = _s3_backend(sql)
    r = web.browser(w.admin).get("/v1/backends")
    assert r.status_code == 200, r.text
    by = {b["backend_id"]: b for b in r.json()["backends"]}
    assert by[str(w.backend_id)]["can_be_home"] is True
    assert by[str(s3)] == {**by[str(s3)], "kind": "s3", "can_be_home": False}
    r = web.browser(w.reader).get("/v1/backends")
    assert r.status_code == 403


def test_an_admin_adds_a_dataset_with_its_root(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    ds = _new_dataset(admin, w, storage_layout="single_account", key_prefix="geo/seismic/")
    assert ds["edge_id"] == str(w.edge_id) and ds["shares"] == []
    assert ds["storage_layout"] == "single_account" and ds["key_prefix"] == "geo/seismic/"
    root = sql.execute(
        "SELECT dataset_id, parent_id, name, kind, uid, gid, mode FROM catalog.file"
        " WHERE file_id = %s",
        (int(ds["root_dir_id"]),),
    ).fetchone()
    assert root == (int(ds["dataset_id"]), None, "", "dir", 0, 0, 0o770)
    listed, cursor = [], None
    while True:
        params = {"limit": 500, **({"cursor": cursor} if cursor else {})}
        page = admin.get("/v1/datasets", params=params).json()
        listed += page["items"]
        if not (cursor := page["next_cursor"]):
            break
    mine = [d for d in listed if d["dataset_id"] == ds["dataset_id"]]
    assert mine and mine[0]["edge_id"] == str(w.edge_id)
    audit = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'dataset.add'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    assert audit["dataset_id"] == ds["dataset_id"]

    # The same name again; S3 as a home; a missing edge; a bad prefix.
    again = admin.post("/v1/datasets", {"name": ds["name"], "backend_id": str(w.backend_id),
                                        "edge_id": str(w.edge_id)})  # fmt: skip
    assert again.status_code == 409 and again.json()["code"] == "name_taken"
    for body, field in (
        ({"backend_id": str(_s3_backend(sql))}, "backend_id"),
        ({"edge_id": "999999999"}, "edge_id"),
        ({"key_prefix": "/abs"}, "key_prefix"),
        ({"name": ".hidden"}, "name"),
        ({"surprise": 1}, "surprise"),
    ):
        b = {"name": f"x-{os.urandom(3).hex()}", "backend_id": str(w.backend_id),
             "edge_id": str(w.edge_id), **body}  # fmt: skip
        r = admin.post("/v1/datasets", b, check=False)
        assert r.status_code == 400, (body, r.text)
        assert r.json()["errors"][0]["field"] == field


def test_an_admin_adds_a_share_and_the_edge_gets_it_in_its_feed(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    ds = _new_dataset(admin, w)
    body = _share_body(w, ds, on_open="auto", wait_threshold_minutes=10, smb_encrypt=True,
                       rewind_depth_days=7, rewind_depth_max_days=30)  # fmt: skip
    r = admin.post("/v1/shares", body)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["root_dir_id"] == ds["root_dir_id"] and s["edge_id"] == str(w.edge_id)
    assert s["trust_level"] == "proven" and s["smb_encrypt"] is True
    assert s["rewind"]["depth_days"] == 7 and s["wait_threshold_minutes"] == 10
    # The seeded share took export id 2 on this edge; the new one is the next.
    assert s["nfs_export_id"] == 3
    # The Kerberos throughput note is among its checks (CLAUDE.md, decision 28).
    assert any(c["code"] == "kerberos_throughput" for c in s["checks"]), s["checks"]

    # The edge's feed carries what it renders.
    from omnifile_service.api.rows import SHARE_SELECT, share_row

    row = sql.execute(SHARE_SELECT + " WHERE share_id = %s", (int(s["share_id"]),)).fetchone()
    cols = [c.name for c in sql.execute(SHARE_SELECT + " LIMIT 0").description]
    fed = share_row(dict(zip(cols, row, strict=True)))
    assert fed["nfs_export_id"] == 3 and fed["smb_encrypt"] is True
    assert fed["root_dir_id"] == ds["root_dir_id"] and fed["host_trust_override"] is False
    ev = sql.execute(
        "SELECT count(*) FROM catalog.feed_event WHERE edge_id = %s AND entity = 'share'"
        " AND entity_id = %s",
        (w.edge_id, int(s["share_id"])),
    ).fetchone()[0]
    assert ev >= 1
    audit = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'share.add'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    assert audit["share_id"] == s["share_id"] and audit["nfs_export_id"] == 3

    # A dataset's root is shared once; a name is taken once per edge.
    r = admin.post("/v1/shares", _share_body(w, ds))
    assert r.status_code == 409 and r.json()["code"] == "dataset_shared"
    other = _new_dataset(admin, w)
    r = admin.post("/v1/shares", _share_body(w, other, name=s["name"]))
    assert r.status_code == 409 and r.json()["code"] == "name_taken"
    # Samba and Windows ignore case: "S-…" and "s-…" would be one share.
    r = admin.post("/v1/shares", _share_body(w, other, name=s["name"].upper()))
    assert r.status_code == 409 and r.json()["code"] == "name_taken", r.text
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql.execute(
            "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, owners_group_sid)"
            " VALUES (%s, %s, %s, ARRAY['smb'], %s)",
            (int(other["dataset_id"]), w.edge_id, s["name"].upper(), w.g_owners),
        )
    # Export ids never move.
    with pytest.raises(psycopg.errors.RaiseException, match="never changes"):
        sql.execute("UPDATE catalog.share SET nfs_export_id = 99 WHERE share_id = %s",
                    (int(s["share_id"]),))  # fmt: skip


def test_every_rule_the_edge_checks_is_checked_first(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    ds = _new_dataset(admin, w)
    cases = [
        ({"name": "global"}, "name"),
        ({"name": ".omni"}, "name"),
        ({"name": "a b"}, "name"),
        ({"protocols": []}, "protocols"),
        ({"protocols": ["afp"]}, "protocols"),
        ({"nfs_sec": []}, "nfs_sec"),
        ({"nfs_sec": ["sys"]}, "nfs_clients"),
        ({"nfs_sec": ["sys"], "nfs_clients": ["*"]}, "nfs_clients"),
        ({"nfs_sec": ["sys"], "nfs_clients": ["10.0.0.0/0"]}, "nfs_clients"),
        # As wide as a wildcard: these two are all of IPv4 (M7 review).
        ({"nfs_sec": ["sys"], "nfs_clients": ["0.0.0.0/1", "128.0.0.0/1"]}, "nfs_clients"),
        ({"nfs_sec": ["sys"], "nfs_clients": ["2001:db8::/16"]}, "nfs_clients"),
        ({"nfs_sec": ["sys"], "nfs_clients": ["10.1.2.0/24"], "root_squash": False}, "root_squash"),
        ({"nfs_sec": ["krb5"], "nfs_clients": ["bad;host"]}, "nfs_clients"),
        ({"protocols": ["smb"], "nfs_sec": ["krb5"]}, "nfs_sec"),
        ({"owners_group_sid": "geo-owners"}, "owners_group_sid"),
        ({"readers_group_sid": "S-1-x"}, "readers_group_sid"),
        ({"on_open": "auto"}, "wait_threshold_minutes"),
        ({"on_open": "wait", "wait_threshold_minutes": 5}, "wait_threshold_minutes"),
        ({"rewind_depth_days": 10, "rewind_depth_max_days": 5}, "rewind_depth_days"),
        ({"dataset_id": "999999999"}, "dataset_id"),
    ]
    for extra, field in cases:
        r = admin.post("/v1/shares", _share_body(w, ds, **extra), check=False)
        assert r.status_code == 400, (extra, r.text)
        assert r.json()["errors"][0]["field"] == field, (extra, r.text)
    # Nothing was made by the refusals.
    n = sql.execute("SELECT count(*) FROM catalog.share WHERE dataset_id = %s",
                    (int(ds["dataset_id"]),)).fetchone()[0]  # fmt: skip
    assert n == 0

    # A host-trusted share over restricted folders needs the recorded override.
    sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids)"
        " VALUES (%s, %s, 488, '[]', %s, '{}')",
        (int(ds["dataset_id"]), os.urandom(32), [w.g_owners]),
    )
    sys_share = _share_body(w, ds, protocols=["nfs3"], nfs_sec=["sys"],
                            nfs_clients=["10.1.2.0/24", "render-01.corp.example"])  # fmt: skip
    r = admin.post("/v1/shares", sys_share, check=False)
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "host_trust_override"
    r = admin.post("/v1/shares", {**sys_share, "host_trust_override": True})
    assert r.status_code == 201, r.text
    assert r.json()["trust_level"] == "host_trusted"


def test_only_signed_in_admins_add_datasets_and_shares(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    ds_body = {"name": "x", "backend_id": str(w.backend_id), "edge_id": str(w.edge_id)}
    for who in (w.steward, w.owner):
        assert web.browser(who).post("/v1/datasets", ds_body).status_code == 403
        r = web.browser(who).post("/v1/shares", _share_body(w, {"dataset_id": str(w.dataset_id)}))
        assert r.status_code == 403 and r.json()["code"] == "role_required"
    token = web.app_token(["Archive.Adopt", "Archive.Read"])
    r = web.call("POST", "/v1/datasets", token=token, json=ds_body, check=False)
    assert r.status_code in (401, 403)


# ------------------------------------------------------------- folders and removal


def _dir(sql, parent: int, ds: int, name: str, kind: str = "dir") -> int:  # type: ignore[no-untyped-def]
    (fid,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, %s, %s)"
        " RETURNING file_id",
        (ds, parent, name, kind),
    ).fetchone()
    return fid


def test_a_folder_is_shared_once_the_root_is(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    ds = _new_dataset(admin, w)
    root, dsid = int(ds["root_dir_id"]), int(ds["dataset_id"])
    projects = _dir(sql, root, dsid, "projects")
    year = _dir(sql, projects, dsid, "2026")
    _dir(sql, projects, dsid, "notes.txt", "file")

    # No root share yet: the edge would have no folder to put it in.
    r = admin.post("/v1/shares", _share_body(w, ds, path="/projects/2026"), check=False)
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "path", r.text
    assert admin.post("/v1/shares", _share_body(w, ds)).status_code == 201

    r = admin.post("/v1/shares", _share_body(w, ds, path="/projects/2026"))
    assert r.status_code == 201, r.text
    assert r.json()["root_dir_id"] == str(year)
    assert r.json()["path"] == "/projects/2026"
    audit = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'share.add'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    assert audit["path"] == "/projects/2026"
    # The edge gets the folder as the share's root.
    from omnifile_service.api.rows import SHARE_SELECT, share_row

    cols = [c.name for c in sql.execute(SHARE_SELECT + " LIMIT 0").description]
    row = sql.execute(
        SHARE_SELECT + " WHERE share_id = %s", (int(r.json()["share_id"]),)
    ).fetchone()
    assert share_row(dict(zip(cols, row, strict=True)))["root_dir_id"] == str(year)

    again = admin.post("/v1/shares", _share_body(w, ds, path="/projects/2026"))
    assert again.status_code == 409 and again.json()["code"] == "dataset_shared"
    for bad in ("/projects/nope", "/projects/notes.txt", "/projects/../x", "projects"):
        r = admin.post("/v1/shares", _share_body(w, ds, path=bad), check=False)
        assert r.status_code == 400 and r.json()["errors"][0]["field"] == "path", (bad, r.text)
    # Folders smbd would serve as another directory (a % substitution, a
    # trailing space it trims) are refused, though they exist (M7 review).
    for odd in ("%U", "trailing "):
        _dir(sql, projects, dsid, odd)
        r = admin.post("/v1/shares", _share_body(w, ds, path=f"/projects/{odd}"), check=False)
        assert r.status_code == 400 and r.json()["errors"][0]["field"] == "path", (odd, r.text)


def _remove(b, share_id: str, etag: str | None):  # type: ignore[no-untyped-def]
    headers = {"If-Match": etag} if etag else {}
    return b.call("DELETE", "/v1/shares/{share_id}", share_id=share_id, headers=headers,
                  check=False)  # fmt: skip


def test_removing_a_share_keeps_its_data_and_tells_the_edge(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    ds = _new_dataset(admin, w)
    root, dsid = int(ds["root_dir_id"]), int(ds["dataset_id"])
    sub = _dir(sql, root, dsid, "projects")
    top = admin.post("/v1/shares", _share_body(w, ds)).json()
    folder = admin.post("/v1/shares", _share_body(w, ds, path="/projects")).json()
    etag = lambda s: admin.get("/v1/shares/{share_id}", share_id=s["share_id"]).headers["etag"]  # noqa: E731

    r = _remove(admin, folder["share_id"], None)
    assert r.status_code == 428, r.text
    # The root share holds the folder the other share lives in.
    r = _remove(admin, top["share_id"], etag(top))
    assert r.status_code == 409 and r.json()["code"] == "share_in_use", r.text
    # Others can't.
    r = _remove(web.browser(w.steward), folder["share_id"], etag(folder))
    assert r.status_code == 403

    r = _remove(admin, folder["share_id"], etag(folder))
    assert r.status_code == 200 and r.json() == {"share_id": folder["share_id"], "removed": True}
    r = _remove(admin, top["share_id"], etag(top))
    assert r.status_code == 200, r.text
    # Only the shares went: the dataset and its folders stay.
    n = sql.execute("SELECT count(*) FROM catalog.share WHERE dataset_id = %s", (dsid,)).fetchone()[
        0
    ]
    assert n == 0
    assert (
        sql.execute("SELECT deleted_at FROM catalog.file WHERE file_id = %s", (sub,)).fetchone()[0]
        is None
    )
    # The edge hears of each removal in its feed.
    evs = sql.execute(
        "SELECT entity_id, detail FROM catalog.feed_event WHERE entity = 'share' AND op = 'delete'"
        " AND edge_id = %s ORDER BY event_id",
        (w.edge_id,),
    ).fetchall()
    assert [e[0] for e in evs][-2:] == [int(folder["share_id"]), int(top["share_id"])]
    assert evs[-1][1]["name"] == top["name"] and int(evs[-1][1]["entity_version"]) >= 2
    audit = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'share.remove'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    assert audit["share_id"] == top["share_id"]
    # The root can be shared again, under another name.
    assert admin.post("/v1/shares", _share_body(w, ds)).status_code == 201
    gone = _remove(admin, "999999999", '"1"')
    assert gone.status_code == 404


def test_a_worm_share_is_never_removed(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    sql.execute(
        "UPDATE catalog.share SET worm_enabled = true, worm_mode = 'unlocked_test',"
        " worm_retention_days = 30 WHERE share_id = %s",
        (w.share_id,),
    )
    etag = admin.get("/v1/shares/{share_id}", share_id=w.share_id).headers["etag"]
    r = _remove(admin, str(w.share_id), etag)
    assert r.status_code == 409 and r.json()["code"] == "worm_violation", r.text
