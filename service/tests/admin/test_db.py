"""The administrator's catalog connection: `catalog/connect.py`'s path, without
an app's group role, with the administrator's own Entra token."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import pytest
from psycopg.conninfo import conninfo_to_dict

from omnifile_service.admin.db import admin_db, jwt_claims, signed_in_as
from omnifile_service.catalog.connect import SCOPE

from .conftest import run_cli


@dataclass
class _Token:
    token: str
    expires_on: float


class FakeCredential:
    def __init__(self, token: str = "fake:admin-token") -> None:
        self.scopes: list[tuple[str, ...]] = []
        self.token = token

    def get_token(self, *scopes: str) -> _Token:
        self.scopes.append(scopes)
        return _Token(self.token, 4_000_000_000.0)


def fake_jwt(claims: object) -> str:
    """An unsigned token with Entra's claim names (fake: never a real token)."""

    def b64(d: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.fake-signature"


ALICE = {
    "oid": "5f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f",
    "upn": "alice.admin@corp.example",
    "tid": "0a0b0c0d-1e1f-4a2b-8c3d-4e5f6a7b8c9d",
    "aud": "https://ossrdbms-aad.database.windows.net",
}


AZURE = {
    "OMNIFILE_PG_HOST": "omni-pg.postgres.database.azure.com",
    "OMNIFILE_PG_DATABASE": "omnifile",
    "OMNIFILE_PG_USER": "OmniFile-DB-Admins",
}


def test_a_connection_string_is_used_as_it_is_as_the_migrations_use_it() -> None:
    url = "host=pg dbname=omnifile user=OmniFile-DB-Admins sslmode=verify-full"
    db = admin_db({"OMNIFILE_DATABASE_URL": url})
    assert db.conninfo == url and db.password is None


def test_entra_sign_in_is_the_administrators_own_and_acts_as_no_app() -> None:
    cred = FakeCredential()
    db = admin_db(AZURE, credential=cred)
    params = conninfo_to_dict(db.conninfo)
    assert "role=" not in params.get("options", ""), params
    assert params["user"] == "OmniFile-DB-Admins"
    assert params["host"] == AZURE["OMNIFILE_PG_HOST"]
    assert params["sslmode"] == "verify-full"
    assert params["application_name"] == "omnifile-admin"
    assert db.kwargs() == {"password": "fake:admin-token"}
    assert cred.scopes == [(SCOPE,)]
    assert "fake:admin-token" not in repr(db)


def test_the_apps_checks_still_apply() -> None:
    with pytest.raises(ValueError, match="not both"):
        admin_db({**AZURE, "OMNIFILE_DATABASE_URL": "host=pg"}, credential=FakeCredential())
    with pytest.raises(ValueError, match="not configured"):
        admin_db({})
    with pytest.raises(ValueError, match="OMNIFILE_PG_SSLMODE"):
        admin_db({**AZURE, "OMNIFILE_PG_SSLMODE": "disable"}, credential=FakeCredential())


def test_without_a_catalog_the_cli_says_so_after_checking_the_answers() -> None:
    ran = run_cli("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch01",
                  env={})  # fmt: skip
    assert ran.code == 2 and "catalog:" in ran.err, ran
    ran = run_cli("backend", "add", "azure", "--live", "omnilive01", env={})
    assert ran.code == 1 and "needs both" in ran.err, ran


def test_the_tokens_claims_name_who_signed_in() -> None:
    assert jwt_claims(fake_jwt(ALICE)) == {k: ALICE[k] for k in ("oid", "upn", "tid")}
    # A managed identity (a job): no upn, its app id.
    assert jwt_claims(fake_jwt({"oid": "o-1", "appid": "a-1"})) == {"oid": "o-1", "appid": "a-1"}
    assert jwt_claims(fake_jwt({"preferred_username": "bob@corp.example"})) == {
        "upn": "bob@corp.example"
    }
    not_tokens = [None, "", "omnifile", "a.b", "a.!!!.c", "a.bm90IGpzb24.c",
                  fake_jwt([1]), fake_jwt({"oid": 7, "upn": ""})]  # fmt: skip
    for t in not_tokens:
        assert jwt_claims(t) == {}, t


def test_signed_in_as_reads_the_token_libpq_signs_in_with() -> None:
    token = fake_jwt(ALICE)
    entra = admin_db(AZURE, credential=FakeCredential(token))
    assert signed_in_as(entra, AZURE)["upn"] == ALICE["upn"]
    url = "host=pg dbname=omnifile user=OmniFile-DB-Admins"
    dsn = admin_db({"OMNIFILE_DATABASE_URL": url})
    assert signed_in_as(dsn, {"PGPASSWORD": token})["oid"] == ALICE["oid"]
    assert signed_in_as(dsn, {}) == {}
    # The connection string's own password comes first, as libpq takes it.
    own = admin_db({"OMNIFILE_DATABASE_URL": f"{url} password=omnifile"})
    assert signed_in_as(own, {"PGPASSWORD": token}) == {}
