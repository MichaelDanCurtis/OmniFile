"""The worker's budget (worker/pool.py), review of #59: writes through a lease reach the
connection, and the lanes keep long holders from starving the queues someone waits on."""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker.pool import PooledConnect, worker_lanes
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Message, dlq_name

from .conftest import run

FAST = ConsumerConfig(backoff_base=0, visibility_timeout=30, idle_poll=0.05)
HOLD_S = 1.5


def _tagged(worker_db_url: str) -> tuple[str, str]:
    name = f"pool-lanes-{uuid.uuid4().hex[:8]}"
    return make_conninfo(as_app(worker_db_url, "worker"), application_name=name), name


def test_a_lease_writes_go_to_the_connection_and_are_put_back(worker_db_url: str) -> None:
    """`conn.row_factory = dict_row` on a lease reaches the connection (it used to stay on the
    lease, so the handler read tuples), and the pool hands the connection out again as it was."""
    url, _ = _tagged(worker_db_url)
    pooled = PooledConnect(url, 1, timeout=0.5)

    async def go() -> None:
        try:
            conn = await pooled()
            conn.row_factory = dict_row
            assert "row_factory" not in vars(conn)
            assert await (await conn.execute("SELECT 1 AS one")).fetchone() == {"one": 1}
            await conn.close()
            with pytest.raises(psycopg.InterfaceError):
                conn.row_factory = tuple_row  # given back: no writes either
            again = await pooled()
            assert again.row_factory is tuple_row
            assert await (await again.execute("SELECT 1")).fetchone() == (1,)
            await again.close()
        finally:
            await pooled.close()

    run(go())


@pytest.fixture
def lane_queues(db: psycopg.Connection) -> list[str]:
    """Eight of the catalog's queues, one message each: six long, one shared, one priority."""
    names = [
        r[0]
        for r in db.execute(
            "SELECT queue_name FROM pgmq.meta WHERE queue_name NOT LIKE '%%\\_dlq'"
            " ORDER BY queue_name"
        )
    ][:8]
    assert len(names) == 8, names
    for q in names:
        db.execute(f"DELETE FROM pgmq.q_{q}")
        db.execute("SELECT pgmq.create(%s)", (dlq_name(q),))
        db.execute(f"DELETE FROM pgmq.q_{dlq_name(q)}")
        db.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (q, Jsonb({"n": 1})))
    return names


async def _lanes(pooled, long_queues: list[str], quick: dict[str, str], admin_url: str,
                 app_name: str) -> dict[str, float]:  # fmt: skip
    """Long handlers (one message each) hold their connection HOLD_S; `quick` maps a queue to
    its role. When each quick queue's message was handled (seconds from the start), the most
    long handlers holding a connection at once, and the most connections the server saw."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    holding = 0
    out: dict[str, float] = {"long_at_once": 0, "peak": 0}
    left = len(long_queues) + len(quick)

    async def slow(msg: Message, conn: psycopg.AsyncConnection) -> None:
        nonlocal holding, left
        holding += 1
        out["long_at_once"] = max(out["long_at_once"], holding)
        await conn.execute("SELECT 1")
        await asyncio.sleep(HOLD_S)
        holding -= 1
        left -= 1

    def fast(role: str):
        async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
            nonlocal left
            await conn.execute("SELECT 1")
            out[role] = loop.time() - start
            left -= 1

        return handler

    consumers = [Consumer(pooled, q, slow, FAST) for q in long_queues]
    consumers += [Consumer(pooled, q, fast(role), FAST) for q, role in quick.items()]
    stop = asyncio.Event()
    async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as admin:

        async def watch() -> None:
            while not stop.is_set():
                cur = await admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
                    (app_name,),
                )
                out["peak"] = max(out["peak"], (await cur.fetchone())[0])
                if left <= 0:
                    stop.set()
                await asyncio.sleep(0.02)

        await asyncio.wait_for(
            asyncio.gather(watch(), *(c.run_forever(stop) for c in consumers)), timeout=60
        )
    for c in consumers:
        await c.close()
    return out


def _run_lanes(worker_db_url: str, queues: list[str], *, lanes: bool) -> dict[str, float]:
    url, name = _tagged(worker_db_url)
    long_queues, shared_q, priority_q = queues[:6], queues[6], queues[7]
    if lanes:
        pooled = PooledConnect(url, 6, priority={priority_q}, reserved=2, long=set(long_queues),
                               long_max=2)  # fmt: skip
    else:
        pooled = PooledConnect(url, 6)

    async def go() -> dict[str, float]:
        try:
            return await _lanes(pooled, long_queues, {shared_q: "shared", priority_q: "priority"},
                                worker_db_url, name)  # fmt: skip
        finally:
            await pooled.close()

    return run(go())


def test_long_holders_leave_the_reserved_and_shared_slots_free(
    worker_db_url: str, lane_queues: list[str]
) -> None:
    """Six long handlers on a budget of six: at most two hold a connection at once, and the
    priority queue and an ordinary one are both served while they do."""
    out = _run_lanes(worker_db_url, lane_queues, lanes=True)
    assert out["long_at_once"] == 2, out
    assert out["priority"] < HOLD_S and out["shared"] < HOLD_S, out
    assert out["peak"] <= 6, out


def test_without_lanes_long_holders_starve_the_priority_queue(
    worker_db_url: str, lane_queues: list[str]
) -> None:
    """What the lanes are for: with one shared budget, the six long handlers take all of it."""
    out = _run_lanes(worker_db_url, lane_queues, lanes=False)
    assert out["long_at_once"] == 6, out
    assert out["priority"] >= HOLD_S, out


def test_lanes_shrink_to_fit_a_small_budget() -> None:
    def lanes(n: int) -> tuple[int, int]:
        p = PooledConnect("host=x", n, priority={"p"}, reserved=2, long={"l"}, long_max=2)
        return p.reserved, p.long_max

    assert lanes(2) == (1, 1)  # always one shared slot
    assert lanes(3) == (2, 1)
    assert lanes(6) == (2, 2)  # small's worker: 2 reserved, 4 shared, at most 2 of them long
    assert PooledConnect("host=x", 6).reserved == 0


def test_the_suites_run_on_the_pool_when_asked(connect) -> None:
    """OMNIFILE_TEST_WORKER_POOL=6 runs the worker, retention and notify suites on the worker's
    pool (tests/pooled.py); unset, on direct connections."""
    import pooled

    if pooled.budget() is None:
        assert not getattr(connect, "lends_connections", False)
        return
    assert isinstance(connect, pooled.PerLoop) and connect.size == pooled.budget()

    async def go() -> None:
        conn = await connect()
        try:
            assert type(conn).__name__ == "Lease"
        finally:
            await conn.close()

    run(go())


def test_the_worker_lanes_name_queues_the_worker_reads() -> None:
    priority, long = worker_lanes()
    assert priority == {"restores", "edge_instructions", "notify"}
    assert long == {"verify", "migrate", "migrate_verify", "snapshot_export", "seed", "reconcile"}
