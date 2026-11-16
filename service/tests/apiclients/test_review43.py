"""The security review of #43: each finding, as the reviewer's probes showed it,
now refused. Each test fails without its fix."""

from __future__ import annotations

import logging
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omnifile_service.apiclients import keys
from omnifile_service.catalog.privileges import as_app

from .support import person, propose, register, world


def _iid(sql, user) -> int:
    (iid,) = sql.execute(
        "SELECT identity_id FROM catalog.identity WHERE sid = %s", (user["sid"],)
    ).fetchone()
    return int(iid)


def _key(browser, c) -> str:
    r = browser.post(
        "/v1/settings/api-clients/{api_client_id}/keys", {}, api_client_id=c["api_client_id"]
    )
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _approve(browser, pid):
    return browser.post(
        "/v1/policies/proposals/{proposal_id}/approve", {"note": "ok"}, proposal_id=pid
    )


# --- 1. self-approval through a client or a minted key ------------------------------


def test_the_owner_cannot_approve_what_their_own_key_proposed(web, sql) -> None:
    w, admin = world(web, sql)
    me = _iid(sql, w.admin)
    c = register(admin, f"key-{uuid.uuid4().hex[:10]}", me, ["Policy.Propose"])
    key = _key(admin, c)
    r = propose(web, w, key)
    assert r.status_code == 201, r.text
    pid = r.json()["proposal_id"]
    kid = key.split(".")[1]
    row = sql.execute(
        "SELECT api_client_id, owner_identity_id, key_id FROM catalog.policy_proposal"
        " WHERE proposal_id = %s",
        (int(pid),),
    ).fetchone()
    assert row == (int(c["api_client_id"]), me, kid)
    r = _approve(admin, pid)
    assert r.status_code == 403 and r.json()["code"] == "self_approval", r.text


def test_neither_the_registrant_nor_the_key_maker_approves(web, sql) -> None:
    w, admin = world(web, sql)
    admin2 = web.browser(w.admin2)
    owner = person(sql, w, "olga", roles="{DataSteward}")
    c = register(admin2, f"key-{uuid.uuid4().hex[:10]}", owner, ["Policy.Propose"])
    key = _key(admin, c)  # admin makes the key; admin2 registered the client
    pid = propose(web, w, key).json()["proposal_id"]
    for who in (admin, admin2):
        r = _approve(who, pid)
        assert r.status_code == 403 and r.json()["code"] == "self_approval", r.text
    third = web.browser(w.user("admin3", roles=["ArchiveAdmin"], sid_rid=3010))
    r = _approve(third, pid)
    assert r.status_code == 200, r.text


def test_the_catalog_refuses_it_too(web, sql) -> None:
    w, admin = world(web, sql)
    me = _iid(sql, w.admin)
    c = register(admin, f"key-{uuid.uuid4().hex[:10]}", me, ["Policy.Propose"])
    pid = propose(web, w, _key(admin, c)).json()["proposal_id"]
    with pytest.raises(psycopg.Error, match="can't approve it") as e:
        sql.execute(
            "UPDATE catalog.policy_proposal SET state = 'approved', decided_by_identity_id = %s,"
            " decided_at = now() WHERE proposal_id = %s",
            (me, int(pid)),
        )
    assert e.value.sqlstate == "OF010"


# --- 2. approval checks the client and its owner again ------------------------------------


def test_approval_rechecks_the_client_and_its_owner(web, sql) -> None:
    w, admin = world(web, sql)
    owner = person(sql, w, "olga", roles="{DataSteward}")
    app = f"app-{uuid.uuid4().hex[:10]}"
    c = register(admin, app, owner, ["Policy.Propose"])
    token = web.idp.app_token(app, ["Policy.Propose"])
    approver = web.browser(w.admin2)

    def etag() -> str:
        return admin.get(
            "/v1/settings/api-clients/{api_client_id}", api_client_id=c["api_client_id"]
        ).json()["etag"]

    def enabled(on: bool) -> None:
        r = admin.call(
            "PATCH",
            "/v1/settings/api-clients/{api_client_id}",
            json={"enabled": on},
            headers={"If-Match": etag()},
            api_client_id=c["api_client_id"],
        )
        assert r.status_code == 200, r.text

    pid = propose(web, w, token).json()["proposal_id"]
    enabled(False)
    r = _approve(approver, pid)
    assert r.status_code == 403 and r.json()["code"] == "client_disabled", r.text
    enabled(True)
    # The owner lost the role since: refused at approval too.
    sql.execute("UPDATE catalog.identity SET app_roles = '{}' WHERE identity_id = %s", (owner,))
    r = _approve(approver, pid)
    assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access", r.text
    # Revoked: the pending proposal shows as stale, and can't be approved.
    sql.execute(
        "UPDATE catalog.identity SET app_roles = '{DataSteward}' WHERE identity_id = %s", (owner,)
    )
    admin.post(
        "/v1/settings/api-clients/{api_client_id}/revoke", {}, api_client_id=c["api_client_id"]
    )
    p = approver.get("/v1/policies/proposals/{proposal_id}", proposal_id=pid).json()
    assert p["stale"] is True and p["stale_reason"] == "client_revoked"
    r = _approve(approver, pid)
    assert r.status_code == 403 and r.json()["code"] == "client_disabled"


# --- 3. refused keys are throttled per address and audited in aggregate ------------------


def test_junk_keys_are_throttled_and_audited_in_aggregate(web, sql) -> None:
    (before,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'api_key.refused'"
    ).fetchone()
    codes = []
    for _ in range(200):
        r = web.call("GET", "/v1/edges", token=f"ofk1.{uuid.uuid4().hex[:16]}.{'A' * 43}")
        codes.append(r.status_code)
    (after,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'api_key.refused'"
    ).fetchone()
    refused = codes.count(401)
    assert set(codes) == {401, 429} and refused <= 25, (refused, set(codes))
    assert codes[-1] == 429
    # One row for the address's unknown keys in this window, not one per key.
    assert after - before == 1
    # The next window's row counts what this one held back.
    gate = web.services.auth.key_refusals
    gate._rows = {k: (v[0] - gate.WINDOW, v[1]) for k, v in gate._rows.items()}
    gate._tokens.clear()
    web.call("GET", "/v1/edges", token=f"ofk1.{uuid.uuid4().hex[:16]}.{'A' * 43}")
    (count,) = sql.execute(
        "SELECT details->>'count' FROM catalog.audit_log WHERE action = 'api_key.refused'"
        " ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    assert int(count) == refused


# --- 4. web writes backends only through the owner-run function ----------------------------


def test_web_cannot_write_backends_but_through_backend_register(db_url, sql) -> None:
    name = f"evil-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(as_app(db_url, "web"), autocommit=True) as c:
        for q in (
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            f" VALUES ('{name}', 'azure_blob', '{{}}', false)",
            'UPDATE catalog.backend SET config = config || \'{"primary_account": "x"}\'',
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(q)
        got = c.execute(
            "SELECT out_outcome FROM catalog.backend_register(%s, 's3', %s, 'user:1', NULL,"
            " NULL, '{}')",
            (name, Jsonb({"account_id": "123456789012", "region": "eu-west-1", "buckets": ["a"]})),
        ).fetchone()
        assert got == ("added",)
        # Only a person, and the row is always private.
        with pytest.raises(psycopg.Error, match="signed-in person"):
            c.execute(
                "SELECT * FROM catalog.backend_register(%s, 's3', '{}', 'app:x', NULL, NULL, '{}')",
                (name + "x",),
            )
    (private,) = sql.execute(
        "SELECT private_network FROM catalog.backend WHERE name = %s", (name,)
    ).fetchone()
    assert private is True
    # No app's session writes a public row, even the worker's (which may write the table).
    with psycopg.connect(as_app(db_url, "worker"), autocommit=True) as c:
        with pytest.raises(psycopg.Error, match="never writes a public backend"):
            c.execute(
                "INSERT INTO catalog.backend (name, kind, config, private_network)"
                " VALUES (%s, 's3', '{}', false)",
                (name + "-pub",),
            )


def test_what_data_uses_never_moves_whoever_writes(web, sql) -> None:
    w, _admin = world(web, sql)
    bid = w.backend_id  # the dataset's home
    with pytest.raises(psycopg.Error, match="holds data"):
        sql.execute(
            'UPDATE catalog.backend SET config = config || \'{"container": "other/data"}\''
            " WHERE backend_id = %s",
            (bid,),
        )
    with pytest.raises(psycopg.Error, match="holds data"):
        sql.execute(
            'UPDATE catalog.backend SET config = config || \'{"primary_account": "x"}\''
            " WHERE backend_id = %s",
            (bid,),
        )
    # An inventory rule may be added: it moves nothing.
    sql.execute(
        'UPDATE catalog.backend SET config = config || \'{"inventory": {"rule": "r"}}\''
        " WHERE backend_id = %s",
        (bid,),
    )
    # An S3 row under data may gain buckets, never lose one.
    (s3,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network) VALUES"
        " (%s, 's3', %s, true) RETURNING backend_id",
        (f"s3-{uuid.uuid4().hex[:8]}", Jsonb({"account_id": "1", "buckets": ["a", "b"]})),
    ).fetchone()
    sql.execute("UPDATE catalog.dataset SET source_backend_id = %s WHERE dataset_id = %s",
                (s3, w.dataset_id))  # fmt: skip
    sql.execute(
        'UPDATE catalog.backend SET config = jsonb_set(config, \'{buckets}\', \'["a","b","c"]\')'
        " WHERE backend_id = %s",
        (s3,),
    )
    with pytest.raises(psycopg.Error, match="never dropped"):
        sql.execute(
            "UPDATE catalog.backend SET config = jsonb_set(config, '{buckets}', '[\"a\"]')"
            " WHERE backend_id = %s",
            (s3,),
        )


# --- 5. a percent-encoded key never reaches a log line --------------------------------


@pytest.mark.parametrize(
    "encode",
    [
        lambda k: "%6F" + k[1:],
        lambda k: k.replace(".", "%2E"),
        lambda k: k.replace(".", "%252E"),
    ],
)
def test_a_percent_encoded_key_is_redacted(encode) -> None:
    key = keys.generate().key
    secret = key.split(".")[2]
    f = keys.RedactKeys()
    for path in (f"/a?k={encode(key)}", f"/a/{encode(key)}"):
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/1.1" %d',
            ("127.0.0.1", "GET", path, 200), None,
        )  # fmt: skip
        f.filter(rec)
        assert secret not in rec.getMessage(), rec.getMessage()
        other = logging.LogRecord("x", logging.WARNING, "x", 1, "saw %s", (path,), None)
        f.filter(other)
        assert secret not in other.getMessage(), other.getMessage()
