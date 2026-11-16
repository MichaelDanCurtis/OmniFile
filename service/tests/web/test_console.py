"""The migrations console and staged plans (portal-api.md 5.22)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from omnifile_service.migrate.progress import progress_sweep
from omnifile_service.migrate.storage_mover import AzureStorageMover, StorageMoverConfig
from omnifile_service.web.routes_console import _STUCK_PAGE

from .conftest import seed
from .test_locations import _migrating
from .test_migrations import _s3_file, _start

T = "2026-11-10T22:00:00Z"


def _wave(**kw):  # type: ignore[no-untyped-def]
    return {"prefixes": ["raw/2019/"], "start_at": T, **kw}


def _plan(admin, w, waves, **kw):  # type: ignore[no-untyped-def]
    return admin.post("/v1/migration-plans", {"dataset_id": str(w.dataset_id), "name": "By year",
                                              "waves": waves, **kw}, check=False)  # fmt: skip


def _count(db_url: str, now: datetime) -> None:
    """The worker's progress sweep (migrate/progress.py), at `now`."""

    async def go() -> None:
        conn = await psycopg.AsyncConnection.connect(db_url, autocommit=True)
        try:
            await progress_sweep(conn, now)
        finally:
            await conn.close()

    asyncio.run(go())


def _row(admin, mid: str) -> dict:  # type: ignore[no-untyped-def]
    (row,) = [m for m in admin.get("/v1/migration-console").json()["migrations"]
              if m["migration_id"] == mid]  # fmt: skip
    return row


def test_the_console_reads_rolled_up_progress(web, sql, db_url):
    """Review of #45: the console reads counters the worker keeps (no scan
    per request); throughput is the difference between its samples."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    fid = _s3_file(sql, w, s3, "a.segy", "raw/2019/a.segy")
    _s3_file(sql, w, s3, "b.segy", "raw/2019/b.segy")
    sql.execute("UPDATE catalog.location SET size = 1000 WHERE backend_id = %s", (int(s3),))
    mid = _start(web, w).json()["migration_id"]
    admin = web.browser(w.admin)
    row = _row(admin, mid)
    assert row["counted_at"] is None and row["eta"]["at"] is None
    assert "not counted yet" in row["eta"]["basis"]
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    _count(db_url, t0)
    # One copy verified since, and one that failed its check and was removed.
    (vid,) = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.location (version_id, file_id, backend_id, container, object_key,"
        " role, state, size, verified, verified_at, committed_at, verified_by, crc64nvme)"
        " VALUES (%s, %s, %s, 'live/data',"
        " 'x/a', 'secondary', 'committed', 1000, true, now() - interval '1 minute', now(),"
        " 'azure_read_crc', '\\x0123456789abcdef'::bytea)",
        (vid, fid, w.backend_id),
    )
    (other,) = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE name = 'b.segy' AND dataset_id = %s",
        (w.dataset_id,)).fetchone()  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.location (version_id, file_id, backend_id, container, object_key,"
        " role, state, size, committed_at) SELECT %s, file_id, %s, 'live/data', 'x/b',"
        " 'secondary', 'deleted', 1000, now() FROM catalog.file WHERE content_version_id = %s",
        (other, w.backend_id, other),
    )
    sql.execute("UPDATE catalog.migration_prefix SET files_failed = 7, last_error ="
                " 'AccessDenied: s3:GetObject' WHERE migration_id = %s", (int(mid),))  # fmt: skip
    _count(db_url, datetime.now(UTC))
    row = _row(admin, mid)
    assert row["bytes"] == {"total": 2000, "verified": 1000}
    assert row["failing_now"] == 1 and row["files"]["failed"] == 7  # now, not ever
    assert row["throughput"]["files_per_h"] == 2 and row["health"] == "attention"
    assert row["eta"]["at"] is not None and "last hour" in row["eta"]["basis"]
    att = admin.get("/v1/migration-console").json()["attention"]
    kinds = {(a["kind"], tuple(a["actions"])) for a in att if a["migration_id"] == mid}
    assert ("verify_failed", ("rerun", "exclude")) in kinds and ("access", ("settings",)) in kinds
    # Objects still in Deep Archive: no time is promised.
    sql.execute("UPDATE catalog.location SET storage_class = 'DEEP_ARCHIVE' WHERE backend_id = %s",
                (int(s3),))  # fmt: skip
    _count(db_url, datetime.now(UTC) + timedelta(seconds=1))
    row = _row(admin, mid)
    assert row["eta"]["at"] is None and "archive class" in row["eta"]["basis"]
    # No samples in the last hour: no throughput, no time.
    sql.execute("DELETE FROM catalog.migration_progress WHERE migration_id = %s", (int(mid),))
    sql.execute("UPDATE catalog.location SET storage_class = NULL WHERE backend_id = %s",
                (int(s3),))  # fmt: skip
    _count(db_url, datetime.now(UTC) + timedelta(seconds=2))
    row = _row(admin, mid)
    assert row["throughput"] is None
    assert row["eta"] == {"at": None, "basis": "no copy was verified in the last hour"}


def test_copy_again_is_refused_when_every_file_is_verified(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    mid = _start(web, w).json()["migration_id"]
    sql.execute(
        "UPDATE catalog.migration_prefix SET state = 'verified', verified_at = now(),"
        " files_total = 0, files_verified = 0 WHERE migration_id = %s",
        (int(mid),),
    )
    r = web.browser(w.admin).post("/v1/migrations/{migration_id}/rerun", {}, migration_id=mid,
                                  check=False)  # fmt: skip
    assert r.status_code == 409 and "nothing to copy" in r.json()["detail"]


def test_a_waves_migrations_are_the_plans(web, sql):
    """Review of #45: an API client whose plan was approved can't cut over,
    cancel or retry the wave's migrations; the admin does it on the plan."""
    w = seed(sql)
    _migrating(sql, w)
    (owner,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', ARRAY[%s])"
        " RETURNING identity_id", (f"{w.domain}-4405", w.g_owners)).fetchone()  # fmt: skip
    adopt = web.app_token(["Archive.Adopt"])
    p = web.call("POST", "/v1/migration-plans", token=adopt, json={
        "dataset_id": str(w.dataset_id), "name": "p", "owner_identity_id": str(owner),
        "waves": [_wave(start_at="2026-01-01T00:00:00Z")]}).json()  # fmt: skip
    admin = web.browser(w.admin)
    assert admin.post("/v1/migration-plans/{plan_id}/approve", {},
                      plan_id=p["plan_id"]).status_code == 200  # fmt: skip
    from omnifile_service.migrate.engine import request_location  # noqa: F401  (the sweep's)

    (wave,) = sql.execute("SELECT wave_id FROM catalog.migration_wave WHERE plan_id = %s",
                          (int(p["plan_id"]),)).fetchone()  # fmt: skip
    mid = _start(web, w, prefix="raw/2019/").json()["migration_id"]
    # Made by the plan: as the sweep makes them.
    sql.execute("UPDATE catalog.migration_prefix SET requested_by = 'system:plans'"
                " WHERE migration_id = %s", (int(mid),))  # fmt: skip
    sql.execute("INSERT INTO catalog.migration_wave_prefix VALUES (%s, 'raw/2019/', %s)",
                (wave, int(mid)))  # fmt: skip
    sql.execute("UPDATE catalog.migration_wave SET state = 'started' WHERE wave_id = %s", (wave,))
    for action in ("cutover", "cancel", "retry"):
        r = web.call("POST", f"/v1/migrations/{{migration_id}}/{action}", token=adopt, json={},
                     migration_id=mid, check=False)  # fmt: skip
        assert r.status_code == 404, (action, r.text)
        r = admin.post(f"/v1/migrations/{{migration_id}}/{action}", {}, migration_id=mid,
                       check=False)  # fmt: skip
        assert r.status_code == 409 and "on the plan" in r.json()["detail"], action
    one = admin.get("/v1/migrations/{migration_id}", migration_id=mid).json()
    assert one["plan"] == {"plan_id": p["plan_id"], "seq": 1}
    # Through the plan: cancel, then the sweep's retry path for a failed one.
    r = admin.post("/v1/migration-plans/{plan_id}/waves/{seq}/cancel", {}, plan_id=p["plan_id"],
                   seq=1)  # fmt: skip
    assert r.status_code == 200, r.text
    (state,) = sql.execute("SELECT state FROM catalog.migration_prefix WHERE migration_id = %s",
                           (int(mid),)).fetchone()  # fmt: skip
    assert state == "cancelled"
    sql.execute("UPDATE catalog.migration_prefix SET state = 'failed', cancelled_at = NULL"
                " WHERE migration_id = %s", (int(mid),))  # fmt: skip
    r = admin.post("/v1/migration-plans/{plan_id}/waves/{seq}/retry", {}, plan_id=p["plan_id"],
                   seq=1)  # fmt: skip
    assert r.status_code == 200
    (state,) = sql.execute("SELECT state FROM catalog.migration_prefix WHERE migration_id = %s",
                           (int(mid),)).fetchone()  # fmt: skip
    assert state == "pending"


def test_the_console_is_for_archive_admins(web, sql):
    w = seed(sql)
    assert web.browser(w.steward).get("/v1/migration-console").status_code == 403
    r = web.call("GET", "/v1/migration-console", token=web.app_token(["Archive.Read"]),
                 check=False)  # fmt: skip
    assert r.status_code == 401
    assert web.browser(w.steward).get("/v1/migration-agents").status_code == 403


def test_agents_drill_down_and_the_agents_view(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    mid = _start(web, w).json()["migration_id"]
    sql.execute(
        "INSERT INTO catalog.mover_agent (name, status, version, host, polled_at)"
        " VALUES ('agent-houston', 'Offline', '3.1', '10.2.0.4', now()) ON CONFLICT (name) DO"
        " UPDATE SET status = 'Offline', polled_at = now()"
    )
    sql.execute(
        "INSERT INTO catalog.restore_wave (migration_id, backend_id, job_id, objects, speed, days,"
        " requested_at) SELECT %s, s.backend_id, 'job-1', 3, 'bulk', 14, now()"
        " FROM catalog.migration_prefix m JOIN catalog.source s USING (source_id)"
        " WHERE m.migration_id = %s",
        (int(mid), int(mid)),
    )
    admin = web.browser(w.admin)
    work = admin.get("/v1/migrations/{migration_id}/agents", migration_id=mid).json()
    assert work["storage_mover"]["agentless"] is True
    assert "agent-houston" in [a["name"] for a in work["storage_mover"]["agents"]]
    assert work["restore_waves"][0]["objects"] == 3 and work["edge"]["edge_id"] == str(w.edge_id)
    view = admin.get("/v1/migration-agents").json()
    assert view["polled"] is True and mid in [j["migration_id"] for j in view["jobs"]]
    att = admin.get("/v1/migration-console").json()["attention"]
    assert any(a["kind"] == "agent_offline" and a["agent"] == "agent-houston" for a in att)


def test_copy_again_and_restore_again_ask_the_engine(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    mid = _start(web, w).json()["migration_id"]
    admin = web.browser(w.admin)
    r = admin.post("/v1/migrations/{migration_id}/rerun", {}, migration_id=mid)
    assert r.status_code == 200, r.text
    r = admin.post("/v1/migrations/{migration_id}/restore-again", {}, migration_id=mid)
    assert r.status_code == 200
    rerun, restore = sql.execute(
        "SELECT rerun_requested_at, restore_requested_at FROM catalog.migration_prefix"
        " WHERE migration_id = %s", (int(mid),)).fetchone()  # fmt: skip
    assert rerun is not None and restore is not None
    audit = sql.execute(
        "SELECT action FROM catalog.audit_log WHERE details->>'migration_id' = %s", (mid,)
    ).fetchall()
    actions = {a for (a,) in audit}
    assert {"migration.rerun", "migration.restore_again"} <= actions
    ext = _start(web, w, prefix="raw/2020/", copier="external").json()["migration_id"]
    r = admin.post("/v1/migrations/{migration_id}/rerun", {}, migration_id=ext, check=False)
    assert r.status_code == 409
    steward = web.browser(w.steward)
    r = steward.post("/v1/migrations/{migration_id}/rerun", {}, migration_id=mid, check=False)
    assert r.status_code == 403


def test_an_admins_plan_is_approved_and_an_apps_is_a_proposal(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    admin = web.browser(w.admin)
    r = _plan(admin, w, [_wave(window={"start": "22:00", "end": "06:00"}),
                         _wave(prefixes=["raw/2020/"], after=1, bandwidth_mbps=500)])  # fmt: skip
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["state"] == "approved" and [x["seq"] for x in p["waves"]] == [1, 2]
    assert p["waves"][0]["window"] == {"start": "22:00", "end": "06:00"}
    assert p["waves"][1]["after"] == 1 and p["waves"][1]["bandwidth_mbps"] == 500
    # An API client's plan is a proposal, with a named owner in the source's groups.
    (owner,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', ARRAY[%s])"
        " RETURNING identity_id", (f"{w.domain}-4401", w.g_owners)).fetchone()  # fmt: skip
    body = {"dataset_id": str(w.dataset_id), "name": "API plan",
            "waves": [_wave(prefixes=["raw/2021/"])]}  # fmt: skip
    adopt = web.app_token(["Archive.Adopt"])
    r = web.call("POST", "/v1/migration-plans", token=adopt, json=body, check=False)
    assert r.status_code == 400  # no owner named
    r = web.call("POST", "/v1/migration-plans", token=adopt,
                 json={**body, "owner_identity_id": str(owner)})  # fmt: skip
    assert r.status_code == 201, r.text
    proposal = r.json()
    assert proposal["state"] == "proposed"
    approved = admin.post("/v1/migration-plans/{plan_id}/approve", {"note": "ok"},
                          plan_id=proposal["plan_id"])  # fmt: skip
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    # An outsider can't be the owner.
    (outsider,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', ARRAY[%s])"
        " RETURNING identity_id", (f"{w.domain}-4402", w.g_other)).fetchone()  # fmt: skip
    r = web.call("POST", "/v1/migration-plans", token=adopt, check=False,
                 json={**body, "name": "x", "waves": [_wave(prefixes=["raw/2022/"])],
                       "owner_identity_id": str(outsider)})  # fmt: skip
    assert r.status_code == 400 and "Owners or Writers" in r.json()["detail"]
    # An app reads its own plans only.
    listed = web.call("GET", "/v1/migration-plans", token=web.app_token(["Archive.Read"])).json()
    assert proposal["plan_id"] in [x["plan_id"] for x in listed["items"]]
    assert p["plan_id"] not in [x["plan_id"] for x in listed["items"]]
    assert {x["proposed_by"] for x in listed["items"]} == {"app:prizmanyx-app"}


def test_an_api_plan_follows_the_api_client_rules(web, sql):
    """As for a policy proposal (5.21): the client's owner must see the
    dataset, and nobody answerable for the client approves its plan."""
    import uuid

    w = seed(sql)
    _migrating(sql, w)
    admin = web.browser(w.admin)
    admin.get("/v1/me")  # the admin's identity row exists once signed in
    (admin_id,) = sql.execute("SELECT identity_id FROM catalog.identity WHERE sid = %s",
                              (w.admin["sid"],)).fetchone()  # fmt: skip
    (owner,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', ARRAY[%s])"
        " RETURNING identity_id", (f"{w.domain}-4404", w.g_owners)).fetchone()  # fmt: skip
    app = f"plan-app-{uuid.uuid4().hex[:8]}"
    # Registered by this admin, owned by someone who can't see the dataset (yet).
    sql.execute(
        "INSERT INTO catalog.api_client (app_id, display_name, scopes, owner_identity_id,"
        " created_by_identity_id) VALUES (%s, 'Plans', '{Archive.Adopt}', %s, %s)",
        (app, owner, admin_id),
    )
    body = {"dataset_id": str(w.dataset_id), "name": "API plan", "owner_identity_id": str(owner),
            "waves": [_wave(prefixes=["raw/2021/"])]}  # fmt: skip
    token = web.idp.app_token(app, ["Archive.Adopt"])
    sql.execute("UPDATE catalog.share SET owners_group_sid = %s WHERE dataset_id = %s",
                (w.g_other, w.dataset_id))  # fmt: skip
    r = web.call("POST", "/v1/migration-plans", token=token, json=body, check=False)
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
    # The owner is an Owner of a share of the dataset: now they can see it.
    sql.execute("UPDATE catalog.share SET owners_group_sid = %s WHERE dataset_id = %s",
                (w.g_owners, w.dataset_id))  # fmt: skip
    r = web.call("POST", "/v1/migration-plans", token=token, json=body)
    assert r.status_code == 201, r.text
    pid = r.json()["plan_id"]
    r = admin.post("/v1/migration-plans/{plan_id}/approve", {}, plan_id=pid, check=False)
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    other = web.browser(w.admin2)
    assert other.post("/v1/migration-plans/{plan_id}/approve", {},
                      plan_id=pid).status_code == 200  # fmt: skip


def test_plan_refusals(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    admin = web.browser(w.admin)
    assert _plan(admin, w, [_wave(prefixes=["raw/2019"])]).status_code == 400
    assert _plan(admin, w, [_wave(prefixes=["elsewhere/"])]).status_code == 400
    r = _plan(admin, w, [_wave(), _wave(prefixes=["raw/2019/a/"])])
    assert r.status_code == 400 and "overlaps" in r.json()["detail"]
    assert _plan(admin, w, [_wave(after=1)]).status_code == 400
    never = [{"from": "2026-11-01T00:00:00Z", "to": "2027-06-01T00:00:00Z"}]
    r = _plan(admin, w, [_wave(blackouts=never)])
    assert r.status_code == 400 and "no minute to start" in r.json()["detail"]
    _start(web, w, prefix="raw/2019/")
    r = _plan(admin, w, [_wave()])
    assert r.status_code == 409 and r.json()["code"] == "migration_conflict"
    # A proposer never decides its own proposal; a steward can't plan.
    assert web.browser(w.steward).post("/v1/migration-plans", {}, check=False).status_code == 403


def test_rescheduling_checks_dependencies_and_versions(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    admin = web.browser(w.admin)
    p = _plan(admin, w, [_wave(), _wave(prefixes=["raw/2020/"], after=1,
                                        start_at="2026-11-11T22:00:00Z")]).json()  # fmt: skip
    pid, v = p["plan_id"], p["version"]

    def patch(seq: int, body):  # type: ignore[no-untyped-def]
        return admin.call("PATCH", "/v1/migration-plans/{plan_id}/waves/{seq}", json=body,
                          plan_id=pid, seq=seq, check=False)  # fmt: skip

    r = patch(2, {"base_version": v, "start_at": "2026-11-09T22:00:00Z"})
    assert r.status_code == 400 and "can't start before" in r.json()["detail"]
    r = patch(1, {"base_version": v, "start_at": "2026-11-12T22:00:00Z"})
    assert r.status_code == 400 and "waits for this one" in r.json()["detail"]
    r = patch(2, {"base_version": v, "start_at": "2026-11-12T23:00:00Z"})
    assert r.status_code == 200, r.text
    assert r.json()["waves"][1]["start_at"] == "2026-11-12T23:00:00.000Z"
    r = patch(2, {"base_version": v, "start_at": "2026-11-13T23:00:00Z"})
    assert r.status_code == 412  # stale version
    sql.execute("UPDATE catalog.migration_wave SET state = 'started' WHERE plan_id = %s"
                " AND seq = 1", (int(pid),))  # fmt: skip
    r = patch(1, {"base_version": v + 1, "start_at": "2026-11-10T23:00:00Z"})
    assert r.status_code == 409
    (n,) = sql.execute("SELECT count(*) FROM catalog.audit_log WHERE action ="
                       " 'migration_plan.reschedule'"
                       " AND details->>'plan_id' = %s", (pid,)).fetchone()  # fmt: skip
    assert n == 1


def test_reject_cancel_read_and_a_manual_waves_cutover(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    admin = web.browser(w.admin)
    (owner,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', ARRAY[%s])"
        " RETURNING identity_id", (f"{w.domain}-4403", w.g_owners)).fetchone()  # fmt: skip
    adopt = web.app_token(["Archive.Adopt"])
    proposal = web.call("POST", "/v1/migration-plans", token=adopt, json={
        "dataset_id": str(w.dataset_id), "name": "p", "owner_identity_id": str(owner),
        "waves": [_wave(prefixes=["raw/2021/"])]}).json()  # fmt: skip
    r = admin.post("/v1/migration-plans/{plan_id}/reject", {"note": "not now"},
                   plan_id=proposal["plan_id"])  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    one = web.call("GET", "/v1/migration-plans/{plan_id}", token=adopt, plan_id=proposal["plan_id"])
    assert one.json()["decision_note"] == "not now"
    p = _plan(admin, w, [_wave(cutover="manual"), _wave(prefixes=["raw/2020/"])]).json()
    # A manual wave cuts over only once verified (decision 24).
    r = admin.post("/v1/migration-plans/{plan_id}/waves/{seq}/cutover", {}, plan_id=p["plan_id"],
                   seq=1, check=False)  # fmt: skip
    assert r.status_code == 409
    mid = _start(web, w, prefix="raw/2019/").json()["migration_id"]
    sql.execute(
        "UPDATE catalog.migration_prefix SET state = 'verified', verified_at = now(),"
        " files_total = 0, files_verified = 0, cutover = 'manual' WHERE migration_id = %s",
        (int(mid),),
    )
    (wid,) = sql.execute("SELECT wave_id FROM catalog.migration_wave WHERE plan_id = %s"
                         " AND seq = 1", (int(p["plan_id"]),)).fetchone()  # fmt: skip
    sql.execute("INSERT INTO catalog.migration_wave_prefix VALUES (%s, 'raw/2019/', %s)",
                (wid, int(mid)))  # fmt: skip
    sql.execute("UPDATE catalog.migration_wave SET state = 'verified' WHERE wave_id = %s", (wid,))
    r = admin.post("/v1/migration-plans/{plan_id}/waves/{seq}/cutover", {},
                   plan_id=p["plan_id"], seq=1)  # fmt: skip
    assert r.status_code == 200, r.text
    (asked,) = sql.execute("SELECT cutover_requested_at FROM catalog.migration_prefix"
                           " WHERE migration_id = %s", (int(mid),)).fetchone()  # fmt: skip
    assert asked is not None
    r = admin.post("/v1/migration-plans/{plan_id}/cancel", {}, plan_id=p["plan_id"])
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert [x["state"] for x in r.json()["waves"]] == ["verified", "cancelled"]


def test_split_by_bytes(web, sql):
    w = seed(sql)
    s3 = _migrating(sql, w)
    for name, key, size in (("a", "raw/2019/a", 900), ("b", "raw/2020/b", 100),
                            ("c", "raw/2021/c", 100), ("d", "raw/top", 5)):  # fmt: skip
        fid = _s3_file(sql, w, s3, f"{name}.segy", key)
        sql.execute("UPDATE catalog.location SET size = %s WHERE file_id = %s", (size, fid))
    (sid,) = sql.execute("SELECT source_id FROM catalog.source WHERE dataset_id = %s",
                         (w.dataset_id,)).fetchone()  # fmt: skip
    # An app reads at dataset level (a signed-in admin sees only folders it can read).
    r = web.call("GET", "/v1/migration-plans/split", token=web.app_token(["Archive.Read"]),
                 params={"source_id": str(sid), "waves": "2"})  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["loose"] == {"files": 1, "bytes": 5}
    assert sorted(x["prefixes"] for x in out["waves"]) == [["raw/2019/"],
                                                            ["raw/2020/", "raw/2021/"]]  # fmt: skip


def test_split_names_only_folders_the_caller_can_read(web, sql):
    """Review of #45: split's prefixes are folder names. A signed-in admin
    sees the folders they can read, and only counts for the rest."""
    from .conftest import add_dir

    w = seed(sql)
    s3 = _migrating(sql, w)
    sql.execute("UPDATE catalog.source SET path = '' WHERE dataset_id = %s", (w.dataset_id,))
    add_dir(sql, w, "2019")
    add_dir(sql, w, "2020", acl=w.acl_secret)
    for key in ("raw/2019/a", "raw/2020/b"):
        _s3_file(sql, w, s3, key.split("/")[-1] + ".segy", key)
    (sid,) = sql.execute("SELECT source_id FROM catalog.source WHERE dataset_id = %s",
                         (w.dataset_id,)).fetchone()  # fmt: skip
    q = {"source_id": str(sid), "waves": "2"}
    reader = web.browser(w.user("split-r", roles=["ArchiveAdmin"], groups=[w.g_readers],
                                sid_rid=3093))  # fmt: skip
    out = reader.get("/v1/migration-plans/split", params=q).json()
    assert [x["prefix"] for x in out["prefixes"]] == ["raw/2019/"]
    assert out["hidden"]["folders"] == 1 and "raw/2020/" not in str(out)
    # No groups at all (an admin's role grants no read): counts only.
    out = web.browser(w.admin).get("/v1/migration-plans/split", params=q).json()
    assert out["prefixes"] == [] and out["hidden"]["folders"] == 2 and out["waves"] == []
    # An app reads at dataset level, once its client's owner can see the dataset.
    out = web.call("GET", "/v1/migration-plans/split", token=web.app_token(["Archive.Read"]),
                   params=q).json()  # fmt: skip
    assert len(out["prefixes"]) == 2


def test_agents_follow_arm_next_links():
    """Review of #45: agents past ARM's first page aren't dropped; a link off
    ARM is never followed (the token would go there)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "skip=2" in str(request.url):
            return httpx.Response(200, json={"value": [{"name": "a3", "properties": {}}],
                                             "nextLink": "https://evil.example/x"})  # fmt: skip
        return httpx.Response(200, json={
            "value": [{"name": "a1", "properties": {"agentStatus": "Online"}},
                      {"name": "a2", "properties": {}}],
            "nextLink": "https://management.azure.com/subscriptions/s/agents?api-version=v&skip=2",
        })  # fmt: skip

    class Tok:
        async def token(self) -> str:
            return "t"

    cfg = StorageMoverConfig(subscription_id="s", resource_group="g", storage_mover="m",
                             project="p", source_endpoints={}, target_endpoints={})  # fmt: skip
    mover = AzureStorageMover(
        cfg, Tok(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    agents = asyncio.run(mover.list_agents())
    assert [a["name"] for a in agents] == ["a1", "a2", "a3"]
    assert len(seen) == 2 and not any("evil" in u for u in seen)


def test_a_finished_migration_is_counted_once_on_real_time(web, sql, db_url):
    """Re-review of #45: a cut-over migration was counted on every sweep (the
    count bumped its row's updated_at). Now it is counted once more and left,
    and a count never touches the migration's row or its version. Real
    database time, not a simulated clock."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    _s3_file(sql, w, s3, "a.segy", "raw/2019/a.segy")
    mid = int(_start(web, w).json()["migration_id"])
    sql.execute(
        "UPDATE catalog.migration_prefix SET state = 'cut_over', verified_at = now(),"
        " cut_over_at = now(), files_total = 0, files_verified = 0 WHERE migration_id = %s",
        (mid,),
    )

    def snapshot():  # type: ignore[no-untyped-def]
        return sql.execute(
            "SELECT m.entity_version, m.updated_at, c.counted_at, c.counted_state"
            "  FROM catalog.migration_prefix m"
            "  LEFT JOIN catalog.migration_count c USING (migration_id)"
            " WHERE m.migration_id = %s", (mid,)).fetchone()  # fmt: skip

    before = snapshot()
    _count(db_url, datetime.now(UTC))
    first = snapshot()
    assert first[3] == "cut_over" and first[2] is not None
    assert first[:2] == before[:2]  # the migration's row is untouched
    for _ in range(2):
        _count(db_url, datetime.now(UTC))
        assert snapshot() == first  # not counted again


def test_failing_now_clears_once_the_files_recover(web, sql, db_url):
    """Re-review of #45: the verify_failed item goes once the file that
    failed its check has a verified copy."""
    w = seed(sql)
    s3 = _migrating(sql, w)
    fid = _s3_file(sql, w, s3, "a.segy", "raw/2019/a.segy")
    mid = _start(web, w).json()["migration_id"]
    (vid,) = sql.execute("SELECT content_version_id FROM catalog.file WHERE file_id = %s",
                         (fid,)).fetchone()  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.location (version_id, file_id, backend_id, container, object_key,"
        " role, state, size, committed_at) VALUES (%s, %s, %s, 'live/data', 'x/a', 'secondary',"
        " 'deleted', 1, now())",
        (vid, fid, w.backend_id),
    )
    admin = web.browser(w.admin)
    _count(db_url, datetime.now(UTC))

    def failing():  # type: ignore[no-untyped-def]
        att = admin.get("/v1/migration-console").json()["attention"]
        return _row(admin, mid)["failing_now"], [a for a in att if a["migration_id"] == mid
                                                 and a["kind"] == "verify_failed"]  # fmt: skip

    n, items = failing()
    assert n == 1 and len(items) == 1
    # Copied again, and this time it matches.
    sql.execute(
        "INSERT INTO catalog.location (version_id, file_id, backend_id, container, object_key,"
        " role, state, size, verified, verified_at, committed_at, verified_by, crc64nvme)"
        " VALUES (%s, %s, %s, 'live/data', 'x/a2', 'secondary', 'committed', 1, true, now(),"
        " now(), 'azure_read_crc', '\\x0123456789abcdef'::bytea)",
        (vid, fid, w.backend_id),
    )
    _count(db_url, datetime.now(UTC))
    n, items = failing()
    assert n == 0 and items == []


def test_a_blocked_wave_can_be_rescheduled_or_cancelled(web, sql):
    """Re-review of #45: a blocked wave isn't a dead end. Dropping `after`
    unblocks it; cancelling the plan cancels it."""
    w = seed(sql)
    _migrating(sql, w)
    admin = web.browser(w.admin)
    p = _plan(admin, w, [_wave(), _wave(prefixes=["raw/2020/"], after=1)]).json()
    pid = p["plan_id"]
    sql.execute("UPDATE catalog.migration_wave SET state = 'cancelled' WHERE plan_id = %s"
                " AND seq = 1", (int(pid),))  # fmt: skip
    sql.execute("UPDATE catalog.migration_wave SET state = 'blocked' WHERE plan_id = %s"
                " AND seq = 2", (int(pid),))  # fmt: skip
    body = {"base_version": p["version"], "after": None}
    r = admin.call("PATCH", "/v1/migration-plans/{plan_id}/waves/{seq}", json=body,
                   plan_id=pid, seq=2)  # fmt: skip
    assert r.status_code == 200, r.text
    w2 = r.json()["waves"][1]
    assert (w2["state"], w2["after"]) == ("planned", None)
    sql.execute("UPDATE catalog.migration_wave SET state = 'blocked' WHERE plan_id = %s"
                " AND seq = 2", (int(pid),))  # fmt: skip
    r = admin.post("/v1/migration-plans/{plan_id}/cancel", {}, plan_id=pid)
    assert r.status_code == 200 and r.json()["waves"][1]["state"] == "cancelled"


def test_plans_refuse_an_external_copier_for_now(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    r = _plan(web.browser(w.admin), w, [_wave(copier="external")])
    assert r.status_code == 422 and r.json()["code"] == "action_not_allowed"
    assert "external copier" in r.json()["detail"]


# --- which objects are stuck -----------------------------------------------------------------

_STUCK = "/v1/migrations/{migration_id}/stuck-copies"


def _stuck_rig(web, sql):  # type: ignore[no-untyped-def]
    w = seed(sql)
    _migrating(sql, w)
    mid = _start(web, w).json()["migration_id"]
    sql.execute(
        "INSERT INTO catalog.migration_stuck_copy (migration_id, kind, object_key)"
        " SELECT %s, 'no_checksum', k FROM unnest(%s::text[]) k",
        (int(mid), ["raw/2019/a.segy", "raw/2019/b.segy", "raw/2019/c.segy", "raw/2019/Z.segy"]),
    )
    sql.execute(
        "INSERT INTO catalog.migration_stuck_copy (migration_id, kind, object_key)"
        " VALUES (%s, 'stranded', 'raw/2019/s.segy')",
        (int(mid),),
    )
    sql.execute(
        "INSERT INTO catalog.migration_count (migration_id, counted_state, bytes_total,"
        " bytes_verified, failing_now, archived_waiting, restores_expiring, counted_at,"
        " copies_no_checksum, copies_stranded, stuck_counted_at)"
        " VALUES (%s, 'copying', 0, 0, 0, 0, 0, now(), 4000000, 1, now())",
        (int(mid),),
    )
    return w, mid


def test_the_console_lists_which_objects_are_stuck_a_page_at_a_time(web, sql):
    w, mid = _stuck_rig(web, sql)
    admin = web.browser(w.admin)
    r = admin.get(_STUCK, migration_id=mid, params={"kind": "no_checksum", "limit": 2})
    page = r.json()
    # In byte order, as S3 lists them. The number is the worker's, not a count of the list.
    assert [i["object_key"] for i in page["items"]] == ["raw/2019/Z.segy", "raw/2019/a.segy"]
    assert page["kind"] == "no_checksum" and page["count"] == 4000000
    assert page["counted_at"] is not None and page["list_limit"] == 10_000
    params = {"kind": "no_checksum", "limit": 2, "cursor": page["next_cursor"]}
    rest = admin.get(_STUCK, migration_id=mid, params=params).json()
    assert [i["object_key"] for i in rest["items"]] == ["raw/2019/b.segy", "raw/2019/c.segy"]
    assert rest["next_cursor"] is None
    # A cursor is for the list it came from.
    bad = admin.get(_STUCK, migration_id=mid, check=False,
                    params={"kind": "stranded", "cursor": page["next_cursor"]})  # fmt: skip
    assert bad.status_code == 400
    other = admin.get(_STUCK, migration_id=mid, params={"kind": "stranded"}).json()
    assert [i["object_key"] for i in other["items"]] == ["raw/2019/s.segy"]
    assert other["count"] == 1
    assert admin.get(_STUCK, migration_id=mid, check=False,
                     params={"kind": "given_up"}).status_code == 400  # fmt: skip
    assert admin.get(_STUCK, migration_id=mid, check=False).status_code == 400
    assert admin.get(_STUCK, migration_id="999999", check=False,
                     params={"kind": "stranded"}).status_code == 404  # fmt: skip


def test_an_object_excluded_since_the_last_look_isnt_listed(web, sql):
    w, mid = _stuck_rig(web, sql)
    sql.execute(
        "INSERT INTO catalog.migration_exclusion (migration_id, object_key, reason, excluded_by)"
        " VALUES (%s, 'raw/2019/a.segy', 'SSE-C', 'test')",
        (int(mid),),
    )
    r = web.browser(w.admin).get(_STUCK, migration_id=mid, params={"kind": "no_checksum"}).json()
    assert [i["object_key"] for i in r["items"]] == [
        "raw/2019/Z.segy", "raw/2019/b.segy", "raw/2019/c.segy"]  # fmt: skip


def test_only_a_signed_in_archive_admin_reads_which_objects_are_stuck(web, sql):
    w, mid = _stuck_rig(web, sql)
    params = {"kind": "no_checksum"}
    assert web.browser(w.steward).get(_STUCK, migration_id=mid, params=params,
                                      check=False).status_code == 403  # fmt: skip
    r = web.call("GET", _STUCK, token=web.app_token(["Archive.Adopt", "Archive.Read"]),
                 migration_id=mid, params=params, check=False)  # fmt: skip
    assert r.status_code in (401, 403)


def test_a_read_of_which_objects_are_stuck_is_audited(web, sql):
    """They are named to someone who may not be able to read them, as in the cutover report."""
    w, mid = _stuck_rig(web, sql)
    web.browser(w.admin).get(_STUCK, migration_id=mid, params={"kind": "no_checksum", "limit": 2})
    (actor, details) = sql.execute(
        "SELECT actor, details FROM catalog.audit_log"
        " WHERE action = 'migration.stuck_copies.read' AND details->>'migration_id' = %s",
        (mid,),
    ).fetchone()
    assert actor.startswith("user:")
    assert details == {"migration_id": mid, "kind": "no_checksum", "rows": 2, "first_page": True}


def test_an_admin_with_no_ad_identity_is_told_no_objects(web, sql):
    w, mid = _stuck_rig(web, sql)
    nosid = web.browser(w.user("cloud-admin", roles=["ArchiveAdmin"], sid=None))
    r = nosid.get(_STUCK, migration_id=mid, params={"kind": "no_checksum"}).json()
    assert r["items"] == [] and r["count"] == 4000000
    assert sql.execute(
        "SELECT details->>'rows' FROM catalog.audit_log"
        " WHERE action = 'migration.stuck_copies.read' AND details->>'migration_id' = %s",
        (mid,),
    ).fetchall() == [("0",)]


def test_a_page_of_stuck_objects_is_in_key_order_whatever_the_plan(web, sql):
    """A keyset is only right in key order, and an index scan would hide a missing `ORDER BY`."""
    _w, mid = _stuck_rig(web, sql)
    for scan in ("enable_indexscan", "enable_indexonlyscan", "enable_bitmapscan"):
        sql.execute(f"SET {scan} = off")
    try:
        rows = sql.execute(
            _STUCK_PAGE, {"mid": int(mid), "kind": "no_checksum", "after": "", "limit": 2}
        ).fetchall()
    finally:
        for scan in ("enable_indexscan", "enable_indexonlyscan", "enable_bitmapscan"):
            sql.execute(f"RESET {scan}")
    assert [k for (k,) in rows] == ["raw/2019/Z.segy", "raw/2019/a.segy"]


def test_a_page_of_stuck_objects_reads_only_its_page(web, sql):
    """A keyset on the primary key: no sort, and no scan of the migration's list."""
    w, mid = _stuck_rig(web, sql)
    sql.execute(
        "INSERT INTO catalog.migration_stuck_copy (migration_id, kind, object_key)"
        " SELECT %s, 'no_checksum', 'raw/2019/n' || lpad(n::text, 6, '0')"
        "   FROM generate_series(1, 20000) n",
        (int(mid),),
    )
    sql.execute("ANALYZE catalog.migration_stuck_copy")
    plan = "\n".join(
        line
        for (line,) in sql.execute(
            "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) " + _STUCK_PAGE,
            {"mid": int(mid), "kind": "no_checksum", "after": "raw/2019/n010000", "limit": 101},
        ).fetchall()
    )
    assert "migration_stuck_copy_pkey" in plan and "Sort" not in plan, plan
    assert "rows=101" in plan and "rows=10000" not in plan, plan
