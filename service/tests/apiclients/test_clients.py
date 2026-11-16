"""API clients (portal-api.md 2.2, 5.21): only registered, enabled clients get
in; roles are cut to their scopes; every client has an owner, and a proposal
is refused when its owner can't see the dataset."""

from __future__ import annotations

import uuid

from .support import person, propose, register, world


def _app() -> str:
    return f"app-{uuid.uuid4().hex[:10]}"


def test_an_unregistered_application_is_refused_even_with_a_good_token(web, sql) -> None:
    app = _app()
    r = web.call("GET", "/v1/edges", token=web.idp.app_token(app, ["Archive.Read"]))
    assert r.status_code == 403 and r.json()["code"] == "client_not_registered"


def test_register_scopes_disable_enable_revoke(web, sql) -> None:
    w, admin = world(web, sql)
    app = _app()
    owner = person(sql, w, "olga", roles="{DataSteward}")
    c = register(admin, app, owner, ["Archive.Read"])
    assert c["state"] == "enabled" and c["owner"]["identity_id"] == str(owner)
    assert c["owner"]["roles"] == ["DataSteward"] and c["keys"] == []
    token = web.idp.app_token(app, ["Archive.Read", "Archive.Adopt"])
    assert web.call("GET", "/v1/edges", token=token).status_code == 200
    # Archive.Adopt is in the token, not in the client's scopes: ignored.
    r = web.call(
        "POST",
        "/v1/migrations",
        token=token,
        check=False,
        json={"dataset_id": str(w.dataset_id), "prefix": ""},
    )
    assert r.status_code == 403 and r.json()["code"] == "role_required", r.text
    # The registration is listed, with how a client gets its token.
    listed = admin.get("/v1/settings/api-clients").json()
    assert any(x["app_id"] == app for x in listed["items"])
    setup = listed["setup"]
    assert setup["scope"] == "api://omnifile-test/.default"
    assert any("certificate" in s for s in setup["steps"])
    assert any("OmniFile API key" in s for s in setup["steps"])
    got = admin.get("/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"])
    assert got.status_code == 200 and got.json()["last_used_at"] is not None

    # Disabling: at once, with no re-auth, and If-Match.
    stale = web.browser(w.user("admin", roles=["ArchiveAdmin"], sid_rid=3003, no_auth_time=True))
    r = stale.call(
        "PATCH",
        "/v1/settings/api-clients/{api_client_id}",
        json={"enabled": False},
        api_client_id=c["api_client_id"],
        check=False,
    )
    assert r.status_code == 428
    r = stale.call(
        "PATCH",
        "/v1/settings/api-clients/{api_client_id}",
        json={"enabled": False},
        headers={"If-Match": c["etag"]},
        api_client_id=c["api_client_id"],
    )
    assert r.status_code == 200 and r.json()["state"] == "disabled", r.text
    r = web.call("GET", "/v1/edges", token=token)
    assert r.status_code == 403 and r.json()["code"] == "client_disabled"
    # Enabling widens access: re-auth.
    r2 = stale.call(
        "PATCH",
        "/v1/settings/api-clients/{api_client_id}",
        json={"enabled": True},
        headers={"If-Match": '"2"'},
        api_client_id=c["api_client_id"],
    )
    assert r2.status_code == 401 and r2.json()["code"] == "reauth_required"
    etag = admin.get(
        "/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"]
    ).json()["etag"]
    r = admin.call(
        "PATCH",
        "/v1/settings/api-clients/{api_client_id}",
        json={"enabled": True, "scopes": ["Archive.Read", "Recall.Request"]},
        headers={"If-Match": etag},
        api_client_id=c["api_client_id"],
    )
    assert r.status_code == 200 and r.json()["scopes"] == ["Recall.Request", "Archive.Read"]
    assert web.call("GET", "/v1/edges", token=token).status_code == 200
    # Revoked: for good.
    r = admin.post(
        "/v1/settings/api-clients/{api_client_id}/revoke", {}, api_client_id=c["api_client_id"]
    )
    assert r.status_code == 200 and r.json()["state"] == "revoked"
    assert web.call("GET", "/v1/edges", token=token).json()["code"] == "client_disabled"
    r = admin.call(
        "PATCH",
        "/v1/settings/api-clients/{api_client_id}",
        json={"enabled": True},
        headers={"If-Match": r.json()["etag"]},
        api_client_id=c["api_client_id"],
    )
    assert r.status_code == 403
    actions = [
        a
        for (a,) in sql.execute(
            "SELECT action FROM catalog.audit_log WHERE details->>'api_client_id' = %s"
            " AND action LIKE 'api_client.%%' ORDER BY audit_id",
            (c["api_client_id"],),
        )
    ]
    assert actions == [
        "api_client.register",
        "api_client.disable",
        "api_client.update",
        "api_client.revoke",
    ]


def test_registering_needs_re_auth_a_known_owner_and_one_registration(web, sql) -> None:
    w, admin = world(web, sql)
    owner = person(sql, w, "oscar")
    stale = web.browser(w.user("admin", roles=["ArchiveAdmin"], sid_rid=3003, no_auth_time=True))
    r = stale.post(
        "/v1/settings/api-clients",
        {"app_id": _app(), "display_name": "x", "scopes": [], "owner_identity_id": str(owner)},
    )
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    r = admin.post(
        "/v1/settings/api-clients",
        {"app_id": _app(), "display_name": "x", "scopes": [], "owner_identity_id": "999999999"},
    )
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "owner_identity_id"
    app = _app()
    register(admin, app, owner, [])
    r = admin.post(
        "/v1/settings/api-clients",
        {"app_id": app, "display_name": "again", "scopes": [], "owner_identity_id": str(owner)},
    )
    assert r.status_code == 409
    # Outside dev mode the client id is a GUID.
    web.services.settings.dev_mode = False
    r = admin.post(
        "/v1/settings/api-clients",
        {
            "app_id": "not-a-guid",
            "display_name": "x",
            "scopes": [],
            "owner_identity_id": str(owner),
        },
    )
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "app_id"
    web.services.settings.dev_mode = True
    # Non-admins can't see the registry.
    r = web.browser(w.steward).get("/v1/settings/api-clients")
    assert r.status_code == 403


def test_a_proposal_is_refused_when_its_owner_cannot_see_the_dataset(web, sql) -> None:
    w, admin = world(web, sql)
    app = _app()
    nobody = person(sql, w, "nora")  # no role, no share
    c = register(admin, app, nobody, ["Policy.Propose"], name="Migration planner")
    token = web.idp.app_token(app, ["Policy.Propose"])
    r = propose(web, w, token)
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access", r.text

    def owner_is(identity_id: int) -> None:
        etag = admin.get(
            "/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"]
        ).json()["etag"]
        r = admin.call(
            "PATCH",
            "/v1/settings/api-clients/{api_client_id}",
            json={"owner_identity_id": str(identity_id)},
            headers={"If-Match": etag},
            api_client_id=c["api_client_id"],
        )
        assert r.status_code == 200, r.text

    # A DataSteward whose role was last seen 40 days ago: not any more.
    owner_is(person(sql, w, "stale", roles="{DataSteward}", seen="now() - interval '40 days'"))
    assert propose(web, w, token).json()["code"] == "owner_lacks_access"
    # An Owner of one of the dataset's shares (through an AD group): yes.
    share_owner = person(sql, w, "sasha", groups=[w.g_owners])
    owner_is(share_owner)
    r = propose(web, w, token)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["api_client"] == {
        "api_client_id": c["api_client_id"],
        "display_name": "Migration planner",
        "app_id": app,
        "owner": {
            "kind": "identity",
            "display_name": "Sasha",
            "app_id": None,
            "identity_id": str(share_owner),
        },
    }
    row = sql.execute(
        "SELECT api_client_id, owner_identity_id FROM catalog.policy_proposal"
        " WHERE proposal_id = %s",
        (int(p["proposal_id"]),),
    ).fetchone()
    assert row == (int(c["api_client_id"]), share_owner)
    # The audit records the client and its owner.
    d = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'policy.propose'"
        " AND details->>'proposal_id' = %s",
        (p["proposal_id"],),
    ).fetchone()[0]
    assert d["api_client"] == {
        "api_client_id": c["api_client_id"],
        "owner_identity_id": str(share_owner),
    }
    # An ArchiveAdmin sees the same on the proposal.
    got = admin.get("/v1/policies/proposals/{proposal_id}", proposal_id=p["proposal_id"]).json()
    assert got["api_client"]["owner"]["identity_id"] == str(share_owner)
    # A person's own proposal has no client.
    mine = web.browser(w.steward).post(
        "/v1/policies/proposals",
        {"dataset_id": str(w.dataset_id), "body": p["body"], "rationale": "mine"},
    )
    assert mine.status_code == 201 and mine.json()["api_client"] is None


def test_sign_in_records_the_roles_owners_are_checked_against(web, sql) -> None:
    w, _admin = world(web, sql)
    web.browser(w.steward)
    roles, seen = sql.execute(
        "SELECT app_roles, app_roles_seen_at FROM catalog.identity WHERE sid = %s",
        (w.steward["sid"],),
    ).fetchone()
    assert roles == ["DataSteward"] and seen is not None
