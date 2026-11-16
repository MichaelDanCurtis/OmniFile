"""Placements on a NAS through the control API (phase A3.3): tickets, the
question before the link, and what `done` does to the annex's file."""

from __future__ import annotations

import base64
import os
from typing import Any

import psycopg
import pytest

from omnifile_service.annex import rules as R
from omnifile_service.api import annex_placements as P
from omnifile_service.catalog.read_set import Folders

from .conftest import E, hashes_of
from .test_annex import (
    DATA,
    DOMAIN,
    FULL,
    GEO,
    OWNERS,
    READ,
    TRAVERSE,
    ace,
    annex,  # noqa: F401 - the fixture
    arrived,
    copy,
    events,
    make_annex,
    nt_acl,
    sd,
)

ADD_FILE = 0x0000_0002
WRITE_DAC = 0x0004_0000

INTERNS = f"{DOMAIN}-2002"
BOB = f"{DOMAIN}-1106"  # a member of GEO, the requester


def _setup(w, annex_id: int, ds: int, filer: int, facade_client, sql, *, hold: int = 14):
    body = R.copy_template(ds)
    origin = {"kind": "nas_folder", "origin": True}
    body["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": origin}, {"on_conflict": "restored_name"},
                                   {"hold_days": hold}]}]  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )
    res = events(w, annex_id, [arrived("dataset/line_1042.segy", DATA)])
    fid = res[0]["file_id"]
    vid = copy(w, facade_client, fid, DATA)
    events(w, annex_id, [{"kind": "copied", "path": "dataset/line_1042.segy", "file_id": fid,
                          "version_id": vid, "size": len(DATA),
                          "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000005"}])  # fmt: skip
    assert w.verify(), "the verify queue reads the copy back in Azure"
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (int(fid),),
    )
    (ident,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)"
        " ON CONFLICT (sid) DO UPDATE SET group_sids = EXCLUDED.group_sids RETURNING identity_id",
        (BOB, [GEO]),
    ).fetchone()
    # The original folder lets GEO add files.
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/dataset",
                          "acl": nt_acl([ace(0, READ | TRAVERSE | ADD_FILE, GEO)])}])  # fmt: skip
    (pid,) = sql.execute(
        "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
        " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
        " hold_days, state) VALUES (%s, %s, %s, %s, 'portal', 'back', 1, %s, 'restored_name',"
        " %s, 'ready') RETURNING placement_id",
        (annex_id, int(fid), int(vid), ident,
         psycopg.types.json.Jsonb({"kind": "nas", "folder": "long-term-storage/dataset",
                                   "name": "line_1042.segy", "filer_id": str(filer),
                                   "origin": True, "display": "x"}), hold),
    ).fetchone()  # fmt: skip
    return int(fid), int(vid), pid


def _place(sql, annex_id: int, fid: int, vid: int, filer: int, *, name: str = "line_1042.segy",
           folder: str = "long-term-storage/dataset", origin: bool = True, hold: int = 14,
           state_: str = "ready") -> int:  # fmt: skip
    """Another placement of the file (by SQL: A3.1 tests the request itself)."""
    (ident,) = sql.execute(
        "SELECT identity_id FROM catalog.identity WHERE sid = %s", (BOB,)
    ).fetchone()
    target = {"kind": "nas", "folder": folder, "name": name, "filer_id": str(filer),
              "display": "x"}  # fmt: skip
    if origin:
        target["origin"] = True
    (pid,) = sql.execute(
        "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
        " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
        " hold_days, state) VALUES (%s, %s, %s, %s, 'portal', 'back', 1, %s, 'restored_name',"
        " %s, %s) RETURNING placement_id",
        (annex_id, fid, vid, ident, psycopg.types.json.Jsonb(target), hold, state_),
    ).fetchone()
    return pid


def tickets(w, annex_id: int, **body) -> dict:
    r = w.edge.post(f"{E}/annex-placement-tickets", {"annex_id": str(annex_id), **body})
    assert r.status_code == 200, r.text
    return r.json()


def step(w, pid: int, body: dict) -> dict:
    r = w.edge.post(f"{E}/annex-placements/{{placement_id}}", body, placement_id=pid)
    assert r.status_code == 200, r.text
    return r.json()


def state(sql, pid: int) -> tuple:
    return sql.execute(
        "SELECT state, fail_reason FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone()


CRC = hashes_of(DATA)["crc64nvme"]
FOLDER_OK = nt_acl([ace(0, READ | TRAVERSE | ADD_FILE, GEO)])
STAGED = nt_acl([ace(0, READ, GEO), ace(0, FULL, OWNERS)])


def test_a_ticket_then_the_question_before_the_link(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    t = tickets(w, annex_id)
    [k] = t["tickets"]
    assert k["placement_id"] == str(pid) and k["origin"] and not k["protect"]
    assert k["folder"] == "long-term-storage/dataset" and k["names"][0] == "line_1042.segy"
    assert k["names"][1] == "line_1042-restored.segy"
    assert k["acl"]["kind"] == "ntfs_sd" and base64.b64decode(k["acl"]["raw"])
    assert k["size"] == len(DATA) and len(k["crc64"]) == 16
    assert k["read"]["mode"]
    assert state(sql, pid) == ("writing", None)
    # Asked again (a restarted unit): the same placement, handed over again.
    assert [x["placement_id"] for x in tickets(w, annex_id)["tickets"]] == [str(pid)]
    step(w, pid, {"step": "filled"})
    step(w, pid, {"step": "read_back", "crc64": k["crc64"]})
    go = step(w, pid, {"step": "go", "acl": STAGED, "folder_acl": FOLDER_OK,
                       "name": "line_1042.segy", "crc64": CRC})  # fmt: skip
    assert go["proceed"], go
    assert state(sql, pid) == ("placing", None)
    step(w, pid, {"step": "linked", "name": "line_1042.segy", "ino": 77, "method": "link"})
    done = step(w, pid, {"step": "done", "outcome": "placed", "name": "line_1042.segy",
                         "attrs": {"nas_file_id": "77", "size": len(DATA),
                                   "mtime_ns": 1, "ctime_ns": 2}})  # fmt: skip
    assert done["proceed"]
    assert state(sql, pid) == ("placed", None)
    af = sql.execute(
        "SELECT nas_state, nas_file_id, hold_until IS NOT NULL, copied_version_id"
        " FROM catalog.annex_file WHERE file_id = %s",
        (fid,),
    ).fetchone()
    assert af == ("present", "77", True, vid)
    (kind,) = sql.execute(
        "SELECT kind FROM catalog.annex_event WHERE annex_id = %s AND file_id = %s"
        " ORDER BY event_id DESC LIMIT 1",
        (annex_id, fid),
    ).fetchone()
    assert kind == "returned"
    # Ended: nothing more is allowed for it.
    again = step(w, pid, {"step": "go", "acl": STAGED, "folder_acl": FOLDER_OK,
                          "name": "line_1042.segy", "crc64": CRC})  # fmt: skip
    assert not again["proceed"]
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action LIKE 'annex.placement.%%'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n >= 5


def test_the_question_before_the_link_checks_every_gate(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    # A staged ACL that would let more people read it: never linked.
    wider = nt_acl([ace(0, READ, GEO), ace(0, READ, "S-1-1-0")])
    go = step(w, pid, {"step": "go", "acl": wider, "folder_acl": FOLDER_OK,
                       "name": "line_1042.segy", "crc64": CRC})  # fmt: skip
    assert not go["proceed"] and go["reason"] == "acl_mismatch"
    assert state(sql, pid) == ("failed", "acl_mismatch")


def test_a_folder_acl_changed_since_the_ticket_refuses_the_link(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    denied = nt_acl([ace(0, READ | ADD_FILE, GEO), ace(1, ADD_FILE, GEO)])
    go = step(w, pid, {"step": "go", "acl": STAGED, "folder_acl": denied,
                       "name": "line_1042.segy", "crc64": CRC})  # fmt: skip
    assert not go["proceed"] and go["reason"] == "access_denied"


def test_paused_placements_wait_and_lost_access_fails(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute("UPDATE catalog.annex_defaults SET placements_paused = true")
    try:
        assert tickets(w, annex_id)["tickets"] == []
        assert state(sql, pid) == ("ready", None)
    finally:
        sql.execute("UPDATE catalog.annex_defaults SET placements_paused = false")
    # The requester no longer reads the file: failed, never ticketed.
    sql.execute("UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([INTERNS], BOB))
    t = tickets(w, annex_id)
    assert t["tickets"] == [] and t["failed"] == [{"placement_id": str(pid),
                                                   "reason": "access_denied"}]  # fmt: skip
    assert state(sql, pid) == ("failed", "access_denied")


def test_a_name_the_ticket_didnt_list_is_refused(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    go = step(w, pid, {"step": "go", "acl": STAGED, "folder_acl": FOLDER_OK,
                       "name": "something-else.segy", "crc64": CRC})  # fmt: skip
    assert not go["proceed"] and go["reason"] == "name_not_allowed"
    assert state(sql, pid)[0] == "failed"


def _go(w, pid: int, name: str = "line_1042.segy", acl: dict | None = None, **kw) -> dict:
    return step(w, pid, {"step": "go", "acl": acl or STAGED, "folder_acl": FOLDER_OK,
                         "name": name, "crc64": CRC, **kw})  # fmt: skip


def _attrs(ino: str = "77", size: int = len(DATA)) -> dict:
    return {"nas_file_id": ino, "size": size, "mtime_ns": 1, "ctime_ns": 2}


def _done(w, pid: int, name: str = "line_1042.segy", outcome: str = "placed", **kw) -> dict:
    return step(w, pid, {"step": "done", "outcome": outcome, "name": name,
                         "attrs": _attrs(), **kw})  # fmt: skip


def _gate(db_url, annex_id: int, fid: int, vid: int, *, ino: str = "77"):
    import asyncio
    from datetime import UTC, datetime

    from omnifile_service.annex import removals
    from omnifile_service.catalog.privileges import as_app

    a = {"annex_id": annex_id, "removal_window": None, "removal_rate_per_hour": 1000,
         "rs_body": {"rules": [{"id": "x", "trigger": "age", "conditions": {},
                                "actions": [{"remove_from_source": True}]}]}}  # fmt: skip
    cand = {"file_id": fid, "version_id": vid, "rule": "x", "size": len(DATA),
            "mtime_ns": 1, "ctime_ns": 2, "nas_file_id": ino}  # fmt: skip

    async def run():
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "api"), autocommit=True, row_factory=psycopg.rows.dict_row
        ) as c:
            return (await removals.file_gate(c, a, cand, datetime.now(UTC)))[:2]

    return asyncio.run(run())


def _back_once(w, sql, annex_id: int, fid: int, vid: int, filer: int) -> None:
    """One whole return to the origin, as the edge reports it."""
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (fid,),
    )
    pid = _place(sql, annex_id, fid, vid, filer)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _done(w, pid)["proceed"]
    assert state(sql, pid)[0] == "placed"


def test_a_returned_file_is_held_and_only_returns_trip_the_breaker(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,  # noqa: F811
) -> None:
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    assert _gate(db_url, annex_id, fid, vid) == ("not_yet", "held")
    sql.execute("UPDATE catalog.annex_file SET hold_until = NULL WHERE file_id = %s", (fid,))
    # Copies elsewhere, however many, aren't a loop: a share, or another name.
    for i in range(5):
        pl = _place(sql, annex_id, fid, vid, filer, name=f"copy{i}.segy", origin=False, hold=0)
        sql.execute(
            "UPDATE catalog.annex_placement SET state = 'placed', outcome = 'placed',"
            " final_name = %s, done_at = now() WHERE placement_id = %s",
            (f"copy{i}.segy", pl),
        )
    for i in range(5):
        pl = _place(sql, annex_id, fid, vid, filer, hold=0)
        sql.execute(
            "UPDATE catalog.annex_placement SET state = 'placed', outcome = 'placed',"
            " final_name = %s, done_at = now() WHERE placement_id = %s",
            (f"line_1042-restored-{i}.segy", pl),
        )
    assert _gate(db_url, annex_id, fid, vid)[1] != "loop_breaker"
    for _ in range(3):
        _back_once(w, sql, annex_id, fid, vid, filer)
    sql.execute("UPDATE catalog.annex_file SET hold_until = NULL WHERE file_id = %s", (fid,))
    assert _gate(db_url, annex_id, fid, vid) == ("refused", "loop_breaker")
    (pinned,) = sql.execute(
        "SELECT pinned_by_breaker FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert pinned
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.breaker.pinned'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1, "the pin is audited"


def test_a_staged_acl_that_drops_a_deny_is_refused(annex, facade_client, sql) -> None:  # noqa: F811
    """S1: read {GEO}, deny {INTERNS}. The same read set without the deny would
    let someone in both groups read the file: decision 51."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    with_deny = nt_acl([ace(1, READ, INTERNS), ace(0, READ, GEO), ace(0, FULL, OWNERS)])
    events(w, annex_id, [{"kind": "changed", "path": "dataset/line_1042.segy",
                          "nas_file_id": "1001", "size": len(DATA),
                          "mtime_ns": "1700000000000000000", "ctime_ns": "1700000000000000005",
                          "acl": with_deny}])  # fmt: skip
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    tickets(w, annex_id)
    go = _go(w, pid, acl=nt_acl([ace(0, READ, GEO), ace(0, FULL, OWNERS)]))
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")


def test_a_staged_acl_keeping_its_deny_goes_on(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    with_deny = nt_acl([ace(1, READ, INTERNS), ace(0, READ, GEO), ace(0, FULL, OWNERS)])
    events(w, annex_id, [{"kind": "changed", "path": "dataset/line_1042.segy",
                          "nas_file_id": "1001", "size": len(DATA),
                          "mtime_ns": "1700000000000000000", "ctime_ns": "1700000000000000005",
                          "acl": with_deny}])  # fmt: skip
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    tickets(w, annex_id)
    assert _go(w, pid, acl=with_deny)["proceed"]


def test_the_placement_accounts_group_is_never_taken_for_the_files(annex, facade_client, sql):  # noqa: F811
    """S8: a read for the descriptor's group counts as that group. It must not
    depend on P5 setting the file's own group."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    domain_users = f"{DOMAIN}-513"
    staged = {
        "kind": "ntfs_sd",
        "raw": base64.b64encode(
            sd(BOB, domain_users, [ace(0, READ, GEO), ace(0, READ, domain_users)])
        ).decode(),
    }
    sql.execute("UPDATE catalog.filer SET placement_sid = %s WHERE filer_id = %s", (BOB, filer))
    go = _go(w, pid, acl=staged)
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")


def test_an_unmapped_allow_in_the_staged_acl_is_a_mismatch(annex, facade_client, sql) -> None:  # noqa: F811
    """S9: harmless in a catalog read set, the wrong way round here."""
    w, annex_id, ds, filer = annex
    sql.execute("UPDATE catalog.filer SET protocol = 'nfs4' WHERE filer_id = %s", (filer,))
    fid, vid, pid = _setup_nfs4(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    staged = {"kind": "nfs4", "raw": base64.b64encode(b"x").decode(), "owner_sid": GEO,
              "group_sid": GEO, "aces": [
                  {"type": "allow", "sid": GEO, "mask": READ, "flags": 0},
                  {"type": "allow", "sid": None, "mask": READ, "flags": 0}]}  # fmt: skip
    go = step(w, pid, {"step": "go", "acl": staged, "name": "line_1042.segy",
                       "folder_acl": {"kind": "nfs4", "raw": staged["raw"], "owner_sid": GEO,
                                      "group_sid": GEO, "aces": [
                           {"type": "allow", "sid": GEO, "mask": READ | ADD_FILE, "flags": 0}]}})  # fmt: skip  # noqa: E501
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")
    # WRITE_DAC alone is read one step later, for whoever that is.
    staged["aces"][1]["mask"] = 0x0004_0000
    go = step(w, pid, {"step": "go", "acl": staged, "name": "line_1042.segy",
                       "folder_acl": {"kind": "nfs4", "raw": staged["raw"], "owner_sid": GEO,
                                      "group_sid": GEO, "aces": [
                           {"type": "allow", "sid": GEO, "mask": READ | ADD_FILE, "flags": 0}]}})  # fmt: skip  # noqa: E501
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")


def _setup_nfs4(w, annex_id, ds, filer, facade_client, sql):
    body = R.copy_template(ds)
    origin = {"kind": "nas_folder", "origin": True}
    body["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": origin}, {"on_conflict": "restored_name"},
                                   {"hold_days": 14}]}]  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )
    acl = {"kind": "nfs4", "raw": base64.b64encode(b"x").decode(), "owner_sid": GEO,
           "group_sid": GEO, "aces": [{"type": "allow", "sid": GEO, "mask": READ, "flags": 0}]}  # fmt: skip  # noqa: E501
    res = events(w, annex_id, [arrived("dataset/line_1042.segy", DATA, acl=acl)])
    fid = res[0]["file_id"]
    vid = copy(w, facade_client, fid, DATA)
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (int(fid),),
    )
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)"
        " ON CONFLICT (sid) DO UPDATE SET group_sids = EXCLUDED.group_sids",
        (BOB, [GEO]),
    )
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/dataset",
                          "acl": {**acl, "aces": [{"type": "allow", "sid": GEO,
                                                   "mask": READ | TRAVERSE | ADD_FILE, "flags": 0}]}}])  # fmt: skip  # noqa: E501
    pid = _place(sql, annex_id, int(fid), int(vid), filer)
    return int(fid), int(vid), pid


def test_the_order_is_enforced(annex, facade_client, sql) -> None:  # noqa: F811
    """S4: a report out of order changes nothing."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    # No ticket yet: neither `go` nor `done` does anything.
    go = _go(w, pid)
    assert (go["proceed"], go["reason"]) == (False, "not_in_order")
    done = _done(w, pid)
    assert (done["proceed"], done["reason"]) == (False, "not_in_order")
    assert state(sql, pid) == ("ready", None)
    (nas,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert nas == "removed"
    # A ticket, and no `go`: still no `done`.
    [k] = tickets(w, annex_id)["tickets"]
    assert not _done(w, pid)["proceed"]
    assert state(sql, pid) == ("writing", None)
    # A report that changes nothing isn't audited as if it had.
    step(w, pid, {"step": "filled"})
    again = step(w, pid, {"step": "filled"})
    assert (again["proceed"], again["reason"]) == (False, "not_in_order")
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.filled'"
        " AND decision = 'allow' AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    # A read-back whose CRC isn't the version's fails the placement.
    assert k["crc64"] != "deadbeefdeadbeef"
    rb = step(w, pid, {"step": "read_back", "crc64": "deadbeefdeadbeef"})
    assert (rb["proceed"], rb["reason"]) == (False, "crc_mismatch")
    assert state(sql, pid) == ("failed", "crc_mismatch")


def test_asking_again_after_a_restart_is_every_gate_again(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    # Restarted at `linking`: `go` again, and access lost since is caught.
    assert _go(w, pid)["proceed"]
    sql.execute("UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([INTERNS], BOB))
    go = _go(w, pid)
    assert (go["proceed"], go["reason"]) == (False, "access_denied")


def test_a_stuck_placement_goes_on_from_its_real_step(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    step(w, pid, {"step": "stuck", "reason": "placeholder_changed"})
    step(w, pid, {"step": "stuck", "reason": "placeholder_changed"})  # hourly, recorded once
    assert state(sql, pid)[0] == "stuck"
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'problem' AND file_id = %s"
        " AND details->>'reason' = 'placement_stuck'",
        (fid,),
    ).fetchone()
    assert n == 1
    # It cleared: `done` from its real step, `placing`.
    assert _done(w, pid)["proceed"]
    assert state(sql, pid)[0] == "placed"


def test_done_claims_the_copy_only_when_the_file_is_that_copy(annex, facade_client, sql) -> None:  # noqa: F811
    """S3: a user changed the file before P5's attributes were journalled. The
    size isn't the version's: nothing is claimed, and the next scan copies it."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    step(w, pid, {"step": "done", "outcome": "placed", "name": "line_1042.segy",
                  "attrs": _attrs(size=len(DATA) + 9)})  # fmt: skip
    assert state(sql, pid)[0] == "placed"
    row = sql.execute(
        "SELECT nas_state, copied_size FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert row[0] == "removed", "not present as the verified copy"
    assert row[1] == len(DATA), "what it was copied as, never what the edge saw"
    res = events(w, annex_id, [arrived("dataset/line_1042.segy", DATA + b"x" * 9, ino="77")])
    assert res[0]["status"] == "applied" and res[0]["copy"] is True


def test_a_file_returned_from_gone_comes_back(annex, facade_client, sql, db_url) -> None:  # noqa: F811
    """S5: its tombstone goes, so the removal gate and later scans see it."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'gone', gone_since = now() WHERE file_id = %s",
        (fid,),
    )
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    (deleted,) = sql.execute(
        "SELECT deleted_at FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert deleted is None
    sql.execute("UPDATE catalog.annex_file SET hold_until = NULL WHERE file_id = %s", (fid,))
    assert _gate(db_url, annex_id, fid, vid)[1] != "gone"
    res = events(w, annex_id, [arrived("dataset/line_1042.segy", DATA, ino="77",
                                       ctime_ns="2", mtime_ns="1")])  # fmt: skip
    assert res[0]["copy"] is False


def test_a_gone_file_whose_name_was_taken_stays_gone(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'gone', gone_since = now() WHERE file_id = %s",
        (fid,),
    )
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    (other,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, size, state, origin)"
        " SELECT dataset_id, parent_id, name, 'file', 1, 'remote', 'annex' FROM catalog.file"
        " WHERE file_id = %s RETURNING file_id",
        (fid,),
    ).fetchone()
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    assert state(sql, pid)[0] == "placed"
    row = sql.execute(
        "SELECT f.deleted_at IS NOT NULL, af.nas_state FROM catalog.file f"
        " JOIN catalog.annex_file af USING (file_id) WHERE f.file_id = %s",
        (fid,),
    ).fetchone()
    assert row == (True, "gone")
    assert other != fid


def test_an_older_version_at_its_origin_never_takes_the_files_name(annex, facade_client, sql):  # noqa: F811
    """S6: the head is another version. Under the file's name the NAS and the
    catalog would disagree, and removals would be refused for good."""
    w, annex_id, ds, filer = annex
    fid, v1, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    v2 = copy(w, facade_client, str(fid), DATA + b"2", base=str(v1))
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    before = sql.execute(
        "SELECT nas_state, copied_version_id FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    [k] = tickets(w, annex_id)["tickets"]
    assert k["name"] == "line_1042.segy"
    assert "line_1042.segy" not in k["names"]
    assert k["names"][0] == "line_1042-restored.segy"
    go = _go(w, pid, name="line_1042.segy")
    assert (go["proceed"], go["reason"]) == (False, "name_not_allowed")
    p2 = _place(sql, annex_id, fid, v1, filer)
    tickets(w, annex_id)
    assert _go(w, p2, name="line_1042-restored.segy")["proceed"]
    _done(w, p2, name="line_1042-restored.segy", outcome="restored_name")
    row = sql.execute(
        "SELECT nas_state, copied_version_id FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert row == before, "the original is as it was"
    assert v2 != v1


def test_a_copy_under_another_name_is_held_and_not_copied_again(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,  # noqa: F811
) -> None:
    """S2: it is a new annexed file, held, referencing the version's blob."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid, name="line_1042-restored.segy")["proceed"]
    _done(w, pid, name="line_1042-restored.segy", outcome="restored_name")
    row = sql.execute(
        "SELECT af.file_id, af.hold_until IS NOT NULL, af.copied_version_id, af.nas_file_id,"
        " l.verified, l.object_key = o.object_key"
        "  FROM catalog.annex_file af"
        "  JOIN catalog.location l ON l.version_id = af.copied_version_id AND l.role = 'primary'"
        "  JOIN catalog.location o ON o.version_id = %s AND o.role = 'primary'"
        " WHERE af.annex_id = %s AND af.path = 'dataset/line_1042-restored.segy'",
        (vid, annex_id),
    ).fetchone()
    assert row is not None
    new, held, cv, ino, verified, same_blob = row
    assert new != fid and held and cv != vid and ino == "77" and verified and same_blob
    # The next scan finds it copied.
    res = events(w, annex_id, [{**arrived("dataset/line_1042-restored.segy", DATA, ino="77"),
                                "mtime_ns": "1", "ctime_ns": "2"}])  # fmt: skip
    assert res[0]["status"] == "applied" and res[0]["copy"] is False
    # And it doesn't leave during its hold.
    assert _gate(db_url, annex_id, new, cv) == ("not_yet", "held")
    # The original stays as it was.
    (nas,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert nas == "removed"


def test_a_copy_not_the_versions_bytes_is_held_and_copied(annex, facade_client, sql, db_url):  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid, name="line_1042-restored.segy")["proceed"]
    step(w, pid, {"step": "done", "outcome": "restored_name", "name": "line_1042-restored.segy",
                  "attrs": _attrs(size=len(DATA) + 1)})  # fmt: skip
    row = sql.execute(
        "SELECT hold_until IS NOT NULL, copied_version_id FROM catalog.annex_file"
        " WHERE annex_id = %s AND path = 'dataset/line_1042-restored.segy'",
        (annex_id,),
    ).fetchone()
    assert row == (True, None)
    res = events(w, annex_id, [{**arrived("dataset/line_1042-restored.segy", DATA + b"x", ino="77"),
                                "mtime_ns": "1", "ctime_ns": "2"}])  # fmt: skip
    assert res[0]["copy"] is True


def test_a_scan_before_done_waits_for_it(annex, facade_client, sql) -> None:  # noqa: F811
    """S11: after an outage a scan can come first. It would recopy the file, and
    a late `done` would move the copied version back."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    ev = {**arrived("dataset/line_1042.segy", DATA, ino="77"), "mtime_ns": "1", "ctime_ns": "2"}
    res = events(w, annex_id, [ev])
    assert (res[0]["status"], res[0]["code"]) == ("rejected", "placement_in_progress")
    res = events(w, annex_id, [{**ev, "path": "dataset/line_1042-restored.segy"}])
    assert res[0]["code"] == "placement_in_progress"
    # Another file in the folder isn't held up.
    assert (
        events(w, annex_id, [arrived("dataset/other.segy", DATA, ino="78")])[0]["status"]
        == "applied"
    )
    _done(w, pid)
    res = events(w, annex_id, [ev])
    assert res[0]["status"] == "applied" and res[0]["copy"] is False


def test_a_new_credential_is_minted_only_when_asked_and_audited(annex, facade_client, sql) -> None:  # noqa: F811
    """S10: every ask used to mint one, unaudited."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    assert len(tickets(w, annex_id)["tickets"]) == 1
    assert tickets(w, annex_id, in_hand=[str(pid)])["tickets"] == []
    [k] = tickets(w, annex_id, in_hand=[str(pid)], renew=[str(pid)])["tickets"]
    assert k["read"]["mode"]
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.ticket_renewed'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    # A placement in hand that no longer passes its gates still fails.
    sql.execute("UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([INTERNS], BOB))
    t = tickets(w, annex_id, in_hand=[str(pid)])
    assert t["failed"] == [{"placement_id": str(pid), "reason": "access_denied"}]


def test_on_another_filer_no_numeric_ids_and_another_edges_annex_is_no_trouble(
    annex,  # noqa: F811
    facade_client,
    sql,
    enrol_edge,
) -> None:
    """S9: on another NAS the numeric ids may be someone else's. And the file's
    own annex may be on another edge: that used to turn the whole ask into a 404."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute("UPDATE catalog.file SET uid = 1234, gid = 5678 WHERE file_id = %s", (fid,))
    [k] = tickets(w, annex_id)["tickets"]
    assert (k["uid"], k["gid"]) == (1234, 5678), "on its own filer, the NAS's own ids"
    # A rule that places it on another filer of this edge, which lets GEO add files.
    a2, _, filer2 = make_annex(sql, w)
    body = R.copy_template(ds)
    loc = {"kind": "nas_folder", "filer_id": str(filer2), "path": "elsewhere"}
    body["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": loc}, {"on_conflict": "restored_name"}]}]  # fmt: skip  # noqa: E501
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )
    sql.execute("UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x',"
                " done_at = now() WHERE placement_id = %s", (pid,))  # fmt: skip
    (acl,) = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first, write_sids, write_deny_sids) VALUES (%s, %s, 488, '[]', '{}', '{}', true,"
        " %s, '{}') RETURNING acl_id",
        (ds, os.urandom(32), [GEO]),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.nas_folder_acl (filer_id, path, acl_id, annex_id, observed_at)"
        " VALUES (%s, 'elsewhere/dataset', %s, %s, now())",
        (filer2, acl, a2),
    )
    p2 = _place(sql, annex_id, fid, vid, filer2, folder="elsewhere/dataset", origin=False)
    [k] = tickets(w, a2)["tickets"]
    assert k["placement_id"] == str(p2)
    assert (k["uid"], k["gid"]) == (None, None), "never another NAS's numbers"
    # The file's own annex moves to another edge: the ask still works for this one.
    other = enrol_edge()
    sql.execute("UPDATE catalog.annex SET edge_id = %s WHERE annex_id = %s",
                (other.edge_id, annex_id))  # fmt: skip
    r = w.edge.post(f"{E}/annex-placement-tickets", {"annex_id": str(a2), "renew": [str(p2)],
                                                     "in_hand": [str(p2)]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert [x["placement_id"] for x in r.json()["tickets"]] == [str(p2)]
    assert _go(w, p2)["proceed"]


def test_a_changed_rule_a_file_back_and_a_descriptor_it_cant_take(annex, facade_client, sql):  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    # The file came back by itself: its origin is no longer free.
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'present' WHERE file_id = %s", (fid,))
    t = tickets(w, annex_id)
    assert t["failed"] == [{"placement_id": str(pid), "reason": "file_present"}]
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    # A rule change that no longer allows the target.
    p2 = _place(sql, annex_id, fid, vid, filer)
    tickets(w, annex_id)
    body = R.normalize(R.copy_template(ds), ds)
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(body), annex_id),
    )
    go = _go(w, p2)
    assert (go["proceed"], go["reason"]) == (False, "target_not_allowed")


def test_a_posix_acl_with_named_entries_cant_be_put_back(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.acl SET raw_kind = 'posix', raw = NULL, entries ="
        ' \'[{"tag": "user", "perms": "r--", "sid": "S-1-1-0"}]\'::jsonb'
        " WHERE acl_id = (SELECT acl_id FROM catalog.file WHERE file_id = %s)",
        (fid,),
    )
    t = tickets(w, annex_id)
    assert t["failed"] == [{"placement_id": str(pid), "reason": "acl_unsupported"}]


def test_done_closes_the_recall_that_brought_the_bytes_back(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    (rid,) = sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, state) VALUES (%s, %s, 'downloading')"
        " RETURNING recall_id",
        (fid, vid),
    ).fetchone()
    sql.execute("UPDATE catalog.annex_placement SET recall_id = %s WHERE placement_id = %s",
                (rid, pid))  # fmt: skip
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    (st,) = sql.execute("SELECT state FROM catalog.recall WHERE recall_id = %s", (rid,)).fetchone()
    assert st == "available"


def test_archived_bytes_wait_for_their_restore(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.location SET logical_tier = 'archive' WHERE version_id = %s", (vid,)
    )
    t = tickets(w, annex_id)
    assert t == {"tickets": [], "failed": []}
    assert state(sql, pid) == ("ready", None)


def test_a_pause_on_the_targets_filer_holds_it(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    a2, _, filer2 = make_annex(sql, w)
    sql.execute("UPDATE catalog.annex SET filer_id = %s WHERE annex_id = %s", (filer, a2))
    sql.execute("UPDATE catalog.annex SET placements_paused = true WHERE annex_id = %s", (a2,))
    assert tickets(w, annex_id)["tickets"] == []
    assert state(sql, pid) == ("ready", None)
    assert filer2


# --- PR #70's second review


def _left(w, annex_id: int, ino: str = "1001") -> dict:
    return events(w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy",
                                 "nas_file_id": ino}])[0]  # fmt: skip


def test_a_file_back_from_gone_and_deleted_again_doesnt_wedge_the_annex(annex, facade_client, sql):  # noqa: F811
    """N1: a real `left`, back, then `left` again. The second tombstone's fixed op
    id made the whole batch a 500, and the annex's event queue stalled for good."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'present' WHERE file_id = %s", (fid,))
    assert _left(w, annex_id)["status"] == "applied"
    (st,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert st == "gone"
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    row = sql.execute(
        "SELECT af.nas_state, f.deleted_at FROM catalog.annex_file af"
        " JOIN catalog.file f USING (file_id) WHERE af.file_id = %s",
        (fid,),
    ).fetchone()
    assert row == ("present", None)
    # N2: the head says it's back, so an as-of listing has it.
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.folder_as_of("
        "(SELECT parent_id FROM catalog.file WHERE file_id = %s), now()) WHERE file_id = %s",
        (fid, fid),
    ).fetchone()
    assert n == 1
    # Deleted again on the NAS.
    assert _left(w, annex_id, ino="77")["status"] == "applied"
    (st,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert st == "gone"
    # And other events go on.
    assert (
        events(w, annex_id, [arrived("dataset/other.segy", DATA, ino="78")])[0]["status"]
        == "applied"
    )


def test_done_lands_when_the_head_moved_after_go(annex, facade_client, sql) -> None:  # noqa: F811
    """N3: the edge reports `done` until it is acknowledged. A 400 meant for ever."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    v2 = copy(w, facade_client, str(fid), DATA + b"2", base=str(vid))
    assert v2 != vid
    done = _done(w, pid)
    assert done["proceed"]
    assert state(sql, pid)[0] == "placed"
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'placed'"
        " ORDER BY event_id DESC LIMIT 1",
        (annex_id,),
    ).fetchone()
    assert details["name_not_allowed_now"] is True
    (st,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert st == "removed", "nothing claimed for a name the ticket no longer covers"


def test_done_lands_when_the_catalog_has_something_else_there(annex, facade_client, sql) -> None:  # noqa: F811
    """N3: a catalog file where the target's folder now is: an uncaught Rejected."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    events(w, annex_id, [arrived("restored", DATA, ino="90")])
    p2 = _place(sql, annex_id, fid, vid, filer, folder="long-term-storage/restored", origin=False)
    sql.execute("UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x',"
                " done_at = now() WHERE placement_id = %s", (pid,))  # fmt: skip
    sql.execute("UPDATE catalog.annex_placement SET state = 'placing' WHERE placement_id = %s",
                (p2,))  # fmt: skip
    done = _done(w, p2)
    assert done["proceed"]
    assert state(sql, p2)[0] == "placed"
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'placed'"
        " ORDER BY event_id DESC LIMIT 1",
        (annex_id,),
    ).fetchone()
    assert details["not_annexed"]


def test_write_dac_for_a_non_reader_is_read_one_step_later(annex, facade_client, sql) -> None:  # noqa: F811
    """N5: the read set alone doesn't prove it."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    for bits in (WRITE_DAC, 0x0008_0000, 0x1000_0000):
        staged = nt_acl([ace(0, READ, GEO), ace(0, bits, INTERNS)])
        p2 = _place(sql, annex_id, fid, vid, filer, name=f"x{bits}.segy", origin=False)
        sql.execute(
            "UPDATE catalog.annex_placement SET state = 'writing', target = target || '{}'"
            " WHERE placement_id = %s",
            (p2,),
        )
        src_ok = _go(w, pid, acl=staged)
        assert (src_ok["proceed"], src_ok["reason"]) == (False, "acl_mismatch"), hex(bits)
        pid = _place(sql, annex_id, fid, vid, filer, state_="writing")
        sql.execute(
            "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x',"
            " done_at = now() WHERE placement_id = %s",
            (p2,),
        )


def test_write_dac_the_catalog_already_gave_stays(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    same = nt_acl([ace(0, READ, GEO), ace(0, FULL, OWNERS), ace(0, WRITE_DAC, INTERNS)])
    events(w, annex_id, [{"kind": "changed", "path": "dataset/line_1042.segy",
                          "nas_file_id": "1001", "size": len(DATA),
                          "mtime_ns": "1700000000000000000", "ctime_ns": "1700000000000000005",
                          "acl": same}])  # fmt: skip
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    tickets(w, annex_id)
    assert _go(w, pid, acl=same)["proceed"]


def test_go_needs_the_read_backs_crc(annex, facade_client, sql) -> None:  # noqa: F811
    """N6: a failed report mustn't let an unread file through, or `done` land
    with no CRC."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    no_crc = step(w, pid, {"step": "go", "acl": STAGED, "folder_acl": FOLDER_OK,
                           "name": "line_1042.segy"})  # fmt: skip
    assert (no_crc["proceed"], no_crc["reason"]) == (False, "crc_mismatch")
    p2 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    bad = _go(w, p2, crc64="deadbeefdeadbeef")
    assert (bad["proceed"], bad["reason"]) == (False, "crc_mismatch")
    p3 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    assert _go(w, p3)["proceed"]
    _done(w, p3)
    (crc,) = sql.execute(
        "SELECT crc64 FROM catalog.annex_placement WHERE placement_id = %s", (p3,)
    ).fetchone()
    assert crc == CRC


def test_only_the_registered_placement_account_counts_as_the_files_owner(annex, facade_client, sql):  # noqa: F811
    """S8: the swap never checked it. An owner that is someone else, with a read
    for itself, passed."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    placer = f"{DOMAIN}-1112"
    sql.execute(
        "UPDATE catalog.acl SET read_sids = read_sids || owner_sid WHERE acl_id ="
        " (SELECT acl_id FROM catalog.file WHERE file_id = %s) AND owner_sid IS NOT NULL",
        (fid,),
    )
    staged = {"kind": "ntfs_sd", "raw": base64.b64encode(sd(placer, GEO, [
        ace(0, READ, GEO), ace(0, FULL, placer)])).decode()}  # fmt: skip
    # Not registered: nothing says this owner is the placement account.
    go = _go(w, pid, acl=staged)
    assert (go["proceed"], go["reason"]) == (False, "placement_account_not_registered")
    # And the refusal says what to do about it.
    (detail,) = sql.execute(
        "SELECT fail_detail FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone()
    assert f"PATCH /v1/filers/{filer}" in detail and "placement_sid" in detail
    sql.execute("UPDATE catalog.filer SET placement_sid = %s WHERE filer_id = %s", (placer, filer))
    p2 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    assert _go(w, p2, acl=staged)["proceed"]
    # Registered, and the owner is someone else: taken as it is.
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x', done_at = now()"
        " WHERE placement_id = %s",
        (p2,),
    )
    p3 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    other = {"kind": "ntfs_sd", "raw": base64.b64encode(sd(INTERNS, GEO, [
        ace(0, READ, GEO), ace(0, READ, INTERNS)])).decode()}  # fmt: skip
    go = _go(w, p3, acl=other)
    assert (go["proceed"], go["reason"]) == (False, "placement_account_not_registered")


def _setup_posix(w, annex_id, ds, filer, facade_client, sql, *, mode: int = 0o640):
    sql.execute("UPDATE catalog.filer SET protocol = 'nfs3' WHERE filer_id = %s", (filer,))
    body = R.copy_template(ds)
    origin = {"kind": "nas_folder", "origin": True}
    body["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": origin}, {"on_conflict": "restored_name"},
                                   {"hold_days": 14}]}]  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )
    acl = {"kind": "posix", "mode": mode, "owner_sid": BOB, "group_sid": GEO, "entries": []}
    fid = events(w, annex_id, [arrived("dataset/line_1042.segy", DATA, acl=acl)])[0]["file_id"]
    vid = copy(w, facade_client, fid, DATA)
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (int(fid),),
    )
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)"
        " ON CONFLICT (sid) DO UPDATE SET group_sids = EXCLUDED.group_sids",
        (BOB, [GEO]),
    )
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/dataset",
                          "acl": {**acl, "mode": 0o775}}])  # fmt: skip
    return int(fid), int(vid), _place(sql, annex_id, int(fid), int(vid), filer)


def _posix_go(w, pid: int, acl: dict, **kw) -> dict:
    folder = {"kind": "posix", "mode": 0o775, "owner_sid": BOB, "group_sid": GEO, "entries": []}
    return step(w, pid, {"step": "go", "acl": acl, "folder_acl": folder,
                         "name": "line_1042.segy", "crc64": CRC, **kw})  # fmt: skip


def test_a_posix_staged_group_is_taken_as_it_is(annex, facade_client, sql) -> None:  # noqa: F811
    """S8: the catalog's group was used for the staged file's, whatever its real
    group, so Domain Users with 0640 passed."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    domain_users = f"{DOMAIN}-513"
    staged = {"kind": "posix", "mode": 0o640, "owner_sid": BOB, "group_sid": domain_users,
              "entries": []}  # fmt: skip
    go = _posix_go(w, pid, staged)
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")
    # The file's own group: on.
    p2 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    assert _posix_go(w, p2, {**staged, "group_sid": GEO})["proceed"]
    # Another group with no read bits doesn't matter.
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x', done_at = now()"
        " WHERE placement_id = %s",
        (p2,),
    )
    p3 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    assert _posix_go(w, p3, {**staged, "mode": 0o600})["proceed"]


def test_a_posix_group_nobody_can_name_is_a_mismatch(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    go = _posix_go(w, pid, {"kind": "posix", "mode": 0o640, "owner_sid": BOB, "entries": []})
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")


def test_a_placement_stuck_before_its_go_can_renew_its_credential(annex, facade_client, sql):  # noqa: F811
    """F4: it stays stuck until `filled`, and the first retry comes an hour on."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    step(w, pid, {"step": "stuck", "reason": "staging_changed"})
    assert state(sql, pid)[0] == "stuck"
    assert tickets(w, annex_id, in_hand=[str(pid)])["tickets"] == []
    [k] = tickets(w, annex_id, in_hand=[str(pid)], renew=[str(pid)])["tickets"]
    assert k["read"]["mode"]
    assert state(sql, pid)[0] == "stuck", "the ask alone changes nothing"
    # After its `go` there is nothing to read.
    p2 = _place(sql, annex_id, fid, vid, filer, name="b.segy", state_="writing")
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'stuck', before_stuck = 'placing'"
        " WHERE placement_id = %s",
        (p2,),
    )
    assert tickets(w, annex_id, in_hand=[str(pid), str(p2)], renew=[str(p2)])["tickets"] == []


def test_a_scan_waits_for_a_placement_stuck_after_its_go(annex, facade_client, sql) -> None:  # noqa: F811
    """S11's other branch: stuck, with `placing` before it."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    step(w, pid, {"step": "stuck", "reason": "placeholder_changed"})
    ev = {**arrived("dataset/line_1042.segy", DATA, ino="77"), "mtime_ns": "1", "ctime_ns": "2"}
    res = events(w, annex_id, [ev])
    assert res[0]["code"] == "placement_in_progress"
    # Stuck before its `go`, the file can't be its doing.
    p2 = _place(sql, annex_id, fid, vid, filer, name="b.segy", state_="writing")
    step(w, p2, {"step": "stuck", "reason": "staging_changed"})
    res = events(w, annex_id, [arrived("dataset/b.segy", DATA, ino="79")])
    assert res[0]["status"] == "applied"


def test_clearing_the_breaker_resets_its_count(annex, facade_client, sql, db_url) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    for _ in range(3):
        _back_once(w, sql, annex_id, fid, vid, filer)
    sql.execute("UPDATE catalog.annex_file SET hold_until = NULL WHERE file_id = %s", (fid,))
    assert _gate(db_url, annex_id, fid, vid) == ("refused", "loop_breaker")
    # An Owner looked: those returns no longer count (as the route sets it).
    sql.execute(
        "UPDATE catalog.annex_file SET pinned_by_breaker = false,"
        " breaker_cleared_at = now() + interval '1 second' WHERE file_id = %s",
        (fid,),
    )
    assert _gate(db_url, annex_id, fid, vid)[1] != "loop_breaker"


def test_a_pause_on_a_retired_annex_holds_nothing(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    a2, _, _ = make_annex(sql, w)
    sql.execute(
        "UPDATE catalog.annex SET filer_id = %s, placements_paused = true, state = 'retired'"
        " WHERE annex_id = %s",
        (filer, a2),
    )
    assert len(tickets(w, annex_id)["tickets"]) == 1


def test_a_report_after_an_abandon_is_recorded_and_changes_nothing(annex, facade_client, sql):  # noqa: F811
    # What happens if the edge was only away: nothing it says can reopen it, so
    # the worst case is a recopy. The file stays as the scan will find it.
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'abandoned',"
        " done_at = now() WHERE placement_id = %s",
        (pid,),
    )
    columns = "nas_state, hold_until"
    before = sql.execute(
        f"SELECT {columns} FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    for ans in (_done(w, pid), _go(w, pid)):
        assert ans["proceed"] is False and ans["reason"] == "abandoned"
    assert state(sql, pid)[0] == "failed"
    assert (
        sql.execute(
            f"SELECT {columns} FROM catalog.annex_file WHERE file_id = %s", (fid,)
        ).fetchone()
        == before
    )
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.late_report'"
        " AND file_id = %s AND details->>'step' = 'done'",
        (fid,),
    ).fetchone()
    assert n == 1


def test_an_unnamed_posix_group_on_a_file_anyone_may_read_proves_nothing_new(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """Root's group is common on an NFS export. On a 0644 file its members are
    readers anyway, whoever they are; on a 0640 one that can't be proved."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql, mode=0o644)
    tickets(w, annex_id)
    go = _posix_go(w, pid, {"kind": "posix", "mode": 0o644, "owner_sid": BOB, "entries": []})
    assert go["proceed"], go


def test_only_something_new_from_the_edge_counts_as_a_report(annex, facade_client, sql) -> None:  # noqa: F811
    """What abandon's 24 hours count from. The hourly `stuck` says nothing new."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)

    def last():
        return sql.execute(
            "SELECT last_report_at FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
        ).fetchone()[0]

    assert _go(w, pid)["proceed"]
    assert last() is not None
    step(w, pid, {"step": "stuck", "reason": "placeholder_changed"})
    first = last()
    assert first is not None
    sql.execute(
        "UPDATE catalog.annex_placement SET last_report_at = now() - interval '25 hours'"
        " WHERE placement_id = %s",
        (pid,),
    )
    before = last()
    step(w, pid, {"step": "stuck", "reason": "placeholder_changed"})
    assert last() == before, "the same trouble again"


# --- the third review ------------------------------------------------------------


def _catalog_nfs4(read_sids, entries, **kw) -> dict:
    # No folder above it keeps anyone out (decision 63's own tests: test_read_set.py).
    return {"read_sids": read_sids, "deny_sids": [], "deny_first": True, "owner_sid": GEO,
            "group_sid": GEO, "raw_kind": "nfs4", "entries": entries,
            "folders": Folders(annexed=False), **kw}  # fmt: skip


def _staged_nfs4(*aces) -> dict:
    return {"kind": "nfs4", "raw": base64.b64encode(b"x").decode(), "owner_sid": GEO,
            "group_sid": GEO, "aces": [{"type": "allow", "sid": s, "mask": m, "flags": f}
                                       for s, m, f in aces]}  # fmt: skip


def _allow(sid, mask=READ, flags=0) -> dict:
    return {"ace": "allow", "sid": sid, "mask": mask, "flags": flags}


WRITE_ACL, WRITE_OWNER = 0x0004_0000, 0x0008_0000
EVERYONE_SID, AUTH_USERS, BUILTIN_USERS = "S-1-1-0", "S-1-5-11", "S-1-5-32-545"


@pytest.mark.parametrize("mask", [WRITE_ACL, WRITE_OWNER, WRITE_ACL | READ, WRITE_OWNER | READ])
def test_an_unmapped_principal_with_control_is_refused_whoever_may_read(mask) -> None:
    """B1: `acl_body` drops an unmapped allow, so `control_sids` never saw it."""
    staged = _staged_nfs4((GEO, READ, 0), (None, mask, 0))
    mine = _catalog_nfs4([GEO], [_allow(GEO)])
    assert not P.within_catalog(staged, "nfs4", mine, None)
    # No exception for a file anyone may read: it can give itself more than read.
    anyone = _catalog_nfs4([GEO, EVERYONE_SID], [_allow(GEO), _allow(EVERYONE_SID)])
    assert not P.within_catalog(staged, "nfs4", anyone, None)
    # Only where it does nothing to this file (inherit-only, and no read).
    inherited = _staged_nfs4((GEO, READ, 0), (None, WRITE_ACL, 0x08))
    assert P.within_catalog(inherited, "nfs4", mine, None)


def test_an_unmapped_reader_passes_only_on_a_literal_everyone() -> None:
    """S2: the read sets call Authenticated Users and BUILTIN\\Users Everyone, and
    a NAS-local principal is neither."""
    staged = _staged_nfs4((GEO, READ, 0), (None, READ, 0))
    assert P.within_catalog(
        staged, "nfs4", _catalog_nfs4([GEO, EVERYONE_SID], [_allow(EVERYONE_SID)]), None
    )
    for like in (AUTH_USERS, BUILTIN_USERS):
        cat = _catalog_nfs4([GEO, EVERYONE_SID], [_allow(GEO), _allow(like)])
        assert not P.within_catalog(staged, "nfs4", cat, None), like
    # An inherit-only Everyone gives this file nothing.
    cat = _catalog_nfs4([GEO, EVERYONE_SID], [_allow(GEO), _allow(EVERYONE_SID, READ, 0x08)])
    assert not P.within_catalog(staged, "nfs4", cat, None)


def test_a_staged_everyone_needs_a_catalog_that_literally_has_it() -> None:
    staged = _staged_nfs4((GEO, READ, 0), (EVERYONE_SID, READ, 0))
    for like in (AUTH_USERS, BUILTIN_USERS):
        cat = _catalog_nfs4([GEO, EVERYONE_SID], [_allow(GEO), _allow(like)])
        assert not P.within_catalog(staged, "nfs4", cat, None), like
    cat = _catalog_nfs4([GEO, EVERYONE_SID], [_allow(GEO), _allow(EVERYONE_SID)])
    assert P.within_catalog(staged, "nfs4", cat, None)
    # The same word the catalog has is fine.
    same = _staged_nfs4((GEO, READ, 0), (AUTH_USERS, READ, 0))
    cat = _catalog_nfs4([GEO, EVERYONE_SID], [_allow(GEO), _allow(AUTH_USERS)])
    assert P.within_catalog(same, "nfs4", cat, None)


def _purge_asked(sql, annex_id, fid) -> None:
    sql.execute(
        "UPDATE catalog.annex_file SET gone_since = now(), purge_requested_at = now(),"
        " purge_requested_by = 'test' WHERE annex_id = %s AND file_id = %s",
        (annex_id, int(fid)),
    )


def test_a_purge_asked_for_after_go_doesnt_stop_done_and_the_file_stays_spoken_for(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """B2: `done` hit annex_file_purge_gone, a 500, retried for ever. It holds
    for one a legal hold refused too: `purge_requested_at` stays set."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _purge_asked(sql, annex_id, fid)
    assert _done(w, pid)["proceed"]
    assert state(sql, pid)[0] == "placed"
    (gone, nas) = sql.execute(
        "SELECT gone_since, nas_state FROM catalog.annex_file WHERE file_id = %s", (int(fid),)
    ).fetchone()
    assert gone is not None and nas != "present"
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE kind = 'placed' AND file_id = %s",
        (int(fid),),
    ).fetchone()
    assert details["not_the_copy"] is True
    assert "not_claimed" not in details, "known to be taken, not stumbled on"


@pytest.mark.parametrize("where", ["_returned", "_adopt"])
def test_done_lands_whatever_claiming_the_file_raises(
    annex,  # noqa: F811
    facade_client,
    sql,
    monkeypatch,
    where,
) -> None:
    """It reports a fact: a 500 here would be retried for ever, with the
    placement holding back scans of its names."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]

    async def boom(*_a, **_k):
        raise psycopg.errors.CheckViolation("a constraint nobody thought of")

    monkeypatch.setattr(P, where, boom)
    name = "line_1042.segy" if where == "_returned" else "line_1042-restored.segy"
    outcome = "placed" if where == "_returned" else "restored_name"
    assert _done(w, pid, name=name, outcome=outcome)["proceed"]
    assert state(sql, pid)[0] == "placed"
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE file_id = %s"
        " AND details->>'placement_id' = %s",
        (int(fid), str(pid)),
    ).fetchone()
    key = "not_claimed" if where == "_returned" else "not_annexed"
    assert details[key] == "CheckViolation", details


def test_a_purge_asked_for_stops_the_gates(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    _purge_asked(sql, annex_id, fid)
    go = _go(w, pid)
    assert (go["proceed"], go["reason"]) == (False, "file_purged")
    p2 = _place(sql, annex_id, int(fid), int(vid), filer)
    got = tickets(w, annex_id)
    assert [x["reason"] for x in got["failed"] if int(x["placement_id"]) == p2] == ["file_purged"]


def test_a_null_catalog_owner_with_a_registered_placer_is_a_refusal_not_a_500(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    placer = f"{DOMAIN}-1112"
    sql.execute("UPDATE catalog.filer SET placement_sid = %s WHERE filer_id = %s", (placer, filer))
    sql.execute(
        "UPDATE catalog.acl SET owner_sid = NULL WHERE acl_id ="
        " (SELECT acl_id FROM catalog.file WHERE file_id = %s)",
        (int(fid),),
    )
    staged = {"kind": "ntfs_sd", "raw": base64.b64encode(sd(placer, GEO, [
        ace(0, READ, GEO), ace(0, FULL, placer)])).decode()}  # fmt: skip
    go = _go(w, pid, acl=staged)
    assert go["proceed"] is False and go["reason"].startswith("acl_unreadable")


def test_a_malformed_body_at_go_is_a_refusal_not_a_500(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    _close(sql, pid)
    for bad in ({"kind": "ntfs_sd", "raw": "not base64!"}, {"kind": "nfs4", "aces": [{"type": 1}]},
                {"kind": "nfs4", "aces": "x"}):  # fmt: skip
        p = _place(sql, annex_id, int(fid), int(vid), filer, state_="writing")
        go = step(w, p, {"step": "go", "acl": bad, "folder_acl": FOLDER_OK,
                         "name": "line_1042.segy", "crc64": CRC})  # fmt: skip
        assert go["proceed"] is False, bad


@pytest.mark.parametrize(
    "attrs",
    [
        {"nas_file_id": "77", "size": "big"},
        {"nas_file_id": "77", "size": len(DATA), "mtime_ns": 2**70},
        {"nas_file_id": 77, "size": len(DATA)},
        {"nas_file_id": "a\x00b", "size": len(DATA)},
    ],
)
def test_done_lands_whatever_attrs_it_is_given(annex, facade_client, sql, attrs) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert step(w, pid, {"step": "done", "outcome": "placed", "name": "line_1042.segy",
                         "attrs": attrs})["proceed"]  # fmt: skip
    assert state(sql, pid)[0] == "placed"


def _hold(sql, fid) -> Any:
    return sql.execute(
        "SELECT hold_until FROM catalog.annex_file WHERE file_id = %s", (int(fid),)
    ).fetchone()[0]


def test_a_hold_is_never_shortened(annex, facade_client, sql) -> None:  # noqa: F811
    """Not by a `done` that isn't the copy, one with no hold at all, or one that
    is the copy with a shorter hold."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql, hold=0)
    sql.execute(
        "UPDATE catalog.annex_file SET hold_until = now() + interval '30 days' WHERE file_id = %s",
        (int(fid),),
    )
    far = _hold(sql, fid)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    # Not the copy: its size isn't the version's.
    assert step(w, pid, {"step": "done", "outcome": "placed", "name": "line_1042.segy",
                         "attrs": _attrs(size=1)})["proceed"]  # fmt: skip
    assert _hold(sql, fid) == far
    # The copy, held for less.
    p2 = _place(sql, annex_id, int(fid), int(vid), filer, hold=1, state_="writing")
    assert _go(w, p2)["proceed"]
    assert _done(w, p2)["proceed"]
    (kind,) = sql.execute(
        "SELECT kind FROM catalog.annex_event WHERE file_id = %s AND details->>'placement_id' = %s",
        (int(fid), str(p2)),
    ).fetchone()
    assert kind == "returned"
    assert _hold(sql, fid) == far


def test_already_there_at_its_origin_is_the_file_back_held(annex, facade_client, sql) -> None:  # noqa: F811
    """It stayed `removed`, with no hold, until a scan came."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert step(w, pid, {"step": "done", "outcome": "already_there", "name": "line_1042.segy",
                         "attrs": _attrs()})["proceed"]  # fmt: skip
    (nas, hold) = sql.execute(
        "SELECT nas_state, hold_until FROM catalog.annex_file WHERE file_id = %s", (int(fid),)
    ).fetchone()
    assert nas == "present" and hold is not None
    # With no attributes to go on too: the edge read those bytes back.
    w, annex_id, ds, filer = annex
    p2 = _place(sql, annex_id, int(fid), int(vid), filer, state_="writing")
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now(),"
        " hold_until = NULL WHERE file_id = %s",
        (int(fid),),
    )
    assert _go(w, p2)["proceed"]
    assert step(w, p2, {"step": "done", "outcome": "already_there",
                        "name": "line_1042.segy", "attrs": None})["proceed"]  # fmt: skip
    (nas,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (int(fid),)
    ).fetchone()
    assert nas == "present"


def _close(sql, pid: int) -> None:
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x', done_at = now()"
        " WHERE placement_id = %s AND state NOT IN ('placed', 'failed', 'cancelled')",
        (pid,),
    )


def _another_file(w, annex_id, filer, facade_client, sql, name: str, *, state_="ready") -> int:
    """Another removed file with its own placement, which passes every gate."""
    path = f"dataset/{name}"
    data = DATA + name.encode()  # its own bytes, or the copy is a dedupe
    fid = events(w, annex_id, [arrived(path, data, ino=f"i-{name}")])[0]["file_id"]
    vid = copy(w, facade_client, fid, data)
    events(w, annex_id, [{"kind": "copied", "path": path, "file_id": fid, "version_id": vid,
                          "size": len(data), "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000005"}])  # fmt: skip
    assert w.verify()
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (int(fid),),
    )
    return _place(sql, annex_id, int(fid), int(vid), filer, name=name, state_=state_)


def test_what_is_in_hand_or_stuck_never_starves_a_new_ticket(annex, facade_client, sql) -> None:  # noqa: F811
    """A filer's LIMIT: ten under way (an outage, say) held back every new one."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    hand = [pid] + [
        _another_file(w, annex_id, filer, facade_client, sql, f"x{i}.segy", state_="writing")
        for i in range(P.TICKETS_PER_ASK - 1)
    ]
    stuck = _another_file(w, annex_id, filer, facade_client, sql, "s.segy", state_="stuck")
    sql.execute(
        "UPDATE catalog.annex_placement SET before_stuck = 'writing' WHERE placement_id = %s",
        (stuck,),
    )
    new = _another_file(w, annex_id, filer, facade_client, sql, "new.segy")
    got = tickets(w, annex_id, in_hand=[str(x) for x in [*hand, stuck]])
    assert [int(t["placement_id"]) for t in got["tickets"]] == [new]
    # A placement in hand is still gated, when there's room in the ask.
    assert got["failed"] == []


def test_a_stuck_renewal_comes_ahead_of_new_tickets(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    for i in range(P.TICKETS_PER_ASK):
        _another_file(w, annex_id, filer, facade_client, sql, f"n{i}.segy")
    p2 = _place(sql, annex_id, fid, vid, filer, state_="stuck")
    sql.execute(
        "UPDATE catalog.annex_placement SET before_stuck = 'writing' WHERE placement_id = %s",
        (p2,),
    )
    got = tickets(w, annex_id, in_hand=[str(p2)], renew=[str(p2)])
    assert int(got["tickets"][0]["placement_id"]) == p2


def _posix_catalog(sql, fid, *, uid=20002, gid=4242) -> None:
    """A file whose owning group has no SID: its catalog ACL has no group, and
    its version records the numbers."""
    sql.execute(
        "UPDATE catalog.acl SET group_sid = NULL WHERE acl_id ="
        " (SELECT acl_id FROM catalog.file WHERE file_id = %s)",
        (int(fid),),
    )
    sql.execute(
        "UPDATE catalog.file_version v SET uid = %s, gid = %s, mode = 416, acl_id = f.acl_id"
        "  FROM catalog.file f WHERE f.file_id = v.file_id AND v.file_id = %s",
        (uid, gid, int(fid)),
    )
    sql.execute(
        "UPDATE catalog.file SET uid = %s, gid = %s WHERE file_id = %s", (uid, gid, int(fid))
    )


UNNAMED_0640 = {"kind": "posix", "mode": 0o640, "owner_sid": BOB, "entries": []}


def test_an_unnamed_group_at_0640_passes_only_as_the_capture_of_this_version(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """Same filer, origin, numbers, mode and ACL: nobody new can read it."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql)
    _posix_catalog(sql, fid)
    tickets(w, annex_id)
    _close(sql, pid)

    def go(body, **kw):
        p = _place(sql, annex_id, fid, vid, filer, state_="writing")
        a = _posix_go(w, p, body, **kw)
        _close(sql, p)
        return a

    shown = {"posix_acls_shown": True}
    assert go(UNNAMED_0640, uid=20002, gid=4242, **shown)["reason"] != "acl_mismatch"
    for why, body, kw in [
        ("no numbers", UNNAMED_0640, shown),
        ("another uid", UNNAMED_0640, {"uid": 1, "gid": 4242, **shown}),
        ("another gid", UNNAMED_0640, {"uid": 20002, "gid": 1, **shown}),
        ("another mode", {**UNNAMED_0640, "mode": 0o660}, {"uid": 20002, "gid": 4242, **shown}),
        ("a named entry", {**UNNAMED_0640, "entries": [
            {"scope": "access", "tag": "group", "perms": "r--", "sid": None}]},
         {"uid": 20002, "gid": 4242, **shown}),
    ]:  # fmt: skip
        assert go(body, **kw)["reason"] == "acl_mismatch", why
    # And a server that hides POSIX ACLs has a refusal of its own.
    assert go(UNNAMED_0640, uid=20002, gid=4242)["reason"] == "acl_hidden"


def test_an_unnamed_group_isnt_proved_by_a_version_of_another_acl(annex, facade_client, sql):  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql)
    _posix_catalog(sql, fid)
    (acl_id,) = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first) VALUES (%s, %s, 416, '[]', '{}', '{}', false) RETURNING acl_id",
        (ds, b"z" * 32),
    ).fetchone()
    sql.execute("UPDATE catalog.file_version SET acl_id = %s WHERE file_id = %s", (acl_id, fid))
    tickets(w, annex_id)
    go = _posix_go(w, pid, UNNAMED_0640, uid=20002, gid=4242, posix_acls_shown=True)
    assert go["reason"] == "acl_mismatch"


def test_same_as_capture_compares_an_nfs4_acl_as_raw_bytes() -> None:
    raw = base64.b64encode(b"xdr").decode()
    src = {"version_uid": 5, "version_gid": 6, "version_acl_id": 9, "acl_id": 9,
           "f_uid": 5, "f_gid": 6, "version_mode": 0o640, "acl_mode": 0o640,
           "raw_kind": "nfs4", "raw": b"xdr", "entries": [], "group_sid": None}  # fmt: skip
    p = {"target": {"origin": True, "filer_id": "3"}}
    v = {"kind": "nfs4", "mode": 0o640, "raw": raw}
    assert P.same_as_capture(v, src, p, 5, 6, 3)
    assert not P.same_as_capture({**v, "raw": base64.b64encode(b"other").decode()}, src, p, 5, 6, 3)
    assert not P.same_as_capture(v, {**src, "raw": None}, p, 5, 6, 3)
    assert not P.same_as_capture(v, src, p, 5, 6, 4), "another filer"
    elsewhere = {"target": {"filer_id": "3"}}
    assert not P.same_as_capture(v, src, elsewhere, 5, 6, 3), "not its origin"
    assert not P.same_as_capture(v, {**src, "version_uid": None}, p, 5, 6, 3), "no number held"
    assert not P.same_as_capture(v, {**src, "version_mode": None}, p, 5, 6, 3)
    assert P.same_as_capture({**v, "mode": 0o4640}, src, p, 5, 6, 3), "setuid comes back at P5"


def test_a_report_after_an_abandon_raises_a_problem_naming_the_file(annex, facade_client, sql):  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'abandoned',"
        " done_at = now() WHERE placement_id = %s",
        (pid,),
    )
    _done(w, pid)
    (path, details) = sql.execute(
        "SELECT path, details FROM catalog.annex_event WHERE kind = 'problem' AND file_id = %s"
        " AND details->>'reason' = 'placement_late_report'",
        (int(fid),),
    ).fetchone()
    assert path == "long-term-storage/dataset/line_1042.segy"
    assert details["step"] == "done"


def test_a_null_catalog_owner_cant_be_stood_in_for() -> None:
    placer = f"{DOMAIN}-1112"
    staged = {"kind": "ntfs_sd", "raw": base64.b64encode(sd(placer, GEO, [
        ace(0, READ, GEO), ace(0, FULL, placer)])).decode()}  # fmt: skip
    with pytest.raises(P.AclError):
        P.staged_sets(staged, "smb", None, placer)


def test_anything_the_acl_check_raises_at_go_is_a_refusal(annex, facade_client, sql, monkeypatch):  # noqa: F811
    """A 500 here is retried for ever."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)

    def boom(*_a, **_k):
        raise TypeError("nobody thought of this")

    monkeypatch.setattr(P, "within_catalog", boom)
    go = _go(w, pid)
    assert go["proceed"] is False and go["reason"].startswith("acl_unreadable")


def test_a_shorter_hold_on_a_file_already_annexed_under_that_name_doesnt_shorten_it(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql, hold=0)
    other = "dataset/line_1042-restored.segy"
    live = events(w, annex_id, [arrived(other, DATA + b"x", ino="i-other")])[0]["file_id"]
    sql.execute(
        "UPDATE catalog.annex_file SET hold_until = now() + interval '30 days' WHERE file_id = %s",
        (int(live),),
    )
    far = _hold(sql, live)
    tickets(w, annex_id)
    assert _go(w, pid, name="line_1042-restored.segy")["proceed"]
    assert _done(w, pid, name="line_1042-restored.segy", outcome="restored_name")["proceed"]
    assert _hold(sql, live) == far


# --- the placement account's report, and what `done` takes ---------------------


REPORT = {"sid": f"{DOMAIN}-1112", "kind": "user", "name": "restorer@OMNI.TEST"}


def _reported(sql, filer):
    return sql.execute(
        "SELECT placement_account, placement_account_at, placement_sid FROM catalog.filer"
        " WHERE filer_id = %s",
        (filer,),
    ).fetchone()


@pytest.mark.parametrize(
    "details",
    [{"error": "a\ud800"}, {"sid": "S-1-5-21-1-2-3-1105", "kind": "user", "name": "a\ud800"}],
)
def test_a_placement_account_report_postgres_cant_hold_isnt_a_500(annex, details) -> None:  # noqa: F811
    import json
    import uuid

    w, annex_id, ds, filer = annex
    edge = w.edge
    r = edge.client.post(
        E.replace("{edge_id}", str(edge.edge_id)) + "/annex-events",
        content=json.dumps(
            {"annex_id": str(annex_id),
             "events": [{"kind": "placement_account", "details": details}]}
        ).encode(),
        headers=edge.headers({"Content-Type": "application/json",
                              "Idempotency-Key": str(uuid.uuid4())}),
    )  # fmt: skip
    assert r.status_code == 200, r.text


def test_the_edge_reports_its_placement_account_and_that_registers_nothing(annex, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    [r] = events(w, annex_id, [{"kind": "placement_account", "details": REPORT}])
    assert r["status"] == "applied", r
    report, at, registered = _reported(sql, filer)
    assert {k: report[k] for k in REPORT} == REPORT and at is not None
    assert registered is None, "a person registers it"
    # The latest says what is now, whether that is an account or why not.
    [r] = events(w, annex_id, [{"kind": "placement_account", "details": {"error": "no winbind"}}])
    assert r["status"] == "applied", r
    assert _reported(sql, filer)[0]["error"] == "no winbind"
    assert "sid" not in _reported(sql, filer)[0]


def test_a_copy_annexs_error_doesnt_overwrite_another_annexs_good_report(annex, sql) -> None:  # noqa: F811
    """Only remove_principal set: an Archive annex places, and a Copy annex on the
    same filer can't. Its {error} says nothing about the other."""
    w, annex_id, ds, filer = annex
    [r] = events(w, annex_id, [{"kind": "placement_account", "details": REPORT}])
    assert r["status"] == "applied", r
    sql.execute(
        "UPDATE catalog.filer SET placement_account = placement_account ||"
        ' \'{"annex_id": "999999"}\' WHERE filer_id = %s',
        (filer,),
    )
    [r] = events(w, annex_id, [{"kind": "placement_account", "details": {"error": "no account"}}])
    assert r["status"] == "applied", r
    assert _reported(sql, filer)[0]["sid"] == REPORT["sid"]
    # Once that report is stale, what this one says is all there is.
    sql.execute(
        "UPDATE catalog.filer SET placement_account_at = now() - interval '7 hours'"
        " WHERE filer_id = %s",
        (filer,),
    )
    events(w, annex_id, [{"kind": "placement_account", "details": {"error": "no account"}}])
    assert "sid" not in _reported(sql, filer)[0]


@pytest.mark.parametrize(
    "details",
    [
        {"sid": "nope", "kind": "user", "name": "x"},
        {"sid": f"{DOMAIN}-1112", "name": "x"},
        {"sid": f"{DOMAIN}-1112", "kind": "user"},
        {"sid": f"{DOMAIN}-1112", "kind": "u" * 33, "name": "x"},
        {"sid": f"{DOMAIN}-1112", "kind": "user", "name": "a\x00b"},
        {"error": 7},
    ],
)
def test_a_report_of_the_wrong_shape_is_rejected_and_changes_nothing(annex, sql, details):  # noqa: F811
    w, annex_id, ds, filer = annex
    [r] = events(w, annex_id, [{"kind": "placement_account", "details": details}])
    assert r["status"] == "rejected", r
    assert _reported(sql, filer)[0] is None


def test_another_edge_cant_report_for_a_filer(annex, enrol_edge, sql) -> None:  # noqa: F811
    """By the certificate, not by anything in the body, which names no filer."""
    w, annex_id, ds, filer = annex
    other = enrol_edge()
    r = other.post(f"{E}/annex-events", {"annex_id": str(annex_id), "events": [
        {"kind": "placement_account", "details": REPORT}]})  # fmt: skip
    assert r.status_code == 404, r.text
    assert _reported(sql, filer)[0] is None


def test_an_annex_on_this_edge_cant_report_for_a_filer_on_another(annex, enrol_edge, sql) -> None:  # noqa: F811
    """No route makes such an annex. If one existed, the filer is checked too."""
    w, annex_id, ds, filer = annex
    other = enrol_edge()
    sql.execute("UPDATE catalog.filer SET edge_id = %s WHERE filer_id = %s",
                (other.edge_id, filer))  # fmt: skip
    [r] = events(w, annex_id, [{"kind": "placement_account", "details": REPORT}])
    assert (r["status"], r["code"]) == ("rejected", "not_this_edges_filer"), r
    assert _reported(sql, filer)[0] is None


@pytest.mark.parametrize(
    "body",
    [
        {"outcome": "something_newer", "name": "line_1042.segy"},
        {"name": "line_1042.segy"},
        {"outcome": "placed", "name": "a/b"},
        {"outcome": "placed", "name": ""},
        {"outcome": "placed", "name": None},
        {"outcome": "placed"},
    ],
)
def test_done_from_an_edge_of_another_version_lands_and_claims_nothing(
    annex,  # noqa: F811
    facade_client,
    sql,
    body,
) -> None:
    """It was a 400, retried for ever, with the placement holding back scans."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    ans = step(w, pid, {"step": "done", "attrs": _attrs(), **body})
    assert ans["proceed"] is True
    assert state(sql, pid)[0] == "placed"
    (nas,) = sql.execute(
        "SELECT nas_state FROM catalog.annex_file WHERE file_id = %s", (int(fid),)
    ).fetchone()
    assert nas == "removed", "nothing is claimed for the file: the scan finds it as it is"
    (kind, details) = sql.execute(
        "SELECT kind, details FROM catalog.annex_event WHERE file_id = %s"
        " AND details->>'placement_id' = %s",
        (int(fid), str(pid)),
    ).fetchone()
    assert kind == "placed"
    assert "outcome_reported" in details or "name_reported" in details, details


def test_a_capture_with_a_named_entry_isnt_proved_though_the_staged_file_has_none() -> None:
    """The catalog dropped an unmapped named entry, and kept the mask. With one,
    the mode's group bits are the mask, so 0640 could have meant an owning group
    with nothing, and the base entries would widen it."""
    src = {"version_uid": 5, "version_gid": 6, "version_acl_id": 9, "acl_id": 9,
           "f_uid": 5, "f_gid": 6, "version_mode": 0o640, "acl_mode": 0o640,
           "raw_kind": "posix", "raw": None, "group_sid": None, "entries": [
               {"scope": "access", "tag": "owner", "perms": "rw-"},
               {"scope": "access", "tag": "owning_group", "perms": "---"},
               {"scope": "access", "tag": "mask", "perms": "r--"},
               {"scope": "access", "tag": "other", "perms": "---"}]}  # fmt: skip
    p = {"target": {"origin": True, "filer_id": "3"}}
    v = {"kind": "posix", "mode": 0o640, "entries": []}
    assert not P.same_as_capture(v, src, p, 5, 6, 3, acls_shown=True)
    base = {**src, "entries": [e for e in src["entries"] if e["tag"] != "mask"]}
    assert P.same_as_capture(v, base, p, 5, 6, 3, acls_shown=True)
    # A server that hides POSIX ACLs looks just like that (`nasfs.rs`: no ACL and
    # no ACLs shown read the same), and its 0640 may be a hidden mask.
    assert not P.same_as_capture(v, base, p, 5, 6, 3)
    # The number moved on the file since this version: it says nothing now.
    assert not P.same_as_capture(v, {**base, "f_uid": 7}, p, 5, 6, 3, acls_shown=True)
    assert not P.same_as_capture(v, {**base, "f_gid": 7}, p, 5, 6, 3, acls_shown=True)
    # The same number with another mapping: the id map moved, so nothing is taken on trust.
    assert not P.same_as_capture(v, {**base, "group_sid": GEO}, p, 5, 6, 3, acls_shown=True)
    # The catalog's ACL row disagrees with the version's mode: not the capture.
    assert not P.same_as_capture(v, {**base, "acl_mode": 0o600}, p, 5, 6, 3, acls_shown=True)


# --- the fourth review ------------------------------------------------------------


ALICE = f"{DOMAIN}-1001"


def _posix_acl(owner: str, mode: int = 0o640) -> dict:
    return {"kind": "posix", "mode": mode, "owner_sid": owner, "group_sid": GEO, "entries": []}


def _owned(w, annex_id, ds, filer, facade_client, sql, *, group: str | None = GEO, owner=ALICE):
    """A POSIX file of Alice's, uid 1001, gid 4242, copied, by real events."""
    body = R.copy_template(ds)
    origin = {"kind": "nas_folder", "origin": True}
    body["rules"] += [{"id": "back", "trigger": "recall_completed", "conditions": {},
                       "actions": [{"place_at": origin}, {"on_conflict": "restored_name"},
                                   {"hold_days": 14}]}]  # fmt: skip
    sql.execute("UPDATE catalog.filer SET protocol = 'nfs3' WHERE filer_id = %s", (filer,))
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )
    acl = _posix_acl(owner)
    if group is None:
        del acl["group_sid"]
    path = "dataset/line_1042.segy"
    ev = arrived(path, DATA, acl=acl, uid=1001, gid=4242, mode=0o640)
    fid = events(w, annex_id, [ev])[0]["file_id"]
    vid = copy(w, facade_client, fid, DATA)
    events(w, annex_id, [{"kind": "copied", "path": path, "file_id": fid, "version_id": vid,
                          "size": len(DATA), "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000005"}])  # fmt: skip
    assert w.verify()
    for sid, groups in ((BOB, [GEO]), (ALICE, [GEO])):
        sql.execute(
            "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)"
            " ON CONFLICT (sid) DO UPDATE SET group_sids = EXCLUDED.group_sids",
            (sid, groups),
        )
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/dataset",
                          "acl": {**_posix_acl(BOB), "mode": 0o775}}])  # fmt: skip
    return int(fid), int(vid), path, acl


def _chown(w, annex_id, path, acl, *, data=DATA, **ids) -> None:
    """A scan sees the file again, with a new owner or group."""
    ev = arrived(path, data, acl=acl, ctime=None, mode=0o640, **ids)
    ev["kind"] = "changed"
    ev["ctime_ns"] = "1700000000000000009"
    res = events(w, annex_id, [ev])
    assert res[0]["status"] in ("applied",), res


def _gone(w, annex_id, path) -> None:
    res = events(w, annex_id, [{"kind": "left", "path": path}])
    assert res[0]["status"] == "applied", res


def test_after_a_chown_an_old_version_goes_to_the_files_owner(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """SB1: the ticket gave the version's uid, and `go` didn't look at the staged
    owner, so the old owner, no longer in the read set, got the file back."""
    w, annex_id, ds, filer = annex
    fid, old, path, acl = _owned(w, annex_id, ds, filer, facade_client, sql)
    # chown to Bob, uid 1999, and a recopy.
    data2 = DATA + b"more"
    _chown(w, annex_id, path, {**acl, "owner_sid": BOB}, data=data2, uid=1999, gid=4242)
    new = copy(w, facade_client, str(fid), data2)
    events(w, annex_id, [{"kind": "copied", "path": path, "file_id": str(fid), "version_id": new,
                          "size": len(data2), "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000009"}])  # fmt: skip
    assert w.verify()
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (fid,),
    )
    p = _place(sql, annex_id, fid, old, filer)
    [k] = tickets(w, annex_id)["tickets"]
    assert (k["uid"], k["gid"]) == (1999, 4242), "the file's, not the version's"
    go = _posix_go(w, p, _posix_acl(ALICE), name="line_1042-restored.segy")
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")
    p2 = _place(sql, annex_id, fid, old, filer, state_="writing")
    go = _posix_go(w, p2, _posix_acl(BOB), name="line_1042-restored.segy")
    assert go["proceed"], go


def test_after_a_chown_and_a_delete_the_owner_is_still_the_files(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """No recopy: the tombstoned file's row says who it last belonged to."""
    w, annex_id, ds, filer = annex
    fid, vid, path, acl = _owned(w, annex_id, ds, filer, facade_client, sql)
    _chown(w, annex_id, path, {**acl, "owner_sid": BOB}, uid=1999, gid=4242)
    _gone(w, annex_id, path)
    p = _place(sql, annex_id, fid, vid, filer)
    [k] = tickets(w, annex_id)["tickets"]
    assert (k["uid"], k["gid"]) == (1999, 4242)
    go = _posix_go(w, p, _posix_acl(ALICE))
    assert (go["proceed"], go["reason"]) == (False, "acl_mismatch")
    p2 = _place(sql, annex_id, fid, vid, filer, state_="writing")
    assert _posix_go(w, p2, _posix_acl(BOB))["proceed"]


def test_an_unmapped_chgrp_then_a_delete_proves_nothing(annex, facade_client, sql) -> None:  # noqa: F811
    """The old gid passed the proof, and the current one was refused: backwards.
    The capture says nothing about who its readers are now, so neither passes."""
    w, annex_id, ds, filer = annex
    fid, vid, path, acl = _owned(w, annex_id, ds, filer, facade_client, sql, group=None, owner=BOB)
    staged = {k: v for k, v in _posix_acl(BOB).items() if k != "group_sid"}
    shown = {"posix_acls_shown": True}
    # Without the chgrp it is the capture.
    p = _place(sql, annex_id, fid, vid, filer, state_="writing")
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'removed', removed_at = now()"
        " WHERE file_id = %s",
        (fid,),
    )
    go = _posix_go(w, p, staged, uid=1001, gid=4242, **shown)
    assert go["proceed"], go
    _close(sql, p)
    sql.execute("UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'x',"
                " done_at = coalesce(done_at, now()) WHERE placement_id = %s", (p,))  # fmt: skip
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'present', removed_at = NULL"
                " WHERE file_id = %s", (fid,))  # fmt: skip
    _chown(w, annex_id, path, acl, uid=1001, gid=5555)
    _gone(w, annex_id, path)
    for gid in (4242, 5555):
        p = _place(sql, annex_id, fid, vid, filer, state_="writing")
        go = _posix_go(w, p, staged, uid=1001, gid=gid, **shown)
        assert (go["proceed"], go["reason"]) == (False, "acl_mismatch"), gid
        _close(sql, p)


def test_a_placed_file_in_a_new_folder_has_the_files_owner_too(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, path, acl = _owned(w, annex_id, ds, filer, facade_client, sql)
    _chown(w, annex_id, path, {**acl, "owner_sid": BOB}, uid=1999, gid=4242)
    _gone(w, annex_id, path)
    _place(sql, annex_id, fid, vid, filer)
    [k] = tickets(w, annex_id)["tickets"]
    assert k["uid"] == 1999 and k["acl"]["owner_sid"] == BOB


def _worker_run(db_url, fn):
    """As the worker's own connections are: autocommit, so a transaction is one."""
    import asyncio

    from omnifile_service.catalog.privileges import as_app

    async def run():
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "worker"), autocommit=True
        ) as conn:
            return await fn(conn)

    return asyncio.run(run())


def test_a_purge_a_legal_hold_refuses_ends_and_a_return_can_go_on(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    """SB2: the request stayed for good: tickets failed file_purged though nothing
    was purged, a requester got a 404, and it outlasted the hold."""
    from datetime import UTC, datetime, timedelta

    from omnifile_service.annex.purge import purge_due, purge_file

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.annex_file SET gone_since = now() - interval '40 days', nas_state = 'gone',"
        " purge_requested_at = now(), purge_requested_by = 'u' WHERE file_id = %s",
        (fid,),
    )
    sql.execute("UPDATE catalog.annex SET deleted_retention_days = 30 WHERE annex_id = %s",
                (annex_id,))  # fmt: skip
    sql.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
    sql.execute("UPDATE catalog.file_version SET legal_hold = true WHERE file_id = %s", (fid,))
    now = datetime.now(UTC)
    assert _worker_run(db_url, lambda c: purge_file(c, annex_id, fid, now)) == "refused"
    (asked, by, refused) = sql.execute(
        "SELECT purge_requested_at, purge_requested_by, purge_refused_at FROM catalog.annex_file"
        " WHERE file_id = %s",
        (fid,),
    ).fetchone()
    assert asked is None and by is None and refused is not None
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'purge_refused' AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    # A retention doesn't ask again every pass, only after a while. (Nor at all with
    # a placement open, which has its own test.)
    _close(sql, pid)
    assert _worker_run(db_url, lambda c: purge_due(c, now)) == 0
    assert sql.execute(
        "SELECT purge_requested_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()[0] is None  # fmt: skip
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'purge_refused' AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1, "no new refusal"
    assert _worker_run(db_url, lambda c: purge_due(c, now + timedelta(days=6, hours=23))) == 0
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'purge_refused' AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1, "not asked again inside the seven days"
    later = now + timedelta(days=7, hours=1)
    assert _worker_run(db_url, lambda c: purge_due(c, later)) == 0
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'purge_refused' AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 2, "asked again, refused again: the hold is still there"
    # The return goes on: the hold keeps the bytes, and doesn't stop them going back.
    pid = _place(sql, annex_id, fid, vid, filer, state_="ready")
    [k] = tickets(w, annex_id)["tickets"]
    assert int(k["placement_id"]) == pid
    assert _go(w, pid)["proceed"]
    assert _done(w, pid)["proceed"]
    (nas, gone) = sql.execute(
        "SELECT nas_state, gone_since FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert nas == "present" and gone is None


def test_a_purge_still_pending_is_taken(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    sql.execute(
        "UPDATE catalog.annex_file SET gone_since = now(), purge_requested_at = now(),"
        " purge_requested_by = 'u', purge_refused_at = now() - interval '1 day'"
        " WHERE file_id = %s",
        (fid,),
    )
    go = _go(w, pid)
    assert (go["proceed"], go["reason"]) == (False, "file_purged")


def test_rows_that_take_no_ticket_dont_use_up_the_ask(annex, facade_client, sql) -> None:  # noqa: F811
    """SS2: ten whose bytes aren't readable yet, and the eleventh waited for ever."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    waiting = [_another_file(w, annex_id, filer, facade_client, sql, f"w{i}.segy")
               for i in range(P.TICKETS_PER_ASK)]  # fmt: skip
    sql.execute(
        "UPDATE catalog.location SET logical_tier = 'archive' WHERE version_id IN"
        " (SELECT version_id FROM catalog.annex_placement WHERE placement_id = ANY(%s))",
        (waiting,),
    )
    new = _another_file(w, annex_id, filer, facade_client, sql, "new.segy")
    got = tickets(w, annex_id)
    assert [int(t["placement_id"]) for t in got["tickets"]] == [new]
    # And never more than an ask's worth.
    for i in range(P.TICKETS_PER_ASK + 2):
        _another_file(w, annex_id, filer, facade_client, sql, f"m{i}.segy")
    got = tickets(w, annex_id)
    assert len(got["tickets"]) == P.TICKETS_PER_ASK


def test_an_ask_looks_at_a_bounded_number_of_rows(annex, facade_client, sql, monkeypatch) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    waiting = [_another_file(w, annex_id, filer, facade_client, sql, f"w{i}.segy")
               for i in range(P.TICKETS_PER_ASK)]  # fmt: skip
    sql.execute(
        "UPDATE catalog.location SET logical_tier = 'archive' WHERE version_id IN"
        " (SELECT version_id FROM catalog.annex_placement WHERE placement_id = ANY(%s))",
        (waiting,),
    )
    _another_file(w, annex_id, filer, facade_client, sql, "new.segy")
    monkeypatch.setattr(P, "TICKET_ROWS_PER_ASK", P.TICKETS_PER_ASK)
    assert tickets(w, annex_id)["tickets"] == []


def _gone_and_due(sql, annex_id, fid) -> None:
    sql.execute(
        "UPDATE catalog.annex_file SET gone_since = now() - interval '40 days', nas_state = 'gone'"
        " WHERE file_id = %s",
        (fid,),
    )
    sql.execute("UPDATE catalog.annex SET deleted_retention_days = 30 WHERE annex_id = %s",
                (annex_id,))  # fmt: skip


def test_a_crash_between_a_retention_and_its_purge_leaves_a_message(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
    monkeypatch,
) -> None:
    """The request committed, then the purge ran: a crash between them left a request
    nothing would ever pick up, the file spoken for, for good."""
    from datetime import UTC, datetime

    from omnifile_service.annex import purge as PU

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    _gone_and_due(sql, annex_id, fid)
    now = datetime.now(UTC)

    async def crash(*a, **kw):
        raise RuntimeError("the worker went away")

    real = PU.purge_file
    monkeypatch.setattr(PU, "purge_file", crash)
    with pytest.raises(RuntimeError):
        _worker_run(db_url, lambda c: PU.purge_due(c, now))
    monkeypatch.setattr(PU, "purge_file", real)
    (asked,) = sql.execute(
        "SELECT purge_requested_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert asked is not None
    msgs = [m for (m,) in sql.execute("SELECT message FROM pgmq.q_retention").fetchall()]
    assert {"op": "annex_purge", "annex_id": str(annex_id), "file_id": str(fid)} in msgs
    # And the worker takes it up.
    assert _worker_run(db_url, lambda c: real(c, annex_id, fid, now)) == "purged"


def test_a_retention_leaves_a_file_on_its_way_back(annex, facade_client, sql, db_url) -> None:  # noqa: F811
    """It purged a file mid-return, and `go` answered not_restorable."""
    from datetime import UTC, datetime

    from omnifile_service.annex.purge import purge_due

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _gone_and_due(sql, annex_id, fid)
    now = datetime.now(UTC)
    assert _worker_run(db_url, lambda c: purge_due(c, now)) == 0
    (asked,) = sql.execute(
        "SELECT purge_requested_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert asked is None
    _close(sql, pid)
    assert _worker_run(db_url, lambda c: purge_due(c, now)) == 1


@pytest.mark.parametrize(
    "state",
    ["requested", "rehydrating", "ready", "writing", "verifying", "securing", "placing", "stuck"],
)
def test_any_open_placement_keeps_a_retention_off(annex, facade_client, sql, db_url, state) -> None:  # noqa: F811
    from datetime import UTC, datetime

    from omnifile_service.annex.purge import purge_due

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute("UPDATE catalog.annex_placement SET state = %s WHERE placement_id = %s",
                (state, pid))  # fmt: skip
    _gone_and_due(sql, annex_id, fid)
    assert _worker_run(db_url, lambda c: purge_due(c, datetime.now(UTC))) == 0


def test_files_on_their_way_back_dont_fill_a_retentions_batch(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    from datetime import UTC, datetime

    from omnifile_service.annex.purge import purge_due

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    other = _another_file(w, annex_id, filer, facade_client, sql, "other.segy")
    (ofid,) = sql.execute("SELECT file_id FROM catalog.annex_placement WHERE placement_id = %s",
                          (other,)).fetchone()  # fmt: skip
    _close(sql, other)
    _gone_and_due(sql, annex_id, ofid)
    _gone_and_due(sql, annex_id, fid)
    # The one with a placement open is the older: it comes first.
    sql.execute("UPDATE catalog.annex_file SET gone_since = now() - interval '50 days'"
                " WHERE file_id = %s", (fid,))  # fmt: skip
    assert _worker_run(db_url, lambda c: purge_due(c, datetime.now(UTC), limit=1)) == 1


def test_a_placement_opened_as_a_retention_asks_stops_it(annex, facade_client, sql, db_url) -> None:  # noqa: F811
    """The check in the update itself: the select saw none, the placement came after."""
    from datetime import UTC, datetime

    from omnifile_service.annex import purge as PU

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _gone_and_due(sql, annex_id, fid)
    now = datetime.now(UTC)

    class Between:
        """A connection whose select answers as if the placement weren't there yet."""

        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, k):
            return getattr(self.conn, k)

        async def execute(self, q, *a, **kw):
            if q.lstrip().startswith("SELECT af.annex_id"):

                class Rows:
                    async def fetchall(self):
                        return [(annex_id, fid, 30)]

                return Rows()
            return await self.conn.execute(q, *a, **kw)

    assert _worker_run(db_url, lambda c: PU.purge_due(Between(c), now)) == 0
    (asked,) = sql.execute(
        "SELECT purge_requested_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert asked is None


def test_a_refusal_it_no_longer_means_is_cleared(annex, facade_client, sql, db_url) -> None:  # noqa: F811
    from datetime import UTC, datetime

    from omnifile_service.annex.purge import purge_file

    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)

    def refused():
        return sql.execute(
            "SELECT purge_refused_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
        ).fetchone()[0]

    # A purge that goes through.
    sql.execute(
        "UPDATE catalog.annex_file SET gone_since = now(), nas_state = 'gone', purge_refused_at"
        " = now(), purge_requested_at = now(), purge_requested_by = 'u' WHERE file_id = %s",
        (fid,),
    )
    assert refused() is not None
    now = datetime.now(UTC)
    assert _worker_run(db_url, lambda c: purge_file(c, annex_id, fid, now)) == "purged"
    assert refused() is None


def test_a_refusal_it_no_longer_means_is_cleared_by_a_return(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.annex_file SET gone_since = now(), nas_state = 'gone',"
        " purge_refused_at = now() WHERE file_id = %s",
        (fid,),
    )
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    assert _done(w, pid)["proceed"]
    (refused,) = sql.execute(
        "SELECT purge_refused_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert refused is None


def test_a_stuck_report_from_before_go_doesnt_outlive_its_resume(annex, facade_client, sql) -> None:  # noqa: F811
    """Stuck before `go`, then past it: an abandon said "removes its staged copy",
    though the file may be under its name."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    step(w, pid, {"step": "stuck", "reason": "trouble", "at": "filled", "name": None})
    assert _go(w, pid)["proceed"]
    (where,) = sql.execute(
        "SELECT last_edge_report FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone()
    assert where is None


def test_a_hidden_acl_refusal_says_so(annex, facade_client, sql) -> None:  # noqa: F811
    """A bare acl_mismatch, after a whole copy, with nothing to go on."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql)
    _posix_catalog(sql, fid)
    tickets(w, annex_id)
    _close(sql, pid)
    p = _place(sql, annex_id, fid, vid, filer, state_="writing")
    a = _posix_go(w, p, UNNAMED_0640, uid=20002, gid=4242)
    assert (a["proceed"], a["reason"]) == (False, "acl_hidden")
    (reason, detail) = sql.execute(
        "SELECT fail_reason, fail_detail FROM catalog.annex_placement WHERE placement_id = %s",
        (p,),
    ).fetchone()
    assert reason == "acl_hidden"
    assert "doesn't show POSIX ACLs" in detail
    # Anything else that fails stays what it was.
    p = _place(sql, annex_id, fid, vid, filer, state_="writing")
    a = _posix_go(w, p, UNNAMED_0640, uid=1, gid=4242)
    assert a["reason"] == "acl_mismatch"
    # An owner that's wrong is wrong, whatever the server shows.
    other = {**UNNAMED_0640, "owner_sid": ALICE}
    for shown in ({}, {"posix_acls_shown": True}):
        p = _place(sql, annex_id, fid, vid, filer, state_="writing")
        a = _posix_go(w, p, other, uid=20002, gid=4242, **shown)
        assert a["reason"] == "acl_mismatch", shown


def test_an_nfs4_staged_owner_must_be_the_catalogs() -> None:
    staged = {**_staged_nfs4((GEO, READ, 0)), "owner_sid": EVERYONE_SID}
    mine = _catalog_nfs4([GEO], [_allow(GEO)])
    assert P.within_catalog({**staged, "owner_sid": GEO}, "nfs4", mine, None)
    assert not P.within_catalog(staged, "nfs4", mine, None)
    assert not P.within_catalog({**staged, "owner_sid": BOB}, "nfs4", mine, None)


def test_a_mount_that_stopped_answering_is_a_problem_at_once(annex, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    details = {"ok": False, "read_only": None, "readable": False, "sec": "sys",
               "error": "/mnt has not answered for 300 s", "acls_shown": None}  # fmt: skip

    def problems():
        return sql.execute(
            "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
            " AND details->>'reason' = 'nas_not_answering'",
            (annex_id,),
        ).fetchone()[0]

    events(w, annex_id, [{"kind": "mount", "details": details}])
    assert problems() == 0, "an ordinary failed mount has its own place"
    events(w, annex_id, [{"kind": "mount", "details": {**details, "hung": True}}])
    assert problems() == 1
    (health,) = sql.execute(
        "SELECT health FROM catalog.filer WHERE filer_id = %s", (filer,)
    ).fetchone()
    assert "has not answered" in health["error"]


def _credential_refused_for(monkeypatch, fid) -> None:
    """One file's backend is away; every other's answers."""
    from omnifile_service.api.dataplane import BackendDataPlane, SimHttpDataPlane

    for plane in (BackendDataPlane, SimHttpDataPlane):
        real = plane.credential

        async def only_that(self, target, access, *, _real=real):
            if target.key.startswith(f"{fid}/"):
                raise RuntimeError("the provider is away")
            return await _real(self, target, access)

        monkeypatch.setattr(plane, "credential", only_that)


def test_a_credential_that_cant_be_had_costs_only_its_own_placement(
    annex,  # noqa: F811
    facade_client,
    sql,
    monkeypatch,
    caplog,
) -> None:
    """One bad backend failed the whole ask, so every placement on the filer sat in
    `writing`, with `ticket_again` audited over and over and no alert."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    other = _another_file(w, annex_id, filer, facade_client, sql, "fine.segy")

    def counts():
        (problems,) = sql.execute(
            "SELECT count(*) FROM catalog.annex_event WHERE kind = 'problem'"
            " AND details->>'reason' = 'ticket_no_credential' AND details->>'placement_id' = %s",
            (str(pid),),
        ).fetchone()
        (audits,) = sql.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action LIKE 'annex.placement.ticket%%'"
            " AND details->>'placement_id' = %s",
            (str(pid),),
        ).fetchone()
        return problems, audits

    import logging

    def alerts():
        return sum("ALERT annex placement" in r.getMessage() for r in caplog.records)

    caplog.set_level(logging.ERROR, logger="omnifile_service.api.annex_placements")
    with monkeypatch.context() as m:
        _credential_refused_for(m, fid)
        got = tickets(w, annex_id)["tickets"]
        assert alerts() == 1
        assert [int(t["placement_id"]) for t in got] == [other]
        assert counts() == (1, 0), "said once, and no ticket was given to audit"
        (state,) = sql.execute(
            "SELECT state FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
        ).fetchone()
        assert state == "writing"
        # The edge asks again and again: not a problem each time, nor an ALERT.
        tickets(w, annex_id)
        assert counts() == (1, 0)
        assert alerts() == 1
        # An hour on, it says so again.
        sql.execute(
            "UPDATE catalog.annex_event SET at = at - interval '2 hours' WHERE kind = 'problem'"
            " AND details->>'placement_id' = %s",
            (str(pid),),
        )
        tickets(w, annex_id)
        assert counts() == (2, 0)
        assert alerts() == 2
    # It comes back: the next ask gives the ticket, and that one is audited.
    got = tickets(w, annex_id)["tickets"]
    assert int(pid) in [int(t["placement_id"]) for t in got]
    assert counts() == (2, 1)


def _refused_stopped_acked(sql, fid) -> None:
    """As after three refusals of later copies, an ArchiveAdmin's stop and acknowledgement."""
    sql.execute(
        "UPDATE catalog.annex_file SET copy_refusals = 3, copy_refused_version_id = 987654321,"
        " copy_refused_at = now(), copy_stopped_at = now(), copy_stopped_by = 'user:admin',"
        " copy_refused_ack_at = now(), copy_refused_ack_by = 'user:admin' WHERE file_id = %s",
        (fid,),
    )


def _refusal_state(sql, fid):
    return sql.execute(
        "SELECT copy_refusals, copy_refused_version_id, copy_stopped_at IS NOT NULL,"
        " copy_refused_ack_at IS NOT NULL FROM catalog.annex_file WHERE file_id = %s",
        (fid,),
    ).fetchone()


def test_a_verified_version_placed_back_ends_the_refusals_the_stop_and_the_acknowledgement(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """Review of #93: the one verify path that reset none of them. A refused file deleted
    on the NAS, an older verified version placed back: verified, but still "Copying
    stopped", and still counted at the bound."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.annex_file SET nas_state = 'gone', gone_since = now() WHERE file_id = %s",
        (fid,),
    )
    sql.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    _refused_stopped_acked(sql, fid)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    _done(w, pid)
    assert _copied_and_verified(sql, fid) == (int(vid), int(vid))
    assert _refusal_state(sql, fid) == (0, None, False, False)


def test_an_unverified_version_placed_back_leaves_them(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """Only a verified version ends them; the worker's trigger does when it is read back."""
    w, annex_id, ds, filer = annex
    fid, v1, v2 = _a_newer_copy_returned(
        w, annex_id, ds, filer, facade_client, sql, verified=False,
        before_done=_refused_stopped_acked,
    )  # fmt: skip
    assert _copied_and_verified(sql, fid) == (v2, None)
    assert _refusal_state(sql, fid) == (3, 987654321, True, True)
    sql.execute(
        "UPDATE catalog.location SET verified = true, verified_at = now(),"
        " verified_by = 'azure_read_crc' WHERE version_id = %s",
        (v2,),
    )
    assert _refusal_state(sql, fid) == (0, None, False, False)


def _a_newer_copy_returned(w, annex_id, ds, filer, facade_client, sql, *, verified: bool,
                           before_done=None):  # fmt: skip
    """A file whose newest version was committed and placed back with no `copied` event for
    it, so the annex file still names the version before. (An older version can't get here:
    at its origin it never takes the file's name.)"""
    fid, v1, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    data2 = bytes(reversed(DATA))
    v2 = int(copy(w, facade_client, str(fid), data2, base=str(v1)))
    assert w.verify()
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    if not verified:
        sql.execute(
            "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
            " WHERE version_id = %s",
            (v2,),
        )
    assert _copied_and_verified(sql, fid) == (int(v1), int(v1))
    p = _place(sql, annex_id, fid, v2, filer)
    tickets(w, annex_id)
    crc = hashes_of(data2)["crc64nvme"]
    a = _go(w, p, crc64=crc)
    assert a["proceed"], a
    if before_done is not None:
        before_done(sql, fid)
    assert _done(w, p)["proceed"]
    return fid, int(v1), v2


def _copied_and_verified(sql, fid):
    return sql.execute(
        "SELECT copied_version_id, verified_version_id FROM catalog.annex_file WHERE file_id = %s",
        (fid,),
    ).fetchone()


def test_a_verification_racing_a_return_isnt_lost(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    """`_returned` read the location without the lock `copied` takes: a verification
    between its trigger and its commit left the annex file never saying so."""
    import threading

    from omnifile_service.catalog.privileges import as_app

    w, annex_id, ds, filer = annex
    fid, v1, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    data2 = bytes(reversed(DATA))
    v2 = int(copy(w, facade_client, str(fid), data2, base=str(v1)))
    assert w.verify()
    sql.execute("UPDATE catalog.annex_file SET nas_state = 'removed' WHERE file_id = %s", (fid,))
    sql.execute(
        "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
        " WHERE version_id = %s",
        (v2,),
    )
    p = _place(sql, annex_id, fid, v2, filer)
    tickets(w, annex_id)
    assert _go(w, p, crc64=hashes_of(data2)["crc64nvme"])["proceed"]
    done: list[dict] = []
    with psycopg.connect(as_app(db_url, "worker")) as worker:
        # The trigger runs now, and finds no copy of v2 to mark.
        worker.execute(
            "UPDATE catalog.location SET verified = true, verified_at = now(),"
            " verified_by = 'azure_read_crc' WHERE version_id = %s AND NOT verified",
            (v2,),
        )
        t = threading.Thread(target=lambda: done.append(_done(w, p)))
        t.start()
        t.join(1.5)  # `done` waits for the worker's commit
        worker.commit()
    t.join(30)
    assert done and done[0]["proceed"]
    assert _copied_and_verified(sql, fid) == (v2, v2)


def test_a_verified_copy_returned_reads_as_verified(
    annex,  # noqa: F811
    facade_client,
    sql,
) -> None:
    """It showed `copied` until the next change: the annex file still named the one before,
    and the worker's trigger had already run."""
    w, annex_id, ds, filer = annex
    fid, v1, v2 = _a_newer_copy_returned(w, annex_id, ds, filer, facade_client, sql,
                                         verified=True)  # fmt: skip
    assert _copied_and_verified(sql, fid) == (v2, v2)


def test_an_unverified_copy_returned_doesnt_count_as_verified(
    annex,  # noqa: F811
    facade_client,
    sql,
    db_url,
) -> None:
    w, annex_id, ds, filer = annex
    fid, v1, v2 = _a_newer_copy_returned(w, annex_id, ds, filer, facade_client, sql,
                                         verified=False)  # fmt: skip
    assert _copied_and_verified(sql, fid) == (v2, None)
    # The removal gate reads the location, which says no.
    assert _gate(db_url, annex_id, fid, v2)[1] != "ok"
    # When it is read back, the worker's trigger says so.
    sql.execute(
        "UPDATE catalog.location SET verified = true, verified_at = now(),"
        " verified_by = 'azure_read_crc' WHERE version_id = %s",
        (v2,),
    )
    assert _copied_and_verified(sql, fid) == (v2, v2)


def test_an_ask_never_gives_more_than_its_limit(annex, facade_client, sql) -> None:  # noqa: F811
    """Going on past rows that took none, it gave 13 against a limit of 10."""
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    _close(sql, pid)
    waiting = [_another_file(w, annex_id, filer, facade_client, sql, f"w{i}.segy")
               for i in range(3)]  # fmt: skip
    sql.execute(
        "UPDATE catalog.location SET logical_tier = 'archive' WHERE version_id IN"
        " (SELECT version_id FROM catalog.annex_placement WHERE placement_id = ANY(%s))",
        (waiting,),
    )
    for i in range(P.TICKETS_PER_ASK + 5):
        _another_file(w, annex_id, filer, facade_client, sql, f"n{i}.segy")
    assert len(tickets(w, annex_id)["tickets"]) == P.TICKETS_PER_ASK


def test_a_late_report_on_a_retired_annexs_placement_is_audited(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'annex_retired',"
        " done_at = now() WHERE placement_id = %s",
        (pid,),
    )
    _done(w, pid)
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.late_report'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE kind = 'problem' AND file_id = %s"
        " AND details->>'reason' = 'placement_late_report'",
        (fid,),
    ).fetchone()
    assert details["ended_as"] == "annex_retired"


def _raw_step(w, pid: int, body: dict):
    """As JSON with its escapes, which is how a NUL or a lone surrogate arrives."""
    import json
    import uuid

    edge = w.edge
    path = E.replace("{edge_id}", str(edge.edge_id)) + f"/annex-placements/{pid}"
    return edge.client.post(
        path,
        content=json.dumps(body).encode(),
        headers=edge.headers(
            {"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())}
        ),
    )


@pytest.mark.parametrize("bad", ["a\x00b", "\ud800"])
@pytest.mark.parametrize("field", ["owner_restored", "pointer_deleted", "name", "outcome", "ino"])
def test_text_postgres_cant_hold_doesnt_make_done_a_500(annex, facade_client, sql, bad, field):  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    body = {"step": "done", "outcome": "placed", "name": "line_1042.segy", "attrs": _attrs()}
    if field == "ino":
        body["attrs"]["nas_file_id"] = bad
    else:
        body[field] = bad
    r = _raw_step(w, pid, body)
    assert r.status_code == 200, r.text
    assert state(sql, pid)[0] == "placed"


@pytest.mark.parametrize("field", ["reason", "detail"])
def test_text_postgres_cant_hold_doesnt_make_a_stuck_report_a_500(annex, facade_client, sql, field):  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    r = _raw_step(w, pid, {"step": "stuck", field: "a\x00\ud800"})
    assert r.status_code == 200, r.text


def test_entries_that_arent_objects_at_go_are_a_refusal(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup_posix(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    go = _posix_go(w, pid, {"kind": "posix", "mode": 0o640, "owner_sid": BOB, "entries": [1]})
    assert go["proceed"] is False
    # Where the proof reads them too: same numbers and mode, so it gets that far.
    _posix_catalog(sql, fid)
    _close(sql, pid)
    p = _place(sql, annex_id, fid, vid, filer, state_="writing")
    go = _posix_go(w, p, {**UNNAMED_0640, "entries": [1]},
                   uid=20002, gid=4242, posix_acls_shown=True)  # fmt: skip
    assert go["proceed"] is False


def test_a_stuck_report_says_where_it_is_for_an_abandon_to_use(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    tickets(w, annex_id)
    assert _go(w, pid)["proceed"]
    step(w, pid, {"step": "stuck", "reason": "trouble", "at": "linked", "name": "line_1042.segy"})
    (where,) = sql.execute(
        "SELECT last_edge_report FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone()
    assert where == {"at": "linked", "name": "line_1042.segy", "reason": "trouble"}


def _ready_for_go(w, annex_id, ds, filer, facade_client, sql) -> int:
    fid, vid, pid = _setup(w, annex_id, ds, filer, facade_client, sql)
    [k] = tickets(w, annex_id)["tickets"]
    step(w, pid, {"step": "filled"})
    step(w, pid, {"step": "read_back", "crc64": k["crc64"]})
    return pid


def test_a_nas_showing_no_acls_is_flagged_and_cleared(annex, sql) -> None:  # noqa: F811
    """Decision 64: the edge checks at every mount."""
    w, annex_id, ds, filer = annex

    def flag():
        return sql.execute(
            "SELECT acls_hidden_since IS NOT NULL, acls_hidden_ack_at IS NOT NULL"
            " FROM catalog.filer WHERE filer_id = %s",
            (filer,),
        ).fetchone()

    events(w, annex_id, [{"kind": "mount", "details": {"ok": True, "acls_shown": True}}])
    assert flag() == (False, False)
    events(w, annex_id, [{"kind": "mount", "details": {"ok": True, "acls_shown": False}}])
    events(w, annex_id, [{"kind": "mount", "details": {"ok": True, "acls_shown": False}}])
    assert flag() == (True, False)
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
        " AND details->>'reason' = 'acls_hidden'",
        (annex_id,),
    ).fetchone()
    assert n == 1, "said once, when it was flagged"
    sql.execute(
        "UPDATE catalog.filer SET acls_hidden_ack_by = 'x', acls_hidden_ack_at = now()"
        " WHERE filer_id = %s",
        (filer,),
    )
    # A failed mount can't tell, and changes nothing; one that they show clears.
    events(w, annex_id, [{"kind": "mount", "details": {"ok": False, "acls_shown": None}}])
    assert flag() == (True, True)
    events(w, annex_id, [{"kind": "mount", "details": {"ok": True, "acls_shown": True}}])
    assert flag() == (False, False), "and the acknowledgement with it"


@pytest.mark.parametrize(
    ("details", "flagged"),
    [
        # An edge from before decision 64 never sends acls_shown: fail closed.
        ({"ok": True}, True),
        ({"ok": False}, True),
        # The annex root wouldn't open, so it couldn't look: fail closed.
        ({"ok": True, "acls_shown": None}, True),
        # A failed mount, from an edge that sends the field, says nothing.
        ({"ok": False, "acls_shown": None}, False),
    ],
    ids=["old-edge", "old-edge-failed", "root-unopened", "failed-mount"],
)
def test_a_mount_report_that_cant_tell_flags_the_filer(annex, sql, details, flagged) -> None:  # noqa: F811, E501
    """The re-review of #85: the filer stayed unflagged, and group-readable grants went on,
    when the report didn't say."""
    w, annex_id, ds, filer = annex
    events(w, annex_id, [{"kind": "mount", "details": details}])
    (got,) = sql.execute(
        "SELECT acls_hidden_since IS NOT NULL FROM catalog.filer WHERE filer_id = %s", (filer,)
    ).fetchone()
    assert got is flagged


def test_a_flagged_filer_places_only_owner_read(annex, facade_client, sql) -> None:  # noqa: F811
    """Decision 64: group-readable placements are refused on a flagged filer."""
    w, annex_id, ds, filer = annex
    pid = _ready_for_go(w, annex_id, ds, filer, facade_client, sql)
    sql.execute("UPDATE catalog.filer SET acls_hidden_since = now() WHERE filer_id = %s",
                (filer,))  # fmt: skip
    go = _go(w, pid)
    assert (go["proceed"], go["reason"]) == (False, P.ACLS_HIDDEN_FILER), go
    assert state(sql, pid)[1] == P.ACLS_HIDDEN_FILER


def test_an_acknowledged_flag_lets_it_go_on(annex, facade_client, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    pid = _ready_for_go(w, annex_id, ds, filer, facade_client, sql)
    sql.execute(
        "UPDATE catalog.filer SET acls_hidden_since = now(), acls_hidden_ack_by = 'x',"
        " acls_hidden_ack_at = now() WHERE filer_id = %s",
        (filer,),
    )
    go = _go(w, pid)
    assert go["proceed"], go


def test_group_readable_is_anyone_but_the_owner() -> None:
    from omnifile_service.catalog.read_set import group_readable

    assert not group_readable([GEO], GEO)
    assert not group_readable([], GEO)
    assert group_readable([GEO, OWNERS], GEO)
    assert group_readable(["S-1-1-0"], GEO)
