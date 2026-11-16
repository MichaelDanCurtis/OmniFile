"""The worker's connection budget (worker/pool.py, OMNIFILE_PG_POOL_MAX) against a real Postgres.

Six consumers on six queues share a pool of two: every message is handled, and the server never
sees more than two of the worker's connections. Without the pool the same consumers hold six.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker.pool import Lease, PooledConnect
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Message, dlq_name

from .conftest import run

QUEUES = 6
PER_QUEUE = 3
FAST = ConsumerConfig(backoff_base=0, visibility_timeout=30, idle_poll=0.05)


@pytest.fixture
def queues(db: psycopg.Connection) -> list[str]:
    """Six of the catalog's own queues (the worker role may use them), emptied."""
    names = [
        r[0]
        for r in db.execute(
            "SELECT queue_name FROM pgmq.meta WHERE queue_name NOT LIKE '%%\\_dlq'"
            " ORDER BY queue_name"
        )
    ][:QUEUES]
    assert len(names) == QUEUES, names
    for q in names:
        db.execute(f"DELETE FROM pgmq.q_{q}")
        db.execute("SELECT pgmq.create(%s)", (dlq_name(q),))
        db.execute(f"DELETE FROM pgmq.q_{dlq_name(q)}")
        for i in range(PER_QUEUE):
            db.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (q, Jsonb({"i": i})))
    return names


def _tagged(worker_db_url: str) -> tuple[str, str]:
    name = f"pool-test-{uuid.uuid4().hex[:8]}"
    return make_conninfo(as_app(worker_db_url, "worker"), application_name=name), name


async def _drive(connect, queues: list[str], admin_url: str, app_name: str) -> tuple[int, int]:
    """Run a consumer per queue until every queue is empty; (messages handled, most connections
    the server saw for `app_name` at once)."""
    handled = 0

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        nonlocal handled
        await conn.execute("SELECT 1")
        await asyncio.sleep(0.05)  # hold the connection, so consumers overlap
        handled += 1

    consumers = [Consumer(connect, q, handler, FAST) for q in queues]
    stop = asyncio.Event()
    peak = 0
    async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as admin:

        async def watch() -> None:
            nonlocal peak
            while not stop.is_set():
                cur = await admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
                    (app_name,),
                )
                peak = max(peak, (await cur.fetchone())[0])
                if handled >= len(queues) * PER_QUEUE:
                    await asyncio.sleep(0.2)  # every consumer polls its empty queue again
                    stop.set()
                await asyncio.sleep(0.01)

        await asyncio.wait_for(
            asyncio.gather(watch(), *(c.run_forever(stop) for c in consumers)), timeout=60
        )
    for c in consumers:
        await c.close()
    return handled, peak


def test_consumers_share_a_pool_of_two_and_every_queue_is_read(
    worker_db_url: str, queues: list[str]
) -> None:
    url, name = _tagged(worker_db_url)
    pooled = PooledConnect(url, 2)

    async def go() -> tuple[int, int]:
        try:
            return await _drive(pooled, queues, worker_db_url, name)
        finally:
            await pooled.close()

    handled, peak = run(go())
    assert handled == QUEUES * PER_QUEUE
    assert 1 <= peak <= 2, peak


def test_without_the_pool_each_consumer_holds_its_own(
    worker_db_url: str, queues: list[str]
) -> None:
    """What the budget is for: the same consumers, each with a connection it keeps."""
    url, name = _tagged(worker_db_url)

    async def direct() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(url, autocommit=True)

    handled, peak = run(_drive(direct, queues, worker_db_url, name))
    assert handled == QUEUES * PER_QUEUE
    assert peak == QUEUES, peak


def test_a_lease_goes_back_once_and_is_unusable_after(worker_db_url: str) -> None:
    url, _ = _tagged(worker_db_url)
    pooled = PooledConnect(url, 1, timeout=0.5)

    async def go() -> None:
        try:
            conn = await pooled()
            assert isinstance(conn, Lease) and not conn.closed
            assert (await (await conn.execute("SELECT 41 + 1")).fetchone())[0] == 42
            await conn.close()
            await conn.close()  # once only
            assert conn.closed
            with pytest.raises(psycopg.InterfaceError):
                await conn.execute("SELECT 1")
            # The one connection is free again, and comes back autocommit.
            async with await pooled() as again:
                await again.set_autocommit(False)
            async with await pooled() as third:
                assert third.autocommit is True
        finally:
            await pooled.close()

    run(go())


def test_a_used_up_pool_times_out_as_an_operational_error(worker_db_url: str) -> None:
    """Consumers and sweeps already retry an OperationalError."""
    url, _ = _tagged(worker_db_url)
    pooled = PooledConnect(url, 1, timeout=0.3)

    async def go() -> None:
        try:
            held = await pooled()
            with pytest.raises(psycopg.OperationalError):
                await pooled()
            await held.close()
            await (await pooled()).close()
        finally:
            await pooled.close()

    run(go())
