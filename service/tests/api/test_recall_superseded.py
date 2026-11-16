"""A recall whose file is replaced or deleted ends `superseded`, in the commit's
own transaction (migration 0059; control-api.md section 4.4).

The chaos soak of 2026-11-08 (seed 148220832, recall 209): a file was overwritten
while its recall was `downloading`; the edge's fill found the new version under the
name and reported the recall failed, and its requester was told "recall failed" for
a file that had only been replaced.
"""

from __future__ import annotations

import pytest

from .conftest import E, World, create_file
from .test_change_actor import SID, changes, op
from .test_upload_flow import DATA, archive, feed_all, read_req, upload

NEW = b"SEG-Y header, shot again" + bytes(range(256)) * 12
ACTIVE = ("requested", "rehydrating", "copying", "downloading")


def archived_file(w: World, facade_client, sql, name: str) -> tuple[int, str]:
    fid = create_file(w, name)
    _, done = upload(w, facade_client, fid)
    archive(sql, fid)
    return fid, done["version_id"]


def start_recall(w: World, fid: int, vid: str, **extra) -> str:
    resp = w.edge.post(f"{E}/recalls", {**read_req(fid, vid), **extra})
    assert resp.status_code == 200 and resp.json()["status"] == "recall", resp.text
    return resp.json()["recall_id"]


def recall(sql, rid: str) -> tuple:
    return sql.execute(
        "SELECT state, superseded_reason, superseded_by_version_id::text, failure,"
        " completed_at IS NOT NULL, pinned FROM catalog.recall WHERE recall_id = %s",
        (int(rid),),
    ).fetchone()


def notified(sql, rid: str) -> int:
    msgs = [m[0] for m in sql.execute("SELECT message FROM pgmq.q_notify").fetchall()]
    return msgs.count({"recall_id": rid})


@pytest.mark.parametrize("state", ACTIVE)
def test_a_new_content_version_ends_the_recall_of_the_one_it_replaces(
    make_world, facade_client, sql, state
):
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "line.segy")
    rid = start_recall(w, fid, v1)
    sql.execute("UPDATE catalog.recall SET state = %s WHERE recall_id = %s", (state, int(rid)))
    told = notified(sql, rid)

    _, done = upload(w, facade_client, fid, NEW)
    v2 = done["version_id"]
    assert v2 != v1

    # Ended in the commit's transaction: not failed, and it says why and by what.
    assert recall(sql, rid) == ("superseded", "replaced", v2, None, True, False)
    # Its requesters are told once more, and the edge hears through the feed.
    assert notified(sql, rid) == told + 1
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.1.0", "recall_superseded": 1}).status_code
        == 200
    )
    events, _ = feed_all(w)
    rec = [e for e in events if e["type"] == "recall.state"][-1]["recall"]
    assert rec["recall_id"] == rid and rec["state"] == "superseded" and rec["error"] is None

    # A fill that met the new file and reported anyway changes nothing.
    late = w.edge.post(
        f"{E}/recalls/{{recall_id}}/failed",
        {"version_id": v1, "reason": f"marker names file {fid} version {v2}; fetch is for {v1}"},
        recall_id=rid,
    )
    assert late.status_code == 204, late.text
    assert recall(sql, rid) == ("superseded", "replaced", v2, None, True, False)
    assert notified(sql, rid) == told + 1


def test_a_delete_ends_the_recall_as_deleted_and_records_who(make_world, facade_client, sql):
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "gone.segy")
    rid = start_recall(w, fid, v1)
    told = notified(sql, rid)
    krb = {"via": "nfs", "uid": 41023, "sid": SID, "sec": "krb5p", "client_addr": "10.1.2.3"}
    (res,) = changes(w, op("unlink", file={"file_id": str(fid)}, actor=krb))
    assert res["status"] == "applied", res

    assert recall(sql, rid) == ("superseded", "deleted", res["version_id"], None, True, False)
    assert notified(sql, rid) == told + 1
    # The tombstone names who deleted it, proven (a Kerberos-only share).
    assert sql.execute(
        "SELECT v.kind, v.actor_proof, i.sid::text FROM catalog.recall r"
        " JOIN catalog.file_version v ON v.version_id = r.superseded_by_version_id"
        " JOIN catalog.identity i ON i.identity_id = v.actor_identity_id"
        " WHERE r.recall_id = %s",
        (int(rid),),
    ).fetchone() == ("tombstone", "proven", SID)


def test_a_recall_someone_asked_for_by_version_is_left_alone(make_world, facade_client, sql):
    """A read in `.snapshots` or Previous Versions is of that version, whatever the
    file holds now: the edge says so (`pinned`), and neither a replace nor a delete
    ends its recall. A second reader who joins pinned pins a recall that wasn't."""
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "kept.segy")
    rid = start_recall(w, fid, v1, pinned=True)
    assert recall(sql, rid)[5] is True
    upload(w, facade_client, fid, NEW)
    assert recall(sql, rid)[:2] == ("requested", None)
    (res,) = changes(w, op("unlink", file={"file_id": str(fid)}))
    assert res["status"] == "applied", res
    assert recall(sql, rid)[:2] == ("requested", None)

    fid2, w1 = archived_file(w, facade_client, sql, "joined.segy")
    rid2 = start_recall(w, fid2, w1)
    assert recall(sql, rid2)[5] is False
    joined = w.edge.post(f"{E}/recalls", {**read_req(fid2, w1, uid=41024), "pinned": True})
    assert joined.json()["recall_id"] == rid2 and joined.json()["joined"] is True
    assert recall(sql, rid2)[5] is True
    upload(w, facade_client, fid2, NEW)
    assert recall(sql, rid2)[:2] == ("requested", None)


def test_a_recall_of_an_older_version_is_left_alone(make_world, facade_client, sql):
    """Only recalls of what was the current content go. A recall of a version the
    file had already moved past (a rewind read, a version restore) is of that
    version, and a later replace is nothing to it."""
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "old.segy")
    _, done = upload(w, facade_client, fid, NEW)
    v2 = done["version_id"]
    old = start_recall(w, fid, v1)
    archive(sql, fid)
    cur = start_recall(w, fid, v2)

    _, done = upload(w, facade_client, fid, DATA + b"again")
    assert recall(sql, old)[:2] == ("requested", None)
    assert recall(sql, cur)[:3] == ("superseded", "replaced", done["version_id"])


def test_a_rename_or_an_attribute_change_ends_nothing(make_world, facade_client, sql):
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "moved.segy")
    rid = start_recall(w, fid, v1)
    d = create_file(w, "dir", kind="dir")
    res = changes(
        w,
        op(
            "rename",
            file={"file_id": str(fid)},
            new_parent={"file_id": str(d)},
            new_name="moved2.segy",
        ),
        op("setattr", file={"file_id": str(fid)}, attrs={"uid": 5, "gid": 6}),
    )
    assert [r["status"] for r in res] == ["applied"] * 2, res
    assert recall(sql, rid)[:2] == ("requested", None)


@pytest.mark.parametrize("state", ["available", "failed", "cancelled"])
def test_a_recall_that_already_ended_stays_as_it_ended(make_world, facade_client, sql, state):
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "done.segy")
    rid = start_recall(w, fid, v1)
    sql.execute(
        "UPDATE catalog.recall SET state = %s, completed_at = now() WHERE recall_id = %s",
        (state, int(rid)),
    )
    told = notified(sql, rid)
    upload(w, facade_client, fid, NEW)
    assert recall(sql, rid)[:2] == (state, None)
    assert notified(sql, rid) == told


def test_the_next_read_after_a_replace_is_of_the_new_version(make_world, facade_client, sql):
    """Nothing is left to join: the replaced file is read as any online file is."""
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "next.segy")
    rid = start_recall(w, fid, v1)
    _, done = upload(w, facade_client, fid, NEW)
    assert recall(sql, rid)[0] == "superseded"
    now = w.edge.post(f"{E}/recalls", read_req(fid, done["version_id"]))
    assert now.status_code == 200 and now.json()["status"] == "online", now.text
    # The old version itself can still be asked for (a rewind read): a new recall.
    again = start_recall(w, fid, v1, pinned=True)
    assert again != rid and recall(sql, again)[:2] == ("requested", None)


def test_a_state_and_its_reason_go_together(sql, make_world, facade_client):
    import psycopg

    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "check.segy")
    rid = start_recall(w, fid, v1)
    with pytest.raises(psycopg.errors.CheckViolation):
        sql.execute(
            "UPDATE catalog.recall SET state = 'superseded' WHERE recall_id = %s", (int(rid),)
        )


def test_a_keep_online_hold_and_a_pin_outlive_the_recall(make_world, facade_client, sql):
    """Both are the file's, not a version's or a recall's: the 14-day hold a
    requester set from their card (decision 33) and a tier pin (decision 29) are
    as they were after the file is replaced, and after it is deleted."""
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "held.segy")
    rid = start_recall(w, fid, v1)
    (ds,) = sql.execute("SELECT dataset_id FROM catalog.file WHERE file_id = %s", (fid,)).fetchone()
    (who,) = sql.execute(
        "INSERT INTO catalog.identity (sid) VALUES ('S-1-5-21-1-2-3-4242') RETURNING identity_id"
    ).fetchone()
    # Holds and pins are audited: they need an actor.
    sql.execute("SELECT set_config('omnifile.actor', 'user:test', false)")
    (hold,) = sql.execute(
        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id, until)"
        " VALUES (%s, %s, 'keep_online', 'Teams', %s, now() + interval '14 days')"
        " RETURNING hold_id",
        (ds, fid, who),
    ).fetchone()
    (pin,) = sql.execute(
        "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, until,"
        " set_by_identity_id) VALUES (%s, %s, 'hot', now() + interval '30 days', %s)"
        " RETURNING pin_id",
        (ds, fid, who),
    ).fetchone()

    def kept() -> tuple:
        h = sql.execute(
            "SELECT released_at IS NULL, until FROM catalog.hold WHERE hold_id = %s", (hold,)
        ).fetchone()
        p = sql.execute(
            "SELECT released_at IS NULL, until, logical_tier::text FROM catalog.tier_pin"
            " WHERE pin_id = %s",
            (pin,),
        ).fetchone()
        return h, p

    before = kept()
    assert before[0][0] is True and before[1][0] is True and before[1][2] == "hot"
    upload(w, facade_client, fid, NEW)
    assert recall(sql, rid)[:2] == ("superseded", "replaced")
    assert kept() == before
    (res,) = changes(w, op("unlink", file={"file_id": str(fid)}))
    assert res["status"] == "applied", res
    assert kept() == before


def test_an_edge_that_predates_the_state_is_told_cancelled(make_world, facade_client, sql):
    """Edges are updated after the service. One that doesn't know `superseded` reads it
    as an unknown state and ends nothing, so a reader of a `wait` share could wait for
    good. Until an edge says it knows the state (its heartbeat), the feed says
    `cancelled`, which every edge treats the same way."""
    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "old-edge.segy")
    rid = start_recall(w, fid, v1)
    upload(w, facade_client, fid, NEW)
    assert recall(sql, rid)[0] == "superseded"

    def last_state() -> str:
        events, _ = feed_all(w)
        return [e for e in events if e["type"] == "recall.state"][-1]["recall"]["state"]

    # No heartbeat yet, and then one from an edge that says nothing of it.
    assert last_state() == "cancelled"
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.1.0", "fills_running": 0}).status_code == 200
    )
    assert last_state() == "cancelled"
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.1.0", "recall_superseded": 0}).status_code
        == 200
    )
    assert last_state() == "cancelled"
    # The catalog and the portal keep the true state; only this edge's feed is put.
    assert recall(sql, rid)[0] == "superseded"
    assert (
        w.edge.post(f"{E}/heartbeat", {"version": "0.1.0", "recall_superseded": 1}).status_code
        == 200
    )
    assert last_state() == "superseded"


def test_a_commit_and_a_recall_step_that_cross_are_told_to_try_again(
    make_world, facade_client, sql, db_url
):
    """#96 review, the lock order. The commit takes the file's row and then the
    recall's (to end it); `recall_filled` and the worker's recall steps take the
    recall's and then the file's. Two of them on one file at the same instant wait on
    each other, and Postgres ends one. This makes that happen and checks what each side
    does with it: the api answers `unavailable` with a retry (the edge retries reports
    and commits), the worker's queue counts a conflict, not a failure, and the one that
    was ended goes through when it is tried again."""
    import threading
    import time

    import psycopg

    from omnifile_service.api.db import map_db_error
    from omnifile_service.worker.queue import CONFLICTS

    w = make_world()
    fid, v1 = archived_file(w, facade_client, sql, "crossed.segy")
    rid = int(start_recall(w, fid, v1))
    sql.execute("UPDATE catalog.recall SET state = 'downloading' WHERE recall_id = %s", (rid,))
    (ds, parent, name) = sql.execute(
        "SELECT dataset_id, parent_id, name FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    commit = (
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size) VALUES (%s, %s, %s, %s, 'content', 'committed', now(), 9)"
    )
    errors: dict[str, BaseException] = {}

    def the_commit() -> None:
        try:
            with psycopg.connect(db_url) as c, c.transaction():
                c.execute(commit, (fid, ds, parent, name))  # the file's row, then the recall's
        except psycopg.Error as e:
            errors["commit"] = e

    with psycopg.connect(db_url) as a:
        try:
            with a.transaction():
                # As `recall_filled`: the recall's row first.
                a.execute("SELECT 1 FROM catalog.recall WHERE recall_id = %s FOR UPDATE", (rid,))
                t = threading.Thread(target=the_commit)
                t.start()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:  # until the commit waits on the recall
                    (n,) = sql.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    ).fetchone()
                    if n:
                        break
                    time.sleep(0.05)
                assert n, "the commit never waited on the recall's row"
                # ... and then the file's.
                a.execute(
                    "UPDATE catalog.file SET resident_bytes = size WHERE file_id = %s", (fid,)
                )
                a.execute(
                    "UPDATE catalog.recall SET state = 'available', completed_at = now()"
                    " WHERE recall_id = %s",
                    (rid,),
                )
        except psycopg.Error as e:
            errors["recall"] = e
        t.join(30)
    assert len(errors) == 1, errors
    ((who, exc),) = errors.items()
    assert isinstance(exc, psycopg.errors.DeadlockDetected), (who, exc)
    # Both sides treat it as "try again".
    told = map_db_error(exc)
    assert told.code == "unavailable" and told.retry_after_s == 1
    assert psycopg.errors.DeadlockDetected in CONFLICTS
    # The one that was ended goes through on its next try, after the other.
    if who == "commit":
        assert recall(sql, str(rid))[0] == "available"
        sql.execute(commit, (fid, ds, parent, name))
        assert recall(sql, str(rid))[0] == "available"  # it had ended already: left as it is
    else:
        assert recall(sql, str(rid))[:2] == ("superseded", "replaced")
