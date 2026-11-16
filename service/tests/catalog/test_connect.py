"""The catalog connection (catalog/connect.py): settings, Entra tokens as passwords, and the pool.

The unit tests use a fake credential and never call Azure. The last test signs in to the test
server for real with a "token" as the password, and shows a new connection gets a renewed one.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from omnifile_service.api.db import Catalog
from omnifile_service.catalog import connect as cc

AZURE = {
    "OMNIFILE_PG_HOST": "omni-pg.postgres.database.azure.com",
    "OMNIFILE_PG_DATABASE": "omnifile",
    "OMNIFILE_PG_USER": "id-omni-api",
    "OMNIFILE_PG_SSLMODE": "verify-full",
    "AZURE_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
}


@dataclass
class Token:
    token: str
    expires_on: int


class FakeCredential:
    """Hands out tok-1, tok-2, ... each valid for `life` seconds from the fake clock's now."""

    def __init__(self, clock: list[float], life: int = 3600) -> None:
        self.clock, self.life, self.calls, self.scopes = clock, life, 0, []

    def get_token(self, *scopes: str) -> Token:
        self.calls += 1
        self.scopes.append(scopes)
        return Token(f"tok-{self.calls}", int(self.clock[0]) + self.life)


# --- settings -----------------------------------------------------------------------------------


def test_the_azure_settings_build_an_entra_connection_as_the_apps_group_role() -> None:
    db = cc.from_env("api", AZURE, credential=FakeCredential([0.0]))
    p = conninfo_to_dict(db.conninfo)
    assert p["host"] == AZURE["OMNIFILE_PG_HOST"] and p["port"] == "5432"
    assert p["dbname"] == "omnifile" and p["user"] == "id-omni-api"
    assert p["sslmode"] == "verify-full" and p["sslrootcert"]
    assert "-c role=omnifile_api" in p["options"]
    assert p["application_name"] == "omnifile-api"
    assert "password" not in p
    assert db.password is not None


@pytest.mark.parametrize("app", ["api", "enrol", "web", "worker"])
def test_every_app_acts_as_its_own_role(app: str) -> None:
    db = cc.from_env(app, AZURE, credential=FakeCredential([0.0]))
    assert f"-c role=omnifile_{app}" in conninfo_to_dict(db.conninfo)["options"]


def test_the_database_url_is_used_as_it_is_for_dev_and_tests() -> None:
    url = "postgresql://dev:pw@localhost:5432/omnifile"
    db = cc.from_env("worker", {"OMNIFILE_DATABASE_URL": url})
    assert db.conninfo == url and db.password is None and db.kwargs() == {}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "not configured"),
        ({**AZURE, "OMNIFILE_DATABASE_URL": "postgresql://x"}, "not both"),
        ({k: v for k, v in AZURE.items() if k != "OMNIFILE_PG_USER"}, "OMNIFILE_PG_USER"),
        ({k: v for k, v in AZURE.items() if k != "OMNIFILE_PG_DATABASE"}, "OMNIFILE_PG_DATABASE"),
        ({**AZURE, "OMNIFILE_PG_SSLMODE": "prefer"}, "OMNIFILE_PG_SSLMODE"),
        ({**AZURE, "OMNIFILE_PG_SSLMODE": "disable"}, "OMNIFILE_PG_SSLMODE"),
        ({**AZURE, "OMNIFILE_PG_PORT": "fivefourthreetwo"}, "OMNIFILE_PG_PORT"),
    ],
)
def test_bad_settings_are_refused(env: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        cc.from_env("api", env, credential=FakeCredential([0.0]))


def test_an_unknown_app_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown app"):
        cc.from_env("relay", AZURE, credential=FakeCredential([0.0]))


def test_require_and_a_given_root_certificate() -> None:
    db = cc.from_env(
        "web", {**AZURE, "OMNIFILE_PG_SSLMODE": "require"}, credential=FakeCredential([0.0])
    )
    assert conninfo_to_dict(db.conninfo)["sslmode"] == "require"
    db = cc.from_env(
        "web",
        {**AZURE, "OMNIFILE_PG_SSLROOTCERT": "/certs/azure.pem"},
        credential=FakeCredential([0.0]),
    )
    assert conninfo_to_dict(db.conninfo)["sslrootcert"] == "/certs/azure.pem"


def test_the_entry_points_exit_with_the_message(monkeypatch) -> None:
    for name in (*cc.AZURE_ENV, "OMNIFILE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="catalog: the catalog is not configured"):
        cc.from_env_or_exit("api")


# --- tokens ---------------------------------------------------------------------------------------


def test_a_token_is_reused_until_five_minutes_before_it_expires() -> None:
    clock = [1_000.0]
    cred = FakeCredential(clock, life=3600)
    password = cc.EntraPassword(cred, now=lambda: clock[0])
    assert password() == "tok-1"
    assert cred.scopes == [("https://ossrdbms-aad.database.windows.net/.default",)]
    clock[0] += 3600 - 301
    assert password() == "tok-1" and cred.calls == 1
    clock[0] += 2  # now 299 s left
    assert password() == "tok-2" and cred.calls == 2
    assert password() == "tok-2" and cred.calls == 2


def test_the_token_is_never_in_a_repr_or_a_log(caplog) -> None:
    clock = [0.0]
    db = cc.CatalogDb("host=x", cc.EntraPassword(FakeCredential(clock), now=lambda: clock[0]))
    caplog.set_level(logging.DEBUG)
    assert db.kwargs() == {"password": "tok-1"}
    logging.getLogger("omnifile_service").info("catalog %r %r", db, db.password)
    assert "tok-1" not in repr(db) and "tok-1" not in repr(db.password)
    assert "tok-1" not in caplog.text


def test_the_pool_asks_for_a_password_for_every_new_connection() -> None:
    clock = [0.0]
    cred = FakeCredential(clock, life=600)
    db = cc.CatalogDb("host=x", cc.EntraPassword(cred, now=lambda: clock[0]))
    catalog = Catalog(db)
    assert callable(catalog.pool.kwargs)

    async def go() -> list[dict]:
        first = await catalog.pool._resolve_kwargs()
        clock[0] += 400  # within five minutes of expiry
        second = await catalog.pool._resolve_kwargs()
        return [first, second]

    first, second = asyncio.run(go())
    assert first["password"] == "tok-1" and second["password"] == "tok-2"
    assert first["autocommit"] is False and "row_factory" in first


def test_a_connection_string_pool_keeps_static_kwargs() -> None:
    catalog = Catalog("host=x")
    assert isinstance(catalog.pool.kwargs, dict) and "password" not in catalog.pool.kwargs


# --- OMNIFILE_PG_POOL_MAX: each app's connection budget (main.bicep sets it at the small size)


def test_the_pool_size_is_the_default_until_it_is_set() -> None:
    assert cc.pool_max(10, {}) == 10
    assert cc.pool_max(10, {"OMNIFILE_PG_POOL_MAX": " "}) == 10
    assert cc.pool_max(None, {}) is None
    assert cc.pool_max(10, {"OMNIFILE_PG_POOL_MAX": "4"}) == 4
    assert cc.pool_max(None, {"OMNIFILE_PG_POOL_MAX": "100"}) == 100


@pytest.mark.parametrize("raw", ["0", "-1", "101", "four", "2.5"])
def test_a_bad_pool_size_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="OMNIFILE_PG_POOL_MAX"):
        cc.pool_max(10, {"OMNIFILE_PG_POOL_MAX": raw})


def test_an_entry_point_exits_on_a_bad_pool_size(monkeypatch) -> None:
    monkeypatch.setenv("OMNIFILE_PG_POOL_MAX", "0")
    with pytest.raises(SystemExit, match="catalog: OMNIFILE_PG_POOL_MAX"):
        cc.pool_max_or_exit(10)


def test_the_catalog_pool_takes_its_size() -> None:
    assert Catalog("host=x").pool.max_size == 10
    size = cc.pool_max(10, {"OMNIFILE_PG_POOL_MAX": "3"})
    assert Catalog("host=x", max_size=size).pool.max_size == 3


@pytest.mark.parametrize("module", ["api", "web", "enrol"])
def test_each_pooled_app_sizes_its_pool_from_the_setting(module: str) -> None:
    """api, web and enrol build their Catalog with OMNIFILE_PG_POOL_MAX (default 10)."""
    from pathlib import Path

    import omnifile_service

    src = (Path(omnifile_service.__file__).parent / module / "__main__.py").read_text()
    calls = [c.split("\n", 1)[0] for c in src.split("Catalog(")[1:]]
    assert calls, f"{module} builds no Catalog"
    for call in calls:
        assert "max_size=pool_max_or_exit(DEFAULT_POOL_MAX)" in call, (
            f"{module}: a Catalog without the pool setting: Catalog({call}"
        )


# --- for real, against the test server ----------------------------------------------------


def test_a_token_signs_in_and_a_new_connection_gets_the_renewed_one(migrated_url: str) -> None:
    """The test server stands in for Azure: the login's password is the current "token"."""
    login = f"entra_{uuid.uuid4().hex[:8]}"
    clock = [0.0]
    cred = FakeCredential(clock, life=600)
    with psycopg.connect(migrated_url, autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {login} LOGIN PASSWORD 'tok-1'")
        admin.execute(f"GRANT omnifile_api TO {login}")
    try:
        p = conninfo_to_dict(migrated_url)
        env = {
            "OMNIFILE_PG_HOST": p.get("host", "localhost"),
            "OMNIFILE_PG_PORT": str(p.get("port", "5432")),
            "OMNIFILE_PG_DATABASE": p["dbname"],
            "OMNIFILE_PG_USER": login,
            "OMNIFILE_PG_SSLMODE": "disable",
            "OMNIFILE_DEV_MODE": "1",
        }
        db = cc.from_env("api", env, credential=cred)
        db = cc.CatalogDb(db.conninfo, cc.EntraPassword(cred, now=lambda: clock[0]))
        with cc.connect(db) as conn:
            who = conn.execute("SELECT session_user, current_user").fetchone()
        assert who == (login, "omnifile_api")

        # The token is renewed; the server now only knows the new one.
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(f"ALTER ROLE {login} PASSWORD 'tok-2'")
        clock[0] += 400

        async def through_the_pool() -> int:
            catalog = Catalog(db, max_size=1)
            try:
                async with catalog.read() as conn:
                    cur = await conn.execute("SELECT count(*) AS n FROM catalog.edge")
                    row = await cur.fetchone()
                return row["n"]
            finally:
                await catalog.close()

        assert asyncio.run(through_the_pool()) >= 0
        assert cred.calls == 2

        async def async_connect() -> str:
            async with await cc.aconnect(db) as conn:
                return (await (await conn.execute("SELECT current_user")).fetchone())[0]

        assert asyncio.run(async_connect()) == "omnifile_api"
    finally:
        with psycopg.connect(migrated_url, autocommit=True) as admin:
            admin.execute(f"DROP OWNED BY {login}")
            admin.execute(f"DROP ROLE {login}")
