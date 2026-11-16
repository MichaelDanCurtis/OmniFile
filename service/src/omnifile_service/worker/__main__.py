"""Run the worker (and the verifier job): ``python -m omnifile_service.worker``.

Environment:
- the catalog: on Azure ``OMNIFILE_PG_HOST``, ``_DATABASE``, ``_USER``, ``_SSLMODE`` and
  ``AZURE_CLIENT_ID`` (Entra sign-in as the managed identity, `catalog.connect`); in dev
  and tests ``OMNIFILE_DATABASE_URL`` (direct connection).
- ``OMNIFILE_STUB_AFTER_MINUTES``: M1's "stub after N minutes" (default 60),
  for datasets without an M3 tiering policy.
- ``OMNIFILE_STORAGE_MOVER``: the migration engine's copier, a JSON
  `migrate.storage_mover.StorageMoverConfig` (Azure Storage Mover, with the
  worker's managed identity). Unset: no copies are started (``copy:
  external`` locations still verify and cut over).
- ``OMNIFILE_ENROL_CRL_URL`` and ``OMNIFILE_ENROL_AUDIENCE``: enrol's
  ``/internal/v0/crl`` and its token audience, for uploading the edge CA's
  CRL to the Roles Anywhere trust anchors named in ``OMNIFILE_AWS_ACCOUNTS``
  (decision 50, `crl_jobs.py`). Needed only when a trust anchor is named.
- ``OMNIFILE_RECALL_EDGE_SILENT_MINUTES`` and ``OMNIFILE_RECALL_NO_FILL_MINUTES``
  (default 60 each, 0: off): fail a `downloading` recall whose edge has sent no
  heartbeat, or has reported no fill running, for that long
  (`recall_timeout.py`). Never on how long the fill takes.
- ``OMNIFILE_PG_POOL_MAX``: at most this many catalog connections, shared by every
  queue and sweep (`pool.py`). Unset: one per queue, kept, and one per running sweep.

Dev and end-to-end only (need ``OMNIFILE_DEV_MODE=1``):
- catalog backends of kind ``sim``: the worker refuses to start with one
  otherwise (it would keep files in memory).
- ``OMNIFILE_DEV_CLOCK_FILE``: the shared virtual clock
  (`backends.sim.clock.OffsetClock`), moved by sim_http's ``/_sim/advance``.
  The worker's "now", and so the scheduler's days and the restore polls,
  follow it.
- ``OMNIFILE_WORKER_MAX_SWEEP_S``: run every sweep at least this often.
- ``OMNIFILE_RECALL_POLL_S``: poll restores (and re-check held High
  recalls) at least this often, instead of every 1 to 15 minutes.
- ``OMNIFILE_STORAGE_MOVER=sim``: copy between the worker's own (sim)
  backends with Storage Mover's semantics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta

import psycopg

from .. import metrics
from ..backends.sim.clock import DevClockRefused, OffsetClock, dev_clock, require_sim_only
from ..catalog.connect import aconnect, from_env_or_exit, pool_max_or_exit
from ..migrate.storage_mover import from_environment
from .backends import BackendRegistry, sim_refusal
from .pool import PooledConnect, worker_pool
from .recalls import RecallConfig
from .runner import Worker
from .stubs import StubPolicy

log = logging.getLogger("omnifile_service.worker")


def _dev_seconds(name: str) -> float | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    if os.environ.get("OMNIFILE_DEV_MODE") != "1":
        raise SystemExit(f"{name} is a dev setting: it needs OMNIFILE_DEV_MODE=1")
    value = float(raw)
    if value <= 0:
        raise SystemExit(f"{name} must be > 0")
    return value


async def main() -> None:
    db = from_env_or_exit("worker")
    try:
        metrics_on = metrics.configure("worker")
    except ValueError as e:
        raise SystemExit(str(e)) from None

    async def direct() -> psycopg.AsyncConnection:
        return await aconnect(db, autocommit=True)

    # OMNIFILE_PG_POOL_MAX: at most that many connections, shared by every consumer and sweep
    # (worker/pool.py). Unset: a connection per consumer, kept, and one per running sweep.
    budget = pool_max_or_exit(None)
    connect = direct if budget is None else worker_pool(db, budget)

    try:
        clock = dev_clock()
    except DevClockRefused as e:
        raise SystemExit(str(e)) from None
    conn = await connect()
    try:
        registry = await BackendRegistry.load(conn, clock=clock)
    finally:
        await conn.close()
    if (why := sim_refusal(registry.kinds(), os.environ)) is not None:
        raise SystemExit(why)
    try:
        require_sim_only(clock, registry.kinds())
    except DevClockRefused as e:
        raise SystemExit(str(e)) from None
    if isinstance(clock, OffsetClock):
        log.warning("dev clock %s: now %s", clock.path, clock.now())
    minutes = int(os.environ.get("OMNIFILE_STUB_AFTER_MINUTES", "60"))
    recall = RecallConfig()
    if (poll := _dev_seconds("OMNIFILE_RECALL_POLL_S")) is not None:
        step = timedelta(seconds=poll)
        recall = RecallConfig(poll_min=step, poll_max=step, poll_no_eta=step, hold_recheck_max=step)
    sweep = _dev_seconds("OMNIFILE_WORKER_MAX_SWEEP_S")
    mover_raw = os.environ.get("OMNIFILE_STORAGE_MOVER")
    if mover_raw == "sim" and os.environ.get("OMNIFILE_DEV_MODE") != "1":
        raise SystemExit("OMNIFILE_STORAGE_MOVER=sim is a dev setting: needs OMNIFILE_DEV_MODE=1")
    try:
        mover = from_environment(mover_raw, registry)
    except ValueError as e:
        raise SystemExit(f"OMNIFILE_STORAGE_MOVER: {e}") from None
    worker = Worker(
        connect,
        registry,
        clock=clock,
        stub_policy=StubPolicy(after=timedelta(minutes=minutes)),
        recall_config=recall,
        max_sweep_every=None if sweep is None else timedelta(seconds=sweep),
        mover=mover,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = []
    if metrics_on:
        collector = metrics.Collector(connect)
        tasks.append(asyncio.create_task(collector.run_forever(stop)))
    try:
        await worker.run_forever(stop)
    finally:
        for t in tasks:
            await t
        metrics.shutdown()
        await registry.close()
        if hasattr(mover, "aclose"):
            await mover.aclose()
        if isinstance(connect, PooledConnect):
            await connect.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per sim/Azure call
    asyncio.run(main())
