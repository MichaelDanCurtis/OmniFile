"""Typed lifecycle events on /v1/events (portal-api.md section 5.11): recall,
restore and proposal events next to the catalog's own, in one order, with a
`types` filter; the catalog events keep their shape."""

from __future__ import annotations

from .conftest import add_dir, add_file, seed


def test_lifecycle_events_ride_the_catalog_feed(web, sql):
    w = seed(sql)
    read = web.app_token(["Archive.Read"])
    ds = {"dataset_id": str(w.dataset_id)}

    def events(**params):  # type: ignore[no-untyped-def]
        r = web.call("GET", "/v1/events", token=read, params={**ds, **params})
        assert r.status_code == 200, r.text
        return r.json()

    start = events(after="now")["next"]
    folder = add_dir(sql, w, "raw")
    fid = add_file(sql, w, "a.segy", parent=folder, tier="archive", state="stub")
    (vid, loc) = sql.execute(
        "SELECT content_version_id, primary_location_id FROM catalog.file WHERE file_id = %s",
        (fid,),
    ).fetchone()
    (recall,) = sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING recall_id",
        (fid, vid, w.edge_id),
    ).fetchone()
    sql.execute("UPDATE catalog.recall SET state = 'rehydrating' WHERE recall_id = %s", (recall,))
    sql.execute("UPDATE catalog.recall SET eta = now() WHERE recall_id = %s", (recall,))  # no event
    (restore,) = sql.execute(
        "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, state,"
        " triggered_by) VALUES (%s, %s, 'azure_copy_rehydrate', 'standard', 'requested',"
        " 'user_recall') RETURNING restore_id",
        (loc, w.backend_id),
    ).fetchone()
    (proposal,) = sql.execute(
        "INSERT INTO catalog.policy_proposal (dataset_id, body, proposed_by_kind, proposed_by_app)"
        " VALUES (%s, '{}', 'application', 'prizmanyx-app') RETURNING proposal_id",
        (w.dataset_id,),
    ).fetchone()
    sql.execute("UPDATE catalog.policy_proposal SET state = 'withdrawn' WHERE proposal_id = %s",
                (proposal,))  # fmt: skip
    items = events(after=start)["items"]
    kinds = [e["kind"] for e in items]
    # The catalog's events keep their shape: thin, data null.
    upsert = next(e for e in items if e["kind"] == "file.upsert" and e["entity_id"] == str(fid))
    assert upsert["data"] is None and upsert["file"]["path"] == "/raw/a.segy"
    # The typed ones carry their facts.
    states = [e["data"]["state"] for e in items if e["kind"] == "recall.state"]
    assert states == ["requested", "rehydrating"]
    rec = next(e for e in items if e["kind"] == "recall.state")
    assert (rec["entity"], rec["entity_id"], rec["file"], rec["entity_version"]) == (
        "recall", str(recall), None, None)  # fmt: skip
    assert rec["data"]["file_id"] == str(fid) and rec["data"]["version_id"] == str(vid)
    rst = next(e for e in items if e["kind"] == "restore.state")
    assert rst["data"] == {"restore_id": str(restore), "file_id": str(fid), "state": "requested",
                           "triggered_by": "user_recall", "migrate_on_complete": False,
                           "expires_at": None}  # fmt: skip
    dec = next(e for e in items if e["kind"] == "proposal.decided")
    assert dec["data"]["state"] == "withdrawn" and dec["entity_id"] == str(proposal)
    # One order: cursors only grow, across both kinds of event.
    cursors = [tuple(int(p) for p in e["cursor"].split(".")) for e in items]
    assert cursors == sorted(cursors)
    assert kinds.index("recall.state") > kinds.index("file.upsert")
    # `types` keeps only the kinds asked for; an unknown one matches nothing.
    only = events(after=start, types="restore.state,proposal.decided,nonsense.kind")["items"]
    assert [e["kind"] for e in only] == ["restore.state", "proposal.decided"]
    assert events(after=start, types="nonsense.kind")["items"] == []
    # Paging through a mixed feed loses nothing.
    seen, cur = [], start
    while True:
        page = events(after=cur, limit=2)
        seen += page["items"]
        if not page["items"]:
            break
        cur = page["next"]
    assert [e["cursor"] for e in seen] == [e["cursor"] for e in items]


def test_retention_drops_lifecycle_events_with_the_feed(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "b.segy", tier="archive", state="stub")
    (vid,) = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id) VALUES (%s, %s, %s)",
        (fid, vid, w.edge_id),
    )
    (txid, eid) = sql.execute(
        "SELECT txid::text, event_id FROM catalog.partner_event WHERE entity = 'recall'"
        " ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    # Nothing to drop: the partner event stays.
    sql.execute("SELECT catalog.feed_drop_before('-infinity')")
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.partner_event WHERE event_id = %s", (eid,)
    ).fetchone()
    assert n == 1
    # Once the floor passes it (as when its week's feed partition is dropped),
    # it goes, and a cursor below the floor is 410.
    saved = sql.execute(
        "SELECT dropped_through_txid, dropped_through_event FROM catalog.feed_retention"
    ).fetchone()
    try:
        sql.execute(
            "UPDATE catalog.feed_retention SET dropped_through_txid = %s::xid8,"
            " dropped_through_event = %s",
            (txid, eid),
        )
        sql.execute("SELECT catalog.feed_drop_before('-infinity')")
        (n,) = sql.execute(
            "SELECT count(*) FROM catalog.partner_event WHERE event_id = %s", (eid,)
        ).fetchone()
        assert n == 0
        r = web.call("GET", "/v1/events", token=web.app_token(["Archive.Read"]),
                     params={"after": f"{txid}.{eid - 1}"})  # fmt: skip
        assert r.status_code == 410
    finally:
        sql.execute(
            "UPDATE catalog.feed_retention SET dropped_through_txid = %s,"
            " dropped_through_event = %s",
            saved,
        )


def test_a_superseded_recalls_event_says_why(web, sql):
    """Migration 0059: a partner following `recall.state` sees `superseded` with its
    reason, so it does not take a replaced file's recall for one still running, or for a
    failure to retry."""
    w = seed(sql)
    read = web.app_token(["Archive.Read"])
    ds = {"dataset_id": str(w.dataset_id)}

    def events(**params):  # type: ignore[no-untyped-def]
        r = web.call("GET", "/v1/events", token=read, params={**ds, **params})
        assert r.status_code == 200, r.text
        return r.json()

    start = events(after="now")["next"]
    fid = add_file(sql, w, "a.segy", tier="archive", state="stub")
    (vid,) = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    (recall,) = sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING recall_id",
        (fid, vid, w.edge_id),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size) VALUES (%s, %s, %s, 'a.segy', 'content', 'committed', now(), 9)",
        (fid, w.dataset_id, w.root_id),
    )
    data = [e["data"] for e in events(after=start, types="recall.state")["items"]]
    assert [d["state"] for d in data] == ["requested", "superseded"]
    assert "superseded_reason" not in data[0]
    assert data[1]["superseded_reason"] == "replaced" and data[1]["recall_id"] == str(recall)
