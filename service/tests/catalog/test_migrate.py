from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from omnifile_service.catalog.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    discover,
    migrate,
)

ALL = [m.version for m in discover()]


def test_files_are_numbered_without_gaps() -> None:
    assert ALL[0] == "0001" and len(ALL) >= 4


def test_applies_to_an_empty_database_then_is_a_no_op(make_database: Callable[[], str]) -> None:
    url = make_database()
    assert migrate(url) == ALL
    assert migrate(url) == []
    with psycopg.connect(url) as c:
        rows = c.execute(
            "SELECT version, sha256 FROM public.schema_migrations ORDER BY 1"
        ).fetchall()
    assert rows == [(m.version, m.sha256) for m in discover()]


def test_concurrent_runners_apply_each_migration_once(make_database: Callable[[], str]) -> None:
    url = make_database()
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(migrate(url))
        except BaseException as e:  # a runner that dies must fail the test (#63 review)
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(v for r in results for v in r) == ALL


def test_an_edited_applied_migration_is_refused(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    url = make_database()
    shutil.copytree(MIGRATIONS_DIR, tmp_path / "m")
    migrate(url, tmp_path / "m")
    last = sorted((tmp_path / "m").glob("*.sql"))[-1]
    last.write_text(last.read_text() + "\n-- edited\n")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(url, tmp_path / "m")


def test_a_documented_in_place_fix_is_accepted_and_nothing_else(
    make_database: Callable[[], str],
) -> None:
    """A database that applied 0033 before its in-place fix (SUPERSEDED)
    migrates on; any other content recorded for 0033 is still refused."""
    from omnifile_service.catalog.migrate import SUPERSEDED

    url = make_database()
    migrate(url)
    (old,) = SUPERSEDED["0033"]
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE public.schema_migrations SET sha256 = %s WHERE version = '0033'", (old,))
    assert migrate(url) == []
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE public.schema_migrations SET sha256 = %s WHERE version = '0033'",
                  ("0" * 64,))  # fmt: skip
    with pytest.raises(MigrationError, match="0033.*changed after it was applied"):
        migrate(url)


def test_migrations_run_as_a_non_superuser_administrator(make_database: Callable[[], str]) -> None:
    """The suite migrates as the deployed job does (tests/conftest.py): a
    role with CREATEROLE and CREATEDB that owns the database, no superuser.
    So a migration that needs a superuser fails here, not in Azure."""
    url = make_database()
    migrate(url)
    with psycopg.connect(url) as c:
        owner, superuser = c.execute(
            "SELECT pg_get_userbyid(p.proowner), r.rolsuper FROM pg_proc p"
            " JOIN pg_roles r ON r.oid = p.proowner WHERE p.oid = 'catalog.seed_merge'::regproc"
        ).fetchone()
    assert (owner, superuser) == ("omnifile_migrator", False)


def test_a_failing_migration_rolls_back_and_is_not_recorded(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    url = make_database()
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_ok.sql").write_text("CREATE TABLE t1 (x int);")
    (d / "0002_bad.sql").write_text("CREATE TABLE t2 (x int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(url, d)
    with psycopg.connect(url) as c:
        assert c.execute("SELECT version FROM public.schema_migrations").fetchall() == [("0001",)]
        assert c.execute("SELECT to_regclass('t2')").fetchone() == (None,)


def _valid(url: str, name: str) -> bool | None:
    with psycopg.connect(url) as c:
        row = c.execute(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid"
            " WHERE x.relname = %s",
            (name,),
        ).fetchone()
    return None if row is None else row[0]


def test_a_no_transaction_migration_builds_concurrently_and_recovers_a_failed_build(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    """#63 review: CONCURRENTLY can't run in a transaction, and a failed concurrent
    build leaves an invalid index that IF NOT EXISTS would keep for good."""
    url = make_database()
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_t.sql").write_text(
        "CREATE TABLE public.t (x int); INSERT INTO public.t VALUES (1), (1);"
    )
    (d / "0002_idx.sql").write_text(
        "-- migrate: no-transaction\n"
        "-- a comment; with a semicolon at its end;\n"
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS t_x_uq\n"
        "    ON public.t (x);\n"
        "CREATE TABLE IF NOT EXISTS public.t2 (y int);\n"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        migrate(url, d)
    assert _valid(url, "t_x_uq") is False  # what a failed concurrent build leaves
    with psycopg.connect(url, autocommit=True) as c:
        assert c.execute("SELECT version FROM public.schema_migrations").fetchall() == [("0001",)]
        c.execute("DELETE FROM public.t WHERE ctid = (SELECT max(ctid) FROM public.t)")
    assert migrate(url, d) == ["0002"]
    assert _valid(url, "t_x_uq") is True
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regclass('public.t2')").fetchone() != (None,)
    assert migrate(url, d) == []


def _idx_dir(tmp_path: Path, stmt: str) -> Path:
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_t.sql").write_text("CREATE TABLE public.t (x int);")
    (d / "0002_idx.sql").write_text(f"-- migrate: no-transaction\n{stmt}\n")
    return d


def test_an_index_left_invalid_by_its_build_is_an_error(
    make_database: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#63 re-review: the check after the build, which the real failure path (an
    exception from the build) never reached."""
    from omnifile_service.catalog import migrate as mig

    url = make_database()
    d = _idx_dir(tmp_path, "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_x ON public.t (x);")
    seen: list[str] = []

    def valid(conn, schema, name):  # type: ignore[no-untyped-def]
        seen.append(name)
        return None if len(seen) == 1 else False  # absent before, invalid after

    monkeypatch.setattr(mig, "_index_valid", valid)
    with pytest.raises(MigrationError, match="t_x is not valid after its build"):
        migrate(url, d)
    assert seen == ["t_x", "t_x"]
    with psycopg.connect(url) as c:
        assert c.execute("SELECT version FROM public.schema_migrations").fetchall() == [("0001",)]


@pytest.mark.parametrize(
    "stmt",
    [
        "CREATE INDEX CONCURRENTLY t_x ON public.t (x);",  # no IF NOT EXISTS
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_x ON t (x);",  # no schema
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "t_x" ON public.t (x);',  # quoted
        "REINDEX INDEX CONCURRENTLY public.t_x;",
        "DROP INDEX CONCURRENTLY IF EXISTS public.t_x;",
    ],
)
def test_any_other_concurrently_form_is_refused_before_anything_runs(
    make_database: Callable[[], str], tmp_path: Path, stmt: str
) -> None:
    url = make_database()
    d = _idx_dir(tmp_path, stmt)
    with pytest.raises(MigrationError, match="the only CONCURRENTLY"):
        migrate(url, d)
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_regclass('public.schema_migrations')").fetchone() == (None,)


def test_a_concurrent_build_held_by_an_open_transaction_says_what_it_waited_on(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    """#63 re-review: a long transaction on the table held the job with no word."""
    url = make_database()
    d = _idx_dir(tmp_path, "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_x ON public.t (x);")
    (d / "0002_idx.sql").rename(d / "0002_idx.sql.later")
    migrate(url, d)
    (d / "0002_idx.sql.later").rename(d / "0002_idx.sql")
    with psycopg.connect(url) as writer:  # a writer that keeps its transaction open
        writer.execute("INSERT INTO public.t VALUES (1)")
        with pytest.raises(MigrationError, match=r"waited 1 s for a lock.*pg_stat_activity"):
            migrate(url, d, build_lock_timeout_s=1)
        writer.rollback()
    assert migrate(url, d) == ["0002"]


def _table_dir(tmp_path: Path) -> Path:
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_t.sql").write_text("CREATE TABLE public.t (x int);")
    return d


def test_a_migration_blocked_by_an_open_reader_gives_up_and_lets_readers_past(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    """Run: an open read transaction, such as a worker in a provider call, holds a table's
    read lock. An `ALTER` waiting for it would make every later reader queue behind it,
    the edge feed included. So it gives up in time, says who is in the way, records
    nothing, and a new reader is only ever held for one timeout."""
    url = make_database()
    d = _table_dir(tmp_path)
    migrate(url, d)
    (d / "0002_alter.sql").write_text("ALTER TABLE public.t ADD COLUMN y int;")
    reader = psycopg.connect(url)
    reader.execute("SELECT * FROM public.t")
    reader_pid = reader.info.backend_pid
    results: list[BaseException | list[str]] = []

    def run_it() -> None:
        try:
            results.append(migrate(url, d, lock_timeout_s=0.5, lock_tries=2))
        except BaseException as e:  # noqa: BLE001 - examined below
            results.append(e)

    runner = threading.Thread(target=run_it, daemon=True)
    started = time.monotonic()
    runner.start()
    try:
        # A reader arriving while it runs is held for at most one timeout.
        waited = []
        deadline = time.monotonic() + 8
        while runner.is_alive() and time.monotonic() < deadline:
            t0 = time.monotonic()
            with psycopg.connect(url, autocommit=True) as new_reader:
                new_reader.execute("SET statement_timeout = '4s'")
                new_reader.execute("SELECT * FROM public.t")
            waited.append(time.monotonic() - t0)
            time.sleep(0.05)
        runner.join(timeout=10)
        assert not runner.is_alive(), "the migration queued behind the reader"
        elapsed = time.monotonic() - started
    finally:
        reader.close()  # frees it, if it queued: a failure must not hang the suite
    runner.join(timeout=10)
    assert waited and max(waited) < 2.0, waited
    assert len(results) == 1 and isinstance(results[0], MigrationError), results
    message = str(results[0])
    assert "0002_alter.sql" in message and "gave up" in message
    assert f"pid {reader_pid}" in message
    # Two tries of half a second and one pause of 5 s between them, at most.
    assert elapsed < 8.0
    with psycopg.connect(url) as c:
        assert c.execute(
            "SELECT count(*) FROM public.schema_migrations WHERE version = '0002'"
        ).fetchone() == (0,)
    assert migrate(url, d) == ["0002"]


def test_a_migration_retries_and_gets_through_when_the_reader_ends(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    url = make_database()
    d = _table_dir(tmp_path)
    migrate(url, d)
    (d / "0002_alter.sql").write_text("ALTER TABLE public.t ADD COLUMN y int;")
    reader = psycopg.connect(url)
    reader.execute("SELECT * FROM public.t")
    timer = threading.Timer(1.0, reader.close)
    timer.start()
    try:
        # The first try times out at 0.5 s, the pause is 5 s, and the reader is gone by then.
        assert migrate(url, d, lock_timeout_s=0.5, lock_tries=3) == ["0002"]
    finally:
        timer.cancel()
        reader.close()


def test_a_plain_statement_in_a_no_transaction_migration_fails_fast_too(
    make_database: Callable[[], str], tmp_path: Path
) -> None:
    """Only a concurrent build, which blocks no reader, may wait for its long timeout."""
    url = make_database()
    d = _table_dir(tmp_path)
    migrate(url, d)
    (d / "0002_alter.sql").write_text(
        "-- migrate: no-transaction\nALTER TABLE public.t ADD COLUMN IF NOT EXISTS y int;\n"
    )
    with psycopg.connect(url) as reader:
        reader.execute("SELECT * FROM public.t")
        started = time.monotonic()
        with pytest.raises(MigrationError, match="0002_alter.sql.*gave up"):
            migrate(url, d, lock_timeout_s=0.5, lock_tries=1, build_lock_timeout_s=30)
        assert time.monotonic() - started < 5
        reader.rollback()
    assert migrate(url, d) == ["0002"]


def test_the_migration_lock_has_a_deadline(make_database: Callable[[], str]) -> None:
    from omnifile_service.catalog.migrate import LOCK_KEY

    url = make_database()
    with psycopg.connect(url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        with pytest.raises(MigrationError, match="migration lock: another runner holds it"):
            migrate(url, lock_wait_s=0.5)


def test_0043_runs_outside_a_transaction() -> None:
    from omnifile_service.catalog.migrate import statements

    (m,) = [x for x in discover() if x.version == "0043"]
    assert m.no_transaction
    stmts = statements(m.sql.decode())
    indexes = [s for s in stmts if "CREATE INDEX" in s]
    assert len(indexes) == 3 and all("CONCURRENTLY IF NOT EXISTS" in s for s in indexes)


@pytest.mark.parametrize(
    "names",
    [["0001_a.sql", "0003_c.sql"], ["0001_a.sql", "1_b.sql"], ["0001_A.sql"]],
)
def test_bad_file_sets_are_rejected(tmp_path: Path, names: list[str]) -> None:
    for n in names:
        (tmp_path / n).write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover(tmp_path)
