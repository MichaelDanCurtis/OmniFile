"""The feed (section 5), snapshot (6), changes (4.1), heartbeat (4.7), idempotency (8)."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta

import psycopg

from .conftest import E, World, create_file


def op(op_name: str, **fields) -> dict:
    return {"op_id": f"op-{uuid.uuid4().hex}", "op": op_name, **fields}


def changes(w: World, *ops: dict, **kw) -> list[dict]:
    resp = w.edge.post(f"{E}/changes", {"ops": list(ops)}, **kw)
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


def page(w: World, after: str, **params) -> dict:
    resp = w.edge.get(f"{E}/feed", {"after": after, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def drain(w: World, after: str = "0.0") -> tuple[list[dict], str]:
    out: list[dict] = []
    while True:
        p = page(w, after, limit=1000)
        out += p["events"]
        if not p["events"]:
            return out, p["next"]
        after = p["next"]


def test_cursor_pages_and_scope(make_world) -> None:
    w, other = make_world(), make_world()
    _, start = drain(w)
    a = create_file(w, "a")
    b = create_file(w, "b")
    create_file(other, "not-mine")
    first = page(w, start, limit=1)
    assert len(first["events"]) == 1 and first["reset_required"] is False
    rest, end = drain(w, first["next"])
    ids = [e["entity_id"] for e in first["events"] + rest if e["type"] == "file.upsert"]
    assert ids == [str(a), str(b)]
    # Nothing new: next still moves (to the horizon), never backwards.
    again = page(w, end)
    assert again["events"] == []
    t1, e1 = (int(x) for x in end.split("."))
    t2, e2 = (int(x) for x in again["next"].split("."))
    assert (t2, e2) >= (t1, e1)


def test_long_poll_wakes_on_a_new_event(make_world) -> None:
    w = make_world()
    _, cur = drain(w)
    out: dict = {}

    def poll() -> None:
        t0 = time.monotonic()
        out["page"] = page(w, cur, wait=5)
        out["took"] = time.monotonic() - t0

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.3)
    fid = create_file(w, "late")
    t.join(10)
    assert [e["entity_id"] for e in out["page"]["events"]] == [str(fid)]
    assert out["took"] < 4


def test_long_poll_returns_empty_after_wait(make_world) -> None:
    w = make_world()
    _, cur = drain(w)
    t0 = time.monotonic()
    p = page(w, cur, wait=1)
    assert p["events"] == [] and 0.9 < time.monotonic() - t0 < 3


def test_reset_required_below_retention(make_world, sql) -> None:
    w = make_world()
    sql.execute(
        "UPDATE catalog.feed_retention SET dropped_through_txid = '5'::xid8,"
        " dropped_through_event = 7"
    )
    try:
        p = page(w, "5.7")
        assert p == {"events": [], "next": "5.7", "reset_required": True}
        assert page(w, "5.8")["reset_required"] is False
    finally:
        sql.execute(
            "UPDATE catalog.feed_retention SET dropped_through_txid = '0'::xid8,"
            " dropped_through_event = 0"
        )


def test_a_restored_catalog_refuses_cursors_from_before(make_world, sql) -> None:
    # After a point-in-time restore, transaction ids are handed out again: a
    # cursor kept from before the restore could silently skip new events.
    w = make_world()
    _, before = drain(w)
    last = create_file(w, "before-restore")
    sql.execute("SELECT catalog.feed_restored()")
    try:
        p = page(w, before)
        assert p == {"events": [], "next": before, "reset_required": True}
        # A new snapshot's cursor carries the incarnation, and reads on.
        snap = w.edge.get(f"{E}/snapshot", {"dataset_id": str(w.dataset_id)})
        assert snap.status_code == 200, snap.text
        as_of = snap.json()["as_of"]
        assert as_of.endswith(".1"), as_of
        fid = create_file(w, "after-restore")
        # Ids move on past anything the lost timeline can have used.
        assert fid > last + (1 << 40)
        events, nxt = drain(w, as_of)
        assert str(fid) in [e["entity_id"] for e in events]
        assert nxt.endswith(".1") and page(w, nxt)["reset_required"] is False
        # "0.0" is everything kept, in any incarnation.
        assert page(w, "0.0")["reset_required"] is False
        ack = w.edge.post(f"{E}/feed/ack", {"cursor": nxt})
        assert ack.status_code == 204, ack.text
        acked = [r[0] for r in sql.execute("SELECT feed_acked_cursor FROM catalog.edge")]
        assert nxt in acked
    finally:
        sql.execute("UPDATE catalog.feed_retention SET incarnation = 0")


def test_feed_rejects_a_bad_cursor(make_world) -> None:
    w = make_world()
    resp = w.edge.get(f"{E}/feed", {"after": "zero"})
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_request"


def test_changes_namespace_ops(make_world, sql) -> None:
    w = make_world()
    local = {"fsid": "1", "ino": "77", "ino_gen": "1"}
    res = changes(
        w,
        op(
            "create",
            parent={"file_id": str(w.root_id)},
            name="dir",
            kind="dir",
            file={"local": local},
            attrs={"mode": 0o755},
        ),
        op(
            "create",
            parent={"local": local},
            name="f.txt",
            kind="file",
            attrs={
                "mode": 0o644,
                "uid": 5,
                "gid": 6,
                "mtime_ns": "1",
                "owner_sid": "S-1-5-21-1-2-3",
            },
        ),
        op("create", parent={"local": local}, name="link", kind="symlink", symlink_target="f.txt"),
    )
    assert [r["status"] for r in res] == ["applied"] * 3
    dir_id, file_id = int(res[0]["file_id"]), int(res[1]["file_id"])
    row = sql.execute(
        "SELECT parent_id, kind, state, mode, uid, owner_sid FROM catalog.file WHERE file_id = %s",
        (file_id,),
    ).fetchone()
    assert row == (dir_id, "file", "landing", 0o644, 5, "S-1-5-21-1-2-3")

    setattr_op = op("setattr", file={"file_id": str(file_id)}, attrs={"mode": 0o600})
    rename_op = op(
        "rename",
        file={"file_id": str(file_id)},
        new_parent={"file_id": str(w.root_id)},
        new_name="moved.txt",
    )
    res = changes(w, setattr_op, rename_op)
    assert [r["status"] for r in res] == ["applied", "applied"]
    # Replays are duplicates, with the first result's ids.
    dup = changes(w, setattr_op)
    assert dup[0]["status"] == "duplicate" and dup[0]["version_id"] == res[0]["version_id"]
    assert sql.execute(
        "SELECT parent_id, name, mode FROM catalog.file WHERE file_id = %s", (file_id,)
    ).fetchone() == (w.root_id, "moved.txt", 0o600)

    # A non-empty directory can't go; its children first (no recursive delete).
    res = changes(w, op("unlink", file={"file_id": str(dir_id)}))
    assert res[0]["status"] == "rejected" and res[0]["code"] == "directory_not_empty"
    link_id = int(
        sql.execute(
            "SELECT file_id FROM catalog.file WHERE parent_id = %s AND name = 'link'", (dir_id,)
        ).fetchone()[0]
    )
    res = changes(
        w,
        op("unlink", file={"file_id": str(link_id)}),
        op("unlink", file={"file_id": str(dir_id)}),
    )
    assert [r["status"] for r in res] == ["applied", "applied"]

    events, _ = drain(w)
    kinds = {(e["type"], e["entity_id"]) for e in events}
    assert ("file.tombstone", str(link_id)) in kinds
    assert ("file.rename", str(file_id)) in kinds or ("file.upsert", str(file_id)) in kinds


def test_changes_rejections_and_conflicts(make_world, sql) -> None:
    w = make_world()
    fid = create_file(w, "taken")
    local = {"fsid": "1", "ino": "9", "ino_gen": "2"}
    res = changes(
        w,
        op("create", parent={"file_id": str(w.root_id)}, name="..", kind="file"),
        op(
            "create",
            parent={"file_id": str(w.root_id)},
            name="taken",
            kind="file",
            file={"local": local},
        ),
        op("create", parent={"local": local}, name="child", kind="file"),
        op("setattr", file={"file_id": str(fid)}, attrs={"acl_sha256": "a" * 64}),
        op(
            "setattr",
            file={"file_id": str(fid)},
            attrs={"mode": 0o600},
            expected_entity_version="999",
        ),
        op("create", parent={"file_id": str(w.root_id)}, name="fine", kind="file"),
    )
    assert [(r["status"], r.get("code")) for r in res] == [
        ("rejected", "name_invalid"),
        ("conflict", "version_conflict"),
        ("rejected", "dependency_failed"),
        ("rejected", "acl_unknown"),
        ("conflict", "version_conflict"),
        ("applied", None),
    ]
    events, _ = drain(w)
    conflicts = [e for e in events if e["type"] == "file.conflict"]
    assert {c["conflict"]["op_id"] for c in conflicts} == {res[1]["op_id"], res[4]["op_id"]}
    assert all(c["conflict"]["current"]["file_id"] == str(fid) for c in conflicts)
    assert any("expected 999" in c["conflict"]["reason"] for c in conflicts)


def test_changes_schema_violation_is_a_400(make_world) -> None:
    w = make_world()
    resp = w.edge.post(f"{E}/changes", {"ops": []}, check=False)
    assert resp.status_code == 400
    resp = w.edge.post(f"{E}/changes", {"ops": [{"op": "create"}]}, check=False)
    assert resp.status_code == 400


def test_snapshot(make_world) -> None:
    w = make_world()
    ids = [create_file(w, f"f{i}") for i in range(3)]
    first = w.edge.get(f"{E}/snapshot", {"dataset_id": str(w.dataset_id), "limit": 2})
    assert first.status_code == 200, first.text
    p = first.json()
    assert p["more"] is True and p["as_of"] and p["dataset"]["dataset_id"] == str(w.dataset_id)
    assert [s["share_id"] for s in p["shares"]] == [str(w.share_id)]
    files = [f["file_id"] for f in p["files"]]
    second = w.edge.get(
        f"{E}/snapshot", {"dataset_id": str(w.dataset_id), "after_file_id": files[-1]}
    ).json()
    assert "as_of" not in second and second["more"] is False
    assert files + [f["file_id"] for f in second["files"]] == [str(w.root_id)] + [
        str(i) for i in ids
    ]
    other = make_world()
    nope = w.edge.get(f"{E}/snapshot", {"dataset_id": str(other.dataset_id)})
    assert nope.status_code == 404


def test_heartbeat(make_world) -> None:
    w = make_world()
    _, cur = drain(w)
    create_file(w, "x")
    resp = w.edge.post(
        f"{E}/heartbeat",
        {"version": "0.1.0", "kernel": "7.0", "guard_state": "armed", "feed_cursor": cur},
    )
    assert resp.status_code == 200
    hb = resp.json()
    assert hb["feed_lag_events"] >= 1 and hb["min_edge_version"] == "0.0.0"
    # Day 20 of 30: about 20 days from now for a fresh certificate.
    renew = datetime.fromisoformat(hb["certificate_renew_after"].replace("Z", "+00:00"))
    now = datetime.fromisoformat(hb["server_time"].replace("Z", "+00:00"))
    assert timedelta(days=19) < renew - now < timedelta(days=21)
    ack = w.edge.post(f"{E}/feed/ack", {"cursor": cur})
    assert ack.status_code == 204


def test_heartbeat_keeps_the_edges_health(make_world, sql) -> None:
    """Decision 37: what the edge reports is kept for the portal and alerts."""
    w = make_world()
    _, cur = drain(w)
    create_file(w, "x")
    body = {
        "version": "0.2.0",
        "kernel": "7.0.0-14-generic",
        "guard_state": "down",
        "feed_cursor": cur,
        "outbox_depth": 12,
        "fills_running": 2,
        "recalls_waiting": 3,
        "data_free_bytes": 10 << 30,
        "data_capacity_bytes": 100 << 30,
        "fsal_connections": 1,
        "recovery_used": 1,
    }
    assert w.edge.post(f"{E}/heartbeat", body).status_code == 200
    row = sql.execute(
        "SELECT software_version, kernel, guard_state, health, last_heartbeat_at"
        " FROM catalog.edge WHERE edge_id = %s",
        (w.edge.edge_id,),
    ).fetchone()
    assert row[:3] == ("0.2.0", "7.0.0-14-generic", "down")
    assert row[4] is not None
    h = row[3]
    assert h["outbox_depth"] == 12 and h["fills_running"] == 2 and h["recalls_waiting"] == 3
    assert h["data_free_bytes"] == 10 << 30 and h["fsal_connections"] == 1
    assert h["recovery_used"] == 1
    assert h["feed_lag_events"] >= 1
    # An older edge sends fewer fields: the gauges it doesn't send are dropped.
    assert w.edge.post(f"{E}/heartbeat", {"version": "0.1.0"}).status_code == 200
    h = sql.execute(
        "SELECT health FROM catalog.edge WHERE edge_id = %s", (w.edge.edge_id,)
    ).fetchone()[0]
    assert h == {}
    for bad in ({"outbox_depth": -1}, {"fills_running": "2"}, {"guard_state": "fine"}):
        resp = w.edge.post(f"{E}/heartbeat", {"version": "0.1.0", **bad}, check=False)
        assert resp.status_code == 400, bad


def test_heartbeat_records_when_a_fill_last_ran(make_world, sql) -> None:
    """Migration 0039: a heartbeat with a fill running stamps last_fill_at;
    one with none keeps the last stamp (worker/recall_timeout.py judges an
    edge idle from it)."""
    w = make_world()

    def last_fill():
        return sql.execute(
            "SELECT last_fill_at FROM catalog.edge WHERE edge_id = %s", (w.edge.edge_id,)
        ).fetchone()[0]

    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.2.0", "fills_running": 0}).status_code == 200
    )
    assert last_fill() is None
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.2.0", "fills_running": 1}).status_code == 200
    )
    first = last_fill()
    assert first is not None
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.2.0", "fills_running": 0}).status_code == 200
    )
    assert last_fill() == first
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.2.0", "fills_running": 3}).status_code == 200
    )
    assert last_fill() > first


def test_heartbeats_keep_the_services_reachable_window(make_world, sql) -> None:
    """Migration 0040: heartbeats arriving keep one window open; one after a
    gap of over 3 minutes (an outage of the service or of the edges' way to
    it) starts a new one."""
    w = make_world()

    def window():
        return sql.execute("SELECT started_at, last_at FROM catalog.heartbeat_window").fetchone()

    assert w.edge.post(f"{E}/heartbeat", {"version": "0.2.0"}).status_code == 200
    started, last = window()
    sql.execute(
        "UPDATE catalog.heartbeat_window SET started_at = now() - interval '2 hours',"
        " last_at = now() - interval '1 minute'"
    )
    assert w.edge.post(f"{E}/heartbeat", {"version": "0.2.0"}).status_code == 200
    started, last = window()
    assert (last - started).total_seconds() > 7000, "the window goes on"
    sql.execute("UPDATE catalog.heartbeat_window SET last_at = now() - interval '4 minutes'")
    assert w.edge.post(f"{E}/heartbeat", {"version": "0.2.0"}).status_code == 200
    started, last = window()
    assert (last - started).total_seconds() < 5, "a gap starts a new window"


def test_min_edge_version_gate(make_world, api) -> None:
    w = make_world()
    api.services.settings.min_edge_version = "0.2.0"
    try:
        old = w.edge.post(f"{E}/reads", {"reads": []})
        assert old.status_code == 426 and old.json()["code"] == "edge_upgrade_required"
        # heartbeat and the feed still work, so an old edge keeps its replica current
        assert w.edge.post(f"{E}/heartbeat", {"version": "0.1.0"}).status_code == 200
        assert w.edge.get(f"{E}/feed", {"after": "0.0"}).status_code == 200
    finally:
        api.services.settings.min_edge_version = "0.0.0"


def test_idempotency_rules(make_world, db_url) -> None:
    w = make_world()
    body = {"reads": []}
    missing = w.edge.client.post(
        f"/v0/edges/{w.edge.edge_id}/reads", json=body, headers=w.edge.headers()
    )
    assert missing.status_code == 400 and "Idempotency-Key" in missing.json()["detail"]
    short = w.edge.post(f"{E}/reads", body, key="short", check=False)
    assert short.status_code == 400
    # A claim still running (status NULL) answers request_in_progress.
    key = "in-progress-" + uuid.uuid4().hex
    with psycopg.connect(db_url, autocommit=True) as c:
        from omnifile_service.api.idempotency import request_hash

        c.execute(
            "INSERT INTO catalog.idempotency_key (principal, key, method, path, request_sha256)"
            " VALUES (%s, %s, 'POST', %s, %s)",
            (
                f"edge:{w.edge.edge_id}",
                key,
                f"/v0/edges/{w.edge.edge_id}/reads",
                request_hash("POST", f"/v0/edges/{w.edge.edge_id}/reads", b'{"reads":[]}'),
            ),
        )
    busy = w.edge.client.post(
        f"/v0/edges/{w.edge.edge_id}/reads",
        content=b'{"reads":[]}',
        headers={**w.edge.headers(), "Idempotency-Key": key, "content-type": "application/json"},
    )
    assert busy.status_code == 409 and busy.json()["code"] == "request_in_progress"
    assert busy.json()["retryable"] is True


def test_the_heartbeat_keeps_the_edge_s_quiet_defaults(make_world, sql) -> None:  # type: ignore[no-untyped-def]
    """Decision 67 (#80 review): the edge reports its defaults, so the portal shows
    what it applies."""
    w = make_world()
    e = w.edge
    r = e.post(f"{E}/heartbeat", {"version": "0.1.0", "quiet_default_s": 10,
                                  "quiet_worm_default_s": 60, "quiet_stale_s": 420})  # fmt: skip
    assert r.status_code == 200, r.text
    (health,) = sql.execute(
        "SELECT health FROM catalog.edge WHERE edge_id = %s", (e.edge_id,)
    ).fetchone()
    assert (health["quiet_default_s"], health["quiet_worm_default_s"]) == (10, 60)
    # And how long it has failed to read them (#80 re-check).
    assert health["quiet_stale_s"] == 420
