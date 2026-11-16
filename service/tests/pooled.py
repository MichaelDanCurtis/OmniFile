"""Run the worker's test suites with its connection budget on (worker/pool.py).

    OMNIFILE_TEST_WORKER_POOL=6 uv run pytest tests/worker tests/retention tests/notify \
        tests/web/test_recall_batches.py

With the variable set, the suites' ``connect`` fixtures hand out connections from the worker's own
pool, in its lanes, as `OMNIFILE_PG_POOL_MAX` does in production: every handler and sweep then
runs on a `Lease`, and every consumer gives its connection back after each batch. Unset, the
fixtures connect directly, as before. A pool belongs to one event loop, so there is one per
loop, closed when the suites' `run` ends that loop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import psycopg

from omnifile_service.worker.pool import PooledConnect, worker_lanes

ENV = "OMNIFILE_TEST_WORKER_POOL"
#: Tests wait this long for a slot before PoolTimeout, not production's two minutes.
TIMEOUT_S = 15.0

_open: dict[asyncio.AbstractEventLoop, list[PooledConnect]] = {}


def budget() -> int | None:
    raw = os.environ.get(ENV, "").strip()
    return int(raw) if raw else None


class PerLoop:
    """The worker's pooled ``connect()``, one pool per running event loop."""

    lends_connections = True

    def __init__(self, url: str, size: int) -> None:
        self.url, self.size = url, size
        self._pools: dict[asyncio.AbstractEventLoop, PooledConnect] = {}

    def _pool(self) -> PooledConnect:
        loop = asyncio.get_running_loop()
        if loop not in self._pools:
            priority, long = worker_lanes()
            p = PooledConnect(self.url, self.size, timeout=TIMEOUT_S, priority=priority,
                              reserved=2, long=long, long_max=2)  # fmt: skip
            self._pools[loop] = p
            _open.setdefault(loop, []).append(p)
        return self._pools[loop]

    async def __call__(self) -> psycopg.AsyncConnection:
        return await self._pool()()

    def lane(self, queue: str) -> Callable[[], Awaitable[psycopg.AsyncConnection]]:
        async def connect() -> psycopg.AsyncConnection:
            return await self._pool().lane(queue)()

        connect.lends_connections = True  # type: ignore[attr-defined]
        return connect


def connect_for(
    url: str, direct: Callable[[], Awaitable[psycopg.AsyncConnection]]
) -> Callable[[], Awaitable[psycopg.AsyncConnection]]:
    """`direct`, or with OMNIFILE_TEST_WORKER_POOL set, the worker's pool over `url`."""
    size = budget()
    return direct if size is None else PerLoop(url, size)


async def _closing[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return await coro
    finally:
        for p in _open.pop(asyncio.get_running_loop(), []):
            await p.close()


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run`, closing the pools the loop made."""
    return asyncio.run(_closing(coro))
