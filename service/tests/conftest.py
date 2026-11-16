"""Suite-wide: the AWS SDK never reads the developer's own AWS setup.

The S3 tests drive `S3Backend` over an in-process fake (tests/s3/s3_fakes.py)
with ``credentials = "default"``, which is botocore's own chain. Left alone,
that chain reads ``~/.aws/config`` and ``~/.aws/credentials``: a profile with
SSO, a ``credential_process`` or a default region elsewhere breaks the tests
(or reaches a real account). So every test runs with empty AWS config files,
no profile, no instance metadata, no OMNIFILE_AWS_* deployment and dummy
keys (only the emulator tests' `default` chain, towards loopback, reads
them), and never calls AWS.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import provider_guard
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.catalog import migrate as _migrate_module

# --- migrations run as the deployed migration job does --------------------------
#
# In Azure the migrations run as the Entra administrator of the Flexible Server:
# not a superuser, but CREATEROLE and CREATEDB, owning the catalog database and
# every object the migrations make (deploy/azure/README.md, step 5). The tests'
# server login is a superuser, which hides anything only a superuser may do (a
# function with SET of a custom parameter, for one: PR #35's 0033). So every
# test's `migrate()` runs as MIGRATOR, a role with the administrator's
# privileges and no more, which owns the database it migrates. The wrapper is
# installed here, before any test module imports `migrate`.

MIGRATOR = "omnifile_migrator"
_GROUPS = ("omnifile_api", "omnifile_enrol", "omnifile_web", "omnifile_worker")
_real_migrate = _migrate_module.migrate


def as_migrator(conninfo: str) -> str:
    """Give `conninfo`'s database to MIGRATOR (made if missing) and return a
    conninfo whose session acts as it (the login must be a superuser)."""
    with psycopg.connect(conninfo, autocommit=True) as c:
        if not c.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[
            0
        ]:
            return conninfo  # already not a superuser: as it is
        # Runners in parallel (test_migrate) would update the same catalog rows.
        c.execute("SELECT pg_advisory_lock(hashtext('omnifile tests: migrator setup'))")
        c.execute(
            sql.SQL(
                "DO $$ BEGIN CREATE ROLE {} NOLOGIN NOSUPERUSER CREATEROLE CREATEDB;"
                " EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $$"
            ).format(sql.Identifier(MIGRATOR))
        )
        db = c.execute("SELECT current_database()").fetchone()[0]
        c.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(db), sql.Identifier(MIGRATOR)
            )
        )
        # Group roles another run made (as a superuser) on a shared server:
        # in Azure the administrator makes them, so it administers them.
        for g in _GROUPS:
            if c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (g,)).fetchone():
                c.execute(
                    sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                        sql.Identifier(g), sql.Identifier(MIGRATOR)
                    )
                )
    params = conninfo_to_dict(conninfo)
    params["options"] = f"{params.get('options', '')} -c role={MIGRATOR}".strip()
    return make_conninfo(**params)


#: Decision 63 hides an annexed file until its edge has read every folder above it on
#: the NAS. Tests written before it seed annexed files without those folders, so a test
#: database records each annex file's folders as an edge's folder pass would, read and
#: open to Everyone, observed at -infinity so any real `folder_acl` event wins. Tests of
#: decision 63 itself opt their annex out first (`omnifile_test.unread_annex`). The same
#: trigger answers decision 65's question for the annex's filer ("not limited") unless it
#: has an answer. Never in the product: only test databases get it.
OPEN_FOLDERS = """
CREATE SCHEMA IF NOT EXISTS omnifile_test;
CREATE TABLE IF NOT EXISTS omnifile_test.unread_annex (annex_id bigint PRIMARY KEY);
GRANT USAGE ON SCHEMA omnifile_test TO PUBLIC;
GRANT SELECT, INSERT ON omnifile_test.unread_annex TO PUBLIC;
CREATE OR REPLACE FUNCTION omnifile_test.read_folders() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    v_acl bigint;
    v_ds  bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM omnifile_test.unread_annex WHERE annex_id = NEW.annex_id) THEN
        RETURN NULL;
    END IF;
    -- Decision 65: as if filer setup had been told the share limits nothing.
    UPDATE catalog.filer SET access_limited = false, access_confirmed_by = 'tests',
                             access_confirmed_at = now()
     WHERE filer_id = (SELECT filer_id FROM catalog.annex WHERE annex_id = NEW.annex_id)
       AND access_limited IS NULL;
    SELECT dataset_id INTO v_ds FROM catalog.annex WHERE annex_id = NEW.annex_id;
    INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, search_sids,
                             search_deny_sids)
    VALUES (v_ds, sha256(convert_to('omnifile tests: an open folder', 'UTF8')), 493, '[]',
            '{S-1-1-0}', '{S-1-1-0}', '{}')
    ON CONFLICT (dataset_id, sha256) DO UPDATE SET search_sids = EXCLUDED.search_sids
    RETURNING acl_id INTO v_acl;
    INSERT INTO catalog.nas_folder_acl (filer_id, path, acl_id, annex_id, observed_at)
    SELECT a.filer_id, d.path, v_acl, a.annex_id, '-infinity'
      FROM catalog.annex a, unnest(catalog.annex_folders(a.path, NEW.path)) AS d (path)
     WHERE a.annex_id = NEW.annex_id
    ON CONFLICT (filer_id, path) DO NOTHING;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS test_read_folders ON catalog.annex_file;
CREATE TRIGGER test_read_folders AFTER INSERT OR UPDATE OF path ON catalog.annex_file
    FOR EACH ROW EXECUTE FUNCTION omnifile_test.read_folders();
"""


def _migrate_as_administrator(conninfo: str, *args, **kwargs):  # type: ignore[no-untyped-def]
    out = _real_migrate(as_migrator(conninfo), *args, **kwargs)
    with psycopg.connect(as_migrator(conninfo), autocommit=True) as c:
        if c.execute("SELECT to_regprocedure('catalog.annex_folders(text, text)')").fetchone()[0]:
            c.execute(OPEN_FOLDERS)
    return out


_migrate_module.migrate = _migrate_as_administrator  # type: ignore[assignment]

#: Variables that would point botocore at a real account, a profile or a role.
_CLEARED = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "AWS_ENDPOINT_URL_SQS",
    "AWS_CA_BUNDLE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    # The service's own AWS settings (backends/s3/config.py): a test that
    # needs a deployment sets them itself.
    "OMNIFILE_AWS_ACCOUNTS",
    "OMNIFILE_AWS_ACCOUNTS_FILE",
    "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE",
    "OMNIFILE_AWS_PRIVATE_NETWORK",
)


@pytest.fixture(scope="session", autouse=True)
def _hermetic_aws(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    home = Path(tmp_path_factory.mktemp("aws"))
    config, credentials = home / "config", home / "credentials"
    config.write_text("")
    credentials.write_text("")
    with pytest.MonkeyPatch.context() as mp:
        for name in _CLEARED:
            mp.delenv(name, raising=False)
        mp.setenv("AWS_CONFIG_FILE", str(config))
        mp.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
        mp.setenv("AWS_ACCESS_KEY_ID", "testing")
        mp.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        mp.setenv("AWS_EC2_METADATA_DISABLED", "true")
        yield


# --- no provider call while the caller holds a transaction id (tests/provider_guard.py) ---------


def pytest_configure(config: pytest.Config) -> None:
    provider_guard.install()


@pytest.fixture(autouse=True)
def _no_id_across_a_provider_call() -> Iterator[None]:
    provider_guard.take_violations()
    yield
    found = provider_guard.take_violations()
    if found:
        provider_guard.GUARD.unlisted.update(found)
        lines = "\n".join(f"  {site}: {call}" for site, call in sorted(found.items()))
        pytest.fail(
            "a provider call was made with a transaction id held, which stops the edge feed for"
            f" every edge until it returns:\n{lines}\nMove the call out of the transaction"
            " (worker.queue.outside; see tests/provider_guard.py). Don't add it to KNOWN.",
            pytrace=False,
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # Only on a whole-suite run does every known site get its chance to be hit.
    args = session.config.args
    whole = session.testscollected > 1500 and not session.config.option.keyword
    if whole and exitstatus == 0 and args and all(a.rstrip("/").endswith("tests") for a in args):
        if provider_guard.stale():
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    guard = provider_guard.GUARD
    if guard.unlisted:
        terminalreporter.write_line(f"provider guard, unlisted: {sorted(guard.unlisted)}")
    terminalreporter.write_line(
        f"provider guard: {len(guard.hit)} of {len(provider_guard.KNOWN)} known sites hit"
        f"; not hit: {sorted(provider_guard.stale())}"
    )
