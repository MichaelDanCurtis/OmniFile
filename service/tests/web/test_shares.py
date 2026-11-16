"""Shares: listing, settings per field and role, the access screen, WORM."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from omnifile_service.api.rows import SHARE_SELECT, share_row
from omnifile_service.catalog.privileges import as_app
from omnifile_service.retention.worm import lock_due_shares

from .conftest import seed


def _etag(b, w) -> str:
    r = b.get("/v1/shares/{share_id}", share_id=w.share_id)
    assert r.status_code == 200, r.text
    assert r.headers["etag"] == r.json()["etag"]
    return r.headers["etag"]


def _patch(b, w, body, etag=None):
    headers = {"If-Match": etag} if etag else {}
    return b.call("PATCH", "/v1/shares/{share_id}/settings", json=body, headers=headers,
                  share_id=w.share_id)  # fmt: skip


def test_share_visibility(web, sql):
    w = seed(sql)

    def ids(b) -> list[str]:  # type: ignore[no-untyped-def]
        # Every page: other tests' shares share the catalog.
        out, cursor = [], None
        while True:
            params = {"limit": 500, **({"cursor": cursor} if cursor else {})}
            page = b.get("/v1/shares", params=params).json()
            out += [s["share_id"] for s in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                return out

    assert str(w.share_id) in ids(web.browser(w.reader))
    assert str(w.share_id) in ids(web.browser(w.admin))
    assert str(w.share_id) not in ids(web.browser(w.outsider))
    r = web.browser(w.outsider).get("/v1/shares/{share_id}", share_id=w.share_id)
    assert r.status_code == 404
    s = web.browser(w.reader).get("/v1/shares/{share_id}", share_id=w.share_id).json()
    assert s["my_access"] == {"owner": False, "writer": False, "reader": True,
                              "can_read_root": True}  # fmt: skip
    assert s["groups"]["owners"]["display_name"].startswith("Owners")
    assert s["nfs_clients"] == []  # ArchiveAdmin only
    assert any(c["code"] == "kerberos_throughput" for c in s["checks"]), s["checks"]


def test_settings_need_if_match_and_the_right_role(web, sql):
    w = seed(sql)
    owner = web.browser(w.owner)
    r = _patch(owner, w, {"rewind_depth_days": 60})
    assert r.status_code == 428 and r.json()["code"] == "precondition_required"
    r = _patch(owner, w, {"rewind_depth_days": 60}, '"999"')
    assert r.status_code == 412 and r.json()["code"] == "precondition_failed"
    etag = _etag(owner, w)
    r = _patch(owner, w, {"rewind_depth_days": 60, "on_open": "auto",
                          "wait_threshold_minutes": 30}, etag)  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["rewind"]["depth_days"] == 60 and r.json()["on_open"] == "auto"
    # Owners can't touch the ArchiveAdmin's fields.
    r = _patch(owner, w, {"rewind_depth_max_days": 365}, r.headers["etag"])
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    r = _patch(owner, w, {"nfs_sec": ["sys"]}, _etag(owner, w))
    assert r.status_code == 403
    # Above the maximum.
    r = _patch(owner, w, {"rewind_depth_days": 400}, _etag(owner, w))
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    # A reader can do nothing here.
    r = _patch(web.browser(w.reader), w, {"on_open": "fail"}, _etag(owner, w))
    assert r.status_code == 403
    audit = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE share_id = %s AND action = 'share.settings'",
        (w.share_id,),
    ).fetchall()
    assert audit and audit[0][0]["rewind_depth_days"] == 60


def test_admin_settings_and_catalog_rules(web, sql):
    w = seed(sql)
    admin = web.browser(w.admin)
    etag = _etag(admin, w)
    r = _patch(admin, w, {"rewind_depth_max_days": 10}, etag)
    assert r.status_code == 200, r.text
    assert r.json()["rewind"] == {**r.json()["rewind"], "depth_days": 10, "max_days": 10}
    # sec=sys needs named clients: the catalog's rule, as 400 with errors.
    r = _patch(admin, w, {"nfs_sec": ["sys"]}, r.headers["etag"])
    assert r.status_code == 400 and r.json()["errors"][0]["field"].startswith("share_host")
    r = _patch(admin, w, {"nfs_sec": ["sys"], "nfs_clients": ["10.0.0.0/24"],
                          "host_trust_override": {"enabled": True, "reason": "legacy hosts"}},
               _etag(admin, w))  # fmt: skip
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["trust_level"] == "host_trusted" and s["host_trust_override"] is True
    assert s["nfs_clients"] == ["10.0.0.0/24"]
    assert any(c["code"] == "host_trusted" for c in s["checks"])
    # Unknown fields.
    r = admin.call(
        "PATCH",
        "/v1/shares/{share_id}/settings",
        json={"worm_enabled": True},
        headers={"If-Match": s["etag"]},
        check=False,
        share_id=w.share_id,
    )
    assert r.status_code == 400


def test_settings_preview_saves_nothing(web, sql):
    w = seed(sql)
    owner = web.browser(w.owner)
    before = _etag(owner, w)
    r = owner.post("/v1/shares/{share_id}/settings/preview", {"nfs_sec": ["krb5p"]},
                   share_id=w.share_id)  # fmt: skip
    assert r.status_code == 200, r.text
    assert any(c["code"] == "kerberos_throughput" for c in r.json()["checks"])
    assert _etag(owner, w) == before
    r = web.browser(w.reader).post("/v1/shares/{share_id}/settings/preview",
                                   {"on_open": "fail"}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 403


def test_access_screen(web, sql):
    w = seed(sql)
    owner = web.browser(w.owner)
    new_readers = f"{w.domain}-2005"
    body = {"owners_group_sid": w.g_owners, "writers_group_sid": None,
            "readers_group_sid": new_readers}  # fmt: skip
    r = owner.call("PUT", "/v1/shares/{share_id}/access", json=body,
                   headers={"If-Match": _etag(owner, w)}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["groups"]["readers"]["sid"] == new_readers
    assert r.json()["groups"]["writers"] is None
    # Owners can't replace the Owners group.
    body["owners_group_sid"] = w.g_other
    r = owner.call("PUT", "/v1/shares/{share_id}/access", json=body,
                   headers={"If-Match": _etag(owner, w)}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 403
    admin = web.browser(w.admin)
    r = admin.call("PUT", "/v1/shares/{share_id}/access", json=body,
                   headers={"If-Match": _etag(admin, w)}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 200
    # The change reaches the edge through the feed (share.upsert).
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.feed_event WHERE entity = 'share' AND entity_id = %s",
        (w.share_id,),
    ).fetchone()
    assert n >= 3
    actions = [a for (a,) in sql.execute(
        "SELECT action FROM catalog.audit_log WHERE share_id = %s", (w.share_id,))]  # fmt: skip
    assert actions.count("share.access") == 2


def _edge_reports(sql, w, health):  # type: ignore[no-untyped-def]
    sql.execute(
        "UPDATE catalog.edge SET health = %s WHERE edge_id = %s",
        (json.dumps(health), w.edge_id),
    )


def test_the_default_shown_is_the_one_the_edge_reports(web, sql):
    """#80 review: the portal shows the edge's own defaults once it reports them,
    and says when it hasn't (an edge that predates decision 67)."""
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    get = lambda: admin.get("/v1/shares/{share_id}", share_id=w.share_id).json()  # noqa: E731
    _edge_reports(sql, w, {"outbox_depth": 0})
    s = get()
    assert (s["quiet_supported"], s["quiet_default_s"]) == (False, 10)
    assert s["edge_name"]
    _edge_reports(sql, w, {"outbox_depth": 0, "quiet_default_s": 15, "quiet_worm_default_s": 90})
    s = get()
    assert (s["quiet_supported"], s["quiet_default_s"]) == (True, 15)
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    assert get()["quiet_default_s"] == 90


def test_a_worm_share_below_its_default_needs_saying_so(web, sql):
    """#80 review: below 60 s on a WORM share is allowed (decision 67), but only
    when confirmed, and the audit says it was."""
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    r = _patch(admin, w, {"quiet_s": 30}, _etag(admin, w))
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "quiet_s_confirmed"
    assert "half-written" in r.json()["errors"][0]["message"]
    r = _patch(admin, w, {"quiet_s": 30, "quiet_s_confirmed": False}, _etag(admin, w))
    assert r.status_code == 400
    r = _patch(admin, w, {"quiet_s": 30, "quiet_s_confirmed": True}, _etag(admin, w))
    assert r.status_code == 200, r.text
    assert r.json()["quiet_s"] == 30
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE share_id = %s"
        " AND action = 'share.settings' ORDER BY audit_id DESC LIMIT 1",
        (w.share_id,),
    ).fetchone()
    assert details["quiet_s"] == {"old": None, "new": 30, "confirmed_below_worm_default": True}
    assert "quiet_s_confirmed" not in details
    # At or above the default, or back to it, needs nothing.
    for v in (60, 600, None):
        r = _patch(admin, w, {"quiet_s": v}, _etag(admin, w))
        assert r.status_code == 200, (v, r.text)
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE share_id = %s"
        " AND action = 'share.settings' ORDER BY audit_id DESC LIMIT 1",
        (w.share_id,),
    ).fetchone()
    assert details["quiet_s"] == {"old": 600, "new": None}


def test_turning_worm_on_over_a_short_quiet_period_needs_saying_so(web, sql):
    """#80 re-check: setting 10 s while the share isn't WORM, then turning WORM on,
    would skip the question. Turning WORM on asks it instead, and audits it."""
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    r = _patch(admin, w, {"quiet_s": 10}, _etag(admin, w))
    assert r.status_code == 200, r.text
    body = {"retention_days": 30, "confirm_name": w.share_name, "window_hours": 2}
    r = _worm(admin, w, "enable", body)
    assert r.status_code == 409 and r.json()["code"] == "worm_violation", r.text
    assert "half-written" in r.json()["detail"]
    assert (
        admin.get("/v1/shares/{share_id}", share_id=w.share_id).json()["worm"]["enabled"] is False
    )
    r = _worm(admin, w, "enable", {**body, "quiet_s_confirmed": True})
    assert r.status_code == 200, r.text
    assert r.json()["worm"]["enabled"] and r.json()["quiet_s"] == 10
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE share_id = %s"
        " AND action = 'share.worm.confirm' ORDER BY audit_id DESC LIMIT 1",
        (w.share_id,),
    ).fetchone()
    assert details["quiet_s"] == 10 and details["quiet_confirmed_below_worm_default"] is True


def test_turning_worm_on_at_the_default_or_longer_asks_nothing(web, sql):
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    assert _patch(admin, w, {"quiet_s": 60}, _etag(admin, w)).status_code == 200
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE share_id = %s"
        " AND action = 'share.worm.confirm' ORDER BY audit_id DESC LIMIT 1",
        (w.share_id,),
    ).fetchone()
    assert "quiet_confirmed_below_worm_default" not in details


def test_a_share_that_isnt_worm_needs_no_confirmation(web, sql):
    w = seed(sql)
    owner = web.browser(w.owner)
    r = _patch(owner, w, {"quiet_s": 10}, _etag(owner, w))
    assert r.status_code == 200, r.text


def test_quiet_period_defaults_with_worm(web, sql):
    """Decision 67: a share with no value takes its default, which follows WORM."""
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    s = admin.get("/v1/shares/{share_id}", share_id=w.share_id).json()
    assert s["quiet_s"] is None and s["quiet_default_s"] == 10
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["quiet_s"] is None and r.json()["quiet_default_s"] == 60
    # An explicit value stands whatever WORM does; the default is still reported.
    r = _patch(admin, w, {"quiet_s": 45, "quiet_s_confirmed": True}, _etag(admin, w))
    assert r.status_code == 200, r.text
    assert r.json()["quiet_s"] == 45 and r.json()["quiet_default_s"] == 60
    r = _worm(admin, w, "cancel", {"confirm_name": w.share_name})
    assert r.status_code == 200, r.text
    assert r.json()["quiet_s"] == 45 and r.json()["quiet_default_s"] == 10


def test_quiet_period_range(web, sql):
    """10 to 600 s, or null. Below today's period partial versions get routine."""
    w = seed(sql)
    admin = web.browser(w.admin)
    for bad in (0, 9, 601, 86400, -60, True, "60", 60.5, [60]):
        # Past the spec on purpose: the service refuses it too.
        r = admin.call("PATCH", "/v1/shares/{share_id}/settings", json={"quiet_s": bad},
                       headers={"If-Match": _etag(admin, w)}, check=False,
                       share_id=w.share_id)  # fmt: skip
        assert r.status_code == 400, bad
        assert r.json()["errors"][0]["field"] == "quiet_s", bad
    for good in (10, 600):
        r = _patch(admin, w, {"quiet_s": good}, _etag(admin, w))
        assert r.status_code == 200, r.text
        assert r.json()["quiet_s"] == good
    r = _patch(admin, w, {"quiet_s": None}, _etag(admin, w))
    assert r.status_code == 200, r.text
    assert r.json()["quiet_s"] is None
    # The catalog holds the same bounds, whoever writes.
    for bad in (9, 601):
        with sql.transaction(), contextlib.suppress(psycopg.errors.CheckViolation):
            sql.execute(
                "UPDATE catalog.share SET quiet_s = %s WHERE share_id = %s", (bad, w.share_id)
            )
            pytest.fail(f"the catalog took {bad}")


def test_quiet_period_roles_and_audit(web, sql):
    """Owners and ArchiveAdmins change it; others see it. Each change is audited
    with its old and new values."""
    w = seed(sql)
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    for who in (w.reader, w.writer, w.steward):
        b = web.browser(who)
        r = _patch(b, w, {"quiet_s": 60}, _etag(owner, w))
        assert r.status_code == 403, who
        assert b.get("/v1/shares/{share_id}", share_id=w.share_id).json()["quiet_s"] is None
    assert sql.execute(
        "SELECT quiet_s FROM catalog.share WHERE share_id = %s", (w.share_id,)
    ).fetchone() == (None,)
    assert _patch(owner, w, {"quiet_s": 60}, _etag(owner, w)).status_code == 200
    assert _patch(admin, w, {"quiet_s": 120}, _etag(admin, w)).status_code == 200
    assert _patch(owner, w, {"quiet_s": None}, _etag(owner, w)).status_code == 200
    rows = sql.execute(
        "SELECT details->'quiet_s' FROM catalog.audit_log"
        " WHERE share_id = %s AND action = 'share.settings' ORDER BY audit_id",
        (w.share_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [
        {"old": None, "new": 60},
        {"old": 60, "new": 120},
        {"old": 120, "new": None},
    ]
    # Another setting's change doesn't touch it, or claim to.
    assert _patch(owner, w, {"quiet_s": 30}, _etag(owner, w)).status_code == 200
    r = _patch(owner, w, {"rewind_depth_days": 5}, _etag(owner, w))
    assert r.status_code == 200 and r.json()["quiet_s"] == 30
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE share_id = %s"
        " AND action = 'share.settings' ORDER BY audit_id DESC LIMIT 1",
        (w.share_id,),
    ).fetchone()
    assert "quiet_s" not in details


def test_quiet_period_reaches_the_edge_in_its_feed(web, sql):
    """The change is a share.upsert, as every share setting is; a share with no
    value says null, so the edge applies its own default."""
    w = seed(sql)
    owner = web.browser(w.owner)
    cols = [c.name for c in sql.execute(SHARE_SELECT + " LIMIT 0").description]

    def fed() -> dict:  # type: ignore[type-arg]
        row = sql.execute(SHARE_SELECT + " WHERE share_id = %s", (w.share_id,)).fetchone()
        return share_row(dict(zip(cols, row, strict=True)))

    def events() -> int:
        (n,) = sql.execute(
            "SELECT count(*) FROM catalog.feed_event WHERE entity = 'share' AND entity_id = %s",
            (w.share_id,),
        ).fetchone()
        return n

    assert fed()["quiet_s"] is None
    before = (events(), fed()["entity_version"])
    assert _patch(owner, w, {"quiet_s": 90}, _etag(owner, w)).status_code == 200
    assert fed()["quiet_s"] == 90
    assert events() == before[0] + 1
    assert fed()["entity_version"] != before[1]


def test_quiet_period_preview_saves_nothing(web, sql):
    w = seed(sql)
    owner = web.browser(w.owner)
    before = _etag(owner, w)
    r = owner.post("/v1/shares/{share_id}/settings/preview", {"quiet_s": 60}, share_id=w.share_id)
    assert r.status_code == 200, r.text
    assert _etag(owner, w) == before
    r = owner.call("POST", "/v1/shares/{share_id}/settings/preview", json={"quiet_s": 5},
                   check=False, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 400


def test_directory_groups(web, sql):
    w = seed(sql)
    owner = web.browser(w.owner)
    r = owner.get("/v1/directory/groups", params={"q": "grp-readers-" + w.share_name[6:]})
    assert r.status_code == 200, r.text
    assert [g["sid"] for g in r.json()["items"]] == [w.g_readers]
    r = owner.get("/v1/directory/groups", params={"q": w.g_other})
    assert [g["sid"] for g in r.json()["items"]] == [w.g_other]
    r = owner.get("/v1/directory/groups", params={"q": "%"})
    assert r.json()["items"] == []  # LIKE wildcards are literal
    r = web.browser(w.reader).get("/v1/directory/groups", params={"q": "grp"})
    assert r.status_code == 403


def _worm(b, w, action, body):
    return b.post(f"/v1/shares/{{share_id}}/worm/{action}", body,
                  headers={"If-Match": _etag(b, w)}, share_id=w.share_id)  # fmt: skip


def test_worm_enable_cancel_extend(web, sql, db_url):
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name + "x"})
    assert r.status_code == 422 and r.json()["code"] == "confirmation_mismatch"
    r = _worm(web.browser(w.owner), w, "enable", {"retention_days": 30,
                                                   "confirm_name": w.share_name})  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    worm = r.json()["worm"]
    assert worm["enabled"] and worm["mode"] == "unlocked_test"
    deadline = datetime.fromisoformat(worm["lock_deadline"].replace("Z", "+00:00"))
    assert timedelta(hours=1.9) < deadline - datetime.now(UTC) <= timedelta(hours=2)
    r = _worm(admin, w, "cancel", {"confirm_name": w.share_name})
    assert r.status_code == 200 and r.json()["worm"]["enabled"] is False
    # Enable again, lock it, and it's one-way.
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name})
    assert r.status_code == 200
    sql.execute("UPDATE catalog.share SET worm_lock_deadline = now() WHERE share_id = %s",
                (w.share_id,))  # fmt: skip

    async def lock():
        conn = await psycopg.AsyncConnection.connect(as_app(db_url, "worker"))
        try:
            await conn.set_autocommit(True)
            return await lock_due_shares(conn, datetime.now(UTC))
        finally:
            await conn.close()

    import asyncio

    assert w.share_id in asyncio.run(lock())
    r = _worm(admin, w, "cancel", {"confirm_name": w.share_name})
    assert r.status_code == 409 and r.json()["code"] == "worm_window_closed"
    r = _worm(admin, w, "extend", {"retention_days": 10})
    assert r.status_code == 409 and r.json()["code"] == "worm_violation"
    r = _worm(admin, w, "extend", {"retention_days": 60})
    assert r.status_code == 200 and r.json()["worm"]["retention_days"] == 60
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name})
    assert r.status_code == 409


def test_worm_is_refused_with_a_smart_policy(web, sql):
    w = seed(sql)  # two accounts: the catalog refuses WORM there too
    admin = web.browser(w.admin)
    r = _worm(admin, w, "enable", {"retention_days": 30, "confirm_name": w.share_name})
    assert r.status_code == 409 and r.json()["code"] == "layout_conflict"


def test_worm_deadlines_follow_the_clock_the_auto_lock_sweeps_with(web, sql, db_url):
    """The M4 e2e race: in dev, web's sessions keep real time while the
    worker sweeps on the shared virtual clock, which an earlier step may
    have moved 16 h ahead. The deadline must be set and checked on the
    service clock, or a 2 h test window is already over when it opens."""
    import asyncio

    ahead = timedelta(hours=16)
    web.services.settings.service_clock = lambda: datetime.now(UTC) + ahead
    w = seed(sql, layout="azure_worm")
    admin = web.browser(w.admin)
    r = _worm(admin, w, "enable", {"retention_days": 7, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    deadline = datetime.fromisoformat(r.json()["worm"]["lock_deadline"].replace("Z", "+00:00"))
    assert timedelta(hours=1.9) < deadline - (datetime.now(UTC) + ahead) <= timedelta(hours=2)

    async def sweep(at: datetime) -> list[int]:
        conn = await psycopg.AsyncConnection.connect(as_app(db_url, "worker"))
        try:
            await conn.set_autocommit(True)
            return await lock_due_shares(conn, at)
        finally:
            await conn.close()

    # The worker's sweep, on the same clock: still inside the window.
    assert w.share_id not in asyncio.run(sweep(datetime.now(UTC) + ahead))
    assert admin.get("/v1/shares/{share_id}", share_id=w.share_id).json()["worm"]["mode"] == (
        "unlocked_test"
    )
    # Cancel is judged on that clock too: allowed inside the window...
    r = _worm(admin, w, "cancel", {"confirm_name": w.share_name})
    assert r.status_code == 200, r.text
    # ...and refused once the service clock passes the deadline, before any sweep.
    r = _worm(admin, w, "enable", {"retention_days": 7, "confirm_name": w.share_name,
                                   "window_hours": 2})  # fmt: skip
    assert r.status_code == 200, r.text
    web.services.settings.service_clock = lambda: datetime.now(UTC) + ahead + timedelta(hours=3)
    r = _worm(admin, w, "cancel", {"confirm_name": w.share_name})
    assert r.status_code == 409 and r.json()["code"] == "worm_window_closed"
    assert w.share_id in asyncio.run(sweep(datetime.now(UTC) + ahead + timedelta(hours=3)))


def _restricted_gauge(db_url: str) -> dict[str, int]:
    import asyncio

    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from omnifile_service import metrics

    reader = InMemoryMetricReader()
    metrics.configure("test", reader=reader)
    try:

        async def connect() -> psycopg.AsyncConnection:
            return await psycopg.AsyncConnection.connect(as_app(db_url, "worker"), autocommit=True)

        asyncio.run(metrics.Collector(connect).collect_once())
        return {
            p.attributes["share"]: p.value
            for rm in reader.get_metrics_data().resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "omnifile.shares.host_trusted_restricted"
            for p in m.data.data_points
        }
    finally:
        metrics.shutdown()


def test_a_folder_restricted_after_a_sec_sys_share_was_made_needs_an_answer(web, sql, db_url):
    """Threat model T7 (the owner's choice, 2026-10-16): a warning, not a
    refusal. An Owner tightens a folder after its share went host-trusted;
    the share's checks, the list of shares and a gauge (for the alert) say
    so until an ArchiveAdmin records an override (or turns on Kerberos)."""
    w = seed(sql)
    admin = web.browser(w.admin)
    # Every folder readable by Everyone, so sec=sys needs no override.
    sql.execute(
        "UPDATE catalog.acl SET read_sids = array_append(read_sids, 'S-1-1-0')"
        " WHERE dataset_id = %s", (w.dataset_id,),
    )  # fmt: skip
    r = _patch(admin, w, {"nfs_sec": ["sys"], "nfs_clients": ["10.0.0.0/24"]}, _etag(admin, w))
    assert r.status_code == 200, r.text
    assert [c["code"] for c in r.json()["checks"]] == ["host_trusted"]
    assert _restricted_gauge(db_url).get(w.share_name) is None

    # An Owner restricts a folder to their group.
    sql.execute(
        "UPDATE catalog.acl SET read_sids = array_remove(read_sids, 'S-1-1-0')"
        " WHERE acl_id = %s", (w.acl_secret,),
    )  # fmt: skip
    s = admin.get("/v1/shares/{share_id}", share_id=w.share_id).json()
    (c,) = [c for c in s["checks"] if c["code"] == "host_trusted_restricted"]
    assert c["severity"] == "error" and c["field"] == "host_trust_override"
    listed, cursor = {}, None
    while True:  # other tests' shares come first in a shared database
        page = admin.get("/v1/shares", params={"cursor": cursor} if cursor else {}).json()
        listed.update({x["share_id"]: x for x in page["items"]})
        cursor = page["next_cursor"]
        if s["share_id"] in listed or not cursor:
            break
    assert any(c["code"] == "host_trusted_restricted" for c in listed[s["share_id"]]["checks"])
    assert _restricted_gauge(db_url).get(w.share_name) == 1

    # The ArchiveAdmin's answer: an override with a reason.
    r = _patch(admin, w, {"host_trust_override": {"enabled": True, "reason": "lab hosts"}},
               s["etag"])  # fmt: skip
    assert r.status_code == 200, r.text
    assert [c["code"] for c in r.json()["checks"]] == ["host_trusted"]
    assert _restricted_gauge(db_url).get(w.share_name) is None
