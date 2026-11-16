"""Apply the catalog's SQL migrations.

Migrations are plain SQL files in ``migrations/`` named ``NNNN_name.sql``. They
run in version order, each inside its own transaction, and each applied file
is recorded in ``public.schema_migrations`` with its SHA-256. Running again
applies only what is new. An applied file whose content has changed is an
error: write a new migration instead of editing an old one. The one
exception is a fix made in place before any real deployment ran the file,
listed in `SUPERSEDED` with the former checksum and why the databases that
applied it need nothing more.

A session-level advisory lock serialises runners, so several service replicas
starting at once apply each migration exactly once. Session locks need a
direct connection (or PgBouncer in session mode): with transaction pooling the
lock and the work could land on different server sessions. The service
connects to Azure Database for PostgreSQL directly, without PgBouncer, for
migrations.

Checksums are over the file's bytes, exactly as stored, and the bytes are what
is executed.

Why not a library: the migrations are plain SQL (PGMQ's install script among
them), there is one database, and there are no downgrades. Alembic would bring
SQLAlchemy for none of its features; yoyo-migrations and similar tools add a
dependency for the same ~100 lines.

**No-transaction migrations.** ``CREATE INDEX CONCURRENTLY`` cannot run inside
a transaction, and a plain ``CREATE INDEX`` on a big table holds its writes for
the whole build. A file whose first line is ``-- migrate: no-transaction`` runs
outside a transaction, one statement at a time, and is recorded only once every
statement has succeeded. So each statement must be safe to run again: ``IF NOT
EXISTS``, ``CREATE OR REPLACE``, ``ON CONFLICT DO NOTHING``. Statements end with
``;`` at the end of a line; comment lines are skipped; no dollar-quoted bodies.

A failed concurrent build leaves an invalid index behind, which ``IF NOT
EXISTS`` would then skip for good. So before each ``CREATE [UNIQUE] INDEX
CONCURRENTLY IF NOT EXISTS <name> ON <schema>.<table>``, an invalid index of
that name is dropped (``DROP INDEX CONCURRENTLY``), and afterwards the index
must be valid. That is the only ``CONCURRENTLY`` a no-transaction migration may
use, spelled so: the name unquoted and the table schema-qualified. Any other
form is refused before anything runs, since it would get neither the drop nor
the check.

**Waits.** No wait is silent. A runner polls for the migration lock for at most
``lock_wait_s`` (10 minutes), then says another runner holds it. A concurrent
build runs under ``lock_timeout`` (``build_lock_timeout_s``, 5 minutes): it waits
for every open transaction that writes the table, and for every older snapshot,
and a long one (a worker's, a forgotten session) would otherwise hold the
migration job with no word. It blocks no reader while it waits. The error names
the statement and points at ``pg_stat_activity``.

**Every other statement fails fast, because of what it does to everyone else.** An
``ALTER TABLE`` waiting for its lock makes every later reader of that table queue
behind it. So one open transaction holding a read lock (a worker in a long
provider call, a forgotten session) would stop the whole catalog, the edge feed
included, for as long as it stayed open. A migration therefore waits
``lock_timeout_s`` (5 s) for each lock, gives its transaction up, lets the queue
drain, and tries again, ``lock_tries`` times in all (about three minutes). Then
it stops with a message naming the file and the sessions in the way. Nothing is
recorded, so running it again is safe. ``lock_timeout`` is an ordinary setting, so
this needs no superuser.

Every run ends by applying each app's database privileges
(`privileges.py`), so a new table gets its grants in the same run.

Usage::

    OMNIFILE_DATABASE_URL=postgresql://... \
    OMNIFILE_DB_MEMBERS=api=id-x-api,enrol=id-x-enrol,web=id-x-web,\
        worker=id-x-worker,worker=id-x-verifier \
        python -m omnifile_service.catalog.migrate
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import privileges

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# pg_advisory_lock key: the bytes of "OMNIMIGR" as a signed 64-bit integer.
LOCK_KEY = int.from_bytes(b"OMNIMIGR", "big", signed=True)

_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
NO_TRANSACTION = b"-- migrate: no-transaction"
_CONCURRENT_INDEX = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<name>[a-z_][a-z0-9_]*)\s+ON\s+(?P<schema>[a-z_][a-z0-9_]*)\.",
    re.IGNORECASE,
)

#: Applied migrations fixed in place since, by version: SHA-256 of a former
#: content -> why its databases need nothing more. Only for a fix that no
#: real deployment had applied before, and whose former content still works
#: where it did apply; anything else is a new migration.
SUPERSEDED: dict[str, dict[str, str]] = {
    "0033": {
        "9640434b32b765561a7b1dd50ecd63db9d32b10af9483a279e436b8b7eca93f2": (
            "seed_merge set omnifile.seed_bypass with a SET clause, which only a superuser"
            " may create on PostgreSQL 15+; where it applied (as a superuser: tests and dev"
            " databases) it works the same, and Azure, which has no superuser, never ran it"
        ),
    },
}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    text PRIMARY KEY,
    name       text NOT NULL,
    sha256     text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The migration set or the database's migration history is inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql).hexdigest()

    @property
    def no_transaction(self) -> bool:
        return self.sql.split(b"\n", 1)[0].strip() == NO_TRANSACTION


def statements(sql: str) -> list[str]:
    """A no-transaction migration's statements: each ends with ';' at the end of
    a line; comment lines are dropped."""
    out: list[str] = []
    cur: list[str] = []
    for line in sql.splitlines():
        if line.lstrip().startswith("--") or not line.strip():
            continue
        cur.append(line)
        if line.rstrip().endswith(";"):
            out.append("\n".join(cur))
            cur = []
    if cur:
        raise MigrationError("a no-transaction migration's last statement has no ';'")
    return out


def _index_valid(conn: psycopg.Connection, schema: str, name: str) -> bool | None:
    """None: no such index; else whether it is valid (a failed concurrent build isn't)."""
    row = conn.execute(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s",
        (schema, name),
    ).fetchone()
    return None if row is None else bool(row[0])


_CONCURRENTLY = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)

#: How long a runner waits for the migration lock, and how long a concurrent build
#: waits for a lock (its wait for older transactions included).
LOCK_WAIT_S = 600.0
BUILD_LOCK_TIMEOUT_S = 300.0
#: How long any other statement waits for one lock, and how many times it tries. The
#: longest a reader can queue behind a waiting migration is one `LOCK_TIMEOUT_S`.
LOCK_TIMEOUT_S = 5.0
LOCK_TRIES = 6
#: The pause between tries doubles from this, up to `LOCK_RETRY_MAX_S`: the queue
#: behind the migration drains while it is off the lock.
LOCK_RETRY_BASE_S = 5.0
LOCK_RETRY_MAX_S = 60.0
#: A lock wait that ran out, and a deadlock with whoever held it.
_LOCK_LOST = (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected)


def _set_lock_timeout(conn: psycopg.Connection, seconds: float) -> None:
    conn.execute(f"SET lock_timeout = '{int(seconds * 1000)}ms'")


def _in_the_way(conn: psycopg.Connection) -> str:
    """The sessions holding a table lock in this database, oldest first. `pg_locks` shows
    every session; `pg_stat_activity` tells a role without `pg_read_all_stats` nothing
    about another's, so the rest may be '?'."""
    rows = conn.execute(
        "SELECT l.pid, coalesce(nullif(a.application_name, ''), '?'), coalesce(a.state, '?'),"
        " coalesce(date_trunc('second', now() - a.xact_start)::text, '?')"
        " FROM (SELECT DISTINCT pid FROM pg_locks WHERE locktype = 'relation' AND granted"
        "        AND pid <> pg_backend_pid() AND database ="
        "        (SELECT oid FROM pg_database WHERE datname = current_database())) l"
        " LEFT JOIN pg_stat_activity a USING (pid)"
        " ORDER BY a.xact_start NULLS LAST, l.pid LIMIT 5"
    ).fetchall()
    if not rows:
        return "none now"
    return "; ".join(f"pid {p} {app} ({state}, open {age})" for p, app, state, age in rows)


def _retrying[T](
    conn: psycopg.Connection, what: str, seconds: float, tries: int, attempt: Callable[[], T]
) -> T:
    """`attempt` until it gets its locks. After `tries`, a `MigrationError`."""
    for n in range(1, tries + 1):
        try:
            return attempt()
        except _LOCK_LOST as e:
            if n == tries:
                raise MigrationError(
                    f"{what} waited {seconds:g} s for a lock, {tries} time(s), and gave up, so"
                    " that readers don't queue behind it. Sessions holding table locks:"
                    f" {_in_the_way(conn)}. Nothing was recorded: run the migration again once"
                    " they end (a worker in a long provider call ends by itself; see"
                    " pg_stat_activity)"
                ) from e
            time.sleep(min(LOCK_RETRY_MAX_S, LOCK_RETRY_BASE_S * 2 ** (n - 1)))
    raise AssertionError("unreachable")


def checked_statements(m: Migration) -> list[str]:
    """A no-transaction migration's statements, refused whole if one uses CONCURRENTLY
    in any form but the one the runner drops and checks."""
    stmts = statements(m.sql.decode())
    for stmt in stmts:
        if _CONCURRENTLY.search(stmt) and not _CONCURRENT_INDEX.match(stmt):
            first = stmt.strip().splitlines()[0]
            raise MigrationError(
                f"{m.path.name}: {first!r}: the only CONCURRENTLY a no-transaction migration"
                " may use is CREATE [UNIQUE] INDEX CONCURRENTLY IF NOT EXISTS <name>"
                " ON <schema>.<table>"
            )
    return stmts


def _run_without_transaction(
    conn: psycopg.Connection,
    m: Migration,
    *,
    lock_timeout_s: float = BUILD_LOCK_TIMEOUT_S,
    statement_lock_timeout_s: float = LOCK_TIMEOUT_S,
    lock_tries: int = LOCK_TRIES,
) -> None:
    stmts = checked_statements(m)
    try:
        for stmt in stmts:
            idx = _CONCURRENT_INDEX.match(stmt)
            first = stmt.strip().splitlines()[0]
            if idx is None:
                # Each statement is safe to run again, by this file format's rule.
                _set_lock_timeout(conn, statement_lock_timeout_s)
                _retrying(
                    conn,
                    f"{m.path.name}: {first!r}",
                    statement_lock_timeout_s,
                    lock_tries,
                    lambda stmt=stmt: conn.execute(stmt),
                )
                continue
            # A concurrent build blocks no reader, so it may wait longer.
            _set_lock_timeout(conn, lock_timeout_s)
            try:
                if _index_valid(conn, idx["schema"], idx["name"]) is False:
                    # Left by a build that failed: IF NOT EXISTS would keep it, invalid, for good.
                    conn.execute(f'DROP INDEX CONCURRENTLY "{idx["schema"]}"."{idx["name"]}"')
                conn.execute(stmt)
            except psycopg.errors.LockNotAvailable as e:
                raise MigrationError(
                    f"{m.path.name}: {first!r} waited {lock_timeout_s:g} s for a lock and gave up:"
                    " an open transaction on its table, or an older snapshot, holds it (see"
                    " pg_stat_activity for long transactions); run the migration again once it ends"
                ) from e
            if _index_valid(conn, idx["schema"], idx["name"]) is not True:
                raise MigrationError(
                    f"{m.path.name}: index {idx['name']} is not valid after its build"
                )
    finally:
        _set_lock_timeout(conn, statement_lock_timeout_s)
    conn.execute(
        "INSERT INTO public.schema_migrations (version, name, sha256) VALUES (%s, %s, %s)",
        (m.version, m.name, m.sha256),
    )


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return the migrations in ``directory``, in version order.

    Every ``.sql`` file must match ``NNNN_name.sql``, versions must be unique,
    and they must run 0001, 0002, ... without gaps.
    """
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        m = _NAME.match(path.name)
        if not m:
            raise MigrationError(f"{path.name}: expected NNNN_lowercase_name.sql")
        found.append(Migration(m["version"], m["name"], path, path.read_bytes()))
    versions = [m.version for m in found]
    expected = [f"{i:04d}" for i in range(1, len(found) + 1)]
    if versions != expected:
        raise MigrationError(f"versions must be {expected}, found {versions}")
    return found


def applied(conn: psycopg.Connection) -> dict[str, str]:
    """Version -> SHA-256 of every migration recorded in the database."""
    rows = conn.execute("SELECT version, sha256 FROM public.schema_migrations").fetchall()
    return {version: sha for version, sha in rows}


def migrate(
    conninfo: str,
    directory: Path = MIGRATIONS_DIR,
    *,
    members: list[tuple[str, str]] | None = None,
    lock_wait_s: float = LOCK_WAIT_S,
    build_lock_timeout_s: float = BUILD_LOCK_TIMEOUT_S,
    lock_timeout_s: float = LOCK_TIMEOUT_S,
    lock_tries: int = LOCK_TRIES,
) -> list[str]:
    """Apply pending migrations, then every app's privileges
    (`privileges.apply`). Returns the versions applied by this call."""
    migrations = discover(directory)
    # Refused before the lock and before anything runs.
    for m in migrations:
        if m.no_transaction:
            checked_statements(m)
    done: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        # Polled, never waited on in one statement: a runner blocked inside pg_advisory_lock
        # holds a transaction that another runner's CREATE INDEX CONCURRENTLY would wait
        # for, while it waits for the lock: a deadlock (#63 review).
        deadline = time.monotonic() + lock_wait_s
        while not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:  # type: ignore[index]
            if time.monotonic() >= deadline:
                raise MigrationError(
                    f"waited {lock_wait_s:g} s for the migration lock: another runner holds it"
                    f" (pg_locks: advisory lock {LOCK_KEY}); run again once it has finished"
                )
            time.sleep(0.2)
        # On an error the connection closes on leaving this block, which
        # releases the session lock; unlocking explicitly only on success
        # means a failed unlock can never hide the original error.
        _set_lock_timeout(conn, lock_timeout_s)
        conn.execute(_CREATE_TABLE)
        history = applied(conn)
        known = {m.version for m in migrations}
        unknown = sorted(set(history) - known)
        if unknown:
            raise MigrationError(f"database has migrations this code lacks: {unknown}")
        for m in migrations:
            if m.version in history and history[m.version] in SUPERSEDED.get(m.version, {}):
                continue  # applied before a documented in-place fix (below)
            if m.version in history and history[m.version] != m.sha256:
                raise MigrationError(
                    f"{m.path.name} changed after it was applied; add a new migration"
                )
        for m in migrations:
            if m.version in history:
                continue
            if m.no_transaction:
                # The connection is autocommit: each statement stands on its own.
                _run_without_transaction(
                    conn,
                    m,
                    lock_timeout_s=build_lock_timeout_s,
                    statement_lock_timeout_s=lock_timeout_s,
                    lock_tries=lock_tries,
                )
                done.append(m.version)
                continue

            def apply_one(m: Migration = m) -> None:
                with conn.transaction():
                    # No parameters: psycopg sends the file as one simple
                    # query, so multiple statements and '%' are fine.
                    conn.execute(m.sql)
                    conn.execute(
                        "INSERT INTO public.schema_migrations (version, name, sha256)"
                        " VALUES (%s, %s, %s)",
                        (m.version, m.name, m.sha256),
                    )

            _retrying(conn, m.path.name, lock_timeout_s, lock_tries, apply_one)
            done.append(m.version)
        if directory == MIGRATIONS_DIR:
            _retrying(
                conn,
                "applying the privileges",
                lock_timeout_s,
                lock_tries,
                lambda: privileges.apply(conn, members),
            )
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return done


def main() -> int:
    url = os.environ.get("OMNIFILE_DATABASE_URL")
    if not url:
        print("OMNIFILE_DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        members = privileges.parse_members(os.environ.get("OMNIFILE_DB_MEMBERS"))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    versions = migrate(url, members=members)
    print(f"applied: {', '.join(versions)}" if versions else "up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
