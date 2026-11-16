"""Recalls ahead of time, the four-step view, restores and downloads, and the
worker's portal jobs over sim."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from watched import Clock, FakeWorker, Watched, drain_job

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes
from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker import portal_jobs
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import Retry

from .conftest import add_file, seed

DATA = b"seismic trace " * 300


async def _one(data: bytes):
    yield data


def stored(sql, w, name, *, data: bytes = DATA, versions: int = 1):
    """A file whose versions are real sim objects (for the worker jobs)."""
    sim = SimBackend.azure_two_account(VirtualClock(datetime.now(UTC)), backend_id=w.backend_id)
    fid = None
    for i in range(versions):
        body = data + bytes([i])
        ref = asyncio.run(sim.put("live/data", f"obj/{name}/{i}", _one(body), None, {}))
        crc = to_bytes(crc64nvme(body))
        if fid is None:
            fid = add_file(sql, w, name, size=len(body), key=ref.key,
                           backend_version=ref.version_id, crc=crc, ago_days=1)  # fmt: skip
        else:
            # A later version of the same file.
            (vid,) = sql.execute(
                "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
                " state, size, acl_id, crc64nvme) VALUES (%s, %s, %s, %s, 'content',"
                " 'pending', %s, %s, %s) RETURNING version_id",
                (fid, w.dataset_id, w.root_id, name, len(body), w.acl_open, crc),
            ).fetchone()
            sql.execute(
                "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
                " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
                " committed_at, crc64nvme) VALUES (%s, %s, %s, %s, %s, %s, 'primary',"
                " 'committed', 'hot', 'Hot', %s, now(), %s)",
                (fid, vid, w.backend_id, ref.container, ref.key, ref.version_id, len(body), crc),
            )
            sql.execute("UPDATE catalog.file_version SET state = 'committed', committed_at = now()"
                        " WHERE version_id = %s", (vid,))  # fmt: skip
            w.versions[name].append(vid)
    return fid, sim


def run_job(db_url: str, fn):
    """As the worker's consumer (worker/queue.py) runs a handler: in one
    transaction, and on `Retry` the handler's writes are COMMITTED (the
    message comes back later). Returns "retry" then."""

    async def go():
        conn = await psycopg.AsyncConnection.connect(as_app(db_url, "worker"), autocommit=True)
        try:
            async with conn.transaction():
                try:
                    return await fn(conn)
                except Retry:
                    return "retry"
        finally:
            await conn.close()

    return asyncio.run(go())


def _queue(sql, name: str) -> list[dict]:
    return [m for (m,) in sql.execute(f"SELECT message FROM pgmq.q_{name} ORDER BY msg_id")]


# --- recalls --------------------------------------------------------------------------------


def test_recall_options_and_ahead_of_time_recall(web, sql):
    w = seed(sql)
    add_file(sql, w, "old.segy", tier="archive", state="stub", size=2 * 10**9)
    add_file(sql, w, "hot.txt")
    reader = web.browser(w.reader)
    fid = w.files["old.segy"]
    r = reader.get("/v1/files/{file_id}/recall-options", file_id=fid)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["needs_restore"] is True and o["online"] is False
    std = next(x for x in o["options"] if x["priority"] == "standard")
    assert std["offered"] and std["eta"] and std["eta_confidence"] == "provider_sla"
    assert o["no_promise_above_bytes"] == 10 * 10**9
    online = reader.get("/v1/files/{file_id}/recall-options", file_id=w.files["hot.txt"]).json()
    assert online["needs_restore"] is False and online["options"] == []

    r = reader.post("/v1/recalls", {"file_id": str(w.files["hot.txt"]), "priority": "standard"})
    assert r.status_code == 409 and r.json()["code"] == "recall_not_needed"

    r = reader.post("/v1/recalls", {"file_id": str(fid), "priority": "standard"})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["joined"] is False and rec["state"] == "requested"
    assert [s["status"] for s in rec["steps"]] == ["current", "pending", "pending", "pending"]
    assert rec["requesters"] == {"count": 1, "me": True}
    rid = rec["recall_id"]
    assert {"recall_id": rid} in _queue(sql, "restores")
    assert {"recall_id": rid} in _queue(sql, "notify")
    # The owner joins it.
    owner = web.browser(w.owner)
    r = owner.post("/v1/recalls", {"file_id": str(fid), "priority": "standard"})
    assert r.status_code == 200 and r.json()["joined"] is True
    assert r.json()["recall_id"] == rid and r.json()["requesters"]["count"] == 2
    # The four-step view follows recall_state_log.
    sql.execute("UPDATE catalog.recall SET state = 'copying' WHERE recall_id = %s", (int(rid),))
    v = reader.get("/v1/recalls/{recall_id}", recall_id=rid).json()
    assert [s["status"] for s in v["steps"]] == ["done", "current", "pending", "pending"]
    assert v["steps"][1]["at"] is not None and v["name"] == "old.segy"
    sql.execute("UPDATE catalog.recall SET state = 'failed' WHERE recall_id = %s", (int(rid),))
    v = reader.get("/v1/recalls/{recall_id}", recall_id=rid).json()
    assert [s["status"] for s in v["steps"]] == ["done", "done", "failed", "pending"]
    assert v["failure"] and "provider" not in v["failure"].lower()
    # Not a requester and can't read it: 404.
    assert web.browser(w.outsider).get("/v1/recalls/{recall_id}",
                                       recall_id=rid).status_code == 404  # fmt: skip
    mine = reader.get("/v1/recalls", params={"scope": "mine"}).json()["items"]
    assert [x["recall_id"] for x in mine] == [rid]
    assert reader.get("/v1/recalls", params={"scope": "all"}).status_code == 403
    r = owner.get("/v1/recalls", params={"scope": "share", "share_id": str(w.share_id)})
    assert [x["recall_id"] for x in r.json()["items"]] == [rid]
    audit = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'recall.request' AND file_id = %s",
        (fid,),
    ).fetchone()
    assert audit == (2,)


def test_admin_sees_recalls_without_names(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "old.segy", tier="archive", state="stub")
    web.browser(w.reader).post("/v1/recalls", {"file_id": str(fid), "priority": "standard"})
    items = web.browser(w.admin).get("/v1/recalls", params={"scope": "all"}).json()["items"]
    mine = [i for i in items if i["file_id"] == str(fid)]
    assert mine and mine[0]["name"] is None and mine[0]["share_id"] is None


def test_partner_recall_with_an_app_token(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "old.segy", tier="archive", state="stub")
    token = web.app_token(["Recall.Request"])
    r = web.call("POST", "/v1/recalls", token=token,
                 json={"file_id": str(fid), "priority": "standard"})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["name"] is None
    row = sql.execute(
        "SELECT via, proof, app_id FROM catalog.recall_requester WHERE recall_id = %s",
        (int(r.json()["recall_id"]),),
    ).fetchone()
    assert row == ("api", "proven", "prizmanyx-app")


# --- restores and downloads -------------------------------------------------------------------


def test_restore_needs_owner_or_writer_and_a_restorable_version(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "report.pdf", versions=2)
    old = w.versions["report.pdf"][0]
    reader = web.browser(w.reader)
    r = reader.post("/v1/files/{file_id}/versions/{version_id}/restore",
                    {"target": "overwrite"}, file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    writer = web.browser(w.writer)
    r = writer.post("/v1/files/{file_id}/versions/{version_id}/restore",
                    {"target": "restored", "name": "a/b"}, file_id=fid, version_id=old,
                    check=False)  # fmt: skip
    assert r.status_code == 400
    r = writer.post("/v1/files/{file_id}/versions/{version_id}/restore",
                    {"target": "restored"}, file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 202, r.text
    vr = r.json()
    assert vr["state"] == "requested" and vr["name"] == "report-restored.pdf"
    assert {"kind": "version_restore", "restore_id": vr["restore_id"]} in _queue(sql, "portal")
    assert writer.get("/v1/version-restores/{restore_id}",
                      restore_id=vr["restore_id"]).status_code == 200  # fmt: skip
    assert web.browser(w.owner).get("/v1/version-restores/{restore_id}",
                                    restore_id=vr["restore_id"]).status_code == 404  # fmt: skip
    # A tombstone can't be restored.
    (tomb,) = sql.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at) VALUES (%s, %s, %s, 'report.pdf', 'tombstone', 'committed', now())"
        " RETURNING version_id",
        (fid, w.dataset_id, w.root_id),
    ).fetchone()
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    r = writer.post("/v1/files/{file_id}/versions/{version_id}/restore", {"target": "restored"},
                    file_id=fid, version_id=tomb)  # fmt: skip
    assert r.status_code in (404, 409)
    # A deleted file's old version restores as a copy, not as current.
    r = writer.post("/v1/files/{file_id}/versions/{version_id}/restore", {"target": "overwrite"},
                    file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "version_not_restorable"


def test_worker_restores_as_current_and_as_copy(web, sql, db_url):
    w = seed(sql)
    fid, sim = stored(sql, w, "model.bin", versions=2)
    old = w.versions["model.bin"][0]
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    owner = web.browser(w.owner)
    ids = []
    for target in ("overwrite", "restored_timestamped"):
        r = owner.post("/v1/files/{file_id}/versions/{version_id}/restore", {"target": target},
                       file_id=fid, version_id=old)  # fmt: skip
        assert r.status_code == 202, r.text
        ids.append(int(r.json()["restore_id"]))
    now = datetime.now(UTC)
    for rid in ids:
        state = run_job(db_url, lambda c, rid=rid: portal_jobs.handle_restore(c, reg, rid, now))
        assert state == "done"
    cur = owner.get("/v1/version-restores/{restore_id}", restore_id=ids[0]).json()
    assert cur["state"] == "done" and cur["result_file_id"] == str(fid)
    (content, size, state) = sql.execute(
        "SELECT content_version_id, size, state FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert content == int(cur["result_version_id"]) and size == len(DATA) + 1
    assert state == "stub"
    versions = owner.get("/v1/files/{file_id}/versions", file_id=fid).json()["items"]
    assert len(versions) == 3 and versions[0]["superseded_at"] is None
    # The bytes are the old version's.
    loc = sql.execute(
        "SELECT container, object_key, backend_version_id, crc64nvme FROM catalog.location"
        " WHERE version_id = %s", (content,)).fetchone()  # fmt: skip
    assert bytes(loc[3]) == to_bytes(crc64nvme(DATA + bytes([0])))
    copy = owner.get("/v1/version-restores/{restore_id}", restore_id=ids[1]).json()
    assert copy["result_file_id"] != str(fid)
    detail = owner.get("/v1/files/{file_id}", file_id=copy["result_file_id"]).json()
    assert re.fullmatch(r"model-restored-\d{4}-\d\d-\d\d-\d{6}Z\.bin", detail["name"])
    verify = _queue(sql, "verify")
    assert len([m for m in verify if "location_id" in m]) >= 2
    # Running a finished job again changes nothing.
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, ids[0], now)) == "done"


def test_a_finished_restore_closes_its_recall(web, sql, db_url):
    """#55 review: the recall that brought an old version's bytes back is in
    `downloading` (no edge fills an old version for a restore). When the
    restore is done the recall is closed as `available` and users are told,
    so it never stays `downloading` (or is failed by the edge timeout)."""
    w = seed(sql)
    fid, sim = stored(sql, w, "back.bin", versions=2)
    old = w.versions["back.bin"][0]
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    r = web.browser(w.owner).post("/v1/files/{file_id}/versions/{version_id}/restore",
                                  {"target": "restored"}, file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 202, r.text
    restore_id = int(r.json()["restore_id"])
    (recall_id,) = sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, state)"
        " VALUES (%s, %s, (SELECT edge_id FROM catalog.dataset WHERE dataset_id = %s),"
        " 'downloading') RETURNING recall_id",
        (fid, old, w.dataset_id),
    ).fetchone()
    sql.execute("UPDATE catalog.version_restore SET recall_id = %s WHERE restore_id = %s",
                (recall_id, restore_id))  # fmt: skip
    sql.execute("DELETE FROM pgmq.q_notify")
    now = datetime.now(UTC)
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, restore_id, now)) == "done"
    (state,) = sql.execute(
        "SELECT state FROM catalog.recall WHERE recall_id = %s", (recall_id,)
    ).fetchone()
    assert state == "available"
    assert {"recall_id": str(recall_id)} in _queue(sql, "notify")


def test_archived_version_restore_waits_for_its_recall(web, sql, db_url):
    w = seed(sql)
    fid = add_file(sql, w, "cold.segy", tier="archive", state="stub", versions=2)
    old = w.versions["cold.segy"][0]
    r = web.browser(w.owner).post("/v1/files/{file_id}/versions/{version_id}/restore",
                                  {"target": "restored"}, file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 202
    vr = r.json()
    assert vr["state"] == "waiting_recall" and vr["recall_id"]
    reg = BackendRegistry.from_mapping({})
    now = datetime.now(UTC)
    assert run_job(db_url, lambda c: portal_jobs.handle_restore(
        c, reg, int(vr["restore_id"]), now)) == "retry"  # fmt: skip
    sql.execute("UPDATE catalog.recall SET state = 'failed' WHERE recall_id = %s",
                (int(vr["recall_id"]),))  # fmt: skip
    state = run_job(
        db_url, lambda c: portal_jobs.handle_restore(c, reg, int(vr["restore_id"]), now)
    )
    assert state == "failed"  # fmt: skip


def test_download_flow(web, sql, db_url):
    w = seed(sql)
    fid, sim = stored(sql, w, "map.tif")
    vid = w.versions["map.tif"][0]
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    reader = web.browser(w.reader)
    r = reader.post("/v1/files/{file_id}/versions/{version_id}/downloads",
                    {"disposition": "attachment"}, file_id=fid, version_id=vid)  # fmt: skip
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["state"] == "preparing" and d["url"] is None
    now = datetime.now(UTC)
    assert run_job(db_url, lambda c: portal_jobs.handle_download(
        c, reg, int(d["download_id"]), now)) == "ready"  # fmt: skip
    got = reader.get("/v1/downloads/{download_id}", download_id=d["download_id"]).json()
    assert got["state"] == "ready" and got["url"].startswith("http")
    exp = datetime.fromisoformat(got["expires_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=9) < exp - now <= timedelta(minutes=10, seconds=1)
    # Only the requester sees it.
    assert web.browser(w.owner).get("/v1/downloads/{download_id}",
                                    download_id=d["download_id"]).status_code == 404  # fmt: skip
    # Audited, and the URL isn't in the audit row.
    (details,) = sql.execute(
        "SELECT details::text FROM catalog.audit_log WHERE action = 'version.download'"
        " AND file_id = %s", (fid,)).fetchone()  # fmt: skip
    assert "http" not in details
    # The sweep clears it once expired.
    sql.execute("UPDATE catalog.download_request SET expires_at = now() - interval '1 second'"
                " WHERE download_id = %s", (int(d["download_id"]),))  # fmt: skip
    run_job(db_url, lambda c: portal_jobs.sweep(c, datetime.now(UTC)))
    row = sql.execute("SELECT state, url FROM catalog.download_request WHERE download_id = %s",
                      (int(d["download_id"]),)).fetchone()  # fmt: skip
    assert row == ("expired", None)


def test_archived_download_needs_recall(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "deep.segy", tier="archive", state="stub")
    r = web.browser(w.reader).post("/v1/files/{file_id}/versions/{version_id}/downloads", {},
                                   file_id=fid, version_id=w.versions["deep.segy"][0])  # fmt: skip
    assert r.status_code == 202
    d = r.json()
    assert d["state"] == "needs_recall" and d["recall_options"]["needs_restore"] is True
    assert not [m for m in _queue(sql, "portal") if m.get("download_id") == d["download_id"]]


def test_sweep_removes_expired_sessions_and_logins(web, sql, db_url):
    w = seed(sql)
    web.browser(w.reader)
    sql.execute("UPDATE catalog.web_session SET idle_expires_at = now() - interval '1 second'"
                " WHERE entra_object_id = %s", (w.reader["oid"],))  # fmt: skip
    web.client.get("/auth/login", follow_redirects=False)
    sql.execute("UPDATE catalog.oidc_login SET expires_at = now() - interval '1 second'")
    run_job(db_url, lambda c: portal_jobs.sweep(c, datetime.now(UTC)))
    assert sql.execute("SELECT count(*) FROM catalog.web_session WHERE entra_object_id = %s",
                       (w.reader["oid"],)).fetchone() == (0,)  # fmt: skip
    assert sql.execute("SELECT count(*) FROM catalog.oidc_login").fetchone() == (0,)


def test_portal_jobs_register_with_the_worker():
    from omnifile_service.worker.runner import Worker

    async def no_conn():
        raise AssertionError("not called")

    worker = Worker(connect=no_conn, registry=BackendRegistry.from_mapping({}))
    assert portal_jobs.QUEUE in worker.consumers
    assert "portal_sweep" in worker.sweeps


def test_a_version_restore_and_a_download_hold_nothing_across_their_calls(web, sql, db_url):
    """Run: their row locks took a transaction id across Copy Blob, which has no time
    promise, and every edge's feed waited for it."""
    w = seed(sql)
    fid, sim = stored(sql, w, "model.bin", versions=2)
    old = w.versions["model.bin"][0]
    provider = Watched(sim, db_url)
    reg = BackendRegistry.from_mapping({w.backend_id: provider})
    jobs = portal_jobs.portal_jobs(FakeWorker(reg, Clock(datetime.now(UTC))))
    sql.execute("DELETE FROM pgmq.q_portal")
    owner = web.browser(w.owner)
    r = owner.post("/v1/files/{file_id}/versions/{version_id}/restore", {"target": "overwrite"},
                   file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 202, r.text
    r2 = owner.post("/v1/files/{file_id}/versions/{version_id}/downloads",
                    {"disposition": "attachment"}, file_id=fid, version_id=old)  # fmt: skip
    assert r2.status_code == 202, r2.text
    stats = drain_job(db_url, jobs, "portal")
    assert stats.dead == 0, stats.errors
    assert {"copy_object", "head", "edge_credential"} <= set(provider.calls())
    assert provider.with_ids() == [] and provider.with_locks() == []
    got = owner.get("/v1/version-restores/{restore_id}", restore_id=r.json()["restore_id"]).json()
    assert got["state"] == "done"
    assert owner.get("/v1/downloads/{download_id}",
                     download_id=r2.json()["download_id"]).json()["state"] == "ready"  # fmt: skip


def test_a_file_deleted_during_a_version_restores_copy_isnt_restored_over(web, sql, db_url):
    """The handler comes back through its checks with its locks taken again."""
    w = seed(sql)
    fid, sim = stored(sql, w, "model.bin", versions=2)
    old = w.versions["model.bin"][0]

    def delete_it(call):
        if call == "copy_object":
            with psycopg.connect(db_url, autocommit=True) as c, c.transaction():
                c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                c.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))

    reg = BackendRegistry.from_mapping({w.backend_id: Watched(sim, db_url, delete_it)})
    jobs = portal_jobs.portal_jobs(FakeWorker(reg, Clock(datetime.now(UTC))))
    sql.execute("DELETE FROM pgmq.q_portal")
    owner = web.browser(w.owner)
    r = owner.post("/v1/files/{file_id}/versions/{version_id}/restore", {"target": "overwrite"},
                   file_id=fid, version_id=old)  # fmt: skip
    assert r.status_code == 202, r.text
    stats = drain_job(db_url, jobs, "portal")
    assert stats.dead == 0, stats.errors
    got = owner.get("/v1/version-restores/{restore_id}", restore_id=r.json()["restore_id"]).json()
    assert got["state"] == "failed" and "deleted" in got["failure"]


# --- a recall whose file was replaced or deleted (migration 0059) ---------------------------


def _commit(sql, w, name: str, kind: str = "content", *, actor: int | None = None,
            proof: str = "proven") -> int:  # fmt: skip
    """A new content version of `name`, or its tombstone, as an edge commits one."""
    fid = w.files[name]
    (vid,) = sql.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size, acl_id, actor_via, actor_proof, actor_identity_id)"
        " VALUES (%s, %s, %s, %s, %s, 'committed', now(), %s, %s, %s, %s, %s)"
        " RETURNING version_id",
        (fid, w.dataset_id, w.root_id, name, kind, None if kind == "tombstone" else 4096,
         w.acl_open, "smb" if actor else None, proof if actor else None, actor),
    ).fetchone()  # fmt: skip
    if kind == "tombstone":
        sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    return vid


def test_a_recall_of_a_replaced_file_reads_as_superseded_in_the_portal(web, sql):
    """The soak's recall 209: the portal said the recall failed. It stopped because the
    file was replaced; the steps it reached stay done and none is marked failed."""
    w = seed(sql)
    fid = add_file(sql, w, "old.segy", tier="archive", state="stub")
    reader = web.browser(w.reader)
    rid = reader.post("/v1/recalls", {"file_id": str(fid), "priority": "standard"}).json()[
        "recall_id"
    ]
    sql.execute("UPDATE catalog.recall SET state = 'rehydrating' WHERE recall_id = %s",
                (int(rid),))  # fmt: skip
    _commit(sql, w, "old.segy")

    r = reader.get("/v1/recalls/{recall_id}", recall_id=rid)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["state"] == "superseded" and v["failure"] is None and v["completed_at"]
    assert v["superseded"] == {
        "reason": "replaced",
        "text": "The file was replaced while it was being recalled.",
        "by": None,
    }
    assert [s["status"] for s in v["steps"]] == ["done", "done", "pending", "pending"]
    listed = reader.get("/v1/recalls", params={"scope": "mine", "state": "superseded"})
    assert [x["recall_id"] for x in listed.json()["items"]] == [rid]
    assert (
        reader.get("/v1/recalls", params={"scope": "mine", "state": "failed"}).json()["items"] == []
    )
    # Nothing is left to join: the file's content is new, and a recall of it is its own.
    assert (
        reader.get("/v1/files/{file_id}/recall-options", file_id=fid).json()["active_recall"]
        is None
    )
    # A recall that didn't end this way says nothing of it.
    other = add_file(sql, w, "other.segy", tier="archive", state="stub")
    plain = reader.post("/v1/recalls", {"file_id": str(other), "priority": "standard"}).json()
    assert plain["superseded"] is None


def test_who_deleted_it_is_told_to_a_reader_and_only_on_proof(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "gone.segy", tier="archive", state="stub")
    reader = web.browser(w.reader)
    rid = reader.post("/v1/recalls", {"file_id": str(fid), "priority": "standard"}).json()[
        "recall_id"
    ]
    (ana,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, display_name) VALUES (%s, 'user', 'Ana Ruiz')"
        " RETURNING identity_id",
        (f"{w.domain}-4411",),
    ).fetchone()
    _commit(sql, w, "gone.segy", "tombstone", actor=ana)
    v = reader.get("/v1/recalls/{recall_id}", recall_id=rid).json()
    assert v["state"] == "superseded" and v["name"] == "gone.segy"
    assert v["superseded"] == {
        "reason": "deleted",
        "text": "The file was deleted while it was being recalled.",
        "by": "Ana Ruiz",
    }
    # An ArchiveAdmin lists every recall but can't read this file: no name, and no one.
    items = web.browser(w.admin).get("/v1/recalls", params={"scope": "all"}).json()["items"]
    (mine,) = [i for i in items if i["recall_id"] == rid]
    assert mine["name"] is None and mine["superseded"]["reason"] == "deleted"
    assert mine["superseded"]["by"] is None

    # A uid an NFS client only claimed names no one, even to a reader.
    fid2 = add_file(sql, w, "claimed.segy", tier="archive", state="stub")
    rid2 = reader.post("/v1/recalls", {"file_id": str(fid2), "priority": "standard"}).json()[
        "recall_id"
    ]
    _commit(sql, w, "claimed.segy", "tombstone", actor=ana, proof="claimed")
    v2 = reader.get("/v1/recalls/{recall_id}", recall_id=rid2).json()
    assert v2["superseded"]["reason"] == "deleted" and v2["superseded"]["by"] is None


def _recall_row(sql, rid) -> tuple:
    return sql.execute(
        "SELECT state, superseded_reason, held_by_jobs, pinned FROM catalog.recall"
        " WHERE recall_id = %s",
        (int(rid),),
    ).fetchone()


def test_a_version_restore_holds_the_recall_it_waits_on(web, sql, db_url):
    """Decision 34: a restore is of the version it names. While it waits, the catalog
    counts it on its recall, here for the file's current version restored under another
    name, joining a recall a reader had started; neither a replace nor a delete of the
    file ends that recall."""
    w = seed(sql)
    fid = add_file(sql, w, "cold.segy", tier="archive", state="stub")
    cur = w.versions["cold.segy"][0]
    reader = web.browser(w.reader)
    rid = reader.post("/v1/recalls", {"file_id": str(fid), "priority": "standard"}).json()[
        "recall_id"
    ]
    assert _recall_row(sql, rid) == ("requested", None, 0, False)
    r = web.browser(w.owner).post(
        "/v1/files/{file_id}/versions/{version_id}/restore",
        {"target": "restored"},
        file_id=fid,
        version_id=cur,
    )
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "waiting_recall" and r.json()["recall_id"] == rid
    assert _recall_row(sql, rid) == ("requested", None, 1, False)
    _commit(sql, w, "cold.segy")
    _commit(sql, w, "cold.segy", "tombstone")
    assert _recall_row(sql, rid) == ("requested", None, 1, False)

    # The worker's own start (its recall's copy had expired): held from the start.
    fid2 = add_file(sql, w, "cold2.segy", tier="archive", state="stub")
    cur2 = w.versions["cold2.segy"][0]
    r = web.browser(w.owner).post(
        "/v1/files/{file_id}/versions/{version_id}/restore",
        {"target": "restored"},
        file_id=fid2,
        version_id=cur2,
    )
    vr = r.json()
    sql.execute(
        "UPDATE catalog.recall SET state = 'available', completed_at = now() WHERE recall_id = %s",
        (int(vr["recall_id"]),),
    )
    reg = BackendRegistry.from_mapping({})
    now = datetime.now(UTC)
    job = lambda c: portal_jobs.handle_restore(c, reg, int(vr["restore_id"]), now)  # noqa: E731
    assert run_job(db_url, job) == "retry"
    (again,) = sql.execute(
        "SELECT recall_id FROM catalog.version_restore WHERE restore_id = %s",
        (int(vr["restore_id"]),),
    ).fetchone()
    assert str(again) != vr["recall_id"]
    # The first recall is no longer waited on; the new one is.
    assert _recall_row(sql, vr["recall_id"])[2] == 0
    assert _recall_row(sql, again) == ("requested", None, 1, False)
    _commit(sql, w, "cold2.segy")
    assert _recall_row(sql, again)[:2] == ("requested", None)


def _restore_of_the_current_version(web, sql, w, name):
    """A copy-restore of `name`'s current version, waiting on a `downloading` recall of
    it: the recall's bytes are back, and no edge has filled the live file yet."""
    fid, sim = stored(sql, w, name)
    cur = w.versions[name][0]
    r = web.browser(w.owner).post(
        "/v1/files/{file_id}/versions/{version_id}/restore",
        {"target": "restored"},
        file_id=fid,
        version_id=cur,
    )
    assert r.status_code == 202, r.text
    restore_id = int(r.json()["restore_id"])
    (recall_id,) = sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, state)"
        " VALUES (%s, %s, (SELECT edge_id FROM catalog.dataset WHERE dataset_id = %s),"
        " 'downloading') RETURNING recall_id",
        (fid, cur, w.dataset_id),
    ).fetchone()
    sql.execute(
        "UPDATE catalog.version_restore SET state = 'waiting_recall', recall_id = %s"
        " WHERE restore_id = %s",
        (recall_id, restore_id),
    )
    assert _recall_row(sql, recall_id) == ("downloading", None, 1, False)
    sql.execute("DELETE FROM pgmq.q_notify")
    return sim, restore_id, recall_id


def _finish(db_url, w, sim, restore_id) -> str:
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    now = datetime.now(UTC)
    return run_job(db_url, lambda c: portal_jobs.handle_restore(c, reg, restore_id, now))


@pytest.mark.parametrize("state", ["downloading", "rehydrating"])
@pytest.mark.parametrize("change", ["tombstone", "content"])
def test_a_restores_recall_is_closed_when_its_file_was_deleted_or_replaced_meanwhile(
    web, sql, db_url, change, state
):
    """#96 review. The owner restores the file's current version under another name, and
    the file is then deleted (or replaced). The restore holds the recall, so the change
    doesn't end it. But no edge fills a deleted file, and none fills a version the file
    has left: left `downloading`, the recall was failed by the idle rule an hour later
    (a deleted file keeps its content version), with a failure card. The restore closes
    it as `available` when it is done.

    `rehydrating`: the restore can read the bytes as soon as the change feed has
    committed the recall's copy, before the recall's own poll marks it `downloading`.
    M3 step 13c caught a restore done 0.7 s before that: the recall was not closed,
    became `downloading` after, and stayed so."""
    w = seed(sql)
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "cur.bin")
    sql.execute("UPDATE catalog.recall SET state = %s WHERE recall_id = %s", (state, recall_id))
    sql.execute("DELETE FROM pgmq.q_notify")
    _commit(sql, w, "cur.bin", change)
    assert _recall_row(sql, recall_id) == (state, None, 1, False)
    assert _queue(sql, "notify") == []
    assert _finish(db_url, w, sim, restore_id) == "done"
    assert _recall_row(sql, recall_id) == ("available", None, 0, False)
    assert {"recall_id": str(recall_id)} in _queue(sql, "notify")
    # Nothing is left for the idle rule to fail.
    assert sql.execute(
        "SELECT count(*) FROM catalog.recall WHERE file_id = (SELECT file_id FROM catalog.recall"
        " WHERE recall_id = %s) AND state IN ('requested', 'rehydrating', 'copying',"
        " 'downloading')",
        (recall_id,),
    ).fetchone() == (0,)


@pytest.mark.parametrize("state", ["downloading", "rehydrating"])
def test_a_restore_that_is_done_leaves_a_recall_a_rewind_reader_pinned(web, sql, db_url, state):
    """Review of #96's additions. A rewind reader's recall is pinned: an edge is filling
    the label it reads. A restore of the same version joined it, and the file was then
    replaced. Done, the restore leaves the recall to the edge. Closed as `available`
    before the edge had `downloading` and its credential, the label was never filled
    and the reader had to ask again."""
    w = seed(sql)
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "pin.bin")
    sql.execute(
        "UPDATE catalog.recall SET state = %s, pinned = true WHERE recall_id = %s",
        (state, recall_id),
    )
    sql.execute("DELETE FROM pgmq.q_notify")
    _commit(sql, w, "pin.bin", "content")
    assert _recall_row(sql, recall_id) == (state, None, 1, True)
    assert _finish(db_url, w, sim, restore_id) == "done"
    assert _recall_row(sql, recall_id) == (state, None, 0, True)
    assert {"recall_id": str(recall_id)} not in _queue(sql, "notify")


@pytest.mark.parametrize(("change", "reason"), [("content", "replaced"), ("tombstone", "deleted")])
def test_a_recall_is_an_ordinary_one_again_once_its_restore_is_done(
    web, sql, db_url, change, reason
):
    """The restore is done while the file is still there with that version: the recall
    is left to the edge, which is filling the live file. The restore no longer waits on
    it, so a replace or a delete after that ends it as it ends any recall. Before, it
    stayed kept, the edge's fill met another file and said nothing, and the recall
    stayed `downloading` (#96 review)."""
    w = seed(sql)
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "live.bin")
    assert _finish(db_url, w, sim, restore_id) == "done"
    assert _recall_row(sql, recall_id) == ("downloading", None, 0, False)
    _commit(sql, w, "live.bin", change)
    assert _recall_row(sql, recall_id) == ("superseded", reason, 0, False)
    assert {"recall_id": str(recall_id)} in _queue(sql, "notify")


def _take_its_name(sql, w, restore_id: int) -> None:
    """Someone makes a file under the restore's name while it waits: its next step
    fails it (`name_taken`)."""
    (name,) = sql.execute(
        "SELECT name FROM catalog.version_restore WHERE restore_id = %s", (restore_id,)
    ).fetchone()
    add_file(sql, w, name)


def _ended_by(sql, recall_id) -> tuple:
    return sql.execute(
        "SELECT superseded_by_version_id, moved_on_version_id FROM catalog.recall"
        " WHERE recall_id = %s",
        (int(recall_id),),
    ).fetchone()


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (("content",), "replaced"),
        (("tombstone",), "deleted"),
        (("content", "tombstone"), "replaced"),
    ],
)
def test_a_restore_that_fails_ends_the_recall_its_file_moved_on_from(
    web, sql, db_url, changes, reason
):
    """#96, second review. The restore holds its recall while the file is replaced or
    deleted, and then fails. It let go, and only a restore that is done closed the
    recall: this one stayed active for a version no edge fills. A deleted file's was
    failed by the idle rule an hour later, with a failure card; a replaced file's stayed
    `downloading` for good. Now it ends when the restore lets go, as it would have at
    the commit that passed it by: the same reason, and the same version named."""
    w = seed(sql)
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "left.bin")
    first = [_commit(sql, w, "left.bin", kind) for kind in changes][0]
    assert _recall_row(sql, recall_id) == ("downloading", None, 1, False)
    assert _ended_by(sql, recall_id) == (None, first)  # kept, and marked
    assert _queue(sql, "notify") == []
    _take_its_name(sql, w, restore_id)
    assert _finish(db_url, w, sim, restore_id) == "failed"
    assert _recall_row(sql, recall_id) == ("superseded", reason, 0, False)
    assert _ended_by(sql, recall_id) == (first, first)
    assert _queue(sql, "notify") == [{"recall_id": str(recall_id)}]
    (failure, done) = sql.execute(
        "SELECT failure, completed_at IS NOT NULL FROM catalog.recall WHERE recall_id = %s",
        (recall_id,),
    ).fetchone()
    assert failure is None and done


def test_a_restore_that_fails_leaves_a_recall_that_is_still_wanted(web, sql, db_url):
    """What must not end when a job lets go: a recall of content the file still holds
    (an edge fills it), one a rewind reader pinned, and one another job still waits on."""
    w = seed(sql)
    # The file is as it was: the recall is an ordinary one again, and the next replace ends it.
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "same.bin")
    _take_its_name(sql, w, restore_id)
    assert _finish(db_url, w, sim, restore_id) == "failed"
    assert _recall_row(sql, recall_id) == ("downloading", None, 0, False)
    _commit(sql, w, "same.bin")
    assert _recall_row(sql, recall_id) == ("superseded", "replaced", 0, False)
    # Pinned by a rewind reader, who still wants the version.
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "pinned.bin")
    sql.execute("UPDATE catalog.recall SET pinned = true WHERE recall_id = %s", (recall_id,))
    _commit(sql, w, "pinned.bin")
    _take_its_name(sql, w, restore_id)
    assert _finish(db_url, w, sim, restore_id) == "failed"
    assert _recall_row(sql, recall_id) == ("downloading", None, 0, True)
    # Two jobs: the first to let go ends nothing, the last does.
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "two.bin")
    r = web.browser(w.owner).post(
        "/v1/files/{file_id}/versions/{version_id}/restore",
        {"target": "restored_timestamped"},
        file_id=w.files["two.bin"],
        version_id=w.versions["two.bin"][0],
    )
    assert r.status_code == 202, r.text
    second = int(r.json()["restore_id"])
    sql.execute(
        "UPDATE catalog.version_restore SET state = 'waiting_recall', recall_id = %s"
        " WHERE restore_id = %s",
        (recall_id, second),
    )
    assert _recall_row(sql, recall_id) == ("downloading", None, 2, False)
    _commit(sql, w, "two.bin", "tombstone")
    _take_its_name(sql, w, restore_id)
    assert _finish(db_url, w, sim, restore_id) == "failed"
    assert _recall_row(sql, recall_id) == ("downloading", None, 1, False)
    _take_its_name(sql, w, second)
    assert _finish(db_url, w, sim, second) == "failed"
    assert _recall_row(sql, recall_id) == ("superseded", "deleted", 0, False)


def test_a_deleted_files_recall_ends_as_deleted_even_with_no_mark(web, sql, db_url):
    """A recall asked for after the file was deleted (a restore of a deleted file's last
    version under a new name), or one kept since before migration 0059, has no mark of
    the commit that passed it by. The idle rule would still fail it an hour after its
    job let go: a deleted file keeps its content version, and no edge fills it. It ends
    as `deleted`, by the file's tombstone. A replaced file's recall with no mark is a
    recall of an old version, which nothing judges; it is left as it was before."""
    w = seed(sql)
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "gone.bin")
    tomb = _commit(sql, w, "gone.bin", "tombstone")
    sql.execute("UPDATE catalog.recall SET moved_on_version_id = NULL")
    _take_its_name(sql, w, restore_id)
    assert _finish(db_url, w, sim, restore_id) == "failed"
    assert _recall_row(sql, recall_id) == ("superseded", "deleted", 0, False)
    assert _ended_by(sql, recall_id) == (tomb, None)
    sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "old.bin")
    _commit(sql, w, "old.bin")
    sql.execute("UPDATE catalog.recall SET moved_on_version_id = NULL")
    _take_its_name(sql, w, restore_id)
    assert _finish(db_url, w, sim, restore_id) == "failed"
    assert _recall_row(sql, recall_id) == ("downloading", None, 0, False)


def test_a_waiting_restore_that_is_deleted_lets_go_of_its_recall(web, sql):
    """Nothing deletes a restore or a placement today. If something ever does, a job
    that was waiting must not leave its recall held for good (and, its file having moved
    on, active for good): a delete is counted like any other end. A placement's is in
    `test_annex_returns.py`."""
    w = seed(sql)
    for name, moved in (("kept.bin", False), ("moved.bin", True)):
        _sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, name)
        if moved:
            _commit(sql, w, name)
        sql.execute("DELETE FROM catalog.version_restore WHERE restore_id = %s", (restore_id,))
        assert _recall_row(sql, recall_id) == (
            ("superseded", "replaced", 0, False) if moved else ("downloading", None, 0, False)
        )
    # A job that had ended is not counted again.
    _sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "ended.bin")
    sql.execute(
        "UPDATE catalog.version_restore SET state = 'failed', failure = 'x' WHERE restore_id = %s",
        (restore_id,),
    )
    assert _recall_row(sql, recall_id) == ("downloading", None, 0, False)
    sql.execute("UPDATE catalog.recall SET pinned = true WHERE recall_id = %s", (recall_id,))
    _commit(sql, w, "ended.bin")
    sql.execute("UPDATE catalog.recall SET pinned = false WHERE recall_id = %s", (recall_id,))
    sql.execute("DELETE FROM catalog.version_restore WHERE restore_id = %s", (restore_id,))
    assert _recall_row(sql, recall_id) == ("downloading", None, 0, False)


def test_a_commit_waits_for_a_job_that_is_letting_go(web, sql, db_url):
    """The commit and the job's end at the same instant. The job's transaction holds the
    recall's row: it has counted itself off, and seen a file that had not moved on, so it
    ended nothing. The commit must wait for it and look at the row again; then it finds
    the recall unheld and ends it. When the commit tested the count in its WHERE, the
    row did not match as it first saw it, it was not waited for, and neither side ended
    the recall."""
    import threading
    import time

    w = seed(sql)
    _sim, restore_id, recall_id = _restore_of_the_current_version(web, sql, w, "race.bin")
    errors: list[BaseException] = []

    def the_commit() -> None:
        try:
            with psycopg.connect(db_url, autocommit=True) as c:
                _commit(c, w, "race.bin")
        except psycopg.Error as e:  # pragma: no cover - shown by the assert below
            errors.append(e)

    with psycopg.connect(db_url) as a:
        with a.transaction():
            a.execute(
                "UPDATE catalog.version_restore SET state = 'failed', failure = 'x'"
                " WHERE restore_id = %s",
                (restore_id,),
            )
            t = threading.Thread(target=the_commit)
            t.start()
            waited = 0
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not waited and t.is_alive():
                (waited,) = sql.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                ).fetchone()
                time.sleep(0.05)
            assert waited, "the commit did not wait for the job's transaction"
        t.join(30)
    assert not errors, errors
    assert _recall_row(sql, recall_id) == ("superseded", "replaced", 0, False)


def test_a_restore_whose_recall_was_superseded_recalls_the_version_again(web, sql, db_url):
    """Codex on #96. A restore holds its recall, so this should not happen; a restore
    made before migration 0059 counted it could find its recall `superseded`. It handled
    `failed`, `cancelled` and `available` only, so it waited on it for ever. The version
    is still wanted: it is recalled again."""
    w = seed(sql)
    fid = add_file(sql, w, "wanted.segy", tier="archive", state="stub", versions=2)
    old = w.versions["wanted.segy"][0]
    r = web.browser(w.owner).post(
        "/v1/files/{file_id}/versions/{version_id}/restore",
        {"target": "restored"},
        file_id=fid,
        version_id=old,
    )
    vr = r.json()
    assert vr["state"] == "waiting_recall"
    sql.execute(
        "UPDATE catalog.recall SET state = 'superseded', superseded_reason = 'replaced',"
        " completed_at = now() WHERE recall_id = %s",
        (int(vr["recall_id"]),),
    )
    reg = BackendRegistry.from_mapping({})
    now = datetime.now(UTC)
    job = lambda c: portal_jobs.handle_restore(c, reg, int(vr["restore_id"]), now)  # noqa: E731
    assert run_job(db_url, job) == "retry"
    state, again = sql.execute(
        "SELECT state, recall_id FROM catalog.version_restore WHERE restore_id = %s",
        (int(vr["restore_id"]),),
    ).fetchone()
    assert state == "waiting_recall" and str(again) != vr["recall_id"]
    assert _recall_row(sql, again) == ("requested", None, 1, False)
    assert {"recall_id": str(again)} in _queue(sql, "restores")


def test_the_migration_counts_the_jobs_that_were_already_waiting(web, sql):
    """Codex on #96. The count is kept by triggers from migration 0059 on. A restore or
    a placement that was already waiting when it ran is counted by its backfill;
    otherwise the next write of the file would end the recall under it. This runs the
    migration's own statement on rows as an earlier catalog left them."""
    from pathlib import Path

    import omnifile_service.catalog as catalog

    text = (Path(catalog.__file__).parent / "migrations/0059_recall_superseded.sql").read_text()
    start = text.index(
        "UPDATE catalog.recall r\n   SET held_by_jobs = catalog.recall_jobs(r.recall_id)"
    )
    backfill = text[start : text.index(";", start) + 1]

    w = seed(sql)
    fid = add_file(sql, w, "before.segy", tier="archive", state="stub")
    cur = w.versions["before.segy"][0]
    r = web.browser(w.owner).post(
        "/v1/files/{file_id}/versions/{version_id}/restore",
        {"target": "restored"},
        file_id=fid,
        version_id=cur,
    )
    rid = r.json()["recall_id"]
    done = add_file(sql, w, "ended.segy", tier="archive", state="stub")
    other = (
        web.browser(w.reader)
        .post("/v1/recalls", {"file_id": str(done), "priority": "standard"})
        .json()["recall_id"]
    )
    # As before the migration: nothing counted.
    sql.execute("UPDATE catalog.recall SET held_by_jobs = 0")
    sql.execute(backfill)
    assert _recall_row(sql, rid)[2] == 1 and _recall_row(sql, other)[2] == 0
    _commit(sql, w, "before.segy")
    assert _recall_row(sql, rid)[:2] == ("requested", None)
