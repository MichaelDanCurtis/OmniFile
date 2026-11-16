"""`python -m omnifile_service.admin backend add|list|show` against a real catalog.

A backend registered here is one the service builds and routes files with; the
same answers again change nothing; a row under data never moves; and every
refusal AGENTS.md asks for (private networking, the live/archive pair, S3 as a
source only, OmniFile's own buckets) holds before anything is written.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from omnifile_service.admin import backends
from omnifile_service.admin.cli import main
from omnifile_service.api.data import upload_container
from omnifile_service.backends.azure.config import AzureBlobConfig
from omnifile_service.backends.s3.config import AwsDeployment, S3Config
from omnifile_service.catalog.privileges import as_app
from omnifile_service.web.routes_sources import resolve_inline
from omnifile_service.worker.tiering import archive_container

from .conftest import Run, run_cli
from .test_db import ALICE, FakeCredential, fake_jwt

ADD_AZURE = ("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch01")

ACCT = "123456789012"
AUDIENCE = "api://omnifile-test-service"
ACCOUNT: dict[str, Any] = {
    "region": "eu-west-1",
    "endpoints": {
        "s3": "https://bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com",
        "s3_control": "https://control.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com",
        "sts": "https://vpce-0123.sts.eu-west-1.vpce.amazonaws.com",
        "sqs": "https://vpce-0def.sqs.eu-west-1.vpce.amazonaws.com",
    },
    "role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-service",
    "event_queues": {"*": f"https://sqs.eu-west-1.amazonaws.com/{ACCT}/omnifile-events"},
    "batch": {
        "role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-batch",
        "manifest_bucket": "omni-ops",
    },
    "staging_bucket": "omni-ops",
}
AWS = {"OMNIFILE_AWS_ACCOUNTS": json.dumps({ACCT: ACCOUNT}),
       "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE": AUDIENCE}  # fmt: skip
ADD_S3 = ("backend", "add", "s3", "--account", ACCT, "--region", "eu-west-1")


def _row(url: str, name: str) -> dict[str, Any] | None:
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(
            "SELECT backend_id, kind, config, private_network FROM catalog.backend WHERE name = %s",
            (name,),
        ).fetchone()


def _audits(url: str) -> list[dict[str, Any]]:
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(
            "SELECT actor, action, decision, details FROM catalog.audit_log"
            " WHERE action LIKE 'backend.%' ORDER BY at, audit_id"
        ).fetchall()


def _dataset(url: str, backend_id: int, *, source_backend_id: int | None = None) -> int:
    tag = uuid.uuid4().hex[:8]
    with psycopg.connect(url) as c:
        (edge_id,) = c.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
            (f"edge-{tag}", uuid.uuid4().bytes * 2),
        ).fetchone()
        (ds,) = c.execute(
            "INSERT INTO catalog.dataset (name, backend_id, source_backend_id, storage_layout,"
            " edge_id) VALUES (%s, %s, %s, 'two_account', %s) RETURNING dataset_id",
            (f"ds-{tag}", backend_id, source_backend_id, edge_id),
        ).fetchone()
    return ds


# ------------------------------------------------------------------- Azure


def test_the_live_and_archive_pair_is_registered_as_the_service_reads_it(
    run: Run, catalog: str
) -> None:
    ran = run(*ADD_AZURE, "--region", "westeurope")
    assert ran.code == 0, ran
    assert ran.out.startswith("added: azure (backend ") and "private network" in ran.out, ran
    assert "restart both apps" in ran.out
    row = _row(catalog, "azure")
    assert row is not None and row["kind"] == "azure_blob" and row["private_network"] is True
    assert row["config"] == {
        "layout": "two_account",
        "accounts": {
            "omnilive01": "https://omnilive01.blob.core.windows.net",
            "omniarch01": "https://omniarch01.blob.core.windows.net",
        },
        "primary_account": "omnilive01",
        "archive_account": "omniarch01",
        "smart": True,
        "container": "omnilive01/data",
        "region": "westeurope",
    }
    # What the worker and api build from it: new files land in the live
    # account (Smart), and an archive move goes to the archive account.
    cfg = AzureBlobConfig.from_mapping(row["config"])
    assert (cfg.layout, cfg.primary_account, cfg.archive_account, cfg.smart) == (
        "two_account", "omnilive01", "omniarch01", True)  # fmt: skip
    assert upload_container(row["config"]) == "omnilive01/data"
    assert archive_container(row["config"], "omnilive01/data") == "omniarch01/data"
    # Audited, as the administrator who ran it.
    (a,) = _audits(catalog)
    assert (a["actor"], a["action"], a["decision"]) == ("system:admin", "backend.add", "allow")
    assert a["details"]["name"] == "azure" and a["details"]["db_user"] == "postgres"
    assert a["details"]["backend_id"] == str(row["backend_id"])


def test_the_same_answers_again_change_nothing(run: Run, catalog: str) -> None:
    first = run(*ADD_AZURE)
    again = run(*ADD_AZURE)
    assert first.code == again.code == 0, (first, again)
    assert again.out.startswith("unchanged: azure"), again
    assert "restart" not in again.out
    assert len(_audits(catalog)) == 1
    with psycopg.connect(catalog) as c:
        assert c.execute("SELECT count(*) FROM catalog.backend").fetchone() == (1,)


def test_the_row_the_guides_sql_made_is_already_as_asked(run: Run, catalog: str) -> None:
    # docs/guide/sql/register-azure-backend.sql (PR #33), which this replaces.
    with psycopg.connect(catalog) as c:
        c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network) VALUES ("
            " 'azure', 'azure_blob', jsonb_build_object("
            "   'layout', 'two_account',"
            "   'accounts', jsonb_build_object("
            "     'omnilive01', format('https://%s.blob.core.windows.net', 'omnilive01'),"
            "     'omniarch01', format('https://%s.blob.core.windows.net', 'omniarch01')),"
            "   'primary_account', 'omnilive01', 'archive_account', 'omniarch01',"
            "   'smart', true, 'container', format('%s/data', 'omnilive01')), true)"
        )
    ran = run(*ADD_AZURE)
    assert ran.code == 0 and ran.out.startswith("unchanged: azure"), ran


def test_an_unused_row_is_updated_and_one_under_data_never_moves(run: Run, catalog: str) -> None:
    assert run(*ADD_AZURE).code == 0
    moved = run("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch02")
    assert moved.code == 0 and moved.out.startswith("updated: azure"), moved
    assert "archive_account: 'omniarch01' -> 'omniarch02'" in moved.out
    upd = _audits(catalog)[-1]
    assert upd["action"] == "backend.update"
    assert upd["details"]["before"]["config"]["archive_account"] == "omniarch01"

    row = _row(catalog, "azure")
    assert row is not None
    _dataset(catalog, row["backend_id"])
    refused = run("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch03")
    assert refused.code == 1, refused
    assert "holds data (1 datasets)" in refused.err, refused
    assert "accounts, archive_account would change" in refused.err, refused
    assert _row(catalog, "azure") == row, "nothing changed"
    for argv in (
        ("backend", "add", "azure", "--live", "omnilive09", "--archive", "omniarch02"),
        ("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch02",
         "--no-smart"),
        ("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch02",
         "--container", "other"),
    ):  # fmt: skip
        r = run(*argv)
        assert r.code == 1 and "never change under data" in r.err, (argv, r)
    # The same answers are still fine, and the region (it moves nothing) may be added.
    assert run("backend", "add", "azure", "--live", "omnilive01", "--archive",
               "omniarch02").out.startswith("unchanged")  # fmt: skip
    region = run("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch02",
                 "--region", "westeurope")  # fmt: skip
    assert region.code == 0 and region.out.startswith("updated"), region


def test_private_networking_is_never_turned_off(run: Run, catalog: str) -> None:
    ran = run(*ADD_AZURE, "--public")
    assert ran.code == 1 and "decision 22" in ran.err, ran
    assert _row(catalog, "azure") is None, "refused before anything is written"
    # Development may register a public row, but never makes a private one public.
    assert run(*ADD_AZURE).code == 0
    ran = run(*ADD_AZURE, "--public", OMNIFILE_DEV_MODE="1")
    assert ran.code == 1 and "never turns it off" in ran.err, ran
    dev = run("backend", "add", "azure", "--name", "dev", "--live", "devlive01", "--archive",
              "devarch01", "--public", OMNIFILE_DEV_MODE="1")  # fmt: skip
    assert dev.code == 0 and "PUBLIC network" in dev.out, dev
    row = _row(catalog, "dev")
    assert row is not None and row["private_network"] is False


def test_a_worm_share_gets_one_account_with_version_level_worm(run: Run, catalog: str) -> None:
    ran = run("backend", "add", "azure", "--account", "omniwormfin", "--worm")
    assert ran.code == 0, ran
    row = _row(catalog, "azure-omniwormfin")
    assert row is not None
    assert row["config"] == {
        "layout": "single_account",
        "accounts": {"omniwormfin": "https://omniwormfin.blob.core.windows.net"},
        "primary_account": "omniwormfin",
        "container": "omniwormfin/data",
        "worm": True,
    }
    cfg = AzureBlobConfig.from_mapping(row["config"])
    assert cfg.worm and cfg.versioning and not cfg.smart and cfg.archive_account is None


def test_a_backend_never_changes_kind(run: Run, catalog: str) -> None:
    assert run(*ADD_AZURE, "--name", "store").code == 0
    ran = run(*ADD_S3, "--buckets", "seismic-raw", "--name", "store", **AWS)
    assert ran.code == 1 and "never changes kind" in ran.err, ran


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (("--live", "omnilive01", "--archive", "omnilive01"), "must be two accounts"),
        (("--live", "omnilive01"), "needs both --live and --archive"),
        (("--archive", "omniarch01"), "needs both --live and --archive"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--account", "x"), "not both"),
        (("--live", "OmniLive01", "--archive", "omniarch01"), "not a storage account name"),
        (("--live", "ab", "--archive", "omniarch01"), "not a storage account name"),
        (("--live", "a" * 25, "--archive", "omniarch01"), "not a storage account name"),
        (("--live", "omni-live", "--archive", "omniarch01"), "not a storage account name"),
        (("--account", "omniwormfin", "--smart"), "Smart needs the two-account layout"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--worm"), "decision 31"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--container", "Data"),
         "not a container name"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--container", "a--b"),
         "not a container name"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--region", "West Europe"),
         "not an Azure region"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--name", "my store"), "--name"),
        (("--live", "omnilive01", "--archive", "omniarch01", "--public"), "decision 22"),
    ],
)  # fmt: skip
def test_azure_answers_the_service_would_refuse_are_refused(argv: tuple, says: str) -> None:
    # Checked before connecting: no catalog is configured here.
    ran = run_cli("backend", "add", "azure", *argv, env={})
    assert ran.code == 1 and says in ran.err, ran


# ---------------------------------------------------------------------- S3


def test_s3_source_buckets_are_registered_against_the_services_aws_settings(
    run: Run, catalog: str, tmp_path: Path
) -> None:
    # The file the guide composes (docs/guide/aws/accounts-json.sh on PR #33),
    # which the deployment passes as OMNIFILE_AWS_ACCOUNTS.
    s3 = "vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com"
    guide = {
        "region": "eu-west-1",
        "endpoints": {
            "s3": f"https://bucket.{s3}",
            "s3_control": f"https://control.{s3}",
            "sts": "https://vpce-0123.sts.eu-west-1.vpce.amazonaws.com",
            "sqs": "https://vpce-0def.sqs.eu-west-1.vpce.amazonaws.com",
            "rolesanywhere": "https://vpce-0456.rolesanywhere.eu-west-1.vpce.amazonaws.com",
        },
        "role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-service",
        "event_queues": {"*": f"https://sqs.eu-west-1.amazonaws.com/{ACCT}/omnifile-events"},
        "batch": {"role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-batch",
                  "manifest_bucket": "omni-ops"},
        "staging_bucket": "omni-ops",
        "roles_anywhere_trust_anchor_arn": f"arn:aws:rolesanywhere:eu-west-1:{ACCT}:trust-anchor/"
        "1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b",
    }  # fmt: skip
    accounts = tmp_path / "aws-accounts.json"
    accounts.write_text(json.dumps({ACCT: guide}))
    env = {
        "OMNIFILE_AWS_ACCOUNTS_FILE": str(accounts),
        # The guide's OMNI_AWS_AUDIENCE: api://<the portal's client id>.
        "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE": "api://8a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    }
    ops = run(*ADD_S3, "--buckets", "seismic-raw,omni-ops", **env)
    assert ops.code == 1 and "operations bucket" in ops.err, ops
    ran = run(*ADD_S3, "--buckets", "seismic-raw,seismic-proc", **env)
    assert ran.code == 0 and ran.out.startswith("added: s3-123456789012"), ran
    row = _row(catalog, f"s3-{ACCT}")
    assert row is not None and row["kind"] == "s3" and row["private_network"] is True
    assert row["config"] == {
        "account_id": ACCT,
        "region": "eu-west-1",
        "buckets": ["seismic-raw", "seismic-proc"],
    }
    # The worker builds it (private, through the interface endpoints) ...
    dep = AwsDeployment.from_env(env)
    built = S3Config.for_backend(row["config"], private_network=True, deployment=dep)
    assert built.private_network and built.endpoint_url == guide["endpoints"]["s3"]

    # ... and a partner's inline source finds it (POST /v1/sources).
    async def find() -> int:
        async with await psycopg.AsyncConnection.connect(catalog, row_factory=dict_row) as db:
            return await resolve_inline(
                db, {"bucket": "seismic-proc", "region": "eu-west-1", "account_id": ACCT}
            )

    assert asyncio.run(find()) == row["backend_id"]
    assert run(*ADD_S3, "--buckets", "seismic-raw,seismic-proc", **env).out.startswith("unchanged")


def test_an_s3_source_under_a_migration_only_gains_buckets(run: Run, catalog: str) -> None:
    assert run(*ADD_S3, "--buckets", "seismic-raw", **AWS).code == 0
    assert run(*ADD_AZURE).code == 0
    s3, home = _row(catalog, f"s3-{ACCT}"), _row(catalog, "azure")
    assert s3 is not None and home is not None
    _dataset(catalog, home["backend_id"], source_backend_id=s3["backend_id"])
    more = run(*ADD_S3, "--buckets", "seismic-raw,seismic-new", **AWS)
    assert more.code == 0 and more.out.startswith("updated"), more
    fewer = run(*ADD_S3, "--buckets", "seismic-new", **AWS)
    assert fewer.code == 1 and "buckets seismic-raw would be dropped" in fewer.err, fewer
    # The home backend's data is held too.
    moved = run("backend", "add", "azure", "--live", "omnilive01", "--archive", "omniarch09")
    assert moved.code == 1 and "holds data" in moved.err


@pytest.mark.parametrize(
    ("argv", "env", "says"),
    [
        (("--buckets", "seismic-raw"), {}, "AWS settings are needed"),
        (("--buckets", "Seismic"), AWS, "not an S3 bucket name"),
        (("--buckets", "a..b"), AWS, "not an S3 bucket name"),
        (("--buckets", "192.168.1.10"), AWS, "not an S3 bucket name"),
        (("--buckets", ","), AWS, "open to every bucket"),
        (("--buckets", "omni-ops"), AWS, "operations bucket"),
        (("--buckets", "seismic-raw", "--public"), AWS, "decision 22"),
        (("--buckets", "seismic-raw", "--archive-class", "STANDARD"), AWS, "--archive-class"),
    ],
)
def test_s3_answers_the_service_would_refuse_are_refused(
    argv: tuple, env: dict[str, str], says: str
) -> None:
    ran = run_cli(*ADD_S3, *argv, env=env)
    assert ran.code == 1 and says in ran.err, ran


@pytest.mark.parametrize(
    ("account", "region", "says"),
    [
        ("1234", "eu-west-1", "12-digit"),
        (ACCT, "eu-west", "not an AWS region"),
        (ACCT, "us-east-1", "region must be eu-west-1"),
        ("999999999999", "eu-west-1", "not configured"),
    ],
)
def test_an_s3_account_or_region_the_deployment_lacks_is_refused(
    account: str, region: str, says: str
) -> None:
    ran = run_cli("backend", "add", "s3", "--account", account, "--region", region,
                  "--buckets", "seismic-raw", env=AWS)  # fmt: skip
    assert ran.code == 1 and says in ran.err, ran


def test_public_s3_only_where_the_aws_settings_are_public(run: Run, catalog: str) -> None:
    public = {
        **ACCOUNT,
        "private_network": False,
        "endpoints": {"s3": "https://s3.eu-west-1.amazonaws.com",
                      "sts": "https://sts.eu-west-1.amazonaws.com",
                      "sqs": "https://sqs.eu-west-1.amazonaws.com"},
    }  # fmt: skip
    env = {"OMNIFILE_AWS_ACCOUNTS": json.dumps({ACCT: public}),
           "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE": AUDIENCE,
           "OMNIFILE_AWS_PRIVATE_NETWORK": "0"}  # fmt: skip
    ran = run(*ADD_S3, "--buckets", "seismic-raw", "--public", **env)
    assert ran.code == 0, ran
    row = _row(catalog, f"s3-{ACCT}")
    assert row is not None and row["private_network"] is False


# --------------------------------------------------------------- list, show


def test_list_and_show(run: Run, catalog: str) -> None:
    assert run(*ADD_AZURE).code == 0
    assert run(*ADD_S3, "--buckets", "seismic-raw", **AWS).code == 0
    az = _row(catalog, "azure")
    assert az is not None
    _dataset(catalog, az["backend_id"])

    ls = run("backend", "list")
    assert ls.code == 0, ls
    lines = ls.out.splitlines()
    assert lines[0].split()[:4] == ["ID", "NAME", "KIND", "PRIVATE"]
    assert "azure" in lines[1] and "1d 0s 0m" in lines[1] and "Smart" in lines[1]
    assert f"s3-{ACCT}" in lines[2] and "buckets seismic-raw" in lines[2]

    js = [json.loads(line) for line in run("backend", "list", "--json").out.splitlines()]
    assert [j["name"] for j in js] == ["azure", f"s3-{ACCT}"]
    assert js[0]["uses"] == {"datasets": 1, "sources": 0, "migrations": 0}

    by_name = json.loads(run("backend", "show", "azure").out)
    by_id = json.loads(run("backend", "show", str(az["backend_id"])).out)
    assert by_name == by_id and by_name["config"]["archive_account"] == "omniarch01"
    missing = run("backend", "show", "nope")
    assert missing.code == 1 and "no backend nope" in missing.err


# ---------------------------------------------------------------- the catalog


def test_an_app_role_cannot_register_storage(catalog: str) -> None:
    # The portal's own role (or any app's) has no write on catalog.backend: the
    # portal writes only through the owner-run catalog.backend_register (0035).
    ran = run_cli(*ADD_AZURE, env={"OMNIFILE_DATABASE_URL": as_app(catalog, "web")})
    assert ran.code == 1 and "Only the catalog's administrator" in ran.err, ran
    assert _row(catalog, "azure") is None


def test_an_unmigrated_catalog_says_to_migrate_first(make_database) -> None:
    ran = run_cli(*ADD_AZURE, env={"OMNIFILE_DATABASE_URL": make_database()})
    assert ran.code == 1 and "run the migrations first" in ran.err, ran


def test_python_dash_m_runs_it(catalog: str) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "omnifile_service.admin", *ADD_AZURE],
        env={"OMNIFILE_DATABASE_URL": catalog, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0 and out.stdout.startswith("added: azure"), out


# ------------------------------------------------- who signed in (review on #37)


@pytest.fixture
def group_login(server_url: str) -> Iterator[tuple[str, str]]:
    """A login whose password is an Entra-shaped token for Alice: it stands in
    for the Entra administrators' group, which a member signs in as with their
    own token. Cluster-wide, so made and dropped here."""
    role, token = f"omnifile_t_admins_{uuid.uuid4().hex[:8]}", fake_jwt(ALICE)
    with psycopg.connect(server_url, autocommit=True) as c:
        c.execute(
            sql.SQL("CREATE ROLE {} LOGIN SUPERUSER PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(token)
            )
        )
    yield role, token
    with psycopg.connect(server_url, autocommit=True) as c:
        c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _signed_in(catalog: str, role: str) -> dict[str, Any]:
    (a,) = _audits(catalog)
    assert a["details"]["db_user"] == role, a
    return a["details"]["entra"]


def test_entra_sign_in_records_the_person_not_only_the_group(
    catalog: str, group_login: tuple[str, str]
) -> None:
    # The Entra way (OMNIFILE_PG_HOST ...), end to end against a real server:
    # no app role, and the credential's token as the password.
    role, token = group_login
    at = conninfo_to_dict(catalog)
    env = {
        "OMNIFILE_PG_HOST": str(at.get("host", "127.0.0.1")),
        "OMNIFILE_PG_PORT": str(at.get("port", "5432")),
        "OMNIFILE_PG_DATABASE": str(at["dbname"]),
        "OMNIFILE_PG_USER": role,
        "OMNIFILE_PG_SSLMODE": "disable",
        "OMNIFILE_DEV_MODE": "1",
    }
    out, err = io.StringIO(), io.StringIO()
    code = main(list(ADD_AZURE), env=env, credential=FakeCredential(token), out=out, err=err)
    assert code == 0, (out.getvalue(), err.getvalue())
    assert _signed_in(catalog, role) == {k: ALICE[k] for k in ("oid", "upn", "tid")}


def test_the_migrations_way_records_the_token_in_pgpassword(
    catalog: str, group_login: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guide's way: a connection string without a password, the token in
    # PGPASSWORD (libpq reads it from the process environment).
    role, token = group_login
    at = conninfo_to_dict(catalog)
    at.pop("password", None)
    at["user"] = role
    monkeypatch.setenv("PGPASSWORD", token)
    ran = run_cli(*ADD_AZURE, env={"OMNIFILE_DATABASE_URL": make_conninfo(**at),
                                   "PGPASSWORD": token})  # fmt: skip
    assert ran.code == 0, ran
    assert _signed_in(catalog, role)["upn"] == ALICE["upn"]


# ------------------------------------------------------ concurrent adds (review on #37)


def _wait_for_a_lock_waiter(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        for _ in range(200):
            (n,) = c.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
            if n:
                return
            time.sleep(0.05)
    raise AssertionError("the second add never waited for the first")


@pytest.mark.parametrize(
    ("second", "outcome"),
    [
        ({"live": "omnilive01", "archive": "omniarch01"}, "unchanged"),
        ({"live": "omnilive01", "archive": "omniarch02"}, "updated"),
    ],
)
def test_a_concurrent_add_of_the_same_name_is_compared_not_refused(
    catalog: str, second: dict[str, str], outcome: str
) -> None:
    first = backends.azure_row(live="omnilive01", archive="omniarch01", env={})
    other = backends.azure_row(**second, env={})
    a = psycopg.connect(catalog)
    a.execute("SELECT 1")  # a transaction open: its add stays uncommitted until a.commit()
    try:
        assert backends.register(a, first).outcome == "added"
        got: dict[str, Any] = {}

        def add_again() -> None:
            with psycopg.connect(catalog, autocommit=True) as b:
                try:
                    got["result"] = backends.register(b, other)
                except backends.Refused as e:
                    got["result"] = e

        t = threading.Thread(target=add_again)
        t.start()
        _wait_for_a_lock_waiter(catalog)  # its INSERT waits on the first's row
        a.commit()
        t.join(30)
    finally:
        a.close()
    r = got["result"]
    assert isinstance(r, backends.Result) and r.outcome == outcome, r
    row = _row(catalog, "azure")
    assert row is not None and row["config"] == other.config
