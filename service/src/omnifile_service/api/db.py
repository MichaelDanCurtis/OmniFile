"""Catalog access for the api and enrol apps: a connection pool and short transactions.

Every write runs in `tx()`, which sets the transaction-local settings the
catalog's triggers read (migration 0003): `omnifile.actor`, and for edge-caused
changes `omnifile.origin_edge_id` / `omnifile.origin_op_id`, so feed events
carry their origin and the edge can skip its own echoes.

Transactions stay short: the feed only serves events below the oldest running
transaction, so a long one holds every edge's feed back (control-api.md
section 5). Nothing here waits on a network call inside a transaction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..catalog.connect import CatalogDb, Db, conninfo_of, pool_kwargs
from .problems import ApiError, from_sqlstate

log = logging.getLogger("omnifile_service.api.db")


#: Connections one replica's pool may hold when OMNIFILE_PG_POOL_MAX is unset.
DEFAULT_POOL_MAX = 10


class Catalog:
    """A pool of connections to the catalog database."""

    def __init__(self, db: Db, *, min_size: int = 1, max_size: int = DEFAULT_POOL_MAX) -> None:
        base = {"row_factory": dict_row, "autocommit": False}
        #: The connection settings, for the few callers that need a sync connection
        #: (Settings, Storage runs `admin.backends.register`, which is sync).
        self.db = db

        async def kwargs() -> dict:
            # Called for every new connection: with Entra sign-in, each gets a fresh token.
            return await pool_kwargs(db, base)

        self.pool = AsyncConnectionPool(
            conninfo_of(db),
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs=kwargs if isinstance(db, CatalogDb) else base,
        )
        self._opened = False

    async def open(self, *, wait: bool = True) -> None:
        """Open the pool on the running event loop. Safe to call again; `tx()`
        and `read()` call it (without waiting) on every use."""
        self._opened = True
        await self.pool.open(wait=wait, timeout=15)

    async def close(self) -> None:
        if self._opened:
            self._opened = False
            await self.pool.close()

    @asynccontextmanager
    async def tx(
        self,
        actor: str,
        *,
        origin_edge_id: int | None = None,
        origin_op_id: str | None = None,
    ) -> AsyncIterator[psycopg.AsyncConnection]:
        """One transaction, committed on success, rolled back on any error.

        Catalog trigger errors (SQLSTATE class OF) become their API codes;
        connection trouble becomes `503 unavailable`.
        """
        try:
            await self.open(wait=False)
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    await set_origin(conn, actor, origin_edge_id, origin_op_id)
                    yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc

    @asynccontextmanager
    async def read(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """A read-only transaction (it never takes a transaction id)."""
        try:
            await self.open(wait=False)
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute("SET TRANSACTION READ ONLY")
                    yield conn
        except psycopg.Error as exc:
            raise map_db_error(exc) from exc


async def set_origin(
    conn: psycopg.AsyncConnection,
    actor: str,
    origin_edge_id: int | None = None,
    origin_op_id: str | None = None,
) -> None:
    await conn.execute(
        "SELECT set_config('omnifile.actor', %s, true),"
        " set_config('omnifile.origin_edge_id', %s, true),"
        " set_config('omnifile.origin_op_id', %s, true)",
        (
            actor,
            "" if origin_edge_id is None else str(origin_edge_id),
            origin_op_id or "",
        ),
    )


def map_db_error(exc: psycopg.Error) -> ApiError:
    mapped = from_sqlstate(exc.sqlstate, _first_line(exc))
    if mapped is not None:
        return mapped
    if isinstance(exc, psycopg.OperationalError | psycopg.errors.SerializationFailure):
        return ApiError("unavailable", "catalog unavailable", retry_after_s=1)
    if isinstance(exc, psycopg.errors.DeadlockDetected):
        return ApiError("unavailable", "catalog busy", retry_after_s=1)
    log.error("catalog error %s: %s", exc.sqlstate, _first_line(exc))
    return ApiError("internal", "catalog error")


def _first_line(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    msg = getattr(diag, "message_primary", None) or str(exc)
    return msg.splitlines()[0] if msg else ""


def id_str(value: int | None) -> str | None:
    """Identifiers go out as decimal strings (control-api.md section 1)."""
    return None if value is None else str(value)
