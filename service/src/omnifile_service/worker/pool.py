"""The worker's catalog connections under a budget (``OMNIFILE_PG_POOL_MAX``).

Without a budget the worker opens a connection per queue consumer, which it keeps, and one per
running sweep: about thirty at once. A small server cannot take that (the small size's B1ms
allows 35 user connections for every app together). With a budget, `PooledConnect` stands in
for the worker's ``connect()``: every connection comes from one pool of at most that many, and
``close()`` returns it to the pool. Consumers then give their connection back after each batch
(`Consumer`, `lends_connections`), so every queue is still read while they share the pool.

The budget is split into lanes (`lane`), so a slow queue can't hold every connection:

- **reserved**: a slice only the latency-critical queues may use (a user's recall, the edges'
  instructions, notifications). Those try the reserved slice first, then the shared one.
- **long**: the queues whose handlers hold their connection for minutes or hours (the upload
  read-back, migration steps and verifies, snapshot exports, seeding, reconcile). At most
  `long_max` of them hold one at once, so part of the shared slice stays free for everything else.
- everything else, and every sweep, takes from the shared slice.

Nothing here nests: a handler or a sweep is handed one connection and never asks for another,
so the pool can't deadlock on itself. A caller that waits longer than `timeout` seconds for a
slot gets `psycopg_pool.PoolTimeout` (an `OperationalError`, which the consumers and sweeps
already retry).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from ..catalog.connect import CatalogDb, Db, conninfo_of, pool_kwargs

#: Seconds a caller waits for a connection from a used-up pool.
DEFAULT_TIMEOUT_S = 120.0

Release = Callable[[], None]


class Lease:
    """A pooled connection as a plain one: everything goes to the connection, reads and writes
    of its attributes alike (``conn.row_factory = ...``), except ``close()``, which gives it
    back to the pool and its lane's slot (once), and ``closed``. `_reset` puts back what a
    holder changed."""

    #: Lease's own fields; any other attribute written is the connection's.
    _OWN = frozenset({"_conn", "_pool", "_release"})

    def __init__(
        self, conn: psycopg.AsyncConnection, pool: AsyncConnectionPool, release: Release
    ) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_release", release)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in Lease._OWN:
            object.__setattr__(self, name, value)
            return
        setattr(self._live(), name, value)

    def _live(self) -> psycopg.AsyncConnection:
        conn = self.__dict__.get("_conn")
        if conn is None:
            raise psycopg.InterfaceError("the connection was given back to the pool")
        return conn

    @property
    def closed(self) -> bool:
        return self._conn is None or self._conn.closed

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await self._pool.putconn(conn)
            finally:
                self._release()

    async def __aenter__(self) -> Lease:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._live(), name)


async def _reset(conn: psycopg.AsyncConnection) -> None:
    # Every worker connection is autocommit with tuple rows, as `aconnect` makes them; a holder
    # may have changed either.
    if not conn.autocommit:
        await conn.set_autocommit(True)
    if conn.row_factory is not tuple_row:
        conn.row_factory = tuple_row


class PooledConnect:
    """The worker's ``connect()`` with at most `max_size` connections open at once, in lanes.

    `reserved` of them only `priority` queues may use; of the rest, at most `long_max` go to the
    `long` queues. Each is cut down when the budget is too small to keep one shared slot free."""

    #: Tells `Consumer` to give its connection back after each batch, and to use its `lane`.
    lends_connections = True

    def __init__(
        self,
        db: Db,
        max_size: int,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        priority: Iterable[str] = (),
        reserved: int = 0,
        long: Iterable[str] = (),
        long_max: int = 0,
    ) -> None:
        base = {"autocommit": True}

        async def kwargs() -> dict:
            return await pool_kwargs(db, base)  # a fresh Entra token per new connection

        self.max_size = max_size
        self.timeout = timeout
        self.priority = frozenset(priority)
        self.long = frozenset(long)
        #: Never so many reserved that nothing else can run, nor so many long that they fill
        #: the shared slice.
        self.reserved = max(0, min(reserved, max_size - 1)) if self.priority else 0
        shared = max_size - self.reserved
        self.long_max = max(1, min(long_max, shared - 1)) if self.long and long_max > 0 else 0
        self._shared = asyncio.Semaphore(shared)
        self._reserved = asyncio.Semaphore(self.reserved)
        self._long = asyncio.Semaphore(self.long_max) if self.long_max else None
        self.pool = AsyncConnectionPool(
            conninfo_of(db),
            min_size=1,
            max_size=max_size,
            open=False,
            timeout=timeout,
            reset=_reset,
            kwargs=kwargs if isinstance(db, CatalogDb) else base,
        )
        self._opened = False

    def lane(self, queue: str) -> Callable[[], Awaitable[psycopg.AsyncConnection]]:
        """``connect()`` for the consumer of `queue`."""

        async def connect() -> psycopg.AsyncConnection:
            return await self._lease(queue)

        connect.lends_connections = True  # type: ignore[attr-defined]
        return connect

    async def __call__(self) -> psycopg.AsyncConnection:
        """A connection from the shared slice (sweeps, the metrics collector, start-up)."""
        return await self._lease(None)

    async def _wait(self, sem: asyncio.Semaphore) -> None:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.timeout)
        except TimeoutError:
            raise PoolTimeout(f"no catalog connection free within {self.timeout:g} s") from None

    async def _slot(self, queue: str | None) -> Release:
        """Wait for a slot in `queue`'s lane; the function that gives it back."""
        if queue in self.priority:
            # The reserved slice first, then the shared one: never behind a long job.
            for sem in (self._reserved, self._shared):
                if not sem.locked():
                    await sem.acquire()
                    return sem.release
            sem = self._reserved if self.reserved else self._shared
            await self._wait(sem)
            return sem.release
        if queue in self.long and self._long is not None:
            long = self._long
            await self._wait(long)
            try:
                await self._wait(self._shared)
            except BaseException:
                long.release()
                raise

            def both() -> None:
                self._shared.release()
                long.release()

            return both
        await self._wait(self._shared)
        return self._shared.release

    async def _lease(self, queue: str | None) -> psycopg.AsyncConnection:
        if not self._opened:
            self._opened = True
            await self.pool.open(wait=False)
        release = await self._slot(queue)
        try:
            conn = await self.pool.getconn()
        except BaseException:
            release()
            raise
        return Lease(conn, self.pool, release)  # type: ignore[return-value]

    async def close(self) -> None:
        if self._opened:
            self._opened = False
            await self.pool.close()


#: The worker's lanes: of each budget, this many are reserved for the latency-critical queues,
#: and at most this many go to the long-holding ones at once.
RESERVED = 2
LONG_MAX = 2


def worker_lanes() -> tuple[frozenset[str], frozenset[str]]:
    """(priority, long): the queues whose handlers someone is waiting on, and those whose
    handlers hold their connection for minutes or hours."""
    from ..exports import QUEUE as SNAPSHOT_EXPORT
    from ..migrate.engine import QUEUE as MIGRATE
    from ..migrate.engine import VERIFY_QUEUE as MIGRATE_VERIFY
    from ..sources.run import SEED_QUEUE
    from .recalls import NOTIFY
    from .recalls import QUEUE as RESTORES
    from .retention_jobs import RECONCILE_QUEUE
    from .stubs import QUEUE as EDGE_INSTRUCTIONS
    from .verify import QUEUE as VERIFY

    return (
        frozenset({RESTORES, EDGE_INSTRUCTIONS, NOTIFY}),
        frozenset({VERIFY, MIGRATE, MIGRATE_VERIFY, SNAPSHOT_EXPORT, SEED_QUEUE, RECONCILE_QUEUE}),
    )


def worker_pool(db: Db, budget: int) -> PooledConnect:
    """The worker's ``connect()`` for a budget of `budget` connections, in its lanes."""
    priority, long = worker_lanes()
    return PooledConnect(
        db, budget, priority=priority, reserved=RESERVED, long=long, long_max=LONG_MAX
    )
