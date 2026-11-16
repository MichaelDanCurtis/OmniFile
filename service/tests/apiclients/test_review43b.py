"""The re-review of #43 (at 071b190): each remaining finding, now refused. Each
test fails without its fix."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from omnifile_service.admin import backends as admin

from .support import person, register, world

MIGRATION = (
    Path(admin.__file__).resolve().parents[1] / "catalog" / "migrations"
    / "0035_settings_api_clients.sql"
)  # fmt: skip
REGISTER = (
    "SELECT out_outcome FROM catalog.backend_register(%s, %s, %s::jsonb, 'user:1', NULL,"
    " NULL, '{}')"
)


def _in_use(sql, w, kind: str, config: dict) -> tuple[int, str]:
    name = f"b-{uuid.uuid4().hex[:8]}"
    (bid,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, %s, %s, true) RETURNING backend_id",
        (name, kind, Jsonb(config)),
    ).fetchone()
    sql.execute(
        "UPDATE catalog.dataset SET source_backend_id = %s WHERE dataset_id = %s",
        (bid, w.dataset_id),
    )
    return int(bid), name


# --- 1. nothing changes under data but an allow-list, on every path ------------------------


AZURE = {"layout": "single_account", "accounts": {"a1": "https://a1.blob.core.windows.net"},
         "primary_account": "a1", "container": "a1/data"}  # fmt: skip
S3 = {"account_id": "123456789012", "region": "eu-west-1", "buckets": ["a"]}


@pytest.mark.parametrize(
    ("kind", "config", "change"),
    [
        ("azure_blob", AZURE, {"versioning": False}),
        ("azure_blob", AZURE, {"recall_container": "elsewhere"}),
        ("azure_blob", AZURE, {"worm": True}),
        ("s3", S3, {"versioning": False}),
        ("s3", S3, {"archive_class": "DEEP_ARCHIVE"}),
        ("s3", S3, {"region": "us-east-1"}),
    ],
)
def test_no_key_outside_the_allow_list_changes_under_data(web, sql, db_url, kind, config, change):
    w, _admin = world(web, sql)
    _bid, name = _in_use(sql, w, kind, config)
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("SET ROLE omnifile_web")
        with pytest.raises(psycopg.Error, match="holds data"):
            c.execute(REGISTER, (name, kind, Jsonb({**config, **change})))
    # The CLI's own check says the same.
    assert admin._change_under_data(kind, config, {**config, **change})


def test_what_only_adds_or_informs_may_change_under_data(web, sql, db_url) -> None:
    w, _admin = world(web, sql)
    _bid, name = _in_use(sql, w, "s3", S3)
    more = {**S3, "buckets": ["a", "b"], "inventory": {"rule": "r"}, "display_name": "Raw"}
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("SET ROLE omnifile_web")
        assert c.execute(REGISTER, (name, "s3", Jsonb(more))).fetchone() == ("updated",)
    assert admin._change_under_data("s3", S3, more) is None


def test_the_cli_and_the_catalog_share_one_allow_list() -> None:
    text = MIGRATION.read_text()
    body = text.split("CREATE FUNCTION catalog.backend_guard()", 1)[1].split("$$;", 1)[0]
    anywhere = set(re.findall(r"k IN \(([^)]*)\)", body)[0].replace("'", "").replace(" ", "")
                   .split(","))  # fmt: skip
    azure_only = set(re.findall(r"k = '([a-z_]+)' AND OLD.kind = 'azure_blob'", body))
    additive = set(re.findall(r"k = '([a-z_]+)' AND OLD.kind = 's3'", body))
    assert admin.UNDER_DATA_MAY_CHANGE == {
        "azure_blob": frozenset(anywhere | azure_only),
        "s3": frozenset(anywhere),
    }
    assert admin.UNDER_DATA_ADDITIVE == {"s3": frozenset(additive)}


# --- 2. an app's login that resets its role still can't write a public row -----------------


def test_an_app_login_that_resets_its_role_is_still_an_app(sql, db_url) -> None:
    login = f"r43w_{uuid.uuid4().hex[:6]}"
    sql.execute(pgsql.SQL("CREATE ROLE {} LOGIN PASSWORD 'x'").format(pgsql.Identifier(login)))
    try:
        sql.execute(pgsql.SQL("GRANT omnifile_worker TO {}").format(pgsql.Identifier(login)))
        p = conninfo_to_dict(db_url)
        p.update(user=login, password="x")
        with psycopg.connect(make_conninfo(**p), autocommit=True,
                             options="-c role=omnifile_worker") as c:  # fmt: skip
            c.execute("SET ROLE NONE")
            with pytest.raises(psycopg.Error, match="never writes a public backend"):
                c.execute(
                    "INSERT INTO catalog.backend (name, kind, config, private_network)"
                    " VALUES (%s, 'azure_blob', '{}', false)",
                    (f"pub-{login}",),
                )
    finally:
        sql.execute(pgsql.SQL("DROP OWNED BY {}").format(pgsql.Identifier(login)))
        sql.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(login)))


# --- 3. a valid key gets in while its address is throttled ---------------------------------


def test_a_valid_key_gets_in_after_junk_from_the_same_address(web, sql) -> None:
    w, admin_b = world(web, sql)
    owner = person(sql, w, "kim", roles="{DataSteward}")
    c = register(admin_b, f"key-{uuid.uuid4().hex[:10]}", owner, ["Archive.Read"])
    key = admin_b.post(
        "/v1/settings/api-clients/{api_client_id}/keys", {}, api_client_id=c["api_client_id"]
    ).json()["key"]
    codes = [
        web.call("GET", "/v1/edges", token=f"ofk1.{uuid.uuid4().hex[:16]}.{'A' * 43}").status_code
        for _ in range(40)
    ]
    assert codes[-1] == 429
    assert web.call("GET", "/v1/edges", token=key).status_code == 200
    # Junk keeps being throttled, and a malformed key too.
    assert web.call("GET", "/v1/edges", token="ofk1.nope").status_code == 429
