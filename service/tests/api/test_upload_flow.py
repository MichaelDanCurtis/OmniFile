"""The M1 data path through the api: create -> upload (real SAS against the sim
facade) -> complete -> feed -> stub -> recall -> fill (sections 3, 4.1-4.4)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from .conftest import E, World, complete_body, create_file, hashes_of, upload_blocks

DATA = b"SEG-Y header" + bytes(range(256)) * 40  # 10,252 bytes


def intent(w: World, file_id: int, data: bytes, **extra) -> dict:
    body = {
        "op_id": f"commit-{uuid.uuid4().hex}",
        "file": {"file_id": str(file_id)},
        "size": len(data),
        "mtime_ns": "1700000000123456789",
        "block_size": 4096,
        **hashes_of(data),
    }
    body.update(extra)
    return body


def upload(w: World, facade_client, file_id: int, data: bytes = DATA) -> tuple[dict, dict]:
    body = intent(w, file_id, data)
    resp = w.edge.post(f"{E}/uploads", body)
    assert resp.status_code == 201, resp.text
    up = resp.json()
    assert up["action"] == "upload"
    upload_blocks(facade_client, up["target"]["url"], data, 4096)
    done = w.edge.post(
        f"{E}/uploads/{{upload_id}}/complete",
        complete_body(data),
        upload_id=up["upload_id"],
    )
    assert done.status_code == 200, done.text
    w.verify()  # the worker reads it back before anything may stub
    return up, done.json()


def feed_all(w: World, after: str = "0.0") -> tuple[list[dict], str]:
    events: list[dict] = []
    while True:
        resp = w.edge.get(f"{E}/feed", {"after": after, "limit": 1000})
        assert resp.status_code == 200, resp.text
        page = resp.json()
        events += page["events"]
        if page["next"] == after or not page["events"]:
            return events, page["next"]
        after = page["next"]


def latest(events: list[dict], file_id: int) -> dict:
    mine = [e for e in events if e.get("file", {}).get("file_id") == str(file_id)]
    assert mine, f"no event for file {file_id}"
    return mine[-1]


def test_upload_commits_a_plain_blob_and_the_feed_shows_it(make_world, facade_client, facade, sql):
    w = make_world()
    fid = create_file(w, "line_1042.segy")
    up, done = upload(w, facade_client, fid)

    # The write SAS is scoped to one blob, create+write, 15 minutes.
    url = urlsplit(up["target"]["url"])
    q = parse_qs(url.query)
    assert q["sp"] == ["cw"] and q["sr"] == ["b"]
    assert url.path == f"/live/data/{fid}/{up['version_id']}"
    assert up["set_tier"] is False and up["upload_id"] == up["version_id"]
    expires = datetime.fromisoformat(up["target"]["expires_at"].replace("Z", "+00:00"))
    cache = datetime.fromisoformat(up["target"]["cache_until"].replace("Z", "+00:00"))
    assert (expires - cache).total_seconds() == 300
    assert 800 < (expires - datetime.now(UTC)).total_seconds() <= 900

    assert done["version_id"] == up["version_id"]
    assert done["retention_until"] is None

    # The blob is plain bytes, and took the account default (Smart) tier.
    blob = facade_client.get(
        w.edge.post(
            f"{E}/credentials",
            {"access": "read", "file_id": str(fid), "version_id": done["version_id"]},
        ).json()["url"],
        headers={"x-ms-version": "2026-04-06"},
    )
    assert blob.status_code == 200 and blob.content == DATA
    assert blob.headers["x-ms-access-tier"] == "Smart"

    row = sql.execute(
        "SELECT f.state, f.resident_bytes, f.size, l.verified, l.verified_by, l.logical_tier,"
        "       encode(l.crc64nvme, 'hex')"
        "  FROM catalog.file f JOIN catalog.location l ON l.location_id = f.primary_location_id"
        " WHERE f.file_id = %s",
        (fid,),
    ).fetchone()
    assert row == (
        "resident",
        len(DATA),
        len(DATA),
        True,
        "azure_read_crc",  # Get Block List has no CRCs: the worker read it back
        "smart",
        hashes_of(DATA)["crc64nvme"],
    )

    events, _ = feed_all(w)
    ev = latest(events, fid)
    assert ev["type"] == "file.upsert"
    f = ev["file"]
    assert f["state"] == "resident" and f["version_id"] == done["version_id"]
    assert f["crc64nvme"] == hashes_of(DATA)["crc64nvme"]
    assert f["location"]["backend_kind"] == "azure_blob"
    assert f["location"]["logical_tier"] == "smart" and f["location"]["archived"] is False
    assert f["location"]["restore_state"] == "not_needed"
    # Events this edge's ops caused carry their origin.
    assert ev["origin"] == {"edge_id": str(w.edge.edge_id), "op_id": json_op(sql, done)}
    # The dataset and share reach the edge through the same feed.
    types = {e["type"] for e in events}
    assert {"dataset.upsert", "share.upsert", "file.upsert"} <= types


def json_op(sql, done: dict) -> str:
    return sql.execute(
        "SELECT edge_op_id FROM catalog.file_version WHERE version_id = %s",
        (int(done["version_id"]),),
    ).fetchone()[0]


def test_upload_intent_is_idempotent_and_checks_the_base_version(make_world, facade_client):
    w = make_world()
    fid = create_file(w, "a.bin")
    body = intent(w, fid, DATA)
    key = "upload-" + uuid.uuid4().hex
    first = w.edge.post(f"{E}/uploads", body, key=key)
    replay = w.edge.post(f"{E}/uploads", body, key=key)
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json() == first.json()
    # Same op_id, new key (the edge restarted): the same pending upload.
    again = w.edge.post(f"{E}/uploads", body)
    assert again.json()["upload_id"] == first.json()["upload_id"]
    # Same key, different body.
    other = w.edge.post(f"{E}/uploads", {**body, "size": 1}, key=key)
    assert other.status_code == 422 and other.json()["code"] == "idempotency_key_reused"

    up = first.json()
    upload_blocks(facade_client, up["target"]["url"], DATA, 4096)
    path = f"{E}/uploads/{{upload_id}}/complete"
    done = w.edge.post(path, complete_body(DATA), upload_id=up["upload_id"])
    assert done.status_code == 200
    # complete is idempotent by upload_id, even with a new key.
    again = w.edge.post(path, complete_body(DATA), upload_id=up["upload_id"])
    assert again.json() == done.json()
    # The committed op replays as dedupe: nothing more to upload.
    replay = w.edge.post(f"{E}/uploads", body)
    assert replay.json() == {"action": "dedupe", "version_id": up["version_id"]}

    # A stale base version is a conflict.
    data2 = b"new bytes" * 100
    stale = w.edge.post(f"{E}/uploads", intent(w, fid, data2, base_version_id=None))
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    ok = w.edge.post(f"{E}/uploads", intent(w, fid, data2, base_version_id=up["version_id"]))
    assert ok.status_code == 201 and ok.json()["action"] == "upload"


def test_complete_checks_the_blob(make_world, facade_client):
    w = make_world()
    fid = create_file(w, "b.bin")
    up = w.edge.post(f"{E}/uploads", intent(w, fid, DATA)).json()
    path = f"{E}/uploads/{{upload_id}}/complete"
    body = complete_body(DATA)
    missing = w.edge.post(path, body, upload_id=up["upload_id"])
    assert missing.status_code == 400 and "block list" in missing.json()["detail"]
    wrong = w.edge.post(path, {**body, "size": 3}, upload_id=up["upload_id"])
    assert wrong.status_code == 400
    upload_blocks(facade_client, up["target"]["url"], DATA[:100], 4096)
    short = w.edge.post(path, body, upload_id=up["upload_id"])
    assert short.status_code == 400 and "holds 100 bytes" in short.json()["detail"]


def test_same_bytes_dedupe_and_abort(make_world, facade_client, sql):
    w = make_world()
    a, b, c = (create_file(w, n) for n in ("a", "b", "c"))
    upload(w, facade_client, a)
    dup = w.edge.post(f"{E}/uploads", intent(w, b, DATA))
    assert dup.status_code == 201 and dup.json()["action"] == "dedupe"
    keys = sql.execute(
        "SELECT object_key FROM catalog.location WHERE file_id = ANY(%s) AND state = 'committed'",
        ([a, b],),
    ).fetchall()
    assert len(keys) == 2 and keys[0] == keys[1]  # one blob, two references
    assert sql.execute("SELECT state FROM catalog.file WHERE file_id = %s", (b,)).fetchone() == (
        "resident",
    )

    up = w.edge.post(f"{E}/uploads", intent(w, c, b"other" * 10)).json()
    abort = w.edge.post(f"{E}/uploads/{{upload_id}}/abort", {}, upload_id=up["upload_id"])
    assert abort.status_code == 204
    assert sql.execute(
        "SELECT count(*) FROM catalog.file_version WHERE version_id = %s",
        (int(up["upload_id"]),),
    ).fetchone() == (0,)
    assert sql.execute("SELECT state FROM catalog.file WHERE file_id = %s", (c,)).fetchone() == (
        "landing",
    )
    gone = w.edge.post(f"{E}/credentials", {"access": "write", "upload_id": up["upload_id"]})
    assert gone.status_code == 404


def test_write_credential_renews_for_a_pending_upload(make_world):
    w = make_world()
    fid = create_file(w, "big.bin")
    up = w.edge.post(f"{E}/uploads", intent(w, fid, DATA)).json()
    cred = w.edge.post(f"{E}/credentials", {"access": "write", "upload_id": up["upload_id"]})
    assert cred.status_code == 200
    assert urlsplit(cred.json()["url"]).path == urlsplit(up["target"]["url"]).path


def test_stub_handshake(make_world, facade_client, sql):
    w = make_world()
    fid = create_file(w, "s.segy")
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]
    sql.execute(
        "UPDATE catalog.file SET stub_version_id = %s, stub_resident_bytes = 512"
        " WHERE file_id = %s",
        (int(vid), fid),
    )
    prep = w.edge.post(f"{E}/files/{{file_id}}/stub/prepare", {"version_id": vid}, file_id=fid)
    assert prep.status_code == 200, prep.text
    lease = prep.json()
    assert lease["resident_bytes"] == 512

    # A forged lease (other size) is refused.
    bad = w.edge.post(
        f"{E}/files/{{file_id}}/stub/commit",
        {"lease_id": lease["lease_id"], "version_id": vid, "resident_bytes": 0},
        file_id=fid,
    )
    assert bad.status_code == 409 and bad.json()["code"] == "stub_not_allowed"

    body = {"lease_id": lease["lease_id"], "version_id": vid, "resident_bytes": 512}
    commit = w.edge.post(f"{E}/files/{{file_id}}/stub/commit", body, file_id=fid)
    assert commit.status_code == 204
    again = w.edge.post(f"{E}/files/{{file_id}}/stub/commit", body, file_id=fid)
    assert again.status_code == 204
    row = sql.execute(
        "SELECT state, resident_bytes, stub_version_id FROM catalog.file WHERE file_id = %s",
        (fid,),
    ).fetchone()
    assert row == ("stub", 512, None)
    events, _ = feed_all(w)
    f = latest(events, fid)["file"]
    assert f["state"] == "stub" and f["resident_bytes"] == 512 and f["stub"] is None


def test_stub_refusals(make_world, facade_client, sql):
    w = make_world()
    fid = create_file(w, "keep.segy")
    pre = w.edge.post(f"{E}/files/{{file_id}}/stub/prepare", {"version_id": "1"}, file_id=fid)
    assert pre.status_code == 409 and pre.json()["code"] == "version_conflict"
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]
    (ident,) = sql.execute(
        "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
        (f"S-1-5-21-9-9-{uuid.uuid4().int % 10**9}",),
    ).fetchone()
    sql.execute(
        "BEGIN; SELECT set_config('omnifile.actor', 'system:test', true);"
        " INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id)"
        f" VALUES ({w.dataset_id}, {w.root_id}, 'keep_online', 'project', {ident}); COMMIT;"
    )
    held = w.edge.post(f"{E}/files/{{file_id}}/stub/prepare", {"version_id": vid}, file_id=fid)
    assert held.status_code == 409
    assert held.json()["code"] == "stub_not_allowed" and "keep_online" in held.json()["detail"]

    skip = w.edge.post(
        f"{E}/files/{{file_id}}/stub/skip",
        {"version_id": vid, "reason": "open_for_write"},
        file_id=fid,
    )
    assert skip.status_code == 204


def archive(sql, file_id: int, size: int | None = None) -> None:
    sql.execute(
        "UPDATE catalog.location SET logical_tier = 'archive', storage_class = 'Archive'"
        " WHERE location_id = (SELECT primary_location_id FROM catalog.file WHERE file_id = %s)",
        (file_id,),
    )
    if size is not None:
        sql.execute(
            "UPDATE catalog.file_version SET size = %s"
            " WHERE version_id = (SELECT content_version_id FROM catalog.file WHERE file_id = %s)",
            (size, file_id),
        )


def read_req(fid: int, vid: str, on_open: str = "wait", uid: int = 41023) -> dict:
    return {
        "file_id": str(fid),
        "version_id": vid,
        "offset": 0,
        "length": 1048576,
        "requester": {
            "uid": uid,
            "via": "nfs",
            "sec": "sys",
            "proof": "claimed",
            "client_addr": "10.1.2.3",
        },
        "on_open": on_open,
    }


def test_online_read_and_recall(make_world, facade_client, sql):
    w = make_world()
    fid = create_file(w, "r.segy")
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]

    online = w.edge.post(f"{E}/recalls", read_req(fid, vid))
    assert online.status_code == 200 and online.json()["status"] == "online"
    got = facade_client.get(
        online.json()["credential"]["url"],
        headers={"x-ms-range": "bytes=0-11", "x-ms-version": "2026-04-06"},
    )
    assert got.status_code == 206 and got.content == DATA[:12]

    archive(sql, fid)
    first = w.edge.post(f"{E}/recalls", read_req(fid, vid))
    assert first.status_code == 200, first.text
    r = first.json()
    assert r["status"] == "recall" and r["joined"] is False and r["state"] == "requested"
    assert r["decision"] == "wait" and r["eta_confidence"] == "provider_sla" and r["eta"]
    assert "r.segy is archived" in r["note"]
    second = w.edge.post(f"{E}/recalls", read_req(fid, vid, on_open="fail", uid=41024))
    assert second.json()["recall_id"] == r["recall_id"] and second.json()["joined"] is True
    assert second.json()["decision"] == "fail"

    # One restore message for the worker, sent with the recall.
    msgs = sql.execute("SELECT message FROM pgmq.q_restores").fetchall()
    assert {"recall_id": r["recall_id"]} in [m[0] for m in msgs]
    assert [m[0] for m in msgs].count({"recall_id": r["recall_id"]}) == 1
    reqs = sql.execute(
        "SELECT uid, via, proof, host(client_addr) FROM catalog.recall_requester"
        " WHERE recall_id = %s ORDER BY uid",
        (int(r["recall_id"]),),
    ).fetchall()
    assert reqs == [(41023, "nfs", "claimed", "10.1.2.3"), (41024, "nfs", "claimed", "10.1.2.3")]

    # The worker would restore and move it to downloading; the feed then
    # carries a read credential for the bytes.
    sql.execute(
        "UPDATE catalog.file SET state = 'stub', resident_bytes = 0 WHERE file_id = %s", (fid,)
    )
    sql.execute(
        "UPDATE catalog.recall SET state = 'downloading' WHERE recall_id = %s",
        (int(r["recall_id"]),),
    )
    events, _ = feed_all(w)
    rec = [e for e in events if e["type"] == "recall.state"][-1]["recall"]
    assert rec["state"] == "downloading" and rec["read_credential"]["mode"] == (
        "azure_user_delegation_sas"
    )
    assert rec["requesters"] == [
        {"via": "nfs", "proof": "claimed"},
        {"via": "nfs", "proof": "claimed"},
    ]

    filled = w.edge.post(
        f"{E}/recalls/{{recall_id}}/filled",
        {"version_id": vid, "resident_bytes": len(DATA)},
        recall_id=r["recall_id"],
    )
    assert filled.status_code == 204
    assert sql.execute(
        "SELECT r.state, f.state, f.resident_bytes FROM catalog.recall r"
        " JOIN catalog.file f USING (file_id) WHERE r.recall_id = %s",
        (int(r["recall_id"]),),
    ).fetchone() == ("available", "resident", len(DATA))
    # A new read after the recall starts a new recall (the old one is done).
    third = w.edge.post(f"{E}/recalls", read_req(fid, vid))
    assert third.json()["recall_id"] != r["recall_id"]


def test_a_failed_fill_fails_the_recall_and_the_next_read_starts_a_new_one(
    make_world, facade_client, sql
):
    """WP3's soak: a fill that failed (the edge's disk full) left the recall
    `downloading` for good; every later read joined it. The edge now reports
    it, and the recall becomes `failed` with the reason."""
    w = make_world()
    fid = create_file(w, "full.segy")
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]
    archive(sql, fid)
    r = w.edge.post(f"{E}/recalls", read_req(fid, vid)).json()
    rid = r["recall_id"]
    url = f"{E}/recalls/{{recall_id}}/failed"
    body = {"version_id": vid, "reason": "No space left on device (os error 28)"}
    # Only a recall the edge was filling can fail this way.
    early = w.edge.post(url, body, recall_id=rid)
    assert early.status_code == 409, early.text
    sql.execute("UPDATE catalog.recall SET state = 'downloading' WHERE recall_id = %s", (int(rid),))
    wrong = w.edge.post(url, {**body, "version_id": "999999"}, recall_id=rid)
    assert wrong.status_code == 409, wrong.text
    failed = w.edge.post(url, body, recall_id=rid)
    assert failed.status_code == 204, failed.text
    state, failure, completed = sql.execute(
        "SELECT state, failure, completed_at IS NOT NULL FROM catalog.recall WHERE recall_id = %s",
        (int(rid),),
    ).fetchone()
    assert state == "failed" and completed
    assert failure == "edge fill failed: No space left on device (os error 28)"
    # Users are told (the Teams card follows), and the feed carries it.
    msgs = [m[0] for m in sql.execute("SELECT message FROM pgmq.q_notify").fetchall()]
    assert {"recall_id": rid} in msgs
    events, _ = feed_all(w)
    rec = [e for e in events if e["type"] == "recall.state"][-1]["recall"]
    assert rec["recall_id"] == rid and rec["state"] == "failed"
    # Repeats are harmless; the next read starts a new recall.
    assert w.edge.post(url, body, recall_id=rid).status_code == 204
    again = w.edge.post(f"{E}/recalls", read_req(fid, vid)).json()
    assert again["recall_id"] != rid and again["joined"] is False


def test_bytes_that_arrive_after_a_reported_failure_make_the_recall_available(
    make_world, facade_client, sql
):
    """#52 review: the edge reported a fill failed, then a later fill (or the
    one whose failure report was delayed behind it) brought the bytes. The
    recall becomes available and the file resident, rather than refusing the
    report and leaving the catalog saying `stub` over a resident file. A
    recall the service failed itself still refuses it."""
    w = make_world()
    fid = create_file(w, "late.segy")
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]
    archive(sql, fid)
    rid = w.edge.post(f"{E}/recalls", read_req(fid, vid)).json()["recall_id"]
    sql.execute("UPDATE catalog.recall SET state = 'downloading' WHERE recall_id = %s", (int(rid),))
    failed = w.edge.post(
        f"{E}/recalls/{{recall_id}}/failed",
        {"version_id": vid, "reason": "inode changed under the fill"},
        recall_id=rid,
    )
    assert failed.status_code == 204, failed.text
    filled = w.edge.post(
        f"{E}/recalls/{{recall_id}}/filled",
        {"version_id": vid, "resident_bytes": len(DATA)},
        recall_id=rid,
    )
    assert filled.status_code == 204, filled.text
    assert sql.execute(
        "SELECT r.state, r.failure, f.state, f.resident_bytes FROM catalog.recall r"
        " JOIN catalog.file f USING (file_id) WHERE r.recall_id = %s",
        (int(rid),),
    ).fetchone() == ("available", None, "resident", len(DATA))

    fid2 = create_file(w, "expired.segy")
    _, done2 = upload(w, facade_client, fid2)
    vid2 = done2["version_id"]
    archive(sql, fid2)
    rid2 = w.edge.post(f"{E}/recalls", read_req(fid2, vid2)).json()["recall_id"]
    sql.execute(
        "UPDATE catalog.recall SET state = 'failed', failure = 'restore expired'"
        " WHERE recall_id = %s",
        (int(rid2),),
    )
    refused = w.edge.post(
        f"{E}/recalls/{{recall_id}}/filled",
        {"version_id": vid2, "resident_bytes": len(DATA)},
        recall_id=rid2,
    )
    assert refused.status_code == 409, refused.text


def test_an_edge_back_after_a_timeout_reports_filled_and_the_recall_is_available(
    make_world, facade_client, sql
):
    """The worker failed a `downloading` recall because its edge went quiet
    (worker/recall_timeout.py). The edge comes back with the bytes and
    reports `filled`: #52's accept-after-edge-failure path makes the recall
    available and the file resident."""
    from omnifile_service.worker.recall_timeout import NO_FILL_PREFIX, SILENT_PREFIX

    for prefix in (SILENT_PREFIX, NO_FILL_PREFIX):
        _back_after_timeout(make_world, facade_client, sql, prefix)


def _back_after_timeout(make_world, facade_client, sql, prefix):
    w = make_world()
    fid = create_file(w, "back.segy")
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]
    archive(sql, fid)
    rid = w.edge.post(f"{E}/recalls", read_req(fid, vid)).json()["recall_id"]
    sql.execute(
        "UPDATE catalog.recall SET state = 'failed', completed_at = now(),"
        " failure = %s WHERE recall_id = %s",
        (f"{prefix} edge 1 has not been heard from for over 60 minutes", int(rid)),
    )
    filled = w.edge.post(
        f"{E}/recalls/{{recall_id}}/filled",
        {"version_id": vid, "resident_bytes": len(DATA)},
        recall_id=rid,
    )
    assert filled.status_code == 204, filled.text
    assert sql.execute(
        "SELECT r.state, r.failure, f.state FROM catalog.recall r"
        " JOIN catalog.file f USING (file_id) WHERE r.recall_id = %s",
        (int(rid),),
    ).fetchone() == ("available", None, "resident")


def test_recall_decisions(make_world, facade_client, sql):
    w = make_world(on_open="auto", threshold=60)
    fid = create_file(w, "huge.segy")
    _, done = upload(w, facade_client, fid)
    archive(sql, fid, size=11 * 1000**3)
    r = w.edge.post(f"{E}/recalls", read_req(fid, done["version_id"], on_open="auto")).json()
    # Over 10 GB: no promised time, and auto without a time fails fast.
    assert r["eta"] is None and r["eta_confidence"] == "none" and r["decision"] == "fail"
    assert "No recall time is promised." in r["note"]

    w2 = make_world(on_open="auto", threshold=24 * 60)
    fid2 = create_file(w2, "small.segy")
    _, done2 = upload(w2, facade_client, fid2)
    archive(sql, fid2)
    r2 = w2.edge.post(f"{E}/recalls", read_req(fid2, done2["version_id"], on_open="auto")).json()
    assert r2["decision"] == "wait"  # 15 h within a 24 h threshold
    # The ETA is the backend's stated Standard upper bound (capabilities():
    # Azure "up to 15 h"), decided by policy.recall.decide_on_open.
    requested = sql.execute(
        "SELECT requested_at, eta FROM catalog.recall WHERE recall_id = %s",
        (int(r2["recall_id"]),),
    ).fetchone()
    assert abs((requested[1] - requested[0]).total_seconds() - 15 * 3600) < 60

    w3 = make_world(on_open="auto", threshold=14 * 60)
    fid3 = create_file(w3, "almost.segy")
    _, done3 = upload(w3, facade_client, fid3)
    archive(sql, fid3)
    r3 = w3.edge.post(f"{E}/recalls", read_req(fid3, done3["version_id"], on_open="auto")).json()
    assert r3["decision"] == "fail" and r3["eta_confidence"] == "provider_sla"  # 15 h > 14 h


def test_reads_keep_the_later_time(make_world, sql):
    w = make_world()
    other = make_world()
    fid = create_file(w, "read-me")
    theirs = create_file(other, "not-yours")
    body = {
        "reads": [
            {"file_id": str(fid), "last_read_at": "2026-10-01T10:00:00Z"},
            {"file_id": str(fid), "last_read_at": "2026-10-01T11:00:00Z"},
            {"file_id": str(theirs), "last_read_at": "2026-10-01T11:00:00Z"},
            {"file_id": "999999999", "last_read_at": "2026-10-01T11:00:00Z"},
        ]
    }
    resp = w.edge.post(f"{E}/reads", body)
    assert resp.status_code == 200
    out = resp.json()
    assert out["accepted"] == 1
    assert sorted(out["unknown_file_ids"]) == sorted([str(theirs), "999999999"])
    older = {"reads": [{"file_id": str(fid), "last_read_at": "2026-09-01T00:00:00Z"}]}
    w.edge.post(f"{E}/reads", older)
    (t,) = sql.execute(
        "SELECT last_read_at FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert t == datetime(2026, 10, 1, 11, tzinfo=UTC)


def test_an_edge_cannot_touch_another_edges_files(make_world, facade_client):
    w = make_world()
    other = make_world()
    theirs = create_file(other, "theirs")
    resp = w.edge.post(f"{E}/uploads", intent(w, theirs, DATA))
    assert resp.status_code == 404
    resp = w.edge.post(f"{E}/files/{{file_id}}/stub/prepare", {"version_id": "1"}, file_id=theirs)
    assert resp.status_code == 404
    # And it cannot call as the other edge.
    resp = w.edge.post(f"{E}/heartbeat", {"version": "0.1.0"}, edge_id=other.edge.edge_id)
    assert resp.status_code == 403 and resp.json()["code"] == "edge_mismatch"


def digest(sql, dataset_id: int) -> tuple[str, list]:
    """The reconciliation digest (control-api.md section 6), and the versions behind it."""
    rows = sql.execute(
        "SELECT file_id, entity_version FROM catalog.file"
        " WHERE dataset_id = %s AND deleted_at IS NULL ORDER BY file_id",
        (dataset_id,),
    ).fetchall()
    acc = 0
    for fid, ev in rows:
        acc ^= int.from_bytes(hashlib.sha256(f"{fid}:{ev}".encode()).digest()[:8], "big")
    return f"{acc:016x}", rows


def test_reads_leave_entity_version_digest_and_feed_alone(make_world, facade_client, sql):
    w = make_world()
    a = create_file(w, "a")
    b = create_file(w, "b")
    upload(w, facade_client, b)
    _, cursor = feed_all(w)
    before = digest(sql, w.dataset_id)
    body = {
        "reads": [
            {"file_id": str(a), "last_read_at": "2026-10-01T10:00:00Z"},
            {"file_id": str(b), "last_read_at": "2026-10-01T10:00:00Z"},
        ]
    }
    assert w.edge.post(f"{E}/reads", body).json()["accepted"] == 2
    assert digest(sql, w.dataset_id) == before
    assert sql.execute(
        "SELECT count(*) FROM catalog.file WHERE file_id = ANY(%s) AND last_read_at IS NOT NULL",
        ([a, b],),
    ).fetchone() == (2,)
    events, _ = feed_all(w, cursor)
    assert [e for e in events if e.get("entity_id") in (str(a), str(b))] == []
    # Any other change still bumps the version, even together with a read time.
    sql.execute("UPDATE catalog.file SET mode = 384, last_read_at = now() WHERE file_id = %s", (a,))
    assert digest(sql, w.dataset_id) != before


def test_smb_and_local_requesters_are_recorded(make_world, facade_client, sql):
    """The smb-guard reports SMB sessions and processes on the edge itself
    (``via = local``); both join the recall with their SID (M2 integration)."""
    w = make_world()
    # An SMB share of the dataset on this edge: SMB sessions are proven there.
    sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols,"
        " owners_group_sid, on_open)"
        " SELECT dataset_id, edge_id, name || '-smb', ARRAY['smb'], owners_group_sid, on_open"
        " FROM catalog.share WHERE share_id = %s",
        (w.share_id,),
    )
    fid = create_file(w, "local.segy")
    _, done = upload(w, facade_client, fid)
    archive(sql, fid)
    smb = read_req(fid, done["version_id"])
    smb["requester"] = {"uid": 20001, "sid": "S-1-5-21-1-2-3-1101", "via": "smb", "proof": "proven"}
    local = read_req(fid, done["version_id"])
    local["requester"] = {
        "uid": 20002,
        "sid": "S-1-5-21-1-2-3-1102",
        "via": "local",
        "proof": "proven",
    }
    first = w.edge.post(f"{E}/recalls", smb)
    assert first.status_code == 200, first.text
    second = w.edge.post(f"{E}/recalls", local)
    assert second.status_code == 200, second.text
    assert second.json()["recall_id"] == first.json()["recall_id"]
    reqs = sql.execute(
        "SELECT uid, via, proof FROM catalog.recall_requester WHERE recall_id = %s ORDER BY uid",
        (int(first.json()["recall_id"]),),
    ).fetchall()
    # The service has only the edge's word for a local process: claimed.
    assert reqs == [(20001, "smb", "proven"), (20002, "local", "claimed")]


def test_online_fetch_reports_filled(make_world, facade_client, sql):
    """After an online fetch (no recall) the edge reports the fill and the
    catalog says resident again (M1 carry-over, control-api.md 4.4)."""
    w = make_world()
    fid = create_file(w, "fetched.segy")
    _, done = upload(w, facade_client, fid)
    vid = done["version_id"]
    sql.execute(
        "UPDATE catalog.file SET state = 'stub', resident_bytes = 4096 WHERE file_id = %s", (fid,)
    )
    url = f"{E}/files/{{file_id}}/filled"
    part = w.edge.post(url, {"version_id": vid, "resident_bytes": 8192}, file_id=fid)
    assert part.status_code == 204
    assert sql.execute(
        "SELECT state, resident_bytes FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone() == ("stub", 8192)
    full = w.edge.post(url, {"version_id": vid, "resident_bytes": len(DATA)}, file_id=fid)
    assert full.status_code == 204
    again = w.edge.post(url, {"version_id": vid, "resident_bytes": len(DATA)}, file_id=fid)
    assert again.status_code == 204
    assert sql.execute(
        "SELECT state, resident_bytes FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone() == ("resident", len(DATA))
    stale = w.edge.post(url, {"version_id": "999999", "resident_bytes": 1}, file_id=fid)
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
