"""Every implemented route is in control-api.yaml, and behaves as it says.

- Each route of the api and enrol apps exists in the spec with its method.
- One walk calls every api route through `EdgeClient`, which checks each
  request and response against the spec (status code declared, body schema,
  required fields).
- Every route with a request body refuses an empty one (`400`): the spec's
  required fields are enforced.
"""

from __future__ import annotations

import uuid

import pytest

from omnifile_service.api.app import ROUTES
from omnifile_service.enrol.app import create_enrol_app

from . import spec
from .conftest import (
    E,
    complete_body,
    create_file,
    csr_pem,
    hashes_of,
    new_key,
    renewal_body,
    upload_blocks,
)
from .test_acls import OPEN as OPEN_ACL
from .test_annex import make_annex
from .test_roles_anywhere import ra_body

ENROL_ROUTES = [("POST", "/v0/enrol"), ("POST", "/internal/v0/sign"), ("POST", "/internal/v0/crl")]


def test_every_route_is_in_the_spec() -> None:
    for method, path, _, _ in ROUTES:
        spec.operation(method, path)
    for method, path in ENROL_ROUTES:
        spec.operation(method, path)


def test_the_enrol_app_serves_exactly_its_routes(enrol_app) -> None:
    served = {
        (m, r.path)
        for r in create_enrol_app(enrol_app.state.svc).routes
        for m in (r.methods or ())
        if m != "HEAD" and r.path != "/healthz"
    }
    assert served == set(ENROL_ROUTES)


def test_a_walk_through_every_route_matches_the_spec(make_world, facade_client, sql) -> None:
    w = make_world()
    e = w.edge
    e.log.clear()
    data = b"x" * 5000
    fid = create_file(w, "walk.bin")
    e.get(f"{E}/feed", {"after": "0.0"})
    e.get(f"{E}/snapshot", {"dataset_id": str(w.dataset_id)})
    with sql.transaction():
        sql.execute("SELECT set_config('omnifile.actor', 'test', true)")
        sql.execute(
            "UPDATE catalog.share SET rewind_depth_max_days = 1, rewind_depth_days = 1"
            " WHERE share_id = %s",
            (w.share_id,),
        )
    (t,) = sql.execute("SELECT now() - interval '1 second'").fetchone()
    e.get(f"{E}/asof", {"dir_id": str(w.root_id), "t": t.isoformat()})
    e.post(f"{E}/feed/ack", {"cursor": "0.0"})
    e.post(f"{E}/heartbeat", {"version": "0.1.0", "feed_cursor": "0.0"})
    body = {
        "op_id": "op-" + uuid.uuid4().hex,
        "file": {"file_id": str(fid)},
        "size": len(data),
        "mtime_ns": "1",
        "block_size": 4096,
        **hashes_of(data),
    }
    up = e.post(f"{E}/uploads", body).json()
    e.post(f"{E}/credentials", {"access": "write", "upload_id": up["upload_id"]})
    upload_blocks(facade_client, up["target"]["url"], data, 4096)
    done = e.post(
        f"{E}/uploads/{{upload_id}}/complete",
        complete_body(data, etag="x"),
        upload_id=up["upload_id"],
    ).json()
    vid = done["version_id"]
    w.verify()
    e.post(f"{E}/credentials", {"access": "read", "file_id": str(fid), "version_id": vid})
    other = create_file(w, "aborted.bin")
    other_body = {
        **body,
        "op_id": "op-" + uuid.uuid4().hex,
        "file": {"file_id": str(other)},
        **hashes_of(b"y" * 5000),
    }
    up2 = e.post(f"{E}/uploads", other_body).json()
    e.post(f"{E}/uploads/{{upload_id}}/abort", {}, upload_id=up2["upload_id"])
    lease = e.post(f"{E}/files/{{file_id}}/stub/prepare", {"version_id": vid}, file_id=fid).json()
    e.post(
        f"{E}/files/{{file_id}}/stub/commit",
        {"lease_id": lease["lease_id"], "version_id": vid, "resident_bytes": 1024},
        file_id=fid,
    )
    e.post(
        f"{E}/files/{{file_id}}/stub/skip",
        {"version_id": vid, "reason": "local_error", "detail": "test"},
        file_id=fid,
    )
    e.post(
        f"{E}/files/{{file_id}}/filled",
        {"version_id": vid, "resident_bytes": len(data)},
        file_id=fid,
    )
    sql.execute("UPDATE catalog.location SET logical_tier = 'archive' WHERE file_id = %s", (fid,))
    rec = e.post(
        f"{E}/recalls",
        {
            "file_id": str(fid),
            "version_id": vid,
            "requester": {"sid": "S-1-5-21-1-2-3-1001", "via": "smb", "proof": "proven"},
            "on_open": "fail",
        },
    ).json()
    assert "recall_id" in rec, rec
    e.post(
        f"{E}/recalls/{{recall_id}}/filled",
        {"version_id": vid, "resident_bytes": len(data)},
        recall_id=rec["recall_id"],
    )
    # A second recall whose fill fails (the edge reports it).
    rec2 = e.post(
        f"{E}/recalls",
        {
            "file_id": str(fid),
            "version_id": vid,
            "requester": {"sid": "S-1-5-21-1-2-3-1001", "via": "smb", "proof": "proven"},
            "on_open": "fail",
        },
    ).json()
    sql.execute(
        "UPDATE catalog.recall SET state = 'downloading' WHERE recall_id = %s",
        (int(rec2["recall_id"]),),
    )
    e.post(
        f"{E}/recalls/{{recall_id}}/failed",
        {"version_id": vid, "reason": "No space left on device"},
        recall_id=rec2["recall_id"],
    )
    e.post(f"{E}/reads", {"reads": [{"file_id": str(fid), "last_read_at": "2026-10-05T00:00:00Z"}]})
    e.post(f"{E}/aws-certificate", ra_body(e, csr_pem(new_key())))
    e.post(f"{E}/certificate", renewal_body(e, csr_pem(new_key())))
    e.post(f"{E}/acls", {"dataset_id": str(w.dataset_id), "acls": [OPEN_ACL]})
    scan = e.post(f"{E}/acl-scans", {"dataset_id": str(w.dataset_id), "kind": "on_change"})
    e.post(
        f"{E}/acl-scans/{{scan_id}}/complete",
        {"entries_seen": 2, "entries_changed": 1},
        scan_id=scan.json()["scan_id"],
    )
    e.post(
        f"{E}/audit",
        {"records": [{"at": "2026-10-15T10:00:00Z", "action": "stub.open", "decision": "allow"}]},
    )
    e.get(f"{E}/digest", {"dataset_id": str(w.dataset_id)})
    export = e.post(f"{E}/snapshot-exports", {"dataset_id": str(w.dataset_id)}).json()
    e.get(f"{E}/snapshot-exports/{{export_id}}", export_id=export["export_id"])

    annex_id, _, _ = make_annex(sql, w)
    e.get(f"{E}/annexes")
    e.post(f"{E}/annex-events", {"annex_id": str(annex_id), "events": [
        {"kind": "scan", "details": {"scan": "quick", "files": 0}}]})  # fmt: skip
    e.get(f"{E}/annexes/{{annex_id}}/records", annex_id=annex_id)
    (run_id,) = sql.execute(
        "INSERT INTO catalog.annex_dry_run (annex_id, body, requested_by)"
        " VALUES (%s, '{\"rules\": []}', 'test') RETURNING run_id",
        (annex_id,),
    ).fetchone()
    e.get(f"{E}/annex-dry-runs", {"annex_id": str(annex_id)})
    e.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "running"}, run_id=run_id)
    e.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "failed", "error": "test"}, run_id=run_id)
    # Removals: not live here, so every candidate is "not yet"; a step on a ticket.
    e.post(f"{E}/annex-removal-tickets", {"annex_id": str(annex_id), "candidates": [
        {"file_id": "1", "version_id": "1", "path": "a/b.segy", "nas_file_id": "9", "size": 1,
         "mtime_ns": "1700000000000000000", "rules_version": 1}]})  # fmt: skip
    (fid,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = (SELECT dataset_id FROM"
        " catalog.annex WHERE annex_id = %s) LIMIT 1",
        (annex_id,),
    ).fetchone()
    (removal_id,) = sql.execute(
        "INSERT INTO catalog.annex_removal (annex_id, file_id, version_id, path, nas_file_id,"
        " size, mtime_ns, rule, ruleset_version, expires_at)"
        " VALUES (%s, %s, 1, 'x', '9', 1, 1, 'r', 1, now() + interval '5 minutes')"
        " RETURNING removal_id",
        (annex_id, fid),
    ).fetchone()
    e.post(f"{E}/annex-removals/{{removal_id}}", {"step": "hide"}, removal_id=removal_id)
    # Placements (A3.3): none ready, then a step on one.
    e.post(f"{E}/annex-placement-tickets", {"annex_id": str(annex_id)})
    (ident,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind) VALUES ('S-1-5-21-9-9-9-4242', 'user')"
        " ON CONFLICT (sid) DO UPDATE SET kind = 'user' RETURNING identity_id"
    ).fetchone()
    (pid,) = sql.execute(
        "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
        " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
        " state) SELECT %s, %s, 1, %s, 'portal', 'back', 1, jsonb_build_object('kind', 'nas',"
        " 'folder', 'x', 'name', 'b.segy', 'filer_id', a.filer_id::text), 'restored_name',"
        " 'writing' FROM catalog.annex a WHERE a.annex_id = %s RETURNING placement_id",
        (annex_id, fid, ident, annex_id),
    ).fetchone()
    e.post(f"{E}/annex-placements/{{placement_id}}", {"step": "filled"}, placement_id=pid)

    called = {(m, t) for m, t, status in e.log if status < 400}
    missing = {(m, p) for m, p, _, _ in ROUTES} - called
    assert not missing, f"routes the walk did not reach successfully: {sorted(missing)}"


BODY_ROUTES = [
    (m, p)
    for m, p, _, _ in ROUTES
    if m == "POST"
    and "required"
    in spec.operation(m, p)
    .get("requestBody", {})
    .get("content", {})
    .get("application/json", {})
    .get("schema", {})
]


@pytest.mark.parametrize(("method", "template"), BODY_ROUTES)
def test_required_fields_are_enforced(make_world, method: str, template: str) -> None:
    w = make_world()
    resp = w.edge.call(
        method,
        template,
        json={},
        check=False,
        upload_id=1,
        file_id=w.root_id,
        recall_id=1,
        scan_id=1,
    )
    assert resp.status_code == 400, (template, resp.text)
    assert resp.json()["code"] == "invalid_request"
    spec.check_response(
        method, template, resp.status_code, resp.headers["content-type"], resp.content
    )
