"""Removals from a NAS through the control API (phase A2.2; wp14-a2.md,
"What writes to the filer"; decision 52). The edge asks for a ticket per
file and again before each write; the service answers from every gate."""

from __future__ import annotations

import psycopg
import pytest

from omnifile_service.annex import rules

from .conftest import E, World
from .test_annex import DATA, arrived, copy, events, make_annex


def copied_file(
    w: World, facade_client, annex_id: int, path: str, ino: str, data: bytes = DATA
) -> dict:
    [res] = events(w, annex_id, [arrived(path, data, ino=ino, rule="move")])
    fid = res["file_id"]
    vid = copy(w, facade_client, fid, data)
    events(w, annex_id, [{"kind": "copied", "path": path, "file_id": fid, "version_id": vid,
                          "size": len(data), "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000005"}])  # fmt: skip
    return {"file_id": fid, "version_id": vid, "path": path, "nas_file_id": ino,
            "size": len(data), "mtime_ns": "1700000000000000000",
            "ctime_ns": "1700000000000000005", "rule": "move", "rules_version": 1}  # fmt: skip


def make_live(sql, annex_id: int, ds: int, *, read: bool = True) -> None:
    """A Move-now rule set, running, its dry run over and confirmed, and every
    copy of the annex read back in Azure just now."""
    body = rules.normalize(rules.move_now_template(ds), ds)
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s, removes = true,"
        " decided_as_archive_admin = true WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(body), annex_id),
    )
    sql.execute(
        "UPDATE catalog.annex SET state = 'active', mode = 'move_now', removals_paused = false,"
        " dry_run_until = now() - interval '1 hour', removals_confirmed_version = ruleset_version"
        " WHERE annex_id = %s",
        (annex_id,),
    )
    if read:
        sql.execute(
            "UPDATE catalog.location l SET azure_read_at = now() FROM catalog.annex_file af"
            " WHERE af.annex_id = %s AND l.version_id = af.copied_version_id",
            (annex_id,),
        )


def tickets(w: World, annex_id: int, cands: list[dict]) -> list[dict]:
    r = w.edge.post(f"{E}/annex-removal-tickets", {"annex_id": str(annex_id),
                                                    "candidates": cands})  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()["results"]


def past_grace(sql, removal_id: str) -> None:
    """The service keeps the grace itself: let it be over."""
    sql.execute("UPDATE catalog.annex_removal SET hidden_at = now() - interval '1 day'"
                " WHERE removal_id = %s", (int(removal_id),))  # fmt: skip


def step(w: World, removal_id: str, name: str, **kw) -> dict:
    r = w.edge.post(f"{E}/annex-removals/{{removal_id}}", {"step": name, **kw},
                    removal_id=removal_id)  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def live(make_world, sql, facade_client):
    w: World = make_world()
    annex_id, ds, _ = make_annex(sql, w)
    events(w, annex_id, [{"kind": "baseline", "details": {"files": 0, "bytes": 0}}])
    f = copied_file(w, facade_client, annex_id, "drop/a.segy", "201")
    assert w.verify() is not None
    make_live(sql, annex_id, ds)
    return w, annex_id, ds, f


def test_no_ticket_unless_removals_are_live(make_world, sql, facade_client) -> None:
    w: World = make_world()
    annex_id, ds, _ = make_annex(sql, w)
    events(w, annex_id, [{"kind": "baseline", "details": {"files": 0, "bytes": 0}}])
    f = copied_file(w, facade_client, annex_id, "drop/a.segy", "211")
    make_live(sql, annex_id, ds)
    cases = [
        ("UPDATE catalog.annex SET removals_confirmed_version = NULL WHERE annex_id = %s",
         "not_confirmed"),
        ("UPDATE catalog.annex SET dry_run_until = now() + interval '1 day' WHERE annex_id = %s",
         "dry_run"),
        ("UPDATE catalog.annex SET removals_paused = true WHERE annex_id = %s", "paused"),
    ]  # fmt: skip
    for change, why in cases:
        make_live(sql, annex_id, ds)
        sql.execute(change, (annex_id,))
        [r] = tickets(w, annex_id, [f])
        assert (r["status"], r["reason"]) == ("not_yet", why), why
    make_live(sql, annex_id, ds)
    sql.execute("UPDATE catalog.annex_defaults SET removals_paused = true")
    try:
        [r] = tickets(w, annex_id, [f])
        assert (r["status"], r["reason"]) == ("not_yet", "paused")
    finally:
        sql.execute("UPDATE catalog.annex_defaults SET removals_paused = false")
    # The edge learns it from GET annexes too (it mounts read-write only then).
    a = [x for x in w.edge.get(f"{E}/annexes").json()["annexes"]
         if x["annex_id"] == str(annex_id)][0]  # fmt: skip
    assert a["removals"]["live"] is True and a["removals"]["grace_minutes"] == 15


def test_a_copy_is_read_back_in_azure_before_its_ticket(make_world, sql, facade_client) -> None:
    w: World = make_world()
    annex_id, ds, _ = make_annex(sql, w)
    events(w, annex_id, [{"kind": "baseline", "details": {"files": 0, "bytes": 0}}])
    f = copied_file(w, facade_client, annex_id, "drop/a.segy", "221")
    w.verify()
    make_live(sql, annex_id, ds, read=False)
    sql.execute("UPDATE catalog.location SET azure_read_at = NULL WHERE file_id = %s",
                (int(f["file_id"]),))  # fmt: skip
    [r] = tickets(w, annex_id, [f])
    assert (r["status"], r["reason"]) == ("not_yet", "needs_azure_read")
    queued = sql.execute(
        "SELECT count(*) FROM pgmq.q_verify WHERE message->>'purpose' = 'removal'"
    ).fetchone()[0]
    assert queued == 1
    # Asked again soon: no second read queued.
    tickets(w, annex_id, [f])
    assert (
        sql.execute(
            "SELECT count(*) FROM pgmq.q_verify WHERE message->>'purpose' = 'removal'"
        ).fetchone()[0]
        == 1
    )
    # The worker reads the verified copy again, and then the ticket comes.
    w.verify()
    assert sql.execute(
        "SELECT azure_read_at IS NOT NULL FROM catalog.location WHERE file_id = %s"
        " AND role = 'primary'",
        (int(f["file_id"]),),
    ).fetchone()[0]
    [r] = tickets(w, annex_id, [f])
    assert r["status"] == "ticket", r


def test_the_whole_removal_and_its_audit(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    assert r["status"] == "ticket", r
    rid = r["removal"]["removal_id"]
    assert r["removal"]["expect"]["nas_file_id"] == "201"
    # A restarted unit asks again: the same ticket, not a second one.
    [again] = tickets(w, annex_id, [f])
    assert again["removal"]["removal_id"] == rid
    assert step(w, rid, "hide")["proceed"] is True
    assert step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")["state"] == "hidden"
    past_grace(sql, rid)
    assert step(w, rid, "delete")["proceed"] is True
    assert step(w, rid, "deleted")["state"] == "deleted"
    step(w, rid, "pointer", written=True)
    assert step(w, rid, "done")["state"] == "done"
    # Asked again after a restart: answered as it is, nothing taken twice.
    assert step(w, rid, "delete")["proceed"] is True
    state, removed = sql.execute(
        "SELECT nas_state, removed_at IS NOT NULL FROM catalog.annex_file WHERE file_id = %s",
        (f["file_id"],),
    ).fetchone()
    assert (state, removed) == ("removed", True)
    kinds = [k for (k,) in sql.execute(
        "SELECT kind FROM catalog.annex_event WHERE annex_id = %s AND kind IN"
        " ('removal_scheduled', 'renamed_for_removal', 'removed') ORDER BY event_id",
        (annex_id,))]  # fmt: skip
    assert kinds == ["removal_scheduled", "renamed_for_removal", "removed"]
    audited = {a for (a,) in sql.execute(
        "SELECT action FROM catalog.audit_log WHERE file_id = %s"
        " AND action LIKE 'annex.removal.%%'", (f["file_id"],))}  # fmt: skip
    assert {"annex.removal.hidden", "annex.removal.deleted"} <= audited
    # The catalog keeps the file: it lives in Azure now.
    assert sql.execute("SELECT deleted_at FROM catalog.file WHERE file_id = %s",
                       (f["file_id"],)).fetchone()[0] is None  # fmt: skip


def test_a_pause_holds_a_hidden_file_and_a_resume_lets_it_go_on(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    past_grace(sql, rid)
    sql.execute("UPDATE catalog.annex SET removals_paused = true WHERE annex_id = %s", (annex_id,))
    out = step(w, rid, "delete")
    # No W2, and no cancel: the file stays hidden, its grace starts again.
    assert out["proceed"] is False and out["reason"] == "paused" and out["state"] == "hidden"
    assert sql.execute("SELECT nas_state FROM catalog.annex_file WHERE file_id = %s",
                       (f["file_id"],)).fetchone()[0] == "removing"  # fmt: skip
    sql.execute("UPDATE catalog.annex SET removals_paused = false WHERE annex_id = %s",
                (annex_id,))  # fmt: skip
    # The pause started the grace again.
    assert step(w, rid, "delete")["reason"] == "grace"
    past_grace(sql, rid)
    assert step(w, rid, "delete")["proceed"] is True


def test_every_gate_on_the_rule_set_and_the_file(live, sql) -> None:
    w, annex_id, ds, f = live
    # Only a version an ArchiveAdmin approved removes.
    sql.execute("UPDATE catalog.annex_ruleset SET decided_as_archive_admin = false"
                " WHERE annex_id = %s AND state = 'active'", (annex_id,))  # fmt: skip
    [r] = tickets(w, annex_id, [f])
    assert (r["status"], r["reason"]) == ("not_yet", "not_archive_admin_approved")
    make_live(sql, annex_id, ds)
    # The rule the edge names must be one that removes.
    for rule in ("recopy", "keep", "nope"):
        [r] = tickets(w, annex_id, [{**f, "rule": rule}])
        assert (r["status"], r["reason"]) == ("refused", "rule_not_removing"), rule
    # A legal hold on a folder above the file (set and released audited).
    sql.execute("SELECT set_config('omnifile.actor', 'test', false)")
    (parent,) = sql.execute("SELECT parent_id FROM catalog.file WHERE file_id = %s",
                            (int(f["file_id"]),)).fetchone()  # fmt: skip
    (hold,) = sql.execute(
        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
        " VALUES (%s, %s, 'legal', 'case 7', 'test') RETURNING hold_id",
        (ds, parent),
    ).fetchone()
    [r] = tickets(w, annex_id, [f])
    assert (r["status"], r["reason"]) == ("refused", "legal_hold")
    sql.execute("UPDATE catalog.hold SET released_at = now() WHERE hold_id = %s", (hold,))
    [r] = tickets(w, annex_id, [f])
    assert r["status"] == "ticket"
    # A hold set after the ticket stops the delete (every gate again before W2).
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    sql.execute("INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
                " VALUES (%s, %s, 'legal', 'case 8', 'test')", (ds, int(f["file_id"])))  # fmt: skip
    past_grace(sql, rid)
    out = step(w, rid, "delete")
    assert out["proceed"] is False and out["reason"] == "legal_hold"
    assert out["state"] == "cancelled", "the edge renames it back (W3)"


def test_a_changed_file_an_expired_ticket_the_rate_and_the_window(live, sql, facade_client):
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [{**f, "size": f["size"] + 1}])
    assert (r["status"], r["reason"]) == ("refused", "changed_since_copied")
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    sql.execute("UPDATE catalog.annex_removal SET expires_at = now() - interval '1 minute'"
                " WHERE removal_id = %s", (int(rid),))  # fmt: skip
    out = step(w, rid, "hide")
    assert out["proceed"] is False and out["reason"] == "ticket_expired"
    g = copied_file(w, facade_client, annex_id, "drop/b.segy", "202", DATA + b"b")
    w.verify()
    make_live(sql, annex_id, ds)
    sql.execute("UPDATE catalog.annex SET removal_rate_per_hour = 1 WHERE annex_id = %s",
                (annex_id,))  # fmt: skip
    [r] = tickets(w, annex_id, [g])
    assert (r["status"], r["reason"]) == ("not_yet", "rate")
    # A window that never opens (from midnight to midnight is empty).
    sql.execute(
        "UPDATE catalog.annex SET removal_rate_per_hour = 1000, removal_window = %s"
        " WHERE annex_id = %s",
        (psycopg.types.json.Jsonb({"start": "00:00", "end": "00:00"}), annex_id),
    )
    [r] = tickets(w, annex_id, [g])
    assert (r["status"], r["reason"]) == ("not_yet", "outside_window")


def test_another_edge_cant_ask_for_or_step_a_removal(live, enrol_edge) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    other = enrol_edge()
    resp = other.post(f"{E}/annex-removals/{{removal_id}}", {"step": "hide"},
                      removal_id=r["removal"]["removal_id"])  # fmt: skip
    assert resp.status_code == 404
    resp = other.post(f"{E}/annex-removal-tickets", {"annex_id": str(annex_id),
                                                      "candidates": [f]})  # fmt: skip
    assert resp.status_code == 404


# --- #61's review: each probe, a test ----------------------------------------------------


def test_another_annex_never_gets_a_ticket_for_this_annexs_file(live, make_world, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    assert r["status"] == "ticket"
    w2 = make_world()
    a2, ds2, _ = make_annex(sql, w2)
    events(w2, a2, [{"kind": "baseline", "details": {"files": 0, "bytes": 0}}])
    make_live(sql, a2, ds2)
    [r2] = tickets(w2, a2, [{**f, "nas_file_id": "999", "path": "x/y"}])
    assert (r2["status"], r2["reason"]) == ("refused", "not_in_annex"), r2


def test_the_edges_cancel_and_a_file_left_hidden_are_recorded(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    sql.execute("SELECT set_config('omnifile.actor', 'test', false)")
    sql.execute("INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
                " VALUES (%s, %s, 'legal', 'case 8', 'test')", (ds, int(f["file_id"])))  # fmt: skip
    past_grace(sql, rid)
    assert step(w, rid, "delete")["state"] == "cancelled"
    # The edge's W3 couldn't put it back: what it says is kept, and raised.
    step(w, rid, "cancelled", reason="legal_hold; the name is taken: it stays hidden")
    reason = sql.execute("SELECT cancel_reason FROM catalog.annex_removal WHERE removal_id = %s",
                         (int(rid),)).fetchone()[0]  # fmt: skip
    assert "stays hidden" in reason
    out = step(w, rid, "stuck", reason="the name is taken",
               hidden_name=".omnifile-removing-1-a.segy")  # fmt: skip
    assert out["state"] == "stuck"
    kinds = [k for (k,) in sql.execute(
        "SELECT kind FROM catalog.annex_event WHERE annex_id = %s AND kind = 'removal_stuck'",
        (annex_id,))]  # fmt: skip
    assert kinds == ["removal_stuck"]


def test_the_service_keeps_the_grace_itself(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    out = step(w, rid, "delete")  # at once: the grace is 15 minutes
    assert out["proceed"] is False and out["reason"] == "grace" and out["state"] == "hidden"


def test_a_ticket_of_an_older_rule_set_is_never_deleted(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    assert r["removal"]["ruleset_version"] is not None
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    body = rules.normalize(rules.move_now_template(ds), ds)
    v = sql.execute("SELECT max(version) + 1 FROM catalog.annex_ruleset WHERE annex_id = %s",
                    (annex_id,)).fetchone()[0]  # fmt: skip
    sql.execute("UPDATE catalog.annex_ruleset SET state = 'superseded' WHERE annex_id = %s"
                " AND state = 'active'", (annex_id,))  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.annex_ruleset (annex_id, version, body, state, owner_identity_id,"
        " proposed_via, decided_by_identity_id, decided_at, removes, decided_as_archive_admin)"
        " SELECT annex_id, %s, %s, 'active', owner_identity_id, 'portal', owner_identity_id,"
        " now(), true, true FROM catalog.annex_ruleset WHERE annex_id = %s AND version = 1",
        (v, psycopg.types.json.Jsonb(body), annex_id),
    )
    sql.execute("UPDATE catalog.annex SET ruleset_version = %s, removals_confirmed_version = %s"
                " WHERE annex_id = %s", (v, v, annex_id))  # fmt: skip
    past_grace(sql, rid)
    out = step(w, rid, "delete")
    assert out["proceed"] is False and out["reason"] == "rule_set_changed"
    # And a candidate decided under the old version waits for the edge to decide again.
    [r2] = tickets(w, annex_id, [{**f, "rules_version": 1}])
    assert (r2["status"], r2["reason"]) == ("not_yet", "rule_set_changed")


def test_the_catalogs_path_not_the_edges(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [{**f, "path": "somewhere/else.txt"}])
    assert (r["status"], r["reason"]) == ("refused", "path_mismatch")


def test_every_step_is_audited_with_the_verified_crc(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    assert r["removal"]["expect"]["crc64"], "the verified copy's CRC, for the edge's re-read"
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    step(w, rid, "delete")  # a hold (the grace): audited too
    past_grace(sql, rid)
    step(w, rid, "delete")
    step(w, rid, "deleted")
    step(w, rid, "pointer", written=True)
    step(w, rid, "done")
    rows = sql.execute(
        "SELECT action, decision, details->>'crc64' FROM catalog.audit_log WHERE file_id = %s"
        " AND action LIKE 'annex.removal.%%' ORDER BY audit_id",
        (int(f["file_id"]),),
    ).fetchall()
    acts = [(a, d) for a, d, _ in rows]
    for want in [("annex.removal.ticket", "allow"), ("annex.removal.hide", "allow"),
                 ("annex.removal.hidden", "allow"), ("annex.removal.delete", "deny"),
                 ("annex.removal.delete", "allow"), ("annex.removal.deleted", "allow"),
                 ("annex.removal.pointer", "allow"), ("annex.removal.done", "allow")]:  # fmt: skip
        assert want in acts, (want, acts)
    assert all(c for _, _, c in rows), "every record names the verified CRC"


def test_a_new_file_where_a_removed_one_was_is_a_new_version(live, sql, db_url) -> None:
    from .test_annex import arrived

    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    past_grace(sql, rid)
    step(w, rid, "delete")
    step(w, rid, "deleted")
    step(w, rid, "done")
    events(w, annex_id, [arrived("drop/a.segy", b"new bytes", ino="777", rule="move")])
    gone, nas_state, deleted, nas_id = sql.execute(
        "SELECT af.gone_since IS NOT NULL, af.nas_state, f.deleted_at IS NOT NULL,"
        " af.nas_file_id FROM catalog.annex_file af JOIN catalog.file f"
        " ON f.file_id = af.file_id WHERE af.file_id = %s",
        (int(f["file_id"]),),
    ).fetchone()
    assert not gone and not deleted, "a removed file is never a NAS delete (purgeable)"
    assert (nas_state, nas_id) == ("present", "777"), "the same catalog file, a new version"
    # And a deleted-file retention never purges a removed copy.
    sql.execute("UPDATE catalog.annex SET deleted_retention_days = 1 WHERE annex_id = %s",
                (annex_id,))  # fmt: skip
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed',"
                " gone_since = now() - interval '9 days' WHERE file_id = %s",
                (int(f["file_id"]),))  # fmt: skip
    import asyncio
    from datetime import UTC, datetime

    from omnifile_service.annex.purge import purge_due

    from .conftest import as_app

    async def run() -> int:
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            return await purge_due(conn, datetime.now(UTC))

    asyncio.run(run())
    assert sql.execute("SELECT purge_requested_at FROM catalog.annex_file WHERE file_id = %s",
                       (int(f["file_id"]),)).fetchone()[0] is None  # fmt: skip


def test_a_copy_that_reads_back_wrong_before_a_removal_is_raised(live, sql, caplog) -> None:
    w, annex_id, ds, f = live
    sql.execute("UPDATE catalog.location SET azure_read_at = NULL, crc64nvme = %s"
                " WHERE file_id = %s AND role = 'primary'",
                (b"\x00" * 8, int(f["file_id"])))  # fmt: skip
    [r] = tickets(w, annex_id, [f])
    assert (r["status"], r["reason"]) == ("not_yet", "needs_azure_read")
    with caplog.at_level("ERROR"):
        w.verify()
    assert any("ALERT annex copy mismatch" in m for m in caplog.messages)
    kinds = [k for (k,) in sql.execute(
        "SELECT kind FROM catalog.annex_event WHERE annex_id = %s AND file_id = %s"
        " AND kind = 'copy_mismatch'", (annex_id, int(f["file_id"])))]  # fmt: skip
    assert kinds == ["copy_mismatch"], "on the annex's Problems list"
    [r] = tickets(w, annex_id, [f])
    assert r["status"] != "ticket"


def test_a_candidate_must_say_which_rule_set_decided_it(live) -> None:
    w, annex_id, ds, f = live
    c = {k: v for k, v in f.items() if k != "rules_version"}
    # The spec requires it too; the service refuses it on its own.
    resp = w.edge.post(f"{E}/annex-removal-tickets", {"annex_id": str(annex_id),
                       "candidates": [c]}, check=False)  # fmt: skip
    assert resp.status_code == 200, resp.text
    [r] = resp.json()["results"]
    assert (r["status"], r["reason"]) == ("refused", "rules_version_required")


def test_a_cancel_reported_again_is_recorded_once(live, sql) -> None:
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    step(w, rid, "hidden", hidden_name=".omnifile-removing-1-a.segy")
    for _ in range(3):  # the edge retries its report
        step(w, rid, "cancelled", reason="changed_in_grace")
    events = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s"
        " AND kind = 'removal_cancelled'", (annex_id,)).fetchone()[0]  # fmt: skip
    audits = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE file_id = %s"
        " AND action = 'annex.removal.cancelled'", (int(f["file_id"]),)).fetchone()[0]  # fmt: skip
    assert (events, audits) == (1, 1)
    # A different outcome (W3 couldn't put it back) is still recorded.
    step(w, rid, "cancelled", reason="changed_in_grace; the name is taken")
    assert sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s"
        " AND kind = 'removal_cancelled'", (annex_id,)).fetchone()[0] == 2  # fmt: skip


def test_a_users_file_a_removal_hid_kept_aside_is_a_problem(live, sql) -> None:
    """#98's E2: W1 hid a user's file saved just before it; its name taken since, the
    edge keeps it under a -replaced- name and the service raises a Problem naming it."""
    w, annex_id, ds, f = live
    [r] = tickets(w, annex_id, [f])
    rid = r["removal"]["removal_id"]
    step(w, rid, "hide")
    kept = "a-replaced-2026-11-12-120000Z.segy"
    step(w, rid, "cancelled", reason="swapped_at_hide", kept_as=kept)
    rows = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
        " AND details->>'reason' = 'removal_kept_aside'",
        (annex_id,),
    ).fetchall()
    assert len(rows) == 1 and rows[0][0]["kept_as"].endswith(kept)
