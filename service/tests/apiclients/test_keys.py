"""OmniFile-issued API keys (portal-api.md 2.2.1; owner decision 2026-11-04):
shown once, kept as a hash, expiring, rotated, revoked at once, per-key rate
limits, and never in a URL, a log line or the audit."""

from __future__ import annotations

import hashlib
import logging
import uuid

import pytest

from omnifile_service.apiclients import keys
from omnifile_service.web.context import TokenBuckets

from .support import person, propose, register, world


def _client(web, sql, scopes, *, owner_roles="{DataSteward}"):
    w, admin = world(web, sql)
    owner = person(sql, w, f"owner{uuid.uuid4().hex[:4]}", roles=owner_roles)
    c = register(admin, f"key-{uuid.uuid4().hex[:10]}", owner, scopes)
    return w, admin, c


def _key(admin, c, **body):
    r = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys", body, api_client_id=c["api_client_id"]
    )
    assert r.status_code == 201, r.text
    return r.json()


def _refusals(sql, key_id):
    return [
        r
        for (r,) in sql.execute(
            "SELECT details->>'reason' FROM catalog.audit_log WHERE action = 'api_key.refused'"
            " AND details->>'key_id' = %s ORDER BY audit_id",
            (key_id,),
        )
    ]


class Logs:
    """The service's log lines (not the test client's own: httpx logs the URLs it sends)."""

    def __init__(self, caplog) -> None:
        self.caplog = caplog

    @property
    def text(self) -> str:
        return "\n".join(
            r.getMessage()
            for r in self.caplog.records
            if not r.name.startswith(("httpx", "httpcore"))
        )


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return Logs(caplog)


def test_a_key_is_shown_once_kept_as_a_hash_and_works(web, sql, logs) -> None:
    w, admin, c = _client(web, sql, ["Archive.Read", "Policy.Propose"])
    issued = _key(admin, c, expires_in_days=30)
    key, k = issued["key"], issued["api_key"]
    assert keys.parse(key) and k["state"] == "active" and k["scopes"] == c["scopes"]
    secret = key.split(".")[2]
    stored = sql.execute(
        "SELECT secret_sha256 FROM catalog.api_key WHERE key_id = %s", (k["key_id"],)
    ).fetchone()[0]
    assert bytes(stored) == hashlib.sha256(secret.encode()).digest()
    # The key is nowhere in the catalog: not in the audit, not in the idempotency store.
    dump = sql.execute(
        "SELECT string_agg(t::text, ' ') FROM (SELECT details FROM catalog.audit_log"
        " UNION ALL SELECT to_jsonb(i) FROM catalog.idempotency_key i) t"
    ).fetchone()[0]
    assert secret not in dump and k["key_id"] in dump
    # Listed without the key.
    got = admin.get("/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"])
    assert secret not in got.text and got.json()["keys"][0]["key_id"] == k["key_id"]
    # It works, as the client, with the owner's rule (the owner is a DataSteward).
    r = web.call("GET", "/v1/edges", token=key)
    assert r.status_code == 200, r.text
    assert propose(web, w, key).status_code == 201
    used = admin.get(
        "/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"]
    ).json()["keys"][0]
    assert used["last_used_at"] is not None and used["last_used_addr"] == "10.1.2.3"
    # Its calls are audited with the key's id, never the key.
    d = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'policy.propose'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()[0]
    assert d["api_client"]["key_id"] == k["key_id"]
    # And no log line holds it.
    assert secret not in logs.text


def test_create_and_rotate_are_never_replayed(web, sql) -> None:
    _w, admin, c = _client(web, sql, ["Archive.Read"])
    same = str(uuid.uuid4())
    a = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys",
        {},
        key=same,
        api_client_id=c["api_client_id"],
    ).json()
    b = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys",
        {},
        key=same,
        api_client_id=c["api_client_id"],
    ).json()
    assert a["key"] != b["key"]


def test_expired_revoked_wrong_unknown_and_disabled_keys_are_refused(web, sql) -> None:
    _w, admin, c = _client(web, sql, ["Archive.Read"])
    key = _key(admin, c)["key"]
    _prefix, kid, secret = key.split(".")
    # A wrong secret for a real id.
    wrong = f"ofk1.{kid}.{'A' * 43}"
    r = web.call("GET", "/v1/edges", token=wrong)
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    # An unknown id, and a malformed key.
    assert web.call("GET", "/v1/edges", token=f"ofk1.{'0' * 16}.{secret}").status_code == 401
    assert web.call("GET", "/v1/edges", token="ofk1.nope").status_code == 401
    # Expired.
    sql.execute(
        "UPDATE catalog.api_key SET created_at = now() - interval '2 days',"
        " expires_at = now() - interval '1 day' WHERE key_id = %s",
        (kid,),
    )
    assert web.call("GET", "/v1/edges", token=key).status_code == 401
    # Revoked.
    k2 = _key(admin, c)
    r = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/revoke",
        {},
        api_client_id=c["api_client_id"],
        key_id=k2["api_key"]["key_id"],
    )
    assert r.status_code == 200 and r.json()["state"] == "revoked"
    assert web.call("GET", "/v1/edges", token=k2["key"]).status_code == 401
    # A disabled client's keys stop with it.
    k3 = _key(admin, c)
    assert web.call("GET", "/v1/edges", token=k3["key"]).status_code == 200
    etag = admin.get(
        "/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"]
    ).json()["etag"]
    admin.call(
        "PATCH",
        "/v1/settings/api-clients/{api_client_id}",
        json={"enabled": False},
        headers={"If-Match": etag},
        api_client_id=c["api_client_id"],
    )
    assert web.call("GET", "/v1/edges", token=k3["key"]).status_code == 401
    # Every refusal is audited by the key's public id, with why.
    assert _refusals(sql, kid) == ["wrong_secret", "expired"]
    assert _refusals(sql, k2["api_key"]["key_id"]) == ["revoked"]
    assert _refusals(sql, k3["api_key"]["key_id"]) == ["client_disabled"]
    assert _refusals(sql, "0" * 16) == ["unknown"]


def test_a_wrong_scope_key_and_an_owner_without_access_are_refused(web, sql) -> None:
    w, admin, c = _client(web, sql, ["Archive.Read", "Policy.Propose"], owner_roles="{}")
    read_only = _key(admin, c, scopes=["Archive.Read"])["key"]
    r = propose(web, w, read_only)
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    # A key can't have a scope its client lacks.
    r = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys",
        {"scopes": ["Archive.Adopt"]},
        api_client_id=c["api_client_id"],
    )
    assert r.status_code == 400
    # The owner has no role and owns no share: a key-signed proposal is refused too.
    proposer = _key(admin, c, scopes=["Policy.Propose"])["key"]
    r = propose(web, w, proposer)
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"


def test_rotation_keeps_the_old_key_for_the_overlap_only(web, sql) -> None:
    _w, admin, c = _client(web, sql, ["Archive.Read"])
    old = _key(admin, c)
    r = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/rotate",
        {"overlap_hours": 2},
        api_client_id=c["api_client_id"],
        key_id=old["api_key"]["key_id"],
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["api_key"]["rotated_from"] == old["api_key"]["key_id"]
    assert new["previous"]["expires_at"] < old["api_key"]["expires_at"]
    assert web.call("GET", "/v1/edges", token=new["key"]).status_code == 200
    assert web.call("GET", "/v1/edges", token=old["key"]).status_code == 200  # the overlap
    # Rotating again with no overlap ends the old one now.
    r = admin.post(
        "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/rotate",
        {"overlap_hours": 0},
        api_client_id=c["api_client_id"],
        key_id=new["api_key"]["key_id"],
    )
    assert r.status_code == 201
    assert web.call("GET", "/v1/edges", token=new["key"]).status_code == 401
    # Rotation and creation need a recent sign-in; revoking doesn't.
    w = _w
    stale = web.browser(w.user("admin", roles=["ArchiveAdmin"], sid_rid=3003, no_auth_time=True))
    r = stale.post(
        "/v1/settings/api-clients/{api_client_id}/keys", {}, api_client_id=c["api_client_id"]
    )
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    acts = [
        a
        for (a,) in sql.execute(
            "SELECT action FROM catalog.audit_log WHERE details->>'api_client_id' = %s"
            " AND action IN ('api_key.create', 'api_key.rotate', 'api_key.revoke')"
            " ORDER BY audit_id",
            (c["api_client_id"],),
        )
    ]
    assert acts == ["api_key.create", "api_key.rotate", "api_key.rotate"]


def test_a_key_in_the_url_is_refused_and_never_logged(web, sql, logs) -> None:
    _w, admin, c = _client(web, sql, ["Archive.Read"])
    key = _key(admin, c)["key"]
    secret = key.split(".")[2]
    for params in ({"api_key": key}, {"x": key.replace(".", "%2E")}):
        web.client.cookies.clear()
        r = web.client.get("/v1/edges", params=params)
        assert r.status_code == 400 and "Authorization header" in r.json()["detail"]
    # Only in Authorization, as Bearer.
    r = web.client.get("/v1/edges", headers={"Authorization": f"ApiKey {key}"})
    assert r.status_code == 401
    assert secret not in logs.text
    # The redaction filter takes keys out of any record, uvicorn's access line included.
    f = keys.RedactKeys()
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "x",
        1,
        '%s - "%s %s HTTP/1.1" %d',
        ("10.0.0.1", "GET", f"/v1/edges?api_key={key}", 400),
        None,
    )
    # uvicorn's access line loses its query string (review of #43).
    assert f.filter(rec) and secret not in rec.getMessage() and "?" not in rec.getMessage()
    rec = logging.LogRecord("x", logging.WARNING, "x", 1, "token %s", (f"Bearer {key}",), None)
    f.filter(rec)
    assert secret not in rec.getMessage()


def test_rate_limits_are_per_key(web, sql) -> None:
    _w, admin, c = _client(web, sql, ["Archive.Read"])
    a, b = _key(admin, c)["key"], _key(admin, c)["key"]
    web.services.limiter = TokenBuckets(rate=0.001, burst=1)
    assert web.call("GET", "/v1/edges", token=a).status_code == 200
    assert web.call("GET", "/v1/edges", token=a).status_code == 429
    assert web.call("GET", "/v1/edges", token=b).status_code == 200


def test_key_format_and_constant_time_match() -> None:
    k = keys.generate()
    kid, secret = keys.parse(k.key)  # type: ignore[misc]
    assert kid == k.key_id and len(secret) == 43 and keys.matches(secret, k.secret_sha256)
    assert not keys.matches("A" * 43, k.secret_sha256)
    assert keys.parse("ofk1." + kid + "." + secret + "x") is None
    assert keys.in_query("x=ofk1.0123456789abcdef.zz") and not keys.in_query("path=/ofk1-data")
