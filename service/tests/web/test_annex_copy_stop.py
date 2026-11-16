"""A copy Azure keeps refusing (#81's follow-up, migration 0058; portal-api.md 5.23):
an ArchiveAdmin stops copying it again, or acknowledges the refusals, with a fresh
sign-in, audited; the file stays on Problems while it is stopped."""

from __future__ import annotations

import uuid

from .conftest import seed
from .test_annex import _annex, _copied, _dataset, _filer

STOP = "/v1/annexes/{annex_id}/files/{file_id}/copy-stop"
RESUME = "/v1/annexes/{annex_id}/files/{file_id}/copy-resume"
ACK = "/v1/annexes/{annex_id}/files/{file_id}/copy-refused-ack"
PROBLEMS = "/v1/annexes/{annex_id}/problems"


def _world(web, sql):  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    acl = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first) VALUES (%s, %s, 420, '[]', %s, '{}', true) RETURNING acl_id",
        (ds, uuid.uuid4().bytes * 2, [w.g_owners, w.g_readers]),
    ).fetchone()[0]
    refused = _copied(sql, w, a, ds, "refused.segy", acl)
    fine = _copied(sql, w, a, ds, "fine.segy", acl)
    # Refused three times, the last 40 days ago: its events are past Problems' 30 days.
    sql.execute(
        "UPDATE catalog.annex_file SET copy_refusals = 3,"
        " copy_refused_at = now() - interval '40 days' WHERE file_id = %s",
        (refused,),
    )
    return w, admin, a["annex_id"], refused, fine


def _audits(sql, action: str, fid: int) -> list[dict]:
    return [r[0] for r in sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = %s AND file_id = %s ORDER BY at",
        (action, fid),
    )]  # fmt: skip


def _listed(b, aid: str) -> dict:
    r = b.get(PROBLEMS, annex_id=aid)
    assert r.status_code == 200, r.text
    return {x["path"]: x for x in r.json()["copies_refused"]}


def test_an_archive_admin_stops_and_resumes_a_refused_copy(web, sql) -> None:  # type: ignore[no-untyped-def]
    w, admin, aid, refused, fine = _world(web, sql)
    got = _listed(admin, aid)["refused.segy"]
    assert (got["at_bound"], got["copy_refusals"], got["copy_stopped"]) == (True, 3, None)
    assert got["alerting"] is True
    assert "fine.segy" not in _listed(admin, aid)

    r = admin.post(STOP, annex_id=aid, file_id=refused)
    assert r.status_code == 200, r.text
    (actor,) = sql.execute(
        "SELECT actor FROM catalog.audit_log WHERE action = 'annex.copy.stopped' AND file_id = %s",
        (refused,),
    ).fetchone()
    assert r.json()["copy_stopped"]["by"] == actor and r.json()["copy_stopped"]["at"]
    assert _audits(sql, "annex.copy.stopped", refused)[-1]["was_stopped"] is False
    # Again: nothing more, and the row says so.
    assert admin.post(STOP, annex_id=aid, file_id=refused).status_code == 200
    assert _audits(sql, "annex.copy.stopped", refused)[-1]["was_stopped"] is True
    (stopped,) = sql.execute(
        "SELECT copy_stopped_at IS NOT NULL FROM catalog.annex_file WHERE file_id = %s",
        (refused,),
    ).fetchone()
    assert stopped
    # Stopped, it stays on Problems, however old its refusals.
    assert _listed(admin, aid)["refused.segy"]["copy_stopped"] is not None
    # A file no read-back refused can't be stopped.
    r = admin.post(STOP, annex_id=aid, file_id=fine)
    assert r.status_code == 409 and r.json()["code"] == "copy_not_refused"

    r = admin.post(RESUME, annex_id=aid, file_id=refused)
    assert r.status_code == 200, r.text
    assert r.json()["copy_stopped"] is None
    assert _audits(sql, "annex.copy.resumed", refused)[-1]["was_stopped"] is True


def test_an_archive_admin_acknowledges_a_refused_copy(web, sql) -> None:  # type: ignore[no-untyped-def]
    w, admin, aid, refused, fine = _world(web, sql)
    r = admin.post(ACK, annex_id=aid, file_id=refused)
    assert r.status_code == 200, r.text
    assert r.json()["copy_refused_ack"]["at"] is not None
    assert _audits(sql, "annex.copy.refused_ack", refused)[-1]["was_acknowledged"] is False
    # Acknowledged, not stopped: still on Problems, said acknowledged.
    got = _listed(admin, aid)["refused.segy"]
    assert got["copy_refused_ack"] is not None and got["copy_stopped"] is None
    r = admin.post(ACK, annex_id=aid, file_id=fine)
    assert r.status_code == 409 and r.json()["code"] == "copy_not_refused"


def test_only_an_archive_admin_with_a_fresh_sign_in_may(web, sql) -> None:  # type: ignore[no-untyped-def]
    w, admin, aid, refused, fine = _world(web, sql)
    owner = web.browser(w.owner)
    steward = web.browser(w.user("steward", roles=["DataSteward"], groups=[w.g_owners]))
    for url in (STOP, RESUME, ACK):
        for who in (owner, steward):
            r = who.post(url, annex_id=aid, file_id=refused)
            assert r.status_code == 403, (url, r.text)
    sql.execute(
        "UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'"
        " WHERE entra_object_id = %s",
        (w.admin["oid"],),
    )
    for url in (STOP, RESUME, ACK):
        r = admin.post(url, annex_id=aid, file_id=refused)
        assert r.status_code == 401 and r.json()["code"] == "reauth_required", url
    assert sql.execute(
        "SELECT copy_stopped_at, copy_refused_ack_at FROM catalog.annex_file WHERE file_id = %s",
        (refused,),
    ).fetchone() == (None, None)
    assert _audits(sql, "annex.copy.stopped", refused) == []
    admin.sign_in(w.admin, reauth=True)
    assert admin.post(STOP, annex_id=aid, file_id=refused).status_code == 200


def test_others_see_only_the_refused_copies_they_may_read(web, sql) -> None:  # type: ignore[no-untyped-def]
    """An ArchiveAdmin sees every one, to act on it; others, through the file's ACL:
    the annex's Owners are in it, a DataSteward in no group isn't."""
    w, admin, aid, refused, fine = _world(web, sql)
    assert admin.post(STOP, annex_id=aid, file_id=refused).status_code == 200
    assert "refused.segy" in _listed(admin, aid)
    assert "refused.segy" in _listed(web.browser(w.owner), aid)
    steward = web.browser(w.user("steward", roles=["DataSteward"]))
    assert _listed(steward, aid) == {}


def test_a_file_stopped_below_the_bound_is_said_to_alert_until_acknowledged(web, sql) -> None:  # type: ignore[no-untyped-def]
    """Review of #93: the row says what the gauge does."""
    w, admin, aid, refused, fine = _world(web, sql)
    sql.execute("UPDATE catalog.annex_file SET copy_refusals = 1 WHERE file_id = %s", (refused,))
    got = _listed(admin, aid)["refused.segy"]
    assert (got["at_bound"], got["alerting"]) == (False, False)
    assert admin.post(STOP, annex_id=aid, file_id=refused).status_code == 200
    got = _listed(admin, aid)["refused.segy"]
    assert (got["at_bound"], got["alerting"]) == (False, True)
    assert admin.post(ACK, annex_id=aid, file_id=refused).status_code == 200
    assert _listed(admin, aid)["refused.segy"]["alerting"] is False


def test_a_list_cut_short_says_how_many_there_are(web, sql, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Review of #93: capped at 200 rows, it looked complete."""
    from omnifile_service.web import routes_annex

    w, admin, aid, refused, fine = _world(web, sql)
    sql.execute("UPDATE catalog.annex_file SET copy_refusals = 1 WHERE file_id = %s", (fine,))
    assert admin.post(STOP, annex_id=aid, file_id=refused).status_code == 200
    p = admin.get(PROBLEMS, annex_id=aid).json()
    assert (len(p["copies_refused"]), p["copies_refused_total"]) == (2, 2)
    monkeypatch.setattr(routes_annex, "REFUSED_LISTED", 1)
    p = admin.get(PROBLEMS, annex_id=aid).json()
    assert [r["path"] for r in p["copies_refused"]] == ["refused.segy"], "the stopped one first"
    assert p["copies_refused_total"] == 2
    # The total is of what the caller may see.
    steward = web.browser(w.user("steward", roles=["DataSteward"]))
    p = steward.get(PROBLEMS, annex_id=aid).json()
    assert (p["copies_refused"], p["copies_refused_total"]) == ([], 0)
