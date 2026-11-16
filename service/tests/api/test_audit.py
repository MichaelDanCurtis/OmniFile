"""`POST /v0/edges/{id}/audit` (control-api.md §4.7): the edge's records in
catalog.audit_log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .conftest import E, create_file


def rec(**kw):
    out = {"at": "2026-10-15T10:00:00Z", "action": "stub.open", "decision": "allow"}
    out.update(kw)
    return out


def rows(sql, edge_id):
    return sql.execute(
        "SELECT action, decision, actor_sid, proof, trust_level, dataset_id, share_id,"
        " file_id, host(client_addr), details, at"
        " FROM catalog.audit_log WHERE actor = %s ORDER BY audit_id",
        (f"edge:{edge_id}",),
    ).fetchall()


def test_records_are_stored_as_the_edge(make_world, sql) -> None:
    w = make_world()
    fid = create_file(w, "a.segy")
    resp = w.edge.post(
        f"{E}/audit",
        {
            "records": [
                rec(
                    action="recall.read",
                    decision="deny",
                    sid="S-1-5-21-1-2-3-1105",
                    uid=11105,
                    proof="claimed",
                    trust_level="host_trusted",
                    file_id=str(fid),
                    client_addr="10.20.0.7",
                    details={"policy": "failfast"},
                ),
                rec(action="admin.guard.restart", decision="info"),
            ]
        },
    )
    assert resp.status_code == 204, resp.text
    got = rows(sql, w.edge.edge_id)
    assert len(got) == 2
    a = got[0]
    assert a[:5] == ("recall.read", "deny", "S-1-5-21-1-2-3-1105", "claimed", "host_trusted")
    # The dataset and share come from the file, never from the edge.
    assert (a[5], a[6], a[7]) == (w.dataset_id, w.share_id, fid)
    assert a[8] == "10.20.0.7"
    assert a[9] == {"policy": "failfast", "uid": 11105}
    assert a[10] == datetime(2026, 10, 15, 10, tzinfo=UTC)
    assert got[1][:2] == ("admin.guard.restart", "info") and got[1][7] is None


def test_an_edge_never_writes_into_another_datasets_history(make_world, sql) -> None:
    w, other = make_world(), make_world()
    theirs = create_file(other, "theirs")
    resp = w.edge.post(f"{E}/audit", {"records": [rec(file_id=str(theirs))]})
    assert resp.status_code == 204, resp.text
    (got,) = rows(sql, w.edge.edge_id)
    assert (got[5], got[6], got[7]) == (None, None, None)
    assert got[9] == {"claimed_file_id": str(theirs)}
    n = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE file_id = %s", (theirs,)
    ).fetchone()[0]
    assert n == 0


def test_bad_records_are_refused_whole(make_world, sql) -> None:
    w = make_world()
    ahead = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bad in (
        rec(at=ahead),
        rec(action="Stub Open"),
        rec(decision="maybe"),
        rec(sid="not-a-sid"),
        rec(uid=-1),
        rec(proof="trust-me"),
        rec(client_addr="10.0.0.300"),
        rec(details=["x"]),
    ):
        resp = w.edge.post(f"{E}/audit", {"records": [rec(), bad]}, check=False)
        assert resp.status_code == 400, (bad, resp.text)
    many = w.edge.post(f"{E}/audit", {"records": [rec()] * 5001}, check=False)
    assert many.status_code == 400
    assert rows(sql, w.edge.edge_id) == []
    # An empty batch is fine, and a slightly fast clock too.
    assert w.edge.post(f"{E}/audit", {"records": []}).status_code == 204
    soon = (datetime.now(UTC) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert w.edge.post(f"{E}/audit", {"records": [rec(at=soon)]}).status_code == 204
