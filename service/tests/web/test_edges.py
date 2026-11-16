"""Edges and their health (portal-api.md section 5.14; decision 37)."""

from __future__ import annotations

import json
import os

from .conftest import seed


def _active(sql, name: str, *, minutes_ago: float | None, guard: str | None, health: dict) -> None:  # type: ignore[no-untyped-def]
    hb = "NULL" if minutes_ago is None else f"now() - interval '{minutes_ago} minutes'"
    sql.execute(
        "INSERT INTO catalog.edge (name, state, enrolled_at, cert_sha256, cert_not_after,"
        " software_version, kernel, guard_state, health, last_heartbeat_at)"
        " VALUES (%s, 'active', now(), %s, now() + interval '30 days', '0.3.0', '7.0', %s, %s,"
        f" {hb})",
        (name, os.urandom(32), guard, json.dumps(health)),
    )


def test_edges_show_health_and_status(web, sql) -> None:  # type: ignore[no-untyped-def]
    seed(sql)  # a pending edge
    t = os.urandom(3).hex()
    half = {"outbox_depth": 3, "data_free_bytes": 50, "data_capacity_bytes": 100}
    _active(sql, f"a-ok-{t}", minutes_ago=1, guard="armed", health=half)
    _active(sql, f"b-full-{t}", minutes_ago=1, guard="armed",
            health={"data_free_bytes": 5, "data_capacity_bytes": 100})  # fmt: skip
    _active(sql, f"c-guard-{t}", minutes_ago=1, guard="down", health={})
    _active(sql, f"d-silent-{t}", minutes_ago=20, guard="armed", health={})
    _active(sql, f"e-never-{t}", minutes_ago=None, guard=None, health={})
    _active(sql, f"f-recovery-{t}", minutes_ago=1, guard="armed", health={"recovery_used": 1})
    _active(sql, f"g-stale-{t}", minutes_ago=1, guard="armed", health={"quiet_stale_s": 300})
    _active(sql, f"h-blip-{t}", minutes_ago=1, guard="armed", health={"quiet_stale_s": 5})
    r = web.call("GET", "/v1/edges", token=web.app_token(["Archive.Read"]))
    assert r.status_code == 200, r.text
    by = {e["name"]: e for e in r.json()["edges"]}
    assert by[f"a-ok-{t}"]["status"] == "ok"
    assert by[f"a-ok-{t}"]["health"]["outbox_depth"] == 3
    assert by[f"a-ok-{t}"]["software_version"] == "0.3.0"
    assert by[f"b-full-{t}"]["status"] == "attention"
    assert by[f"c-guard-{t}"]["status"] == "attention"
    assert by[f"d-silent-{t}"]["status"] == "silent"
    assert by[f"e-never-{t}"]["status"] == "silent"
    # The break-glass account was used and not sealed again.
    assert by[f"f-recovery-{t}"]["status"] == "attention"
    assert by[f"f-recovery-{t}"]["health"]["recovery_used"] == 1
    # The shares' quiet periods can't be read (#80 re-check): five minutes is a problem.
    assert by[f"g-stale-{t}"]["status"] == "attention"
    assert by[f"h-blip-{t}"]["status"] == "ok"
    assert any(e["status"] == "pending" for e in by.values())


def test_edges_are_for_admins_and_readers(web, sql) -> None:  # type: ignore[no-untyped-def]
    r = web.call("GET", "/v1/edges", token=web.app_token([]))
    assert r.status_code == 403 and r.json()["code"] == "role_required"


# ------------------------------------------------------------------ adding edges (section 5.15)

ENROL_URL = "https://enrol.omnifile.internal"
CA_SHA = "ab" * 32


def _enrolment_on(web) -> None:  # type: ignore[no-untyped-def]
    web.services.settings.enrol_url = ENROL_URL
    web.services.settings.enrol_server_ca_sha256 = CA_SHA


def test_an_admin_adds_an_edge_and_gets_its_bundle_once(web, sql) -> None:  # type: ignore[no-untyped-def]
    import hashlib

    from omnifile_service.enrol import bundle

    _enrolment_on(web)
    w = seed(sql)
    admin = web.browser(w.admin)
    name = f"edge-{os.urandom(3).hex()}"
    r = admin.post("/v1/edges", {"name": name})
    assert r.status_code == 201, r.text
    out = r.json()
    fields = bundle.read(out["bundle"])
    assert fields["url"] == ENROL_URL and fields["server_ca_sha256"] == CA_SHA
    row = sql.execute(
        "SELECT state, enrolment_token_sha256, enrolment_expires_at > now() + interval '23 hours'"
        " FROM catalog.edge WHERE edge_id = %s",
        (int(out["edge_id"]),),
    ).fetchone()
    # Only the token's hash is kept, and the bundle lives 24 hours.
    assert row[0] == "pending"
    assert bytes(row[1]) == hashlib.sha256(fields["token"].encode()).digest()
    assert row[2] is True
    listed = admin.get("/v1/edges").json()["edges"]
    assert any(e["name"] == name and e["status"] == "pending" for e in listed)
    audit = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'edge.add'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    assert audit == {"edge_id": out["edge_id"], "name": name}
    assert "token" not in json.dumps(audit)
    # Nor does the idempotency store keep the bundle (it holds the token).
    stored = sql.execute(
        "SELECT count(*) FROM catalog.idempotency_key WHERE response::text LIKE %s",
        ("%" + out["bundle"] + "%",),
    ).fetchone()[0]
    assert stored == 0

    # The same name again is refused; a bad name too.
    assert admin.post("/v1/edges", {"name": name}).json()["code"] == "name_taken"
    bad = admin.post("/v1/edges", {"name": "Edge_1"}, check=False)
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "name"


def test_a_new_bundle_replaces_the_old_and_only_before_enrolment(web, sql) -> None:  # type: ignore[no-untyped-def]
    from omnifile_service.enrol import bundle

    _enrolment_on(web)
    w = seed(sql)
    admin = web.browser(w.admin)
    first = admin.post("/v1/edges", {"name": f"edge-{os.urandom(3).hex()}"}).json()
    again = admin.post("/v1/edges/{edge_id}/bundle", edge_id=first["edge_id"])
    assert again.status_code == 201, again.text
    t1, t2 = bundle.read(first["bundle"])["token"], bundle.read(again.json()["bundle"])["token"]
    assert t1 != t2
    import hashlib

    kept = bytes(sql.execute(
        "SELECT enrolment_token_sha256 FROM catalog.edge WHERE edge_id = %s",
        (int(first["edge_id"]),),
    ).fetchone()[0])  # fmt: skip
    assert kept == hashlib.sha256(t2.encode()).digest(), "the first bundle no longer works"
    for b in (first["bundle"], again.json()["bundle"]):
        stored = sql.execute(
            "SELECT count(*) FROM catalog.idempotency_key WHERE response::text LIKE %s",
            ("%" + b + "%",),
        ).fetchone()[0]
        assert stored == 0, "a bundle's token is never kept in the clear"

    sql.execute("UPDATE catalog.edge SET state = 'active', cert_sha256 = %s, enrolled_at = now()"
                " WHERE edge_id = %s", (os.urandom(32), int(first["edge_id"])))  # fmt: skip
    r = admin.post("/v1/edges/{edge_id}/bundle", edge_id=first["edge_id"])
    assert r.status_code == 409 and r.json()["code"] == "edge_enrolled"
    gone = admin.post("/v1/edges/{edge_id}/bundle", edge_id="999999999")
    assert gone.json()["code"] == "not_found"


def test_only_signed_in_admins_add_edges(web, sql) -> None:  # type: ignore[no-untyped-def]
    _enrolment_on(web)
    w = seed(sql)
    r = web.browser(w.steward).post("/v1/edges", {"name": "edge-x"})
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    # An app, even with every role, can't: a bundle is a credential.
    r = web.call("POST", "/v1/edges", token=web.app_token(["Archive.Adopt", "Archive.Read"]),
                 json={"name": "edge-y"}, check=False)  # fmt: skip
    assert r.status_code in (401, 403)


def test_without_enrolment_settings_the_portal_says_what_is_missing(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    r = web.browser(w.admin).post("/v1/edges", {"name": "edge-z"})
    assert r.status_code == 503 and "OMNIFILE_ENROL_PUBLIC_URL" in r.json()["detail"]


def test_an_admin_revokes_a_lost_edge(web, sql) -> None:  # type: ignore[no-untyped-def]
    """M7 review: a stolen filer kept renewing itself; only SQL stopped it.
    Revoked, its certificate is refused on every control call
    (api.clientauth, enrol), and it can never renew."""
    w = seed(sql)
    name = f"lost-{os.urandom(3).hex()}"
    _active(sql, name, minutes_ago=1, guard="armed", health={})
    (edge_id,) = sql.execute("SELECT edge_id FROM catalog.edge WHERE name = %s",
                             (name,)).fetchone()  # fmt: skip
    admin = web.browser(w.admin)
    # A recent sign-in is required.
    sql.execute("UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'"
                " WHERE entra_object_id = %s", (w.admin["oid"],))  # fmt: skip
    r = admin.post("/v1/edges/{edge_id}/revoke", {}, edge_id=edge_id)
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    admin.sign_in(w.admin, reauth=True)
    r = admin.post("/v1/edges/{edge_id}/revoke", {}, edge_id=edge_id)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "revoked"
    state, at = sql.execute("SELECT state, revoked_at FROM catalog.edge WHERE edge_id = %s",
                            (edge_id,)).fetchone()  # fmt: skip
    assert state == "revoked" and at is not None
    assert not any(e["name"] == name for e in admin.get("/v1/edges").json()["edges"])
    audit = sql.execute("SELECT details FROM catalog.audit_log WHERE action = 'edge.revoke'"
                        " ORDER BY audit_id DESC LIMIT 1").fetchone()[0]  # fmt: skip
    assert audit == {"edge_id": str(edge_id), "name": name}
    # Decision 50: the CRL for Roles Anywhere is queued with the revocation.
    queued = sql.execute("SELECT message FROM pgmq.q_roles_anywhere_crl"
                         " WHERE message->>'edge_id' = %s", (str(edge_id),)).fetchall()  # fmt: skip
    assert [q[0] for q in queued] == [{"reason": "revoke", "edge_id": str(edge_id)}]
    # Again: the same answer, nothing new.
    again = admin.post("/v1/edges/{edge_id}/revoke", {}, edge_id=edge_id)
    assert again.status_code == 200 and again.json()["state"] == "revoked"
    queued = sql.execute("SELECT 1 FROM pgmq.q_roles_anywhere_crl"
                         " WHERE message->>'edge_id' = %s", (str(edge_id),)).fetchall()  # fmt: skip
    assert len(queued) == 1
    # Only a signed-in ArchiveAdmin.
    r = web.browser(w.steward).post("/v1/edges/{edge_id}/revoke", {}, edge_id=edge_id)
    assert r.status_code == 403
    r = web.call("POST", "/v1/edges/{edge_id}/revoke", json={}, edge_id=edge_id,
                 token=web.app_token(["Archive.Adopt", "Archive.Read"]), check=False)  # fmt: skip
    assert r.status_code in (401, 403)
    assert admin.post("/v1/edges/{edge_id}/revoke", {}, edge_id="999999999").status_code == 404


def test_the_wizard_names_release_images_and_invents_no_link(web, sql, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """WP18: Add edge's wizard. A release publishes only the update feed, so without
    OMNIFILE_APPLIANCE_IMAGES_URL there is no link, only the release folder's file names."""
    from omnifile_service.web import routes_edges

    w = seed(sql)
    monkeypatch.setattr(routes_edges, "__version__", "0.4.0rc2")
    admin = web.browser(w.admin)
    r = admin.get("/v1/edges/appliance")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["release"] == "0.4.0-rc.2"  # the release's VERSION, not PEP 440
    files = {i["platform"]: i for i in a["images"]}
    assert files["hyperv"]["file"] == "omnifile-edge_0.4.0-rc.2.vhdx"
    assert files["vmware"]["file"] == "omnifile-edge_0.4.0-rc.2.ova"
    assert files["kvm"]["file"] == "omnifile-edge_0.4.0-rc.2.qcow2"
    assert files["azure"]["file"] == "omnifile-edge_0.4.0-rc.2.vhd"
    assert "aws" not in files  # no EC2 image is built
    assert all(i["url"] is None for i in a["images"]) and a["checksums"] is None
    assert a["enrolment_configured"] is False and a["code_ttl_s"] == 24 * 3600
    _enrolment_on(web)
    web.services.settings.appliance_images_url = "https://downloads.example.test/omnifile/0.4.0"
    a = admin.get("/v1/edges/appliance").json()
    assert a["enrolment_configured"] is True
    assert {i["platform"]: i["url"] for i in a["images"]}["kvm"] == (
        "https://downloads.example.test/omnifile/0.4.0/omnifile-edge_0.4.0-rc.2.qcow2"
    )
    assert a["checksums"] == "https://downloads.example.test/omnifile/0.4.0/SHA256SUMS"
    # A build that isn't a release names no file.
    monkeypatch.setattr(routes_edges, "__version__", "0+unknown")
    a = admin.get("/v1/edges/appliance").json()
    assert a["release"] is None and all(i["file"] is None for i in a["images"])
    # ArchiveAdmins only, never an app.
    assert web.browser(w.steward).get("/v1/edges/appliance").status_code == 403
    r = web.call("GET", "/v1/edges/appliance", token=web.app_token(["Archive.Read"]), check=False)
    assert r.status_code in (401, 403)
