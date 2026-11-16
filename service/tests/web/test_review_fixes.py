"""Regressions from the M4 portal review, decision 34 (restore targets) and
the front end's contract gaps. Each test failed before its fix."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omnifile_service.backends.base import Throttled
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes
from omnifile_service.catalog.privileges import as_app
from omnifile_service.notify import facts
from omnifile_service.web.context import TokenBuckets
from omnifile_service.web.routes_rewind import restored_name
from omnifile_service.worker import portal_jobs
from omnifile_service.worker.backends import BackendRegistry

from .conftest import _acl, add_dir, add_file, dev_params, location_query, seed
from .test_rewind_recalls import _queue, run_job, stored

GiB = 1024**3


class Flaky:
    """A sim whose named calls raise Throttled the first time."""

    def __init__(self, sim, *fail: str) -> None:
        self.sim, self.fail, self.calls = sim, set(fail), {}

    def __getattr__(self, name):
        return getattr(self.sim, name)

    async def _call(self, name, *a, **kw):
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.fail and self.calls[name] == 1:
            raise Throttled("busy")
        return await getattr(self.sim, name)(*a, **kw)

    async def copy_object(self, *a, **kw):
        return await self._call("copy_object", *a, **kw)

    async def head(self, *a, **kw):
        return await self._call("head", *a, **kw)


def _restore(web, w, fid, vid, target, user=None):
    b = web.browser(user or w.owner)
    r = b.post("/v1/files/{file_id}/versions/{version_id}/restore", {"target": target},
               file_id=fid, version_id=vid)  # fmt: skip
    return r


def _restored_files(sql, w, prefix):
    return sql.execute(
        "SELECT file_id, state FROM catalog.file WHERE parent_id = %s AND name LIKE %s",
        (w.root_id, prefix + "-restored%"),
    ).fetchall()


def _pending_versions(sql, fid):
    return sql.execute(
        "SELECT count(*) FROM catalog.file_version WHERE file_id = %s AND state = 'pending'",
        (fid,),
    ).fetchone()[0]


# --- B1: the identity's groups are the session's ---------------------------------------------


def test_sign_in_keeps_domain_users_on_the_identity_for_teams(web, sql, db_url):
    w = seed(sql)
    acl_du = _acl(sql, w.dataset_id, [f"{w.domain}-513"])
    fid = add_file(sql, w, "du.txt", acl=acl_du)
    b = web.browser(w.reader)
    assert b.get("/v1/files/{file_id}", file_id=fid).status_code == 200
    (iid,) = sql.execute(
        "SELECT identity_id FROM catalog.identity WHERE sid = %s", (w.reader["sid"],)
    ).fetchone()

    async def teams_can_read():
        conn = await psycopg.AsyncConnection.connect(as_app(db_url, "api"))
        try:
            return await facts.can_read(conn, fid, iid)
        finally:
            await conn.close()

    assert asyncio.run(teams_can_read()) is True


# --- B2: a throttled restore leaves nothing behind and resumes ---------------------------------


@pytest.mark.parametrize("target", ["restored", "overwrite"])
def test_throttled_copy_writes_nothing_and_retries_cleanly(web, sql, db_url, target):
    w = seed(sql)
    fid, sim = stored(sql, w, "model.bin", versions=2)
    old = w.versions["model.bin"][0]
    rid = int(_restore(web, w, fid, old, target).json()["restore_id"])
    flaky = Flaky(sim, "copy_object")
    reg = BackendRegistry.from_mapping({w.backend_id: flaky})
    now = datetime.now(UTC)
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, rid, now)) == "retry"
    assert _restored_files(sql, w, "model") == []
    assert _pending_versions(sql, fid) == 0
    (state,) = sql.execute("SELECT state FROM catalog.version_restore WHERE restore_id = %s",
                           (rid,)).fetchone()  # fmt: skip
    assert state == "requested"
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, rid, now)) == "done"
    if target == "restored":
        assert len(_restored_files(sql, w, "model")) == 1
    assert _pending_versions(sql, fid) == 0


def test_restore_resumes_from_copying_without_copying_again(web, sql, db_url):
    w = seed(sql)
    fid, sim = stored(sql, w, "model.bin", versions=2)
    rid = int(_restore(web, w, fid, w.versions["model.bin"][0], "restored").json()["restore_id"])
    flaky = Flaky(sim, "head")
    reg = BackendRegistry.from_mapping({w.backend_id: flaky})
    now = datetime.now(UTC)
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, rid, now)) == "retry"
    row = sql.execute("SELECT state, copy_key FROM catalog.version_restore WHERE restore_id = %s",
                      (rid,)).fetchone()  # fmt: skip
    assert row[0] == "copying" and row[1].endswith(f"restores/{rid}")
    assert _restored_files(sql, w, "model") == []
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, rid, now)) == "done"
    assert flaky.calls["copy_object"] == 1
    assert len(_restored_files(sql, w, "model")) == 1


# --- N6: a version archived after the request is recalled by the worker ------------------------


def test_restore_starts_a_recall_when_the_version_became_archived(web, sql, db_url):
    w = seed(sql)
    fid = add_file(sql, w, "map.tif", versions=2)
    old = w.versions["map.tif"][0]
    rid = int(_restore(web, w, fid, old, "restored").json()["restore_id"])
    sql.execute("UPDATE catalog.location SET logical_tier = 'archive', storage_class = 'Archive'"
                " WHERE version_id = %s", (old,))  # fmt: skip
    reg = BackendRegistry.from_mapping({})
    now = datetime.now(UTC)
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, rid, now)) == "retry"
    state, recall_id = sql.execute(
        "SELECT state, recall_id FROM catalog.version_restore WHERE restore_id = %s", (rid,)
    ).fetchone()
    assert state == "waiting_recall" and recall_id is not None
    assert {"recall_id": str(recall_id)} in _queue(sql, "restores")
    (via,) = sql.execute("SELECT via FROM catalog.recall_requester WHERE recall_id = %s",
                         (recall_id,)).fetchone()  # fmt: skip
    assert via == "portal"
    # Again: it joins the same recall and keeps waiting.
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, rid, now)) == "retry"
    assert sql.execute("SELECT count(*) FROM catalog.recall WHERE file_id = %s",
                       (fid,)).fetchone() == (1,)  # fmt: skip


# --- S1: version routes respect the rewind depth --------------------------------------------------


def test_versions_beyond_the_rewind_depth_are_404_everywhere(web, sql):
    w = seed(sql, depth=1, depth_max=90)
    fid = add_file(sql, w, "old.txt", versions=3, ago_days=10)
    old = w.versions["old.txt"][0]
    owner = web.browser(w.owner)
    listed = [v["version_id"] for v in
              owner.get("/v1/files/{file_id}/versions", file_id=fid).json()["items"]]  # fmt: skip
    assert listed == [str(w.versions["old.txt"][-1])]
    assert _restore(web, w, fid, old, "restored").status_code == 404
    r = owner.post("/v1/files/{file_id}/versions/{version_id}/downloads", {}, file_id=fid,
                   version_id=old)  # fmt: skip
    assert r.status_code == 404
    r = owner.get("/v1/files/{file_id}/recall-options", params={"version_id": str(old)},
                  file_id=fid)  # fmt: skip
    assert r.status_code == 404
    # The current version is still fine.
    assert _restore(web, w, fid, w.versions["old.txt"][-1], "restored").status_code == 202


# --- S3: no auth_time, no recent sign-in ------------------------------------------------------


def test_missing_auth_time_never_counts_as_recent(web, sql):
    w = seed(sql)
    r = web.client.get("/auth/login", follow_redirects=False)
    assert '"auth_time"' in location_query(r)["claims"]
    admin = {**w.user("legal", roles=["ArchiveAdmin"], groups=[w.g_readers], sid_rid=3050),
             "no_auth_time": True}  # fmt: skip
    b = web.browser(admin)
    r = b.post("/v1/holds", {"items": [str(w.root_id)], "kind": "legal", "reason": "case"})
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    # A re-auth sign-in that doesn't prove a fresh sign-in is refused.
    r1 = web.client.get("/auth/login", params={"reauth": "1"}, follow_redirects=False)
    r2 = web.client.get(r1.headers["location"] + "&" + urlencode(dev_params(admin)),
                        follow_redirects=False)  # fmt: skip
    r3 = web.client.get(r2.headers["location"], follow_redirects=False)
    assert r3.status_code == 401


# --- S4: WORM cancel ends at the deadline ------------------------------------------------------


def _etag(b, w):
    return b.get("/v1/shares/{share_id}", share_id=w.share_id).headers["etag"]


def test_worm_cancel_after_the_deadline_is_refused_before_the_sweep_runs(web, sql):
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    r = admin.post("/v1/shares/{share_id}/worm/enable",
                   {"retention_days": 30, "confirm_name": w.share_name},
                   headers={"If-Match": _etag(admin, w)}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 200, r.text
    sql.execute("UPDATE catalog.share SET worm_lock_deadline = now() - interval '1 second'"
                " WHERE share_id = %s", (w.share_id,))  # fmt: skip
    r = admin.post("/v1/shares/{share_id}/worm/cancel", {"confirm_name": w.share_name},
                   headers={"If-Match": _etag(admin, w)}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "worm_window_closed"
    assert sql.execute("SELECT worm_enabled FROM catalog.share WHERE share_id = %s",
                       (w.share_id,)).fetchone() == (True,)  # fmt: skip


# --- N5: WORM enable and a Smart policy can't race ---------------------------------------------


def test_worm_enable_waits_for_a_concurrent_policy_change(web, sql, db_url):
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    etag = _etag(admin, w)
    (iid,) = sql.execute("INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
                         (f"{w.domain}-3999",)).fetchone()  # fmt: skip
    other = psycopg.connect(db_url)
    other.execute("SELECT 1 FROM catalog.dataset WHERE dataset_id = %s FOR UPDATE",
                  (w.dataset_id,))  # fmt: skip
    other.execute(
        "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
        " VALUES (%s, 1, %s, %s)",
        (w.dataset_id, Jsonb({"mode": "smart"}), iid),
    )
    out = {}

    def enable():
        out["r"] = admin.post("/v1/shares/{share_id}/worm/enable",
                              {"retention_days": 30, "confirm_name": w.share_name},
                              headers={"If-Match": etag}, share_id=w.share_id)  # fmt: skip

    t = threading.Thread(target=enable)
    t.start()
    time.sleep(0.5)
    other.commit()
    other.close()
    t.join(10)
    assert out["r"].status_code == 409 and out["r"].json()["code"] == "layout_conflict"


# --- S6: Owners' audit view hides files they can't read ---------------------------------------


def test_owner_audit_hides_rows_about_unreadable_files(web, sql):
    w = seed(sql)
    readers_only = _acl(sql, w.dataset_id, [w.g_readers])
    fid = add_file(sql, w, "hr.segy", acl=readers_only, tier="archive", state="stub")
    r = web.browser(w.reader).post("/v1/recalls", {"file_id": str(fid), "priority": "standard"})
    assert r.status_code == 201
    params = {"share_id": str(w.share_id), "action": "recall.request"}
    rows = web.browser(w.owner).get("/v1/audit", params=params).json()["items"]
    assert str(fid) not in [x["file_id"] for x in rows]
    rows = web.browser(w.admin).get("/v1/audit", params=params).json()["items"]
    assert str(fid) in [x["file_id"] for x in rows]


# --- N4: fee estimates in GiB, as the cost model ------------------------------------------------


def test_recall_fee_is_per_gib(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "big.segy", tier="archive", state="stub", size=10 * GiB)
    admin = web.browser(w.admin)
    table = admin.get("/v1/price-tables/{backend_kind}", backend_kind="sim").json()
    per_gb = Decimal(table["classes"]["Archive"]["retrieval_per_gb"])
    opts = web.browser(w.reader).get("/v1/files/{file_id}/recall-options", file_id=fid).json()
    std = next(o for o in opts["options"] if o["priority"] == "standard")
    assert Decimal(std["fee_estimate"]["amount"]) == (per_gb * 10).quantize(Decimal("0.01"))


# --- N7: sign-in starts are rate-limited --------------------------------------------------------


def test_login_is_rate_limited_per_client(web, sql):
    web.services.login_limiter = TokenBuckets(0.001, 3)
    codes = [web.client.get("/auth/login", follow_redirects=False).status_code for _ in range(5)]
    assert codes[:3] == [302, 302, 302] and codes[3:] == [429, 429]


# --- decision 34: restore targets -------------------------------------------------------------


def test_restored_names():
    at = datetime(2026, 10, 6, 9, 30, 5, tzinfo=UTC)
    assert restored_name("line.segy", "restored", at) == "line-restored.segy"
    assert restored_name("a.tar.gz", "restored", at) == "a.tar-restored.gz"
    assert restored_name(".bashrc", "restored", at) == ".bashrc-restored"
    assert (
        restored_name("README", "restored_timestamped", at) == "README-restored-2026-10-06-093005Z"
    )
    assert restored_name("x.bin", "restored_timestamped", at) == "x-restored-2026-10-06-093005Z.bin"
    long = restored_name("é" * 200 + ".dat", "restored_timestamped", at)
    assert len(long.encode()) <= 255 and long.endswith("Z.dat")
    # An extension that leaves no room for a stem (Codex P2 on #8): still at most 255 bytes,
    # so xfs can hold the restored placeholder.
    for target in ("restored", "restored_timestamped"):
        for name in ("a." + "x" * 253, "a." + "é" * 126, "." * 255):
            out = restored_name(name, target, at)
            assert 0 < len(out.encode()) <= 255 and "/" not in out, (name, out)
            assert "-restored" in out


def test_a_taken_restored_name_is_409_and_the_worker_rechecks(web, sql, db_url):
    w = seed(sql)
    fid, sim = stored(sql, w, "report.pdf", versions=2)
    old = w.versions["report.pdf"][0]
    add_file(sql, w, "report-restored.pdf")
    r = _restore(web, w, fid, old, "restored")
    assert r.status_code == 409 and r.json()["code"] == "name_taken"
    r = _restore(web, w, fid, old, "restored_timestamped")
    assert r.status_code == 202
    rid, name = int(r.json()["restore_id"]), r.json()["name"]
    add_file(sql, w, name)  # appears before the worker runs
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(
        c, reg, rid, datetime.now(UTC))) == "failed"  # fmt: skip
    (failure,) = sql.execute("SELECT failure FROM catalog.version_restore WHERE restore_id = %s",
                             (rid,)).fetchone()  # fmt: skip
    assert failure.startswith("name_taken")
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'version.restore'"
        " AND details->>'restore_id' = %s", (str(rid),)).fetchone()  # fmt: skip
    assert details["target"] == "restored_timestamped" and details["name"] == name


def test_overwrite_on_a_worm_share_adds_a_version(web, sql, db_url):
    w = seed(sql, layout="azure_worm")
    sql.execute("UPDATE catalog.share SET worm_enabled = true, worm_mode = 'unlocked_test',"
                " worm_retention_days = 30 WHERE share_id = %s", (w.share_id,))  # fmt: skip
    sim = SimBackend.azure_worm(VirtualClock(datetime.now(UTC)), backend_id=w.backend_id)

    async def put(data, key):
        async def one():
            yield data

        return await sim.put("worm/data", key, one(), None, {})

    datas = [b"v1 bytes", b"v2 bytes"]
    refs = [asyncio.run(put(d, "obj/f")) for d in datas]
    fid = add_file(sql, w, "f.dat", versions=2, key="obj/f", crc=to_bytes(crc64nvme(datas[0])))
    for vid, ref, d in zip(w.versions["f.dat"], refs, datas, strict=True):
        sql.execute("UPDATE catalog.location SET container = 'worm/data', backend_version_id = %s,"
                    " crc64nvme = %s, size = %s WHERE version_id = %s",
                    (ref.version_id, to_bytes(crc64nvme(d)), len(d), vid))  # fmt: skip
    old = w.versions["f.dat"][0]
    r = _restore(web, w, fid, old, "overwrite")
    assert r.status_code == 202, r.text
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(
        c, reg, int(r.json()["restore_id"]), datetime.now(UTC))) == "done"  # fmt: skip
    versions = web.browser(w.owner).get("/v1/files/{file_id}/versions", file_id=fid).json()
    assert [v["seq"] for v in versions["items"]] == [3, 2, 1]
    assert versions["items"][0]["retention_until"] is not None  # WORM retains the new one


# --- front-end contract gaps ------------------------------------------------------------------


def test_identity_ids_let_the_spa_recognise_its_own_rows(web, sql):
    w = seed(sql)
    steward = web.browser(w.steward)
    me = steward.get("/v1/me").json()
    assert me["identity_id"] is not None
    r = steward.post(
        "/v1/policies/proposals",
        {
            "dataset_id": str(w.dataset_id),
            "body": {"mode": "scheduled", "archive_day": 365},
            "rationale": "r",
        },
    )
    assert r.json()["proposed_by"]["identity_id"] == me["identity_id"]  # fmt: skip


def test_holds_by_file_and_hold_ids_on_entries(web, sql):
    w = seed(sql)
    folder = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id)"
        " VALUES (%s, %s, 'survey', 'dir', %s) RETURNING file_id",
        (w.dataset_id, w.root_id, w.acl_open),
    ).fetchone()[0]
    fid = add_file(sql, w, "a.segy", parent=folder)
    other = add_file(sql, w, "b.segy")
    owner = web.browser(w.owner)
    r = owner.post("/v1/holds", {"items": [str(folder)], "kind": "keep_online", "reason": "run"})
    hold_id = r.json()["holds"][0]["hold_id"]
    owner.post("/v1/holds", {"items": [str(other)], "kind": "keep_online", "reason": "x"})
    listed = owner.get("/v1/holds", params={"file_id": str(fid)}).json()["items"]
    assert [h["hold_id"] for h in listed] == [hold_id]
    entry = owner.get("/v1/files/{file_id}", file_id=fid).json()
    assert entry["holds"]["items"][0]["hold_id"] == hold_id
    assert entry["holds"]["items"][0]["inherited_from"] == str(folder)


def test_proposal_share_overrides_apply_only_on_approval(web, sql):
    w = seed(sql)
    token = web.app_token(["Policy.Propose"])
    body = {"dataset_id": str(w.dataset_id), "body": {"mode": "scheduled", "archive_day": 365},
            "rationale": "r", "share_overrides": [{"share_id": str(w.share_id), "on_open": "auto",
                                                   "wait_threshold_minutes": 20}]}  # fmt: skip
    r = web.call("POST", "/v1/policies/proposals", token=token, json=body)
    assert r.status_code == 201, r.text
    pid = r.json()["proposal_id"]
    assert r.json()["share_overrides"] == body["share_overrides"]
    on_open = lambda: sql.execute(  # noqa: E731
        "SELECT on_open, wait_threshold_minutes FROM catalog.share WHERE share_id = %s",
        (w.share_id,)).fetchone()  # fmt: skip
    assert on_open() == ("wait", None)
    bad = {**body, "share_overrides": [{"share_id": "999999999", "on_open": "fail"}]}
    assert web.call("POST", "/v1/policies/proposals", token=token, json=bad).status_code == 400
    r = web.browser(w.admin).post("/v1/policies/proposals/{proposal_id}/approve", {},
                                  proposal_id=pid)  # fmt: skip
    assert r.status_code == 200, r.text
    assert on_open() == ("auto", 20)


def test_recall_carries_its_folder_and_me_shows_pending_groups(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "old.segy", tier="archive", state="stub")
    r = web.browser(w.reader).post("/v1/recalls", {"file_id": str(fid), "priority": "standard"})
    assert r.json()["parent_id"] == str(w.root_id)
    fresh = w.user("many-groups", groups=[w.g_readers], sid_rid=3060, overage=True)
    me = web.browser(fresh).get("/v1/me").json()
    assert me["groups_source"] == "pending"


def test_an_outer_shares_groups_grant_nothing_inside_a_nested_share(web, sql):
    """Codex P1 on #8: a file belongs to the deepest share whose root contains it, and only
    that share's groups give roles on it. Owners and Writers of the outer share who can read
    a file in the inner share may not restore it."""
    w = seed(sql)
    inner = {k: f"{w.domain}-{rid}" for k, rid in (("owners", 2101), ("writers", 2102))}
    acl = _acl(sql, w.dataset_id, [w.g_owners, w.g_writers, inner["owners"], inner["writers"]])
    d = add_dir(sql, w, "inner", acl=acl)
    sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols, nfs_sec,"
        " owners_group_sid, writers_group_sid, on_open, rewind_depth_days,"
        " rewind_depth_max_days)"
        " VALUES (%s, %s, %s, %s, ARRAY['nfs4'], ARRAY['krb5'], %s, %s, 'wait', 30, 90)",
        (w.dataset_id, w.edge_id, w.share_name + "-inner", d, inner["owners"], inner["writers"]),
    )  # fmt: skip
    fid = add_file(sql, w, "inner.dat", parent=d, acl=acl, versions=2)
    old = w.versions["inner.dat"][0]
    path = "/v1/files/{file_id}/versions/{version_id}/restore"
    for outer in (w.writer, w.owner):
        b = web.browser(outer)
        assert b.get("/v1/files/{file_id}", file_id=fid).status_code == 200  # it can read
        r = b.post(path, {"target": "restored"}, file_id=fid, version_id=old)
        assert (r.status_code, r.json()["code"]) == (403, "forbidden"), r.text
    inner_writer = w.user("inner-writer", groups=[inner["writers"]], sid_rid=3010)
    r = web.browser(inner_writer).post(path, {"target": "restored"}, file_id=fid, version_id=old)
    assert r.status_code == 202, r.text
    # Outside the inner share, the outer share's Writers keep their role.
    top = add_file(sql, w, "top.dat", versions=2)
    r = web.browser(w.writer).post(
        path, {"target": "restored"}, file_id=top, version_id=w.versions["top.dat"][0]
    )
    assert r.status_code == 202, r.text
