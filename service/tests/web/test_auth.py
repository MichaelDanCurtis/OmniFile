"""Sign-in (BFF), sessions, CSRF, re-auth and partner tokens (portal-api.md 2)."""

from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from omnifile_service.web import __main__ as web_main
from omnifile_service.web.auth import safe_return_to
from omnifile_service.web.context import LOGIN_COOKIE, SESSION_COOKIE
from omnifile_service.web.devidp import DevClient, DevIdp

from .conftest import API_AUDIENCE, APP_ID, ORIGIN, TENANT, dev_params, location_query, seed


def _set_cookies(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


def test_login_redirects_with_pkce_and_a_lax_login_cookie(web):
    r = web.client.get("/auth/login", params={"return_to": "/files/7"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(f"{ORIGIN}/dev-idp/authorize?")
    q = location_query(r)
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["response_type"] == "code" and q["scope"] == "openid profile"
    assert q["redirect_uri"] == f"{ORIGIN}/auth/callback"
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32
    assert "prompt" not in q
    [cookie] = [c for c in _set_cookies(r) if c.startswith(LOGIN_COOKIE)]
    for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attr in cookie
    assert "Domain" not in cookie


def test_reauth_login_forces_prompt(web):
    r = web.client.get("/auth/login", params={"reauth": "1"}, follow_redirects=False)
    q = location_query(r)
    assert q["prompt"] == "login" and q["max_age"] == "0"


@pytest.mark.parametrize(
    "value",
    ["https://evil.test/x", "//evil.test/x", "/\\evil.test", "javascript:alert(1)",
     "/\x00x", "/x\ny", "http:/evil", "", "evil", "/" + "a" * 3000],
)  # fmt: skip
def test_return_to_never_leaves_the_origin(value):
    assert safe_return_to(value) == "/"


def test_open_redirect_is_refused_end_to_end(web, sql):
    w = seed(sql)
    b = web.browser()
    r = b.sign_in(w.reader, return_to="//evil.test/steal")
    assert r.headers["location"] == "/"
    r = b.sign_in(w.reader, return_to="/browse/12?as_of=2026-01-01")
    assert r.headers["location"] == "/browse/12?as_of=2026-01-01"


def test_sign_in_sets_a_strict_host_cookie_and_records_the_identity(web, sql):
    w = seed(sql)
    b = web.browser()
    r = b.sign_in(w.reader)
    [cookie] = [c for c in _set_cookies(r) if c.startswith(SESSION_COOKIE + "=")]
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
        assert attr in cookie
    assert any(c.startswith(LOGIN_COOKIE + "=;") for c in _set_cookies(r))
    s = b.get("/auth/session").json()
    assert s["authenticated"] and s["user"]["sid"] == w.reader["sid"]
    assert s["user"]["reader_of"] == [str(w.share_id)]
    row = sql.execute(
        "SELECT entra_object_id::text, group_sids::text[], groups_source FROM catalog.identity"
        " WHERE sid = %s",
        (w.reader["sid"],),
    ).fetchone()
    # The same set as the session: token groups, Domain Users, Authenticated Users.
    assert row == (w.reader["oid"], sorted([w.g_readers, f"{w.domain}-513", "S-1-5-11"]),
                   "token")  # fmt: skip
    # Only the hash of the cookie is stored.
    assert sql.execute(
        "SELECT count(*) FROM catalog.web_session WHERE session_sha256 = %s", (b.session.encode(),)
    ).fetchone() == (0,)
    audit = sql.execute(
        "SELECT action FROM catalog.audit_log WHERE actor_sid = %s", (w.reader["sid"],)
    ).fetchall()
    assert ("portal.sign_in",) in audit


def test_session_fixation_a_planted_cookie_is_not_kept(web, sql):
    w = seed(sql)
    victim = web.browser(w.reader)
    planted = victim.session
    # The attacker's cookie is in the browser when the owner signs in.
    b = web.browser()
    b.session = planted
    b.sign_in(w.owner)
    assert b.session != planted
    victim.session = planted
    assert victim.get("/v1/me").status_code == 401
    assert b.get("/v1/me").json()["sid"] == w.owner["sid"]


def test_callback_refuses_a_state_from_another_browser(web, sql):
    w = seed(sql)
    r1 = web.client.get("/auth/login", follow_redirects=False)
    r2 = web.client.get(r1.headers["location"] + "&" + urlencode(dev_params(w.reader)),
                        follow_redirects=False)  # fmt: skip
    web.client.cookies.clear()
    web.client.cookies.set(LOGIN_COOKIE, "x" * 43, domain="portal.test")
    r3 = web.client.get(r2.headers["location"], follow_redirects=False)
    assert r3.status_code == 401 and r3.json()["code"] == "unauthenticated"


def test_callback_state_is_single_use_and_expires(web, sql):
    w = seed(sql)
    r1 = web.client.get("/auth/login", follow_redirects=False)
    state = location_query(r1)["state"]
    r2 = web.client.get(r1.headers["location"] + "&" + urlencode(dev_params(w.reader)),
                        follow_redirects=False)  # fmt: skip
    callback = r2.headers["location"]
    assert web.client.get(callback, follow_redirects=False).status_code == 302
    web.client.cookies.set(LOGIN_COOKIE, state, domain="portal.test")
    again = web.client.get(callback, follow_redirects=False)
    assert again.status_code == 401
    # An expired login row.
    r1 = web.client.get("/auth/login", follow_redirects=False)
    state = location_query(r1)["state"]
    sql.execute(
        "UPDATE catalog.oidc_login SET expires_at = now() - interval '1 second' WHERE state = %s",
        (state,),
    )
    r2 = web.client.get(r1.headers["location"] + "&" + urlencode(dev_params(w.reader)),
                        follow_redirects=False)  # fmt: skip
    assert web.client.get(r2.headers["location"], follow_redirects=False).status_code == 401


def test_callback_without_code_is_refused(web):
    r = web.client.get("/auth/callback", params={"state": "x"}, follow_redirects=False)
    assert r.status_code == 400


def test_idle_and_absolute_expiry(web, sql):
    w = seed(sql)
    b = web.browser(w.reader)
    assert b.get("/v1/me").status_code == 200
    sql.execute(
        "UPDATE catalog.web_session SET idle_expires_at = now() - interval '1 second'"
        " WHERE entra_object_id = %s",
        (w.reader["oid"],),
    )
    r = b.get("/v1/me")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    b.sign_in(w.reader)
    sql.execute(
        "UPDATE catalog.web_session SET expires_at = now() - interval '1 second',"
        " idle_expires_at = now() - interval '1 second' WHERE entra_object_id = %s",
        (w.reader["oid"],),
    )
    assert b.get("/v1/me").status_code == 401


def test_no_cookie_is_401(web):
    web.client.cookies.clear()
    r = web.client.get("/v1/me")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    web.client.cookies.set(SESSION_COOKIE, "forged", domain="portal.test")
    assert web.client.get("/v1/shares").status_code == 401


def test_logout_needs_csrf_and_ends_the_session(web, sql):
    w = seed(sql)
    b = web.browser(w.reader)
    r = b.call("POST", "/auth/logout", csrf=False)
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    r = b.call("POST", "/auth/logout")
    assert r.status_code == 200
    assert r.json()["logout_url"].startswith(f"{ORIGIN}/dev-idp/logout?")
    assert any(c.startswith(SESSION_COOKIE + "=;") for c in _set_cookies(r))
    assert b.get("/v1/me").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-CSRF-Token": "wrong", "Origin": ORIGIN},
        {"X-CSRF-Token": "<token>"},
        {"X-CSRF-Token": "<token>", "Origin": "https://evil.test"},
        {"X-CSRF-Token": "<token>", "Origin": "null"},
    ],
)
def test_csrf_header_and_origin_are_both_required(web, sql, headers):
    w = seed(sql)
    b = web.browser(w.owner)
    h = {k: (b.csrf if v == "<token>" else v) for k, v in headers.items()}
    r = b.call("POST", "/v1/pins", json={"items": [str(w.root_id)], "tier": "hot",
                                         "reason": "x"}, headers=h, csrf=False)  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    # Another user's token is not this session's.
    other = web.browser(w.reader)
    r = b.call("POST", "/v1/pins", json={"items": [str(w.root_id)], "tier": "hot",
                                         "reason": "x"},
               headers={"X-CSRF-Token": other.csrf, "Origin": ORIGIN}, csrf=False)  # fmt: skip
    assert r.status_code == 403


def test_security_headers_everywhere(web, sql):
    w = seed(sql)
    b = web.browser(w.reader)
    for r in (
        b.get("/v1/me"),
        web.client.get("/healthz"),
        web.client.get("/auth/login", follow_redirects=False),
    ):
        h = r.headers
        assert h["content-security-policy"].startswith("default-src 'self'; script-src 'self'")
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert h["strict-transport-security"] == "max-age=31536000"
    assert b.get("/v1/me").headers["cache-control"] == "no-store"


def test_a_user_with_no_app_role_gets_only_me(web, sql):
    w = seed(sql)
    b = web.browser(w.user("norole", roles=[], groups=[w.g_readers], sid_rid=3010))
    assert b.get("/v1/me").status_code == 200
    r = b.get("/v1/shares")
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    r = b.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id)
    assert r.status_code == 403


def test_with_named_people_every_ad_user_is_a_user_and_nobody_else_is(web, sql):
    """Decision 89: OMNIFILE_WEB_ROLE_HOLDERS=people makes a token with an AD SID a User;
    a guest or cloud-only account (no SID) gets only what Entra gave."""
    w = seed(sql)
    web.services.settings.role_holders = "people"
    ad = w.user("plain", roles=[], groups=[w.g_readers], sid_rid=3012)
    b = web.browser(ad)
    assert b.get("/v1/me").json()["roles"] == ["User"]
    assert b.get("/v1/shares").status_code == 200
    # What it sees is still the files' ACLs: the reader group's share and nothing more.
    assert [s["share_id"] for s in b.get("/v1/shares").json()["items"]] == [str(w.share_id)]
    details = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'portal.sign_in'"
        " AND actor_sid = %s ORDER BY 1 DESC LIMIT 1",
        (ad["sid"],),
    ).fetchone()[0]
    assert details["roles"] == ["User"] and details["user_role"] == "tenant"
    # A named DataSteward keeps the role and is a User as well.
    named = web.browser(w.user("named", roles=["DataSteward"], sid_rid=3013))
    assert named.get("/v1/me").json()["roles"] == ["DataSteward", "User"]
    # No SID (a guest, a cloud-only account): no role unless Entra gave one.
    guest_oid = "4b5c0f70-9a77-4a40-8b7e-6f6e0f7c0a01"
    guest = web.browser({"oid": guest_oid, "name": "Guest", "roles": []})
    assert guest.get("/v1/me").json()["roles"] == []
    assert guest.get("/v1/shares").json()["code"] == "role_required"
    # With groups (the default), the same AD user has no role.
    web.services.settings.role_holders = "groups"
    again = web.browser(w.user("plain2", roles=[], groups=[w.g_readers], sid_rid=3014))
    assert again.get("/v1/me").json()["roles"] == []
    assert again.get("/v1/shares").status_code == 403


def test_the_role_holders_setting_is_groups_or_people():
    assert web_main.role_holders(None) == "groups"
    assert web_main.role_holders("people") == "people"
    with pytest.raises(SystemExit):
        web_main.role_holders("everyone")


def test_roles_from_the_token_are_filtered(web, sql):
    w = seed(sql)
    b = web.browser(w.user("x", roles=["User", "Policy.Propose", "God"], sid_rid=3011))
    assert b.get("/v1/me").json()["roles"] == ["User"]


def test_reauth_is_required_for_worm_and_legal_holds(web, sql):
    w = seed(sql, layout="azure_worm")
    b = web.browser(w.admin)
    sql.execute(
        "UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'"
        " WHERE entra_object_id = %s",
        (w.admin["oid"],),
    )
    etag = b.get("/v1/shares/{share_id}", share_id=w.share_id).headers["etag"]
    r = b.post("/v1/shares/{share_id}/worm/enable",
               {"retention_days": 30, "confirm_name": w.share_name},
               headers={"If-Match": etag}, share_id=w.share_id)  # fmt: skip
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    assert r.json()["max_age_s"] == 600
    r = b.post("/v1/holds", {"items": [str(w.root_id)], "kind": "legal", "reason": "case 1"})
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    b.sign_in(w.admin, reauth=True)
    r = b.post("/v1/holds", {"items": [str(w.root_id)], "kind": "legal", "reason": "case 1"})
    # Re-authenticated; but roles never grant read, so the root is skipped as unknown.
    assert r.status_code == 201, r.text
    assert r.json()["skipped"][0]["code"] == "not_found"


def test_groups_overage_waits_for_the_worker(web, sql):
    w = seed(sql)
    user = {**w.reader, "overage": True}
    b = web.browser(user)
    r = b.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id)
    assert r.status_code == 503 and r.json()["code"] == "groups_pending"
    assert r.json()["retry_after_s"] == 5
    (n,) = sql.execute(
        "SELECT count(*) FROM pgmq.q_identity_resolve m JOIN catalog.identity i"
        " ON m.message->>'identity_id' = i.identity_id::text WHERE i.sid = %s",
        (w.reader["sid"],),
    ).fetchone()
    assert n == 1
    # The worker resolves them from Graph.
    sql.execute(
        "UPDATE catalog.identity SET group_sids = %s, groups_source = 'graph',"
        " groups_resolved_at = now() WHERE sid = %s",
        ([w.g_readers], w.reader["sid"]),
    )
    assert b.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id).status_code == 200
    assert b.get("/v1/me").json()["groups_source"] == "graph"


def test_a_group_removed_in_ad_is_followed_before_the_next_sign_in(web, sql):
    """M7 review: a session's groups came from its token and lasted 10 hours.
    Groups older than `groups_refresh_every` are refreshed through the worker,
    and a newer Graph resolution replaces them, removals included."""
    from datetime import timedelta

    w = seed(sql)
    b = web.browser(w.reader)
    assert b.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id).status_code == 200
    # Old enough: a refresh is asked for (once per interval).
    web.services.settings.groups_refresh_every = timedelta(0)
    b.get("/v1/me")
    (n,) = sql.execute(
        "SELECT count(*) FROM pgmq.q_identity_resolve m JOIN catalog.identity i"
        " ON m.message->>'identity_id' = i.identity_id::text WHERE i.sid = %s",
        (w.reader["sid"],),
    ).fetchone()
    assert n >= 1
    web.services.settings.groups_refresh_every = timedelta(minutes=15)
    # The worker finds the user is no longer in Readers.
    sql.execute(
        "UPDATE catalog.identity SET group_sids = '{}', groups_source = 'graph',"
        " groups_resolved_at = now() + interval '1 second' WHERE sid = %s",
        (w.reader["sid"],),
    )
    r = b.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id)
    assert r.status_code == 404, r.text
    assert b.get("/v1/me").json()["groups_source"] == "graph"


# --- partner bearer tokens ----------------------------------------------------------------


def test_app_token_roles_and_refusals(web, sql):
    w = seed(sql)
    body = {"dataset_id": str(w.dataset_id), "body": {"mode": "scheduled", "archive_day": 365},
            "rationale": "cold data"}  # fmt: skip
    ok = web.app_token(["Policy.Propose"])
    assert web.call("POST", "/v1/policies/proposals", token=ok, json=body).status_code == 201
    # A role it doesn't have.
    r = web.call("POST", "/v1/recalls", token=ok, json={"file_id": str(w.root_id),
                                                         "priority": "standard"})  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    # Session-only routes refuse bearer tokens.
    assert web.call("GET", "/v1/shares", token=ok).status_code == 401


def _claims(web, **over):
    now = int(time.time())
    c = {
        "iss": web.idp.issuer,
        "aud": API_AUDIENCE,
        "tid": TENANT,
        "azp": APP_ID,
        "idtyp": "app",
        "roles": ["Policy.Propose"],
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}  # fmt: skip


@pytest.mark.parametrize(
    "over",
    [
        {"idtyp": None},  # a delegated (user) token
        {"aud": "api://someone-else"},
        {"tid": "another-tenant"},
        {"iss": "https://login.microsoftonline.com/x/v2.0"},
        {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
    ],
)
def test_bad_app_tokens_are_401(web, sql, over):
    w = seed(sql)
    token = web.idp.sign(_claims(web, **over))
    r = web.call("GET", "/v1/datasets/{dataset_id}/policies", token=token,
                 dataset_id=w.dataset_id)  # fmt: skip
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


def test_tokens_from_another_key_are_401(web, sql):
    w = seed(sql)
    rogue = DevIdp(web.idp.issuer, TENANT, {APP_ID: DevClient(APP_ID, "s")}, {}, API_AUDIENCE)
    for kid in (rogue.kid, web.idp.kid):
        rogue.kid = kid
        token = rogue.sign(_claims(web))
        r = web.call("GET", "/v1/datasets/{dataset_id}/policies", token=token,
                     dataset_id=w.dataset_id)  # fmt: skip
        assert r.status_code == 401
        # Why is logged, never told to the caller (M7 review).
        assert r.json()["detail"] in ("token rejected", "token signed by an unknown key"), r.text
    # alg=none is refused before any key lookup.
    import jwt

    token = jwt.encode(_claims(web), key=None, algorithm="none")
    assert web.call("GET", "/v1/datasets/{dataset_id}/policies", token=token,
                    dataset_id=w.dataset_id).status_code == 401  # fmt: skip


def test_a_valid_app_token_reads_policies(web, sql):
    w = seed(sql)
    token = web.idp.sign(_claims(web, roles=["Archive.Read"]))
    r = web.call("GET", "/v1/datasets/{dataset_id}/policies", token=token,
                 dataset_id=w.dataset_id)  # fmt: skip
    assert r.status_code == 200 and r.json()["items"] == []


def test_dev_issuer_and_secret_need_dev_mode():
    with pytest.raises(SystemExit):
        web_main.check_dev("dev", None, None)
    with pytest.raises(SystemExit):
        web_main.check_dev("entra", "secret", "0")
    web_main.check_dev("dev", "secret", "1")
    web_main.check_dev("entra", None, None)


def test_dev_idp_refuses_unknown_redirect_and_plain_pkce(web):
    base = {"client_id": "portal-client", "response_type": "code", "state": "s" * 32,
            "code_challenge": "c" * 43, "code_challenge_method": "S256",
            "redirect_uri": "https://evil.test/cb", "login_hint": "x"}  # fmt: skip
    assert web.client.get("/dev-idp/authorize", params=base).status_code == 400
    plain = {**base, "redirect_uri": f"{ORIGIN}/auth/callback", "code_challenge_method": "plain"}
    assert web.client.get("/dev-idp/authorize", params=plain).status_code == 400


def test_session_last_seen_is_refreshed(web, sql):
    w = seed(sql)
    b = web.browser(w.reader)
    sql.execute(
        "UPDATE catalog.web_session SET last_seen_at = now() - interval '5 minutes',"
        " idle_expires_at = now() + interval '1 minute' WHERE entra_object_id = %s",
        (w.reader["oid"],),
    )
    b.get("/v1/me")
    (idle,) = sql.execute(
        "SELECT idle_expires_at - now() FROM catalog.web_session WHERE entra_object_id = %s",
        (w.reader["oid"],),
    ).fetchone()
    assert idle > timedelta(minutes=55)


def test_unknown_routes_are_404(web, sql):
    w = seed(sql)
    b = web.browser(w.reader)
    for path in ("/v1/nothing", "/v1/sources-and-more"):
        r = b.get(path, check=False)
        assert r.status_code == 404 and r.json()["code"] == "not_found"
