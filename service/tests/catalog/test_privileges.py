"""Each app's database privileges (catalog/privileges.py; threat model T6).

The rest of the suite runs every app's connections under its group role, so a
missing grant fails those tests. These check what the roles must NOT do."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql

from omnifile_service.catalog import privileges
from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app, parse_members


@pytest.fixture(scope="module")
def url(make_database: Callable[[], str]) -> str:
    u = make_database()
    migrate(u)
    return u


@pytest.fixture
def as_role(url: str) -> Iterator[Callable[[str], psycopg.Connection]]:
    opened: list[psycopg.Connection] = []

    def open_(app: str) -> psycopg.Connection:
        c = psycopg.connect(as_app(url, app), autocommit=True)
        opened.append(c)
        return c

    yield open_
    for c in opened:
        c.close()


def denied(conn: psycopg.Connection, stmt: str) -> bool:
    try:
        conn.execute(stmt)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


@pytest.mark.parametrize("app", sorted(privileges.ROLES))
def test_no_app_can_change_the_schema_or_bypass_the_triggers(as_role, app: str) -> None:
    c = as_role(app)
    assert c.execute("SELECT current_user").fetchone()[0] == privileges.ROLES[app]
    assert denied(c, "CREATE TABLE catalog.x (a int)")
    assert denied(c, "DROP TABLE catalog.share")
    assert denied(c, "TRUNCATE catalog.file_version")
    assert denied(c, "ALTER TABLE catalog.audit_log DISABLE TRIGGER audit_log_no_update")
    assert denied(c, "ALTER TABLE catalog.file_version DISABLE TRIGGER USER")
    assert denied(c, "SELECT pgmq.create('evil')")
    assert denied(c, "SELECT catalog.feed_restored()")
    assert denied(c, "UPDATE catalog.feed_retention SET dropped_through_event = 0")
    assert denied(c, "UPDATE catalog.time_partition SET to_ts = now()")
    assert denied(c, "DELETE FROM catalog.audit_log")


def test_no_app_writes_feed_events_and_only_web_reads_sessions(as_role) -> None:
    ins = (
        "INSERT INTO catalog.feed_event (entity, entity_id, entity_version, op)"
        " VALUES ('share', 1, 1, 'upsert')"
    )
    conflict = "SELECT catalog.feed_conflict(1, -1, 'op', 'why')"
    for app in sorted(privileges.ROLES):
        assert denied(as_role(app), ins), app
    for app in ("web", "enrol", "worker"):
        assert denied(as_role(app), conflict), app
    # The api adds the conflict events of POST .../changes, and only those.
    as_role("api").execute(conflict)
    for app in ("api", "enrol"):
        assert denied(as_role(app), "SELECT 1 FROM catalog.web_session LIMIT 1"), app
        assert denied(as_role(app), "SELECT 1 FROM catalog.oidc_login LIMIT 1"), app
    # The worker deletes expired sessions, and can't make one.
    w = as_role("worker")
    w.execute("DELETE FROM catalog.web_session WHERE false")
    assert denied(w, "UPDATE catalog.web_session SET expires_at = now() WHERE false")


def test_web_writes_versions_only_for_holds_and_retention(as_role) -> None:
    web = as_role("web")
    web.execute("UPDATE catalog.file_version SET legal_hold = legal_hold WHERE false")
    web.execute("UPDATE catalog.file_version SET retention_until = retention_until WHERE false")
    assert denied(web, "UPDATE catalog.file_version SET state = 'deleted' WHERE false")
    assert denied(web, "DELETE FROM catalog.file_version WHERE false")
    assert denied(web, "UPDATE catalog.location SET verified = true WHERE false")
    assert denied(as_role("enrol"), "UPDATE catalog.file SET size = 0 WHERE false")


def test_queues_send_only_where_the_app_sends_and_only_the_worker_reads(as_role) -> None:
    web = as_role("web")
    web.execute("SELECT pgmq.send('notify', '{}'::jsonb)")
    assert denied(web, "SELECT pgmq.send('edge_instructions', '{}'::jsonb)")
    assert denied(web, "SELECT * FROM pgmq.read('notify', 30, 1)")
    assert denied(as_role("enrol"), "SELECT pgmq.send('notify', '{}'::jsonb)")
    w = as_role("worker")
    w.execute("SELECT * FROM pgmq.read('notify', 0, 10)")
    w.execute("SELECT * FROM pgmq.metrics('notify')")


def test_owner_work_is_the_workers_through_owner_run_functions(as_role) -> None:
    w = as_role("worker")
    w.execute("SELECT catalog.ensure_time_partitions('catalog.feed_event', 'week', 8)")
    w.execute("SELECT * FROM catalog.default_partition_rows()")
    w.execute("SELECT catalog.analyze_namespace()")
    for app in ("api", "web", "enrol"):
        c = as_role(app)
        assert denied(c, "SELECT catalog.ensure_time_partitions('catalog.feed_event', 'week', 8)")
        assert denied(c, "SELECT catalog.feed_drop_before(now())")
        assert denied(c, "SELECT catalog.analyze_namespace()")


def test_trigger_functions_run_as_their_owner(url: str) -> None:
    with psycopg.connect(url) as c:
        rows = c.execute(
            "SELECT p.proname, p.prosecdef, p.proconfig FROM pg_proc p"
            " WHERE p.pronamespace = 'catalog'::regnamespace"
            "   AND p.prorettype = 'trigger'::regtype"
        ).fetchall()
    assert rows
    for name, secdef, config in rows:
        assert secdef, name
        assert any(s.startswith("search_path=") for s in config or []), name


def test_a_rerun_takes_back_what_the_lists_no_longer_grant(url: str, as_role) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("GRANT DELETE ON catalog.share TO omnifile_enrol")
        c.execute("GRANT UPDATE ON catalog.file_version TO omnifile_web")
    migrate(url)  # nothing to apply: the privileges are applied again
    assert denied(as_role("enrol"), "DELETE FROM catalog.share WHERE false")
    assert denied(as_role("web"), "UPDATE catalog.file_version SET state = state WHERE false")


def test_members_are_granted_their_apps_role(url: str) -> None:
    login = f"id-test-web-{uuid.uuid4().hex[:6]}"
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(login)))
        try:
            privileges.apply(c, {"web": login})
            assert c.execute(
                "SELECT pg_has_role(%s, 'omnifile_web', 'USAGE')", (login,)
            ).fetchone()[0]
            with pytest.raises(ValueError, match="unknown app"):
                privileges.apply(c, {"relay": login})
        finally:
            c.execute(sql.SQL("REVOKE omnifile_web FROM {}").format(sql.Identifier(login)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))
    assert parse_members(" api=id-x-api, worker=id-x-worker,worker=id-x-verifier ") == [
        ("api", "id-x-api"),
        ("worker", "id-x-worker"),
        ("worker", "id-x-verifier"),
    ]
    assert parse_members(None) == []
    with pytest.raises(ValueError, match="app=role"):
        parse_members("api")


@pytest.mark.parametrize("app", sorted(privileges.ROLES))
def test_no_app_can_run_an_owner_run_trigger_function_on_a_table_of_its_own(as_role, app) -> None:
    """M7 review B4: with TEMP and EXECUTE, a role could attach a catalog trigger
    function (which runs as the owner) to its own temporary table, and so
    rewrite another file's versions or forge feed events."""
    c = as_role(app)
    if app == "worker":
        # The worker keeps temporary tables (reconcile), but not the functions.
        c.execute("CREATE TEMP TABLE t (file_id bigint, version_id bigint, kind text)")
        assert denied(
            c,
            "CREATE TRIGGER x AFTER INSERT ON t FOR EACH ROW"
            " EXECUTE FUNCTION catalog.file_version_after_commit()",
        )
    else:
        assert denied(c, "CREATE TEMP TABLE t (file_id bigint)")


def test_definer_functions_look_up_nothing_in_public(url: str) -> None:
    with psycopg.connect(url) as c:
        rows = c.execute(
            "SELECT p.proname, p.proconfig FROM pg_proc p"
            " WHERE p.pronamespace = 'catalog'::regnamespace AND p.prosecdef"
        ).fetchall()
    assert rows
    for name, config in rows:
        assert "search_path=pg_catalog, pg_temp" in (config or []), (name, config)


def test_older_grants_and_moved_memberships_are_taken_back(url: str, as_role) -> None:
    """M7 review S6: grants to PUBLIC or straight to a login (the old setup
    guide's way) and a login moved to another app don't survive a run."""
    a, b = (f"id-test-{x}-{uuid.uuid4().hex[:6]}" for x in "ab")
    with psycopg.connect(url, autocommit=True) as c:
        for login in (a, b):
            c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(login)))
        try:
            privileges.apply(c, [("web", a), ("web", b)])
            c.execute("GRANT DELETE ON catalog.share TO PUBLIC")
            c.execute(
                sql.SQL("GRANT UPDATE ON catalog.file_version TO {}").format(sql.Identifier(a))
            )
            # b moves from web to enrol.
            privileges.apply(c, [("web", a), ("enrol", b)])
            has = c.execute(
                "SELECT pg_has_role(%s, 'omnifile_web', 'MEMBER'),"
                " pg_has_role(%s, 'omnifile_web', 'MEMBER'),"
                " pg_has_role(%s, 'omnifile_enrol', 'MEMBER'),"
                " has_table_privilege(%s, 'catalog.file_version', 'UPDATE'),"
                " has_table_privilege('public', 'catalog.share', 'DELETE')",
                (a, b, b, a),
            ).fetchone()
            assert has == (True, False, True, False, False), has
        finally:
            for login in (a, b):
                for role in privileges.ROLES.values():
                    c.execute(
                        sql.SQL("REVOKE {} FROM {}").format(
                            sql.Identifier(role), sql.Identifier(login)
                        )
                    )
                c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


def test_a_temp_grant_the_admin_could_not_take_back_is_reported(url: str, caplog) -> None:
    """M7 re-review: on Azure the migrating administrator may not own the
    database, and PostgreSQL then only warns. The run says so."""
    from omnifile_service.catalog.privileges import _check_temp

    with psycopg.connect(url, autocommit=True) as c:
        assert _check_temp(c)
        db = c.execute("SELECT current_database()").fetchone()[0]
        c.execute(sql.SQL("GRANT TEMP ON DATABASE {} TO omnifile_web").format(sql.Identifier(db)))
        try:
            with caplog.at_level("ERROR"):
                assert not _check_temp(c)
            assert any(
                "omnifile_web can still create temporary tables" in r.message
                for r in caplog.records
            )
        finally:
            c.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM omnifile_web").format(sql.Identifier(db))
            )
