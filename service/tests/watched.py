"""A provider that says who was holding what each time it was called.

A transaction id held across a provider call stops the edge feed for every edge (its
horizon is the oldest open id), and a read lock held across one blocks an ``ALTER``.
Wrap a backend in `Watched` and run the real worker code over it. Each provider call
asks Postgres which of this database's sessions hold an id or a lock on the catalog's
main tables. Only this database's sessions count: on a shared server, another suite's
open transaction must neither fail the test nor pass it."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker.jobs import QueueJob
from omnifile_service.worker.queue import Consumer, ConsumerConfig, ConsumerStats

#: The provider's calls, as `StorageBackend` names them.
CALLS = frozenset(
    {
        "put", "get_range", "committed_blocks", "head", "list", "list_versions",
        "list_all_versions", "set_class", "copy_object", "restore", "restore_status",
        "delete", "set_retention", "set_legal_hold", "set_tags", "get_tags", "set_metadata",
        "edge_credential", "read_changes", "batch_restore", "batch_checksum",
        "batch_checksums", "batch_job", "ensure_job", "start_run", "run_status", "running_run",
    }
)  # fmt: skip
#: The tables an `ALTER` of the catalog is most likely to want.
TABLES = ("location", "file", "file_version", "backend")


@dataclass(frozen=True)
class Seen:
    call: str
    #: Sessions in this database holding a transaction id.
    ids: int
    #: The catalog tables other sessions hold a lock on.
    tables: frozenset[str]


def look(url: str, call: str) -> Seen:
    with psycopg.connect(url, autocommit=True, application_name="watched") as c:
        (ids,) = c.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND backend_xid IS NOT NULL AND pid <> pg_backend_pid()"
        ).fetchone()
        rows = c.execute(
            "SELECT DISTINCT r.relname FROM pg_locks l"
            " JOIN pg_class r ON r.oid = l.relation"
            " JOIN pg_namespace n ON n.oid = r.relnamespace"
            " WHERE l.pid <> pg_backend_pid() AND l.granted AND n.nspname = 'catalog'"
            " AND r.relname = ANY(%s)",
            (list(TABLES),),
        ).fetchall()
    return Seen(call, ids, frozenset(r[0] for r in rows))


class Watched:
    """`backend`, reporting each call in `seen`. `during`, if set, runs at each call,
    while the worker is waiting on it."""

    def __init__(self, backend: Any, url: str, during: Callable[[str], None] | None = None) -> None:
        self._backend = backend
        self._url = url
        self._during = during
        self.seen: list[Seen] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._backend, name)
        if name not in CALLS or not callable(attr):
            return attr

        def called(*args: Any, **kwargs: Any) -> Any:
            # Before the call is made, and blocking, so nothing moves meanwhile.
            self.seen.append(look(self._url, name))
            if self._during is not None:
                self._during(name)
            return attr(*args, **kwargs)

        return called

    def calls(self) -> list[str]:
        return [s.call for s in self.seen]

    def with_ids(self) -> list[Seen]:
        return [s for s in self.seen if s.ids]

    def with_locks(self) -> list[Seen]:
        return [s for s in self.seen if s.tables]


@dataclass
class FakeWorker:
    """What the job modules read from `runner.Worker`, without its other jobs."""

    registry: Any
    clock: Any = None
    mover: Any = None
    retention_config: Any = None
    consumer_config: ConsumerConfig = field(default_factory=ConsumerConfig)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def drain_job(url: str, jobs: list[Any], queue: str) -> ConsumerStats:
    """Run one of a job module's queues, with its own handler and settings, as the
    worker does: through a `Consumer`."""
    (job,) = [j for j in jobs if isinstance(j, QueueJob) and j.queue == queue]
    return drain_queue(url, queue, job.handler)


def drain_queue(
    url: str, queue: str, handler: Callable[[Any, psycopg.AsyncConnection], Awaitable[None]]
) -> ConsumerStats:
    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(as_app(url, "worker"), autocommit=True)

    async def go() -> ConsumerStats:
        c = Consumer(connect, queue, handler, ConsumerConfig(backoff_base=0))
        try:
            await c.drain(limit=50)
        finally:
            await c.close()
        return c.stats

    return asyncio.run(go())


class Interleaved:
    """`conn`, running `between` just before the first statement that contains `sql`:
    what another session does between two statements of the code under test."""

    def __init__(
        self, conn: psycopg.AsyncConnection, sql: str, between: Callable[[], None]
    ) -> None:
        self._conn = conn
        self._sql = sql
        self._between = between
        self.ran = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        if not self.ran and self._sql in str(query):
            self.ran = True
            self._between()
        return await self._conn.execute(query, *args, **kwargs)
