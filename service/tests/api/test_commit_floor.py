"""A file's versions commit in order, whoever's clock wrote them (migration
0031). A seeded S3 object's version commits at its LastModified, which can
be ahead of the catalog's clock; an edge op on it (POST .../changes, on the
database's clock) used to supersede it before it began, and the catalog
refused it: a user's delete ended as a conflict in the edge's outbox (M5
exit test, step 15)."""

from __future__ import annotations

import uuid

from .conftest import E, create_file


def op(op_name: str, **fields) -> dict:
    return {"op_id": f"op-{uuid.uuid4().hex}", "op": op_name, **fields}


def test_an_op_on_a_file_whose_version_is_dated_ahead_commits_after_it(make_world, sql) -> None:
    w = make_world()
    f = create_file(w, "seeded.segy")
    g = create_file(w, "other.segy")
    # As the seeder commits an object whose LastModified is ahead of the catalog's clock.
    for fid in (f, g):
        sql.execute(
            "UPDATE catalog.file_version SET committed_at = now() + interval '2 days'"
            " WHERE version_id = (SELECT current_version_id FROM catalog.file WHERE file_id = %s)",
            (fid,),
        )
    (ahead,) = sql.execute(
        "SELECT v.committed_at FROM catalog.file f JOIN catalog.file_version v"
        "  ON v.version_id = f.current_version_id WHERE f.file_id = %s",
        (f,),
    ).fetchone()
    actor = {"via": "nfs", "uid": 20002, "sec": "sys"}
    resp = w.edge.post(
        f"{E}/changes",
        {
            "ops": [
                op("setattr", file={"file_id": str(g)}, attrs={"uid": 5, "gid": 6}, actor=actor),
                op("unlink", file={"file_id": str(f)}, actor=actor),
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    res = resp.json()["results"]
    assert [r["status"] for r in res] == ["applied", "applied"], res
    # The delete reached the catalog, at the superseded version's time: never before it.
    rows = sql.execute(
        "SELECT kind, committed_at, superseded_at FROM catalog.file_version"
        " WHERE file_id = %s ORDER BY seq",
        (f,),
    ).fetchall()
    assert rows[-1][0] == "tombstone" and rows[-1][1] == ahead, rows
    assert rows[-2][2] == ahead, rows
    (deleted,) = sql.execute(
        "SELECT deleted_at IS NOT NULL FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone()
    assert deleted
    (uid,) = sql.execute("SELECT uid FROM catalog.file WHERE file_id = %s", (g,)).fetchone()
    assert uid == 5


def test_a_version_on_time_keeps_its_own_commit_time(make_world, sql) -> None:
    w = make_world()
    f = create_file(w, "a.segy")
    resp = w.edge.post(
        f"{E}/changes", {"ops": [op("setattr", file={"file_id": str(f)}, attrs={"uid": 7})]}
    )
    assert resp.status_code == 200 and resp.json()["results"][0]["status"] == "applied"
    rows = sql.execute(
        "SELECT committed_at, superseded_at FROM catalog.file_version"
        " WHERE file_id = %s ORDER BY seq",
        (f,),
    ).fetchall()
    # The new version's own time (the transaction's), after the one it superseded.
    assert rows[-1][0] >= rows[-2][0] and rows[-2][1] == rows[-1][0], rows
