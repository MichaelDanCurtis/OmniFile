"""Migrations (portal-api.md sections 5.17, 5.18): the lifecycle routes, who
may act, and the cutover report. Ported from line B (PR #10)."""

from __future__ import annotations

import time
import uuid

from .conftest import _acl, add_dir, add_file, seed
from .test_locations import _migrating
from .test_sources import BUCKET

OTHER_APP = "another-partner"


def _start(web, w, **kw):  # type: ignore[no-untyped-def]
    body = {"dataset_id": str(w.dataset_id), "prefix": "raw/2019/", **kw}
    return web.call("POST", "/v1/migrations", token=web.app_token(["Archive.Adopt"]), json=body)


def _other(web, roles):  # type: ignore[no-untyped-def]
    return web.idp.app_token(OTHER_APP, roles)


def _verified(sql, mid: str) -> None:
    sql.execute(
        "UPDATE catalog.migration_prefix SET state = 'verified', verified_at = now(),"
        " files_total = 0, files_verified = 0 WHERE migration_id = %s",
        (int(mid),),
    )


def test_a_migration_is_started_read_and_listed(web, sql):
    w = seed(sql)
    s3 = _migrating(sql, w)
    r = _start(web, w, cutover="manual", copier="external")
    assert r.status_code == 202, r.text
    m = r.json()
    assert (m["state"], m["cutover"], m["copier_kind"], m["mode"]) == (
        "pending", "manual", "external", "serve")  # fmt: skip
    assert (m["from_backend_id"], m["to_backend_id"]) == (s3, str(w.backend_id))
    assert m["files"]["excluded"] == 0 and m["source_deletes_pending"] == 0
    assert m["created_by"] == "app:prizmanyx-app" and m["copier"]["last_run"] is None
    (action,) = sql.execute(
        "SELECT action FROM catalog.audit_log WHERE details->>'migration_id' = %s",
        (m["migration_id"],),
    ).fetchone()
    assert action == "migration.start"
    read = web.app_token(["Archive.Read"])
    one = web.call("GET", "/v1/migrations/{migration_id}", token=read,
                   migration_id=m["migration_id"])  # fmt: skip
    assert one.status_code == 200 and one.json()["cutover"] == "manual"
    # A location is a migration too, with an automatic cutover.
    loc = web.call("POST", "/v1/locations", token=web.app_token(["Archive.Adopt"]),
                   json={"dataset_id": str(w.dataset_id), "prefix": "raw/2020/",
                         "to_backend_id": str(w.backend_id)})  # fmt: skip
    assert loc.status_code == 202
    listed = web.call("GET", "/v1/migrations", token=read,
                      params={"dataset_id": str(w.dataset_id), "limit": 1}).json()  # fmt: skip
    assert [x["prefix"] for x in listed["items"]] == ["raw/2020/"] and listed["next_cursor"]
    assert listed["items"][0]["cutover"] == "auto"
    rest = web.call("GET", "/v1/migrations", token=read,
                    params={"dataset_id": str(w.dataset_id),
                            "cursor": listed["next_cursor"]}).json()  # fmt: skip
    assert [x["migration_id"] for x in rest["items"]] == [m["migration_id"]]
    admin = web.browser(w.admin)
    assert admin.get("/v1/migrations", params={"state": "pending"}).status_code == 200
    # migration.started on the feed, with its facts.
    events = web.call("GET", "/v1/events", token=read,
                      params={"dataset_id": str(w.dataset_id),
                              "types": "migration.started"}).json()["items"]  # fmt: skip
    started = [e for e in events if e["entity_id"] == m["migration_id"]]
    assert started[0]["data"]["prefix"] == "raw/2019/" and started[0]["file"] is None
    assert started[0]["data"]["from_backend_id"] == s3


def test_start_refusals(web, sql):
    w = seed(sql)
    r = _start(web, w)
    assert r.status_code == 409 and r.json()["code"] == "migration_conflict"
    _migrating(sql, w)
    assert _start(web, w, mode="hand_off").json()["code"] == "invalid_request"
    r = web.call("POST", "/v1/migrations", token=web.app_token(["Archive.Read"]),
                 json={"dataset_id": str(w.dataset_id), "prefix": "raw/"})  # fmt: skip
    assert r.status_code == 403
    body = {"dataset_id": str(w.dataset_id), "prefix": "raw/", "copier": "rsync"}
    r = web.call("POST", "/v1/migrations", token=web.app_token(["Archive.Adopt"]), check=False,
                 json=body)  # fmt: skip
    assert r.status_code == 400
    assert _start(web, w).status_code == 202
    assert _start(web, w).json()["code"] == "migration_conflict"  # overlaps


def test_actions_are_for_the_app_that_started_it_or_an_admin(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    mid = _start(web, w, cutover="manual", copier="external").json()["migration_id"]
    other = _other(web, ["Archive.Adopt", "Archive.Read"])
    for action in ("cutover", "cancel", "retry"):
        r = web.call("POST", f"/v1/migrations/{{migration_id}}/{action}", token=other, json={},
                     migration_id=mid)  # fmt: skip
        assert r.status_code == 404, (action, r.text)
    r = web.call("POST", "/v1/migrations/{migration_id}/runs", token=other, migration_id=mid,
                 json={"run_ref": "x", "state": "running",
                       "started_at": "2026-10-15T08:00:00Z"})  # fmt: skip
    assert r.status_code == 404
    r = web.call("GET", "/v1/migrations/{migration_id}/tombstones", token=other,
                 migration_id=mid)  # fmt: skip
    assert r.status_code == 404
    # Reads are dataset-level for any reader app.
    assert web.call("GET", "/v1/migrations/{migration_id}", token=other,
                    migration_id=mid).status_code == 200  # fmt: skip
    # A steward is no ArchiveAdmin; an ArchiveAdmin may act on any migration.
    steward = web.browser(w.steward)
    assert steward.post("/v1/migrations/{migration_id}/cancel", {},
                        migration_id=mid).status_code == 403  # fmt: skip
    admin = web.browser(w.admin)
    r = admin.post("/v1/migrations/{migration_id}/cancel", {}, migration_id=mid)
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert r.json()["cancelled_at"] is not None


def test_manual_cutover(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    adopt = web.app_token(["Archive.Adopt"])
    mid = _start(web, w, cutover="manual").json()["migration_id"]

    def cutover():  # type: ignore[no-untyped-def]
        return web.call("POST", "/v1/migrations/{migration_id}/cutover", token=adopt, json={},
                        migration_id=mid)  # fmt: skip

    def queued() -> int:
        return sql.execute(
            "SELECT count(*) FROM pgmq.q_migrate WHERE message->>'migration_id' = %s", (mid,)
        ).fetchone()[0]

    before = queued()
    r = cutover()  # asked early: it waits for verification, nothing is woken
    assert r.status_code == 200 and r.json()["cutover_requested_at"] is not None
    assert r.json()["state"] == "pending" and queued() == before
    _verified(sql, mid)
    sql.execute("UPDATE catalog.migration_prefix SET cutover_requested_at = NULL"
                " WHERE migration_id = %s", (int(mid),))  # fmt: skip
    assert cutover().status_code == 200 and queued() == before + 1  # verified: woken
    again = cutover()
    assert again.status_code == 200 and queued() == before + 1  # asked twice: the same
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'migration.cutover'"
        " AND details->>'migration_id' = %s", (mid,)).fetchone()  # fmt: skip
    assert n == 2
    auto = _start(web, w, prefix="raw/2020/").json()["migration_id"]
    r = web.call("POST", "/v1/migrations/{migration_id}/cutover", token=adopt, json={},
                 migration_id=auto)  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "migration_conflict"
    r = web.call("POST", "/v1/migrations/{migration_id}/cutover", token=adopt, check=False,
                 json={"now": True}, migration_id=mid)  # fmt: skip
    assert r.status_code == 400


def test_cancel_and_start_again(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    adopt = web.app_token(["Archive.Adopt"])
    mid = _start(web, w).json()["migration_id"]
    r = web.call("POST", "/v1/migrations/{migration_id}/cancel", token=adopt, json={},
                 migration_id=mid)  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    r = web.call("POST", "/v1/migrations/{migration_id}/cancel", token=adopt, json={},
                 migration_id=mid)  # fmt: skip
    assert r.status_code == 409
    # The prefix is free again; the location view shows the cancelled one.
    again = _start(web, w)
    assert again.status_code == 202 and again.json()["migration_id"] != mid
    loc = web.call("GET", "/v1/locations/{migration_id}", token=adopt, migration_id=mid)
    assert loc.json()["state"] == "cancelled"
    read = web.app_token(["Archive.Read"])
    cancelled = web.call("GET", "/v1/events", token=read,
                         params={"dataset_id": str(w.dataset_id),
                                 "types": "migration.cancelled"}).json()["items"]  # fmt: skip
    assert [e["entity_id"] for e in cancelled] == [mid]


def test_retry_a_failed_migration(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    adopt = web.app_token(["Archive.Adopt"])
    mid = _start(web, w).json()["migration_id"]
    r = web.call("POST", "/v1/migrations/{migration_id}/retry", token=adopt, json={},
                 migration_id=mid)  # fmt: skip
    assert r.status_code == 409  # only a failed one
    sql.execute("UPDATE catalog.migration_prefix SET state = 'failed', last_error = 'boom'"
                " WHERE migration_id = %s", (int(mid),))  # fmt: skip
    r = web.call("POST", "/v1/migrations/{migration_id}/retry", token=adopt, json={},
                 migration_id=mid)  # fmt: skip
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["last_error"]) == ("pending", None)
    params = {"state": "failed", "dataset_id": str(w.dataset_id)}
    failed = web.call("GET", "/v1/migrations", token=adopt, params=params).json()
    assert failed["items"] == []


def test_an_external_copier_reports_its_runs(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    adopt = web.app_token(["Archive.Adopt"])
    mid = _start(web, w, copier="external").json()["migration_id"]

    def report(**kw):  # type: ignore[no-untyped-def]
        body = {"run_ref": "pz-1", "state": "running", "started_at": "2026-10-15T08:00:00Z",
                **kw}  # fmt: skip
        return web.call("POST", "/v1/migrations/{migration_id}/runs", token=adopt, json=body,
                        migration_id=mid)  # fmt: skip

    r = report(counters={"copied": 10})
    assert r.status_code == 202, r.text
    m = r.json()
    assert m["copier"]["runs_reported"] == 1
    assert m["copier"]["last_run"]["run_ref"] == "pz-1"
    assert m["copier"]["last_run"]["counters"] == {"copied": 10}
    r = report(state="succeeded", finished_at="2026-10-15T09:00:00Z", counters={"copied": 12})
    assert r.json()["copier"]["runs_reported"] == 1  # the same run, replaced
    # A run copying again sends a verified prefix back to verifying.
    _verified(sql, mid)
    r = report(run_ref="pz-2", started_at="2026-10-15T10:00:00Z")
    assert r.json()["state"] == "verifying" and r.json()["verified_at"] is None
    assert r.json()["copier"]["last_run"]["started_at"] == "2026-10-15T10:00:00.000Z"
    r = web.call("POST", "/v1/migrations/{migration_id}/runs", token=adopt, check=False,
                 json={"run_ref": "x", "state": "done", "started_at": "2026-10-15T08:00:00Z"},
                 migration_id=mid)  # fmt: skip
    assert r.status_code == 400
    auto = _start(web, w, prefix="raw/2020/").json()["migration_id"]
    r = web.call("POST", "/v1/migrations/{migration_id}/runs", token=adopt, migration_id=auto,
                 json={"run_ref": "x", "state": "running",
                       "started_at": "2026-10-15T08:00:00Z"})  # fmt: skip
    assert r.status_code == 409


def _s3_file(sql, w, s3: str, name: str, key: str, *, version: str | None = None,
             etag: str | None = '"e1"') -> int:  # fmt: skip
    """A file whose bytes are an object of the source bucket."""
    fid = add_file(sql, w, name, tier="hot", state="remote")
    sql.execute(
        "UPDATE catalog.location SET backend_id = %s, container = %s, object_key = %s,"
        " backend_version_id = %s, etag = %s WHERE file_id = %s",
        (int(s3), BUCKET, key, version, etag, fid),
    )
    return fid


def _delete(sql, w, fid: int, *, edge: bool = True) -> None:
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    sql.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size, edge_id, edge_op_id)"
        " SELECT file_id, dataset_id, parent_id, name, 'tombstone', 'committed', now(), size,"
        " %s, %s FROM catalog.file WHERE file_id = %s",
        (w.edge_id if edge else None, f"op-{fid}" if edge else None, fid),
    )


def test_the_cutover_report_lists_deletes_never_applied(web, sql):
    w = seed(sql)
    s3 = _migrating(sql, w)
    gone = _s3_file(sql, w, s3, "line_1042.segy", "raw/2019/line_1042.segy")
    versioned = _s3_file(sql, w, s3, "line_7.segy", "raw/2019/line_7.segy", version="v3")
    _s3_file(sql, w, s3, "kept.segy", "raw/2019/kept.segy")
    from_s3 = _s3_file(sql, w, s3, "s3gone.segy", "raw/2019/s3gone.segy")
    outside = _s3_file(sql, w, s3, "other.segy", "raw/2020/other.segy")
    for fid in (gone, versioned, outside):
        _delete(sql, w, fid)
    _delete(sql, w, from_s3, edge=False)  # the change feed's: already done in S3
    mid = _start(web, w).json()["migration_id"]
    adopt = web.app_token(["Archive.Adopt"])
    first = web.call("GET", "/v1/migrations/{migration_id}/tombstones", token=adopt,
                     migration_id=mid, params={"limit": 1})  # fmt: skip
    assert first.status_code == 200, first.text
    rep = first.json()
    assert rep["applied_by_omnifile"] is False and rep["source_deletes_pending"] == 2
    (item,) = rep["items"]
    assert item["file_id"] == str(gone) and item["path"] == "/line_1042.segy"
    assert item["source"] == {"backend_id": s3, "container": BUCKET,
                              "key": "raw/2019/line_1042.segy", "backend_version_id": None,
                              "etag": '"e1"', "verify_etag_before_delete": True}  # fmt: skip
    assert item["deleted_by"] == {"kind": "edge", "edge_id": str(w.edge_id),
                                  "identity_id": None, "proof": "unknown"}  # fmt: skip
    rest = web.call("GET", "/v1/migrations/{migration_id}/tombstones", token=adopt,
                    migration_id=mid, params={"cursor": rep["next_cursor"]}).json()  # fmt: skip
    (item2,) = rest["items"]
    assert item2["source"]["backend_version_id"] == "v3"
    assert item2["source"]["verify_etag_before_delete"] is False and rest["next_cursor"] is None
    one = web.call("GET", "/v1/migrations/{migration_id}", token=adopt, migration_id=mid)
    assert one.json()["source_deletes_pending"] == 2
    # An ArchiveAdmin with an AD identity reads the full list too (owner, 2026-11-04).
    admin = web.browser(w.admin)
    r = admin.get("/v1/migrations/{migration_id}/tombstones", migration_id=mid)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 2 and r.json()["source_deletes_pending"] == 2
    # On cut_over, the event says how many deletes are waiting.
    sql.execute(
        "UPDATE catalog.migration_prefix SET state = 'cut_over', verified_at = now(),"
        " cut_over_at = now(), files_total = 0, files_verified = 0 WHERE migration_id = %s",
        (int(mid),),
    )
    read = web.app_token(["Archive.Read"])
    # The feed is read below its horizon (a transaction still open anywhere,
    # another test's under load, holds an event back until it commits), so
    # the event can take a moment to show: asked again until it does.
    deadline = time.monotonic() + 20
    while True:
        cut = web.call("GET", "/v1/events", token=read,
                       params={"dataset_id": str(w.dataset_id),
                               "types": "migration.cut_over"}).json()["items"]  # fmt: skip
        if cut or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert cut, "the cut_over event never showed"
    assert cut[0]["data"]["source_deletes_pending"] == 2
    assert cut[0]["kind"] == "migration.cut_over" and cut[0]["entity"] == "migration"


def test_an_admin_session_reads_every_row_and_is_audited(web, sql):
    """Owner, 2026-11-04: a signed-in ArchiveAdmin reads the full list (keys
    and paths, never contents), even rows for files they can't read or that
    no share covers. Every read that returns rows is audited. A steward
    gets 403."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    open_ = _s3_file(sql, w, s3, "open.segy", "raw/2019/open.segy")
    secret = _s3_file(sql, w, s3, "secret.segy", "raw/2019/secret.segy")
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (w.acl_secret, secret))
    hidden_dir = add_dir(sql, w, "owners-only", acl=w.acl_secret)
    under = _s3_file(sql, w, s3, "under.segy", "raw/2019/under.segy")
    sql.execute("UPDATE catalog.file SET parent_id = %s WHERE file_id = %s", (hidden_dir, under))
    for fid in (open_, secret, under):
        _delete(sql, w, fid)
    mid = _start(web, w).json()["migration_id"]
    reader_admin = w.user("reader-admin", roles=["ArchiveAdmin"], groups=[w.g_readers],
                          sid_rid=3011)  # fmt: skip
    sql.execute("DELETE FROM catalog.share WHERE dataset_id = %s", (w.dataset_id,))
    rep = web.browser(reader_admin).get("/v1/migrations/{migration_id}/tombstones",
                                        migration_id=mid).json()  # fmt: skip
    assert sorted(i["path"] for i in rep["items"]) == [
        "/open.segy", "/owners-only/under.segy", "/secret.segy"]  # fmt: skip
    assert rep["source_deletes_pending"] == 3 and rep["applied_by_omnifile"] is False
    (actor, details) = sql.execute(
        "SELECT actor, details FROM catalog.audit_log"
        " WHERE action = 'migration.tombstones.read' AND details->>'migration_id' = %s"
        " ORDER BY audit_id DESC LIMIT 1", (mid,)).fetchone()  # fmt: skip
    assert actor.startswith("user:")
    assert (details["rows"], details["first_page"]) == (3, True)
    steward = web.browser(w.steward)
    assert steward.get("/v1/migrations/{migration_id}/tombstones", migration_id=mid,
                       check=False).status_code == 403  # fmt: skip


def test_an_admin_without_an_ad_sid_reads_no_rows(web, sql):
    """Review of #41: a cloud-only ArchiveAdmin (no on-premises SID) matched
    an Everyone ACL. No AD identity reads nothing (owner, 2026-11-04), and
    nothing is audited as read."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    world = _acl(sql, w.dataset_id, ["S-1-1-0"])
    fid = _s3_file(sql, w, s3, "pub.segy", "raw/2019/pub.segy")
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (world, fid))
    _delete(sql, w, fid)
    mid = _start(web, w).json()["migration_id"]
    nosid = web.browser(w.user("cloud-admin", roles=["ArchiveAdmin"], sid=None))
    rep = nosid.get("/v1/migrations/{migration_id}/tombstones", migration_id=mid).json()
    assert rep["items"] == [] and rep["source_deletes_pending"] == 1
    # Audited all the same: every read of the list is (owner, 2026-11-04).
    rows = sql.execute(
        "SELECT details->>'rows' FROM catalog.audit_log WHERE action = 'migration.tombstones.read'"
        " AND details->>'migration_id' = %s", (mid,)).fetchall()  # fmt: skip
    assert rows == [("0",)]
    # An admin with a SID does read it.
    rep = web.browser(w.admin).get("/v1/migrations/{migration_id}/tombstones",
                                   migration_id=mid).json()  # fmt: skip
    assert [i["file_id"] for i in rep["items"]] == [str(fid)]


def test_a_portal_started_report_is_read_in_full_by_a_registered_client(web, sql):
    """Review of #41: a migration started in the portal has no app of its own.
    A registered API client with Archive.Adopt reads its full report (owner,
    2026-11-04); Archive.Read alone gets 404. So does the ArchiveAdmin."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    secret = _s3_file(sql, w, s3, "s.segy", "raw/2019/s.segy")
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (w.acl_secret, secret))
    _delete(sql, w, secret)
    admin = web.browser(w.admin)
    r = admin.post("/v1/migrations", {"dataset_id": str(w.dataset_id), "prefix": "raw/2019/"})
    assert r.status_code == 202, r.text
    mid = r.json()["migration_id"]
    rep = admin.get("/v1/migrations/{migration_id}/tombstones", migration_id=mid).json()
    assert rep["source_deletes_pending"] == 1
    assert [i["file_id"] for i in rep["items"]] == [str(secret)]
    full = web.call("GET", "/v1/migrations/{migration_id}/tombstones",
                    token=web.app_token(["Archive.Adopt"]), migration_id=mid)  # fmt: skip
    assert full.status_code == 200 and [i["file_id"] for i in full.json()["items"]] == [str(secret)]
    read = web.call("GET", "/v1/migrations/{migration_id}/tombstones",
                    token=web.app_token(["Archive.Read"]), migration_id=mid)  # fmt: skip
    assert read.status_code == 404
    # An app's own migration stays its own: another app gets 404.
    own = _start(web, w, prefix="raw/2020/").json()["migration_id"]
    other = web.call("GET", "/v1/migrations/{migration_id}/tombstones",
                     token=_other(web, ["Archive.Adopt"]), migration_id=own)  # fmt: skip
    assert other.status_code == 404


def test_only_a_registered_client_reads_a_portal_started_report(web, sql):
    """WP11's registry (#43): an unregistered app never reaches the route
    (403 client_not_registered); a registered one whose scopes don't include
    Archive.Adopt gets 404 even when its token claims it; and the check
    itself needs a registered client (`api_client`), not just the role."""
    import asyncio

    from omnifile_service.web.context import Principal
    from omnifile_service.web.routes_migrations import _registered_client

    w = seed(sql)
    s3 = _migrating(sql, w)
    fid = _s3_file(sql, w, s3, "x.segy", "raw/2019/x.segy")
    _delete(sql, w, fid)
    admin = web.browser(w.admin)
    mid = admin.post("/v1/migrations", {"dataset_id": str(w.dataset_id),
                                        "prefix": "raw/2019/"}).json()["migration_id"]  # fmt: skip
    path = "/v1/migrations/{migration_id}/tombstones"
    unregistered = web.idp.app_token("never-registered", ["Archive.Adopt"])
    r = web.call("GET", path, token=unregistered, migration_id=mid, check=False)
    assert r.status_code == 403 and r.json()["code"] == "client_not_registered"
    # A client of its own (the shared test apps keep their scopes for other tests).
    app = f"read-only-{uuid.uuid4().hex[:8]}"
    sql.execute(
        "INSERT INTO catalog.api_client (app_id, display_name, scopes, owner_identity_id,"
        " created_by_identity_id) SELECT %s, 'Read only', '{Archive.Read}', owner_identity_id,"
        " owner_identity_id FROM catalog.api_client WHERE app_id = 'prizmanyx-app'",
        (app,),
    )
    claims = web.idp.app_token(app, ["Archive.Adopt", "Archive.Read"])
    r = web.call("GET", path, token=claims, migration_id=mid, check=False)
    assert r.status_code == 404
    r = web.call("GET", path, token=web.app_token(["Archive.Adopt"]), migration_id=mid)
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    # The role alone is not enough: no registered client, no full report.
    bare = Principal(kind="app", roles=frozenset({"Archive.Adopt"}), app_id="x")
    assert asyncio.run(_registered_client(None, bare)) is False
    registered = Principal(kind="app", roles=frozenset({"Archive.Adopt"}), app_id="x",
                           api_client={"api_client_id": 1, "owner_identity_id": 2})  # fmt: skip
    assert asyncio.run(_registered_client(None, registered)) is True


def test_an_api_client_never_reads_another_clients_report(web, sql):
    """Partner isolation (the lead's reading of the owner's rule): even with
    Archive.Adopt and Archive.Read, another app gets 404 for a migration an
    app started, and its attempt reads nothing. The starting app reads it."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    fid = _s3_file(sql, w, s3, "x.segy", "raw/2019/x.segy")
    _delete(sql, w, fid)
    mid = _start(web, w).json()["migration_id"]
    token = _other(web, ["Archive.Adopt", "Archive.Read"])
    other = web.call("GET", "/v1/migrations/{migration_id}/tombstones", token=token,
                     migration_id=mid)  # fmt: skip
    assert other.status_code == 404 and "items" not in other.json()
    (audited,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'migration.tombstones.read'"
        " AND details->>'migration_id' = %s", (mid,)).fetchone()  # fmt: skip
    assert audited == 0
    own = web.call("GET", "/v1/migrations/{migration_id}/tombstones",
                   token=web.app_token(["Archive.Read"]), migration_id=mid)  # fmt: skip
    assert [i["file_id"] for i in own.json()["items"]] == [str(fid)]


def test_sources_say_whether_a_migration_is_under_way(web, sql):
    """Review of #41: the portal's Start migration offer came from the first
    500 migrations. Each source now carries `active_migrations` (not counting
    cancelled ones)."""
    w = seed(sql)
    _migrating(sql, w)
    read = web.app_token(["Archive.Read"])

    def active() -> int:
        (s,) = web.call("GET", "/v1/sources", token=read,
                        params={"dataset_id": str(w.dataset_id)}).json()["items"]  # fmt: skip
        return s["active_migrations"]

    assert active() == 0
    mid = _start(web, w).json()["migration_id"]
    assert active() == 1
    web.browser(w.admin).post("/v1/migrations/{migration_id}/cancel", {}, migration_id=mid)
    assert active() == 0


def test_the_cutover_report_names_who_deleted(web, sql):
    """deleted_by fills identity_id and proof from the actor the edge sent
    with the unlink (control-api.md 4.1, migration 0030); unknown when the
    edge's reconcile pass found the delete."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    (ident,) = sql.execute(
        "INSERT INTO catalog.identity (sid, posix_id) VALUES ('S-1-5-21-7-7-7-1107', 20002)"
        " RETURNING identity_id"
    ).fetchone()
    by_user = _s3_file(sql, w, s3, "a.segy", "raw/2019/a.segy")
    found = _s3_file(sql, w, s3, "b.segy", "raw/2019/b.segy")
    for fid in (by_user, found):
        _delete(sql, w, fid)
    sql.execute(
        "UPDATE catalog.file_version SET actor_via = 'nfs', actor_uid = 20002,"
        " actor_sid = 'S-1-5-21-7-7-7-1107', actor_sec = 'krb5', actor_proof = 'proven',"
        " actor_identity_id = %s WHERE file_id = %s AND kind = 'tombstone'",
        (ident, by_user),
    )
    sql.execute(
        "UPDATE catalog.file_version SET actor_via = 'unknown', actor_proof = 'unknown'"
        " WHERE file_id = %s AND kind = 'tombstone'",
        (found,),
    )
    mid = _start(web, w).json()["migration_id"]
    rep = web.call("GET", "/v1/migrations/{migration_id}/tombstones",
                   token=web.app_token(["Archive.Adopt"]), migration_id=mid).json()  # fmt: skip
    by = {i["file_id"]: i["deleted_by"] for i in rep["items"]}
    assert by[str(by_user)] == {"kind": "edge", "edge_id": str(w.edge_id),
                                "identity_id": str(ident), "proof": "proven"}  # fmt: skip
    assert by[str(found)] == {"kind": "edge", "edge_id": str(w.edge_id),
                              "identity_id": None, "proof": "unknown"}  # fmt: skip


def test_an_admin_excludes_objects_that_cannot_be_verified(web, sql):
    w = seed(sql)
    s3 = _migrating(sql, w)
    _s3_file(sql, w, s3, "sse_c.segy", "raw/2019/sse_c.segy")
    mid = _start(web, w).json()["migration_id"]
    body = {"object_keys": ["raw/2019/sse_c.segy", "raw/2019/none.segy", "raw/2020/x"],
            "reason": "SSE-C: S3 can't give a checksum"}  # fmt: skip
    # Never a partner.
    r = web.call("POST", "/v1/migrations/{migration_id}/exclude", token=web.app_token(
        ["Archive.Adopt"]), json=body, migration_id=mid)  # fmt: skip
    assert r.status_code in (401, 403)
    admin = web.browser(w.admin)
    admin.sign_in(w.admin, reauth=True)
    r = admin.post("/v1/migrations/{migration_id}/exclude", body, migration_id=mid)
    assert r.status_code == 200, r.text
    assert r.json()["files"]["excluded"] == 1  # the others aren't the prefix's live files
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'migration.exclude'"
        " AND details->>'migration_id' = %s", (mid,)).fetchone()  # fmt: skip
    assert (details["requested"], details["excluded"]) == (3, 1)
    bad = admin.post("/v1/migrations/{migration_id}/exclude", {"object_keys": [], "reason": "x"},
                     migration_id=mid, check=False)  # fmt: skip
    assert bad.status_code == 400


def test_a_migration_shows_its_stuck_copies_as_numbers_to_those_who_may_see_it(web, sql):
    """After a give-up, the Migrations page had a stuck migration and no reason. A count says
    why, and carries no key or provider text. A DataSteward doesn't read migrations at all."""
    w = seed(sql)
    _migrating(sql, w)
    mid = _start(web, w, cutover="manual", copier="external").json()["migration_id"]
    admin = web.browser(w.admin)
    path = "/v1/migrations/{migration_id}"
    # Not counted yet: nothing to say.
    assert admin.get(path, migration_id=mid).json()["stuck_copies"] == {
        "given_up": 0, "stranded": 0, "no_checksum": 0}  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.migration_count (migration_id, counted_state, bytes_total,"
        " bytes_verified, failing_now, archived_waiting, restores_expiring, counted_at,"
        " copies_given_up, copies_stranded, copies_no_checksum)"
        " VALUES (%s, 'verifying', 0, 0, 0, 0, 0, now(), 3, 2, 1)",
        (int(mid),),
    )
    stuck = {"given_up": 3, "stranded": 2, "no_checksum": 1}
    assert admin.get(path, migration_id=mid).json()["stuck_copies"] == stuck
    read = web.app_token(["Archive.Read"])
    listed = web.call("GET", "/v1/migrations", token=read,
                      params={"dataset_id": str(w.dataset_id)}).json()["items"]  # fmt: skip
    assert [m["stuck_copies"] for m in listed if m["migration_id"] == mid] == [stuck]
    # And the console's row carries them too.
    (row,) = [m for m in admin.get("/v1/migration-console").json()["migrations"]
              if m["migration_id"] == mid]  # fmt: skip
    assert row["stuck_copies"] == stuck and row["health"] == "attention"
    steward = web.browser(w.steward)
    assert steward.get(path, migration_id=mid).status_code == 403
    assert steward.get("/v1/migrations").status_code == 403
    assert steward.get("/v1/migration-console").status_code == 403
