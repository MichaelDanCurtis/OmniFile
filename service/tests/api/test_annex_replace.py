"""A placement that replaces the file at its origin (phase A3.4; decisions 82 to 85):
the replace gate at the ticket and at each question, the reports, and what `done`
makes of it."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from omnifile_service.annex import replace as RP
from omnifile_service.annex import rules as R

from .conftest import E
from .test_annex import DATA, annex, copy, events  # noqa: F401 - the fixture
from .test_annex_placements import (
    BOB,
    INTERNS,
    _close,
    _gate,
    _go,
    _setup,
    state,
    step,
    tickets,
)

NEW = DATA + b"2"
TARGET = {"kind": "nas", "folder": "long-term-storage/dataset", "name": "line_1042.segy",
          "display": "x", "origin": True}  # fmt: skip
HIDDEN = "line_1042.segy"


def _overwrite_rules(sql, annex_id: int, ds: int) -> None:
    body = R.copy_template(ds)
    body["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                                   {"on_conflict": "overwrite_keep_version"},
                                   {"hold_days": 14}]}]  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )


def _world(ax, facade_client, sql, *, read_fresh: bool = True, on_conflict: str =
           "overwrite_keep_version", target: dict | None = None):  # fmt: skip
    """The file present on the NAS as its verified current copy v2, and a placement
    of v1 to its origin: (w, annex_id, fid, v1, v2, pid)."""
    w, annex_id, ds, filer = ax
    fid, v1, first = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, first)
    _overwrite_rules(sql, annex_id, ds)
    v2 = copy(w, facade_client, str(fid), NEW, base=str(v1))
    events(w, annex_id, [{"kind": "copied", "path": "dataset/line_1042.segy", "file_id": str(fid),
                          "version_id": v2, "size": len(NEW),
                          "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000009"}])  # fmt: skip
    assert w.verify()
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'present', removed_at = NULL,"
        " nas_file_id = '88', size = %s, mtime_ns = 1700000000000000000,"
        " ctime_ns = 1700000000000000009 WHERE file_id = %s",
        (len(NEW), fid),
    )
    sql.execute(
        "UPDATE catalog.location SET azure_read_at = CASE WHEN %s THEN now()"
        " ELSE now() - interval '30 days' END WHERE version_id = %s",
        (read_fresh, int(v2)),
    )
    (ident,) = sql.execute(
        "SELECT identity_id FROM catalog.identity WHERE sid = %s", (BOB,)
    ).fetchone()
    t = {**(target or TARGET), "filer_id": str(filer)}
    (pid,) = sql.execute(
        "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
        " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
        " hold_days, state) VALUES (%s, %s, %s, %s, 'portal', 'back', 1, %s, %s, 14, 'ready')"
        " RETURNING placement_id",
        (annex_id, fid, v1, ident, psycopg.types.json.Jsonb(t), on_conflict),
    ).fetchone()
    return w, annex_id, fid, int(v1), int(v2), pid


def _row(sql, pid: int) -> dict:
    return sql.execute(
        "SELECT state, outcome, replace_state, replace_refused, replaced_version_id,"
        " replaced_crc64, replace_hidden_name, replaced_aside_name, fail_reason"
        " FROM catalog.annex_placement WHERE placement_id = %s",
        (pid,),
    ).fetchone()


def _hidden(w, pid: int) -> dict:
    return step(w, pid, {"step": "replace_hidden",
                         "hidden_name": RP.hidden_name(pid, HIDDEN)})  # fmt: skip


def _past_grace(sql, pid: int) -> None:
    sql.execute(
        "UPDATE catalog.annex_placement SET replace_hidden_at = now() - interval '1 hour'"
        " WHERE placement_id = %s",
        (pid,),
    )


def _ask(w, pid: int, which: str, phase: str | None = None, name: str = HIDDEN) -> dict:
    body = {"step": which}
    if phase:
        body["phase"] = phase
    if phase == "delete":
        body["name"] = name  # the new file's final name (#98's E1)
    return step(w, pid, body)


def _audits(sql, fid: int, action: str) -> list:
    return sql.execute(
        "SELECT decision, details FROM catalog.audit_log WHERE file_id = %s AND action = %s"
        " ORDER BY at, audit_id",
        (fid, f"annex.placement.{action}"),
    ).fetchall()


def test_a_verified_current_copy_is_replaced_and_done_makes_a_new_head(
    annex,  # noqa: F811
    facade_client,
    sql,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    [k] = tickets(w, annex_id)["tickets"]
    r = k["replace"]
    assert r == {"version_id": str(v2), "nas_file_id": "88", "size": len(NEW),
                 "mtime_ns": "1700000000000000000", "ctime_ns": "1700000000000000009",
                 "crc64": r["crc64"], "hidden_name": RP.hidden_name(pid, HIDDEN)}  # fmt: skip
    # The older version may take its own name now: first in the list.
    assert k["names"][0] == "line_1042.segy" and not k["same_content"]
    assert _row(sql, pid)[2:5] == ("offered", None, v2)
    step(w, pid, {"step": "filled"})
    step(w, pid, {"step": "read_back", "crc64": k["crc64"]})
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    assert _row(sql, pid)[2] == "hidden"
    # Its grace, kept by the service too.
    out = _ask(w, pid, "replace_delete_go", "place")
    assert (out["proceed"], out["hold"], out["reason"]) == (False, True, "grace")
    _past_grace(sql, pid)
    assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    assert _ask(w, pid, "replace_delete_go", "delete")["proceed"]
    step(w, pid, {"step": "replaced"})
    done = step(w, pid, {"step": "done", "outcome": "replaced", "name": "line_1042.segy",
                         "attrs": {"nas_file_id": "99", "size": len(DATA), "mtime_ns": 1,
                                   "ctime_ns": 2}})  # fmt: skip
    assert done["proceed"]
    row = _row(sql, pid)
    assert row[:3] == ("placed", "replaced", "replaced") and row[4] == v2
    head, blob, superseded = sql.execute(
        "SELECT f.content_version_id, l.object_key = o.object_key,"
        "       (SELECT state FROM catalog.file_version WHERE version_id = %s)"
        "  FROM catalog.file f"
        "  JOIN catalog.location l ON l.version_id = f.content_version_id AND l.role = 'primary'"
        "  JOIN catalog.location o ON o.version_id = %s AND o.role = 'primary'"
        " WHERE f.file_id = %s",
        (v2, v1, fid),
    ).fetchone()
    assert head not in (v1, v2), "a new head version"
    assert blob, "it references the placed version's blob: no copy"
    assert superseded is not None, "the replaced version stays in the file's history"
    af = sql.execute(
        "SELECT nas_state, nas_file_id, copied_version_id, verified_version_id,"
        " hold_until IS NOT NULL FROM catalog.annex_file WHERE file_id = %s",
        (fid,),
    ).fetchone()
    assert af == ("present", "99", head, head, True)
    # Every step audited with the replaced version and its CRC.
    for action in ("replace_offered", "replace_go", "replace_hidden", "replace_delete_go",
                   "replaced"):  # fmt: skip
        rows = _audits(sql, fid, action)
        assert rows, action
        assert rows[-1][1]["replaced_version_id"] == str(v2)
        assert rows[-1][1]["replaced_crc64"] == r["crc64"]


@pytest.mark.parametrize(
    "why", ["changed", "legal_hold", "removal_open", "other_replace", "removing", "uncopied_head"]
)
def test_a_replace_is_refused_and_the_placement_takes_a_restored_name(
    annex,  # noqa: F811
    facade_client,
    sql,
    why,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    if why == "changed":
        sql.execute("UPDATE catalog.annex_file SET size = size + 1 WHERE file_id = %s", (fid,))
    elif why == "legal_hold":
        sql.execute("SELECT set_config('omnifile.actor', 'test', false)")
        sql.execute("INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
                    " SELECT dataset_id, file_id, 'legal', 'case', 'test' FROM catalog.file"
                    " WHERE file_id = %s", (fid,))  # fmt: skip
    elif why == "removal_open":
        sql.execute(
            "INSERT INTO catalog.annex_removal (annex_id, file_id, version_id, path, nas_file_id,"
            " size, mtime_ns, ctime_ns, rule, ruleset_version, issued_at, expires_at)"
            " VALUES (%s, %s, %s, 'dataset/line_1042.segy', '88', 1, 1, 1, 'x', 1, now(),"
            " now())",
            (annex_id, fid, v2),
        )
    elif why == "removing":
        sql.execute(
            "UPDATE catalog.annex_file SET nas_state = 'removing' WHERE file_id = %s", (fid,)
        )
    elif why == "uncopied_head":
        # The head moved past the copy (a copy in flight): not the copy any more.
        sql.execute(
            "UPDATE catalog.annex_file SET copied_version_id = %s WHERE file_id = %s", (v1, fid)
        )
    elif why == "other_replace":
        sql.execute(
            "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
            " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
            " state, replace_state) SELECT annex_id, file_id, version_id, requester_identity_id,"
            " 'portal', rule, 1, jsonb_set(target, '{name}', '\"x\"'), on_conflict, 'placing',"
            " 'hidden' FROM catalog.annex_placement WHERE placement_id = %s",
            (pid,),
        )
    t = tickets(w, annex_id)
    [k] = [x for x in t["tickets"] if x["placement_id"] == str(pid)]
    assert k["replace"] is None
    # An older version without its replace never takes the file's name.
    assert "line_1042.segy" not in k["names"]
    row = _row(sql, pid)
    why = {"removing": "removal_open", "uncopied_head": "changed"}.get(why, why)
    assert row[2:4] == ("refused", why)
    assert _audits(sql, fid, "replace_refused")[-1][0] == "deny"
    assert _ask(w, pid, "replace_go")["reason"] == "not_in_order"
    restored = "line_1042-restored.segy"
    assert _go(w, pid, name=restored)["proceed"]
    step(w, pid, {"step": "done", "outcome": "restored_name", "name": restored,
                  "attrs": {"nas_file_id": "99", "size": len(DATA), "mtime_ns": 1,
                            "ctime_ns": 2}})  # fmt: skip
    assert _row(sql, pid)[1] == f"overwrite_refused_{why}"


def test_an_unverified_or_stale_copy_waits_for_its_read_then_falls_back(
    annex,  # noqa: F811
    facade_client,
    sql,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql, read_fresh=False)
    # Held while the in-Azure read is under way; the read is queued once.
    assert tickets(w, annex_id)["tickets"] == []
    asked = sql.execute(
        "SELECT azure_read_requested_at IS NOT NULL FROM catalog.location WHERE version_id = %s",
        (v2,),
    ).fetchone()[0]
    assert asked
    assert _row(sql, pid)[0] == "ready" and _row(sql, pid)[2] is None
    # Within its wait, still held.
    assert tickets(w, annex_id)["tickets"] == []
    sql.execute(
        "UPDATE catalog.annex_placement SET replace_asked_at = now() - %s WHERE placement_id = %s",
        (RP.AZURE_READ_WAIT - timedelta(minutes=1), pid),
    )
    assert tickets(w, annex_id)["tickets"] == []
    # Past it: a ticket without a replace.
    sql.execute(
        "UPDATE catalog.annex_placement SET replace_asked_at = now() - %s WHERE placement_id = %s",
        (RP.AZURE_READ_WAIT + timedelta(minutes=1), pid),
    )
    [k] = tickets(w, annex_id)["tickets"]
    assert k["replace"] is None
    assert _row(sql, pid)[2:4] == ("refused", "azure_read_pending")
    # An unverified copy is held the same way.
    w2, annex_id2, fid2, _, v22, pid2 = _world(annex, facade_client, sql)
    _close(sql, pid)
    sql.execute("UPDATE catalog.location SET verified = false WHERE version_id = %s", (v22,))
    assert [x for x in tickets(w2, annex_id2)["tickets"] if x["placement_id"] == str(pid2)] == []


def test_the_file_already_holding_the_placed_bytes_is_left_alone(
    annex,  # noqa: F811
    facade_client,
    sql,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    # The placed version's CRC and size are the current copy's.
    sql.execute(
        "UPDATE catalog.location l SET crc64nvme = o.crc64nvme FROM catalog.location o"
        " WHERE l.version_id = %s AND o.version_id = %s AND o.role = 'primary'",
        (v2, v1),
    )
    sql.execute("UPDATE catalog.file_version SET size = %s WHERE version_id = %s",
                (len(DATA), v2))  # fmt: skip
    [k] = tickets(w, annex_id)["tickets"]
    assert k["replace"] is None and k["same_content"] and k["names"][0] == "line_1042.segy"
    assert _row(sql, pid)[2] == "same"


def test_each_question_checks_every_gate_again(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    # Changed between the ticket and the hide: refused, never hidden.
    sql.execute("UPDATE catalog.annex_file SET ctime_ns = ctime_ns + 1 WHERE file_id = %s", (fid,))
    out = _ask(w, pid, "replace_go")
    assert (out["proceed"], out["hold"], out["reason"]) == (False, False, "changed")
    assert _row(sql, pid)[2:4] == ("refused", "changed")
    assert state(sql, pid)[0] == "placing", "the placement itself goes on, under -restored"


def test_a_hold_placed_in_the_grace_refuses_the_link_and_the_delete(
    annex,  # noqa: F811
    facade_client,
    sql,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    _past_grace(sql, pid)
    assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    sql.execute("SELECT set_config('omnifile.actor', 'test', false)")
    sql.execute("INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
                " SELECT dataset_id, file_id, 'legal', 'case', 'test' FROM catalog.file"
                " WHERE file_id = %s", (fid,))  # fmt: skip
    for phase in ("place", "delete"):
        out = _ask(w, pid, "replace_delete_go", phase)
        assert (out["proceed"], out["hold"], out["reason"]) == (False, False, "legal_hold")
    # The edge puts the old file back aside (the new one has the name): a problem
    # names both, and nothing is deleted.
    aside = "line_1042-replaced-2026-11-11-120000Z.segy"
    step(w, pid, {"step": "replace_aside", "name": aside, "reason": "legal_hold"})
    step(w, pid, {"step": "replace_aside", "name": aside, "reason": "legal_hold"})  # once
    row = _row(sql, pid)
    assert row[2] == "kept_aside" and row[7] == aside
    problems = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
        " AND details->>'reason' = 'replace_kept_aside'",
        (annex_id,),
    ).fetchall()
    assert len(problems) == 1
    d = problems[0][0]
    assert d["kept_as"].endswith(aside) and "line_1042.segy" in d["detail"]
    step(w, pid, {"step": "done", "outcome": "replaced_old_kept", "name": "line_1042.segy",
                  "attrs": {"nas_file_id": "99", "size": len(DATA), "mtime_ns": 1,
                            "ctime_ns": 2}})  # fmt: skip
    assert _row(sql, pid)[:2] == ("placed", "replaced_old_kept")


def test_a_paused_placement_holds_the_hidden_file_and_lost_access_ends_it(
    annex,  # noqa: F811
    facade_client,
    sql,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    _past_grace(sql, pid)
    sql.execute("UPDATE catalog.annex_defaults SET placements_paused = true")
    try:
        out = _ask(w, pid, "replace_delete_go", "place")
        assert (out["proceed"], out["hold"], out["reason"]) == (False, True, "paused")
    finally:
        sql.execute("UPDATE catalog.annex_defaults SET placements_paused = false")
    sql.execute("UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([INTERNS], BOB))
    out = _ask(w, pid, "replace_delete_go", "place")
    assert (out["proceed"], out["hold"]) == (False, False)
    assert out["state"] == "failed" and state(sql, pid) == ("failed", "access_denied")
    # What the edge then does to the old file is still recorded.
    step(w, pid, {"step": "replace_put_back", "reason": "access_denied"})
    assert _row(sql, pid)[2] == "put_back"


def test_reports_need_their_own_names(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    r = w.edge.post(
        f"{E}/annex-placements/{{placement_id}}",
        {"step": "replace_hidden", "hidden_name": ".omnifile-replacing-1-other"},
        placement_id=pid,
    )
    assert r.status_code == 400
    r = w.edge.post(
        f"{E}/annex-placements/{{placement_id}}",
        {"step": "replace_aside", "name": "line_1042.segy"},
        placement_id=pid,
    )
    assert r.status_code == 400


def test_a_departure_while_hidden_is_refused_and_a_replace_counts_toward_the_breaker(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,  # noqa: F811
) -> None:
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    [res] = events(w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy",
                                  "nas_file_id": "88"}])  # fmt: skip
    assert res["status"] == "rejected" and res["code"] == "placement_in_progress"
    (gone,) = sql.execute(
        "SELECT gone_since FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert gone is None
    # A replace is a return to the origin: four in 30 days pin the file.
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'placed', outcome = 'replaced',"
        " replace_state = 'replaced', final_name = 'line_1042.segy', done_at = now()"
        " WHERE placement_id = %s",
        (pid,),
    )
    for _ in range(3):
        sql.execute(
            "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
            " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
            " state, outcome, final_name, done_at) SELECT annex_id, file_id, version_id,"
            " requester_identity_id, 'portal', rule, 1, target, on_conflict, 'placed',"
            " 'replaced', final_name, now() FROM catalog.annex_placement WHERE placement_id = %s",
            (pid,),
        )
    sql.execute("UPDATE catalog.annex_file SET hold_until = NULL WHERE file_id = %s", (fid,))
    assert _gate(db_url, annex_id, fid, v2, ino="88")[1] == "loop_breaker"


def test_replace_go_before_a_ticket_or_twice_is_out_of_order(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    assert _ask(w, pid, "replace_go")["reason"] == "not_in_order"
    tickets(w, annex_id)
    assert _ask(w, pid, "replace_go")["reason"] == "not_in_order", "go comes first"
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_delete_go", "place")["reason"] == "not_in_order"
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    _hidden(w, pid)  # repeated after a restart: once
    assert len(_audits(sql, fid, "replace_hidden")) == 1
    assert _ask(w, pid, "replace_go")["reason"] == "not_in_order"


def test_only_the_files_own_name_in_its_own_folder_is_replaced() -> None:
    """Decision 82: anywhere else the file takes a `-restored` name."""
    src = {"file_filer_id": 7, "file_annex_path": "long-term-storage", "path": "dataset/a.segy"}
    own = {"kind": "nas", "filer_id": "7", "folder": "long-term-storage/dataset", "name": "a.segy"}
    p = {"target": own, "on_conflict": "overwrite_keep_version"}
    assert RP.at_own_path(p, src) and RP.wanted(p, src)
    for other in ({"folder": "long-term-storage/other"}, {"name": "b.segy"}, {"filer_id": "8"},
                  {"kind": "share"}):  # fmt: skip
        assert not RP.at_own_path({"target": {**own, **other}}, src), other
    assert not RP.wanted({**p, "on_conflict": "restored_name"}, src)
    assert not RP.at_own_path(p, None)
    root = {**src, "file_annex_path": ""}
    assert RP.at_own_path({"target": {**own, "folder": "dataset"}}, root)


def test_lost_access_at_the_delete_keeps_the_placement_and_the_old_file(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """The new file has its name by then: the placement goes on, the old file is kept."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    _past_grace(sql, pid)
    assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    sql.execute("UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([INTERNS], BOB))
    out = _ask(w, pid, "replace_delete_go", "delete")
    assert (out["proceed"], out["hold"], out["reason"]) == (False, False, "access_denied")
    assert state(sql, pid)[0] == "placing", "not failed: the new file is under its name"
    assert _row(sql, pid)[2:4] == ("hidden", "access_denied")


def test_a_new_verified_copy_after_the_ticket_is_not_the_one_offered(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """The file there must still be the version the ticket named, not a newer copy,
    however verified: the edge was told that one's attributes."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    v3 = copy(w, facade_client, str(fid), NEW + b"3", base=str(v2))
    events(w, annex_id, [{"kind": "copied", "path": "dataset/line_1042.segy", "file_id": str(fid),
                          "version_id": v3, "size": len(NEW) + 1,
                          "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000009"}])  # fmt: skip
    assert w.verify()
    sql.execute("UPDATE catalog.annex_file SET size = %s WHERE file_id = %s", (len(NEW) + 1, fid))
    sql.execute("UPDATE catalog.location SET azure_read_at = now() WHERE version_id = %s", (v3,))
    out = _ask(w, pid, "replace_go")
    assert (out["proceed"], out["reason"]) == (False, "changed")


# --- After the reviews of #98 -------------------------------------------------------


def _to_hidden(w, sql, annex_id: int, pid: int) -> None:
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _hidden(w, pid)
    _past_grace(sql, pid)


def _rule_says(sql, annex_id: int, conflict: str) -> None:
    """An Owner's edit of the return rule, approved and running."""
    body = sql.execute(
        "SELECT body FROM catalog.annex_ruleset WHERE annex_id = %s AND state = 'active'",
        (annex_id,),
    ).fetchone()[0]
    for r in body["rules"]:
        if r["id"] == "back":
            r["actions"] = [a if "on_conflict" not in a else {"on_conflict": conflict}
                            for a in r["actions"]]  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(body), annex_id),
    )


def test_the_delete_is_refused_where_the_new_file_isnt_at_the_origin(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """E1: the delete question names the new file's name; any other is refused."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    _to_hidden(w, sql, annex_id, pid)
    out = _ask(w, pid, "replace_delete_go", "delete")
    assert out["reason"] == "not_in_order", "the delete phase only after the place phase's yes"
    assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    for name in ("line_1042-restored.segy", None):
        out = _ask(w, pid, "replace_delete_go", "delete", name=name)
        assert (out["proceed"], out["hold"], out["reason"]) == (False, False, "not_origin_name")
    assert _row(sql, pid)[2:4] == ("hidden", "not_origin_name")
    assert state(sql, pid)[0] == "placing"


@pytest.mark.parametrize("at", ["ticket", "replace_go", "place", "delete"])
def test_a_rule_edited_to_restored_name_stops_the_replace(
    annex,  # noqa: F811
    facade_client,
    sql,
    at,
) -> None:
    """S1: every replace question reads the running rule again."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    if at == "ticket":
        _rule_says(sql, annex_id, "restored_name")
        [k] = tickets(w, annex_id)["tickets"]
        assert k["replace"] is None and "line_1042.segy" not in k["names"]
        assert _row(sql, pid)[2:4] == ("refused", "rule_changed")
        return
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    if at != "replace_go":
        assert _ask(w, pid, "replace_go")["proceed"]
        _hidden(w, pid)
        _past_grace(sql, pid)
    if at == "delete":
        assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    _rule_says(sql, annex_id, "restored_name")
    out = (
        _ask(w, pid, "replace_go") if at == "replace_go" else _ask(w, pid, "replace_delete_go", at)
    )
    assert (out["proceed"], out["hold"], out["reason"]) == (False, False, "rule_changed")
    want = "refused" if at == "replace_go" else "hidden"
    assert _row(sql, pid)[2:4] == (want, "rule_changed")
    assert state(sql, pid)[0] == "placing", "the placement goes on, the old file kept"


def test_an_overwrite_rule_needs_an_archive_admin() -> None:
    """S2: a replace deletes a NAS file, so its rule set is approved as one that removes."""
    copy_ = R.normalize(R.copy_template(1), 1)
    over = R.copy_template(1)
    over["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                                   {"on_conflict": "overwrite_keep_version"}]}]  # fmt: skip
    over = R.normalize(over, 1)
    assert not R.needs_archive_admin(copy_, copy_)
    assert R.replaces_on_nas(over) == ["back"]
    assert R.needs_archive_admin(over, over), "also when the running set has it already"


def test_replace_facts_are_taken_only_in_their_order(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """S3: the hide only after a replace_go yes, the delete only after a delete-phase
    yes; a replay is a no-op, anything else is refused and changes nothing."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    out = _hidden(w, pid)
    assert out["reason"] == "not_in_order" and _row(sql, pid)[2] == "offered"
    assert _ask(w, pid, "replace_go")["proceed"]
    assert _hidden(w, pid)["reason"] is None and _row(sql, pid)[2] == "hidden"
    assert _hidden(w, pid)["reason"] is None, "a replay"
    _past_grace(sql, pid)
    out = step(w, pid, {"step": "replaced"})
    assert out["reason"] == "not_in_order" and _row(sql, pid)[2] == "hidden"
    assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    assert step(w, pid, {"step": "replaced"})["reason"] == "not_in_order", "no delete yes"
    assert _ask(w, pid, "replace_delete_go", "delete")["proceed"]
    assert step(w, pid, {"step": "replaced"})["reason"] is None
    assert _row(sql, pid)[2] == "replaced"
    aside = "line_1042-replaced-2026-11-11-120000Z.segy"
    out = step(w, pid, {"step": "replace_aside", "name": aside, "reason": "x"})
    assert out["reason"] == "not_in_order" and _row(sql, pid)[2] == "replaced"
    assert step(w, pid, {"step": "replace_put_back", "reason": "x"})["reason"] == "not_in_order"
    assert [d for d, _ in _audits(sql, fid, "replaced")].count("allow") == 1


def test_a_removal_waits_while_a_replace_is_open(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    """S4: the removal gate knows about a replace that may have the file hidden."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    _to_hidden(w, sql, annex_id, pid)
    assert _gate(db_url, annex_id, fid, v2, ino="88") == ("refused", "replace_open")


def test_a_failed_placement_keeps_its_hidden_file_protected_until_it_is_back(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    """S5: the protection is keyed on the replace, not on the placement's end."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    _to_hidden(w, sql, annex_id, pid)
    sql.execute("UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([INTERNS], BOB))
    out = _ask(w, pid, "replace_delete_go", "place")
    assert out["state"] == "failed"
    [res] = events(w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy",
                                  "nas_file_id": "88"}])  # fmt: skip
    assert res["status"] == "rejected" and res["code"] == "placement_in_progress"
    assert _gate(db_url, annex_id, fid, v2, ino="88")[1] == "replace_open"
    # The edge puts it back: the replace closes, and a delete is a delete again.
    assert step(w, pid, {"step": "replace_put_back", "reason": "access_denied"})["reason"] is None
    [res] = events(w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy",
                                  "nas_file_id": "88"}])  # fmt: skip
    assert res["status"] != "rejected"


def test_an_offer_that_ended_with_no_yes_protects_nothing(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """S5: an offer the edge never got a `replace_go` yes for hid nothing."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _row(sql, pid)[2] == "offered"
    _close(sql, pid)
    [res] = events(w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy",
                                  "nas_file_id": "88"}])  # fmt: skip
    assert res["status"] != "rejected"


def test_done_says_replaced_only_with_the_edges_fact(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """S8: `replaced` while the replace is still `hidden` claims nothing."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    _to_hidden(w, sql, annex_id, pid)
    assert _ask(w, pid, "replace_delete_go", "place")["proceed"]
    step(w, pid, {"step": "done", "outcome": "replaced", "name": "line_1042.segy",
                  "attrs": {"nas_file_id": "99", "size": len(DATA), "mtime_ns": 1,
                            "ctime_ns": 2}})  # fmt: skip
    assert _row(sql, pid)[:3] == ("placed", "replace_unconfirmed", "hidden")
    (head,) = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert head == v2, "no new head"


def test_a_file_gone_by_a_users_hand_closes_the_replace(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """The edge's `replace_gone`: nothing hidden, nothing deleted by it; the name was free."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    _to_hidden(w, sql, annex_id, pid)
    assert step(w, pid, {"step": "replace_gone"})["reason"] is None
    assert step(w, pid, {"step": "replace_gone"})["reason"] is None, "a replay"
    assert _row(sql, pid)[2] == "gone"


def test_a_name_too_long_to_hide_is_never_offered() -> None:
    """E5: a hidden name over 255 bytes: no replace, the file takes `-restored`."""
    import asyncio
    from datetime import UTC, datetime

    name = "x" * 240 + ".segy"
    src = {"file_filer_id": 7, "file_annex_path": "lts", "path": f"d/{name}"}
    p = {"placement_id": 1, "on_conflict": "overwrite_keep_version",
         "target": {"kind": "nas", "filer_id": "7", "folder": "lts/d", "name": name}}  # fmt: skip
    out = asyncio.run(RP.gate(None, p, src, datetime.now(UTC)))
    assert out == ("refused", "name_too_long", None)


def test_the_azure_read_for_a_replace_is_made_by_the_worker(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """Codex P2: the verifier reads an already verified copy again for a replace."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql, read_fresh=False)
    assert tickets(w, annex_id)["tickets"] == []
    assert w.verify()
    (fresh,) = sql.execute(
        "SELECT azure_read_at > now() - interval '1 hour' FROM catalog.location"
        " WHERE version_id = %s AND role = 'primary'",
        (v2,),
    ).fetchone()
    assert fresh
    [k] = tickets(w, annex_id)["tickets"]
    assert k["replace"] is not None


# --- The confirm pass of #98 ---------------------------------------------------------


def test_a_refusal_after_a_replace_go_yes_leaves_the_replace_open(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """C7: once a yes is given the edge may have hidden the file: a later refusal of
    replace_go (asked again after a crash) records why and leaves the state open."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    _rule_says(sql, annex_id, "restored_name")
    out = _ask(w, pid, "replace_go")
    assert (out["proceed"], out["reason"]) == (False, "rule_changed")
    assert _row(sql, pid)[2:4] == ("offered", "rule_changed")
    [res] = events(w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy",
                                  "nas_file_id": "88"}])  # fmt: skip
    assert res["status"] == "rejected" and res["code"] == "placement_in_progress"


def test_the_offer_again_after_a_yes_records_only_why(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    """C8: an edge that lost its journal asks for its ticket again after a yes; a
    gate that now refuses records why and keeps the replace open."""
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from omnifile_service.api import annex_placements as AP
    from omnifile_service.catalog.privileges import as_app

    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _ask(w, pid, "replace_go")["proceed"]
    sql.execute("UPDATE catalog.annex_file SET size = size + 1 WHERE file_id = %s", (fid,))

    async def again():
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "api"), autocommit=True, row_factory=psycopg.rows.dict_row
        ) as c:
            p = await (
                await c.execute(
                    "SELECT * FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
                )
            ).fetchone()
            src = await AP._source(c, p)
            ctx = SimpleNamespace(edge=SimpleNamespace(principal="edge:test"))
            return await AP._offer_replace(c, ctx, p, src, datetime.now(UTC))

    assert asyncio.run(again()) is True
    assert _row(sql, pid)[2:4] == ("offered", "changed")


def test_an_approval_waits_for_a_replace_answer_and_the_answer_sees_it(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    """C9: an approval held open against a replace question: the question waits for
    it and then reads the rule as approved, never the one replaced."""
    import asyncio
    import threading

    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    locked, release = threading.Event(), threading.Event()

    async def approval():
        async with await psycopg.AsyncConnection.connect(db_url) as c:
            async with c.transaction():
                await c.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (R.ruleset_lock_key(annex_id),),
                )
                body = (
                    await (
                        await c.execute(
                            "SELECT body FROM catalog.annex_ruleset WHERE annex_id = %s"
                            " AND state = 'active'",
                            (annex_id,),
                        )
                    ).fetchone()
                )[0]
                for r in body["rules"]:
                    if r["id"] == "back":
                        r["actions"] = [a if "on_conflict" not in a
                                        else {"on_conflict": "restored_name"}
                                        for a in r["actions"]]  # fmt: skip
                await c.execute(
                    "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s"
                    " AND state = 'active'",
                    (psycopg.types.json.Jsonb(body), annex_id),
                )
                locked.set()
                await asyncio.get_running_loop().run_in_executor(None, release.wait)

    out: list = []
    t = threading.Thread(target=lambda: asyncio.run(approval()))
    t.start()
    assert locked.wait(10)
    q = threading.Thread(target=lambda: out.append(_ask(w, pid, "replace_go")))
    q.start()
    q.join(1.5)
    assert q.is_alive(), "the question waits for the approval's commit"
    release.set()
    t.join(30)
    q.join(30)
    assert (out[0]["proceed"], out[0]["reason"]) == (False, "rule_changed")


@pytest.mark.parametrize("how", ["mismatch", "missing"])
def test_a_bad_read_for_a_replace_leaves_the_copy_unread(
    annex,  # noqa: F811
    facade_client,
    sql,
    how,
) -> None:
    """C10: the verifier's read for a replace that doesn't match, or can't be made,
    never marks the copy read: the replace keeps waiting, then takes `-restored`."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql, read_fresh=False)
    if how == "mismatch":
        sql.execute(
            "UPDATE catalog.location SET crc64nvme = '\\x0102030405060708'::bytea"
            " WHERE version_id = %s AND role = 'primary'",
            (v2,),
        )
    else:
        sql.execute(
            "UPDATE catalog.location SET object_key = object_key || '-gone'"
            " WHERE version_id = %s AND role = 'primary'",
            (v2,),
        )
    assert tickets(w, annex_id)["tickets"] == []
    w.verify()
    (read,) = sql.execute(
        "SELECT azure_read_at > now() - interval '1 hour' FROM catalog.location"
        " WHERE version_id = %s AND role = 'primary'",
        (v2,),
    ).fetchone()
    assert read is False
    assert tickets(w, annex_id)["tickets"] == [], "still waiting for a good read"


def test_replace_gone_out_of_order_changes_nothing(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """C10: replace_gone after the replace closed is refused and changes nothing."""
    w, annex_id, fid, v1, v2, pid = _world(annex, facade_client, sql)
    _to_hidden(w, sql, annex_id, pid)
    assert step(w, pid, {"step": "replace_put_back", "reason": "x"})["reason"] is None
    out = step(w, pid, {"step": "replace_gone"})
    assert out["reason"] == "not_in_order"
    assert _row(sql, pid)[2] == "put_back"
