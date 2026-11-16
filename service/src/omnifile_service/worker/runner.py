"""Wire the worker: one consumer per queue, plus periodic sweeps.

Built in (M1): the `restores`, `edge_instructions` and `verify` (upload
read-back, `verify.py`) consumers and the stub sweep. M3 tracks add
`jobs.Job`s: `retention_jobs.retention_jobs(worker)` (versioning, WORM,
change-feed sync, reconcile) and `tiering_jobs.tiering_jobs(worker)` (policy
and tiering). A `QueueJob` for a queue that is already consumed replaces
that consumer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import psycopg

from ..backends.base import Clock, SystemClock
from .backends import BackendRegistry
from .jobs import Job, QueueJob, SweepJob
from .queue import Consumer, ConsumerConfig, Message
from .recalls import QUEUE as RESTORES
from .recalls import RecallConfig, handle_restore
from .stubs import QUEUE as EDGE_INSTRUCTIONS
from .stubs import StubPolicy, handle_edge_instruction, sweep_stub_candidates
from .verify import verify_jobs

log = logging.getLogger(__name__)

Connect = Callable[[], Awaitable[psycopg.AsyncConnection]]


@dataclass
class Worker:
    connect: Connect
    registry: BackendRegistry
    clock: Clock = field(default_factory=SystemClock)
    stub_policy: StubPolicy = field(default_factory=StubPolicy)
    recall_config: RecallConfig = field(default_factory=RecallConfig)
    consumer_config: ConsumerConfig = field(default_factory=ConsumerConfig)
    sweep_every: timedelta = timedelta(minutes=1)
    #: Load the M3 tracks' jobs (retention_jobs, tiering_jobs).
    track_jobs: bool = True
    #: The retention track's intervals (`retention_jobs.RetentionConfig`); None: defaults.
    retention_config: Any = None
    #: Dev and e2e: run every sweep at least this often (virtual time moves
    #: days in seconds, so hourly sweeps must run every few seconds).
    max_sweep_every: timedelta | None = None
    #: More jobs (tests); applied last, so they replace same-named queues.
    extra_jobs: list[Job] = field(default_factory=list)
    #: The migration engine's copier (`migrate.Mover`): Azure Storage Mover in
    #: production, `SimMover` in tests and e2e; None: no copies are started.
    mover: Any = None

    def __post_init__(self) -> None:
        async def restores(msg: Message, conn: psycopg.AsyncConnection) -> None:
            await handle_restore(
                msg, conn, registry=self.registry, now=self.clock.now(), config=self.recall_config
            )

        async def instructions(msg: Message, conn: psycopg.AsyncConnection) -> None:
            await handle_edge_instruction(
                msg, conn, registry=self.registry, now=self.clock.now(), policy=self.stub_policy
            )

        self.restores = Consumer(self.connect, RESTORES, restores, self.consumer_config)
        self.instructions = Consumer(
            self.connect, EDGE_INSTRUCTIONS, instructions, self.consumer_config
        )
        self.consumers: dict[str, Consumer] = {
            RESTORES: self.restores,
            EDGE_INSTRUCTIONS: self.instructions,
        }
        self.sweeps: dict[str, SweepJob] = {}
        # Upload read-back (control-api.md section 4.2): nothing is stubbed
        # until the service has read the committed version back.
        jobs: list[Job] = list(verify_jobs(self.registry, self.consumer_config))
        if self.track_jobs:
            jobs += _track_jobs(self)
        jobs += self.extra_jobs
        for job in jobs:
            if isinstance(job, QueueJob):
                self.consumers[job.queue] = Consumer(
                    self.connect, job.queue, job.handler, job.config or self.consumer_config
                )
            else:
                self.sweeps[job.name] = job
        if self.max_sweep_every is not None:
            cap = self.max_sweep_every
            self.sweep_every = min(self.sweep_every, cap)
            self.sweeps = {n: replace(j, every=min(j.every, cap)) for n, j in self.sweeps.items()}
        self.restores = self.consumers[RESTORES]
        self.instructions = self.consumers[EDGE_INSTRUCTIONS]

    async def sweep(self) -> list[int]:
        conn = await self.connect()
        try:
            await conn.set_autocommit(True)
            return await sweep_stub_candidates(conn, self.clock.now(), self.stub_policy)
        finally:
            await conn.close()

    async def run_sweep(self, job: SweepJob) -> int:
        conn = await self.connect()
        try:
            await conn.set_autocommit(True)
            return await job.fn(conn, self.clock.now())
        finally:
            await conn.close()

    async def run_once(self) -> None:
        """Every sweep once, then drain every queue (tests and cron-style runs)."""
        await self.sweep()
        for job in self.sweeps.values():
            await self.run_sweep(job)
        for consumer in self.consumers.values():
            await consumer.drain()

    async def _sweeper(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                queued = await self.sweep()
                if queued:
                    log.info("stub sweep queued %d files", len(queued))
            except psycopg.Error as e:
                log.warning("stub sweep failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.sweep_every.total_seconds())
            except TimeoutError:
                pass

    async def _job_sweeper(self, job: SweepJob, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                done = await self.run_sweep(job)
                if done:
                    log.info("sweep %s did %d", job.name, done)
            except Exception as e:  # noqa: BLE001 - a sweep failure must not stop the worker
                log.warning("sweep %s failed: %s", job.name, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=job.every.total_seconds())
            except TimeoutError:
                pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.gather(
                *(c.run_forever(stop) for c in self.consumers.values()),
                self._sweeper(stop),
                *(self._job_sweeper(j, stop) for j in self.sweeps.values()),
            )
        finally:
            for c in self.consumers.values():
                await c.close()


def _track_jobs(worker: Worker) -> list[Job]:
    """Jobs from the M3, M4 and M5 tracks. Each module is optional, so one track's work
    in progress never stops the other's from loading."""
    jobs: list[Job] = []
    try:
        from .retention_jobs import retention_jobs
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.retention_jobs":
            raise
        log.debug("no retention jobs")
    else:
        jobs += retention_jobs(worker)
    try:
        from .tiering_jobs import tiering_jobs  # type: ignore[import-not-found]
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.tiering_jobs":
            raise
        log.debug("no tiering jobs")
    else:
        jobs += tiering_jobs(worker)
    try:
        from .portal_jobs import portal_jobs  # type: ignore[import-not-found]
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.portal_jobs":
            raise
        log.debug("no portal jobs")
    else:
        jobs += portal_jobs(worker)
    try:
        from .notify_jobs import notify_jobs  # type: ignore[import-not-found]
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.notify_jobs":
            raise
        log.debug("no notify jobs")
    else:
        jobs += notify_jobs(worker)
    try:
        from .seed_jobs import seed_jobs
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.seed_jobs":
            raise
        log.debug("no seed jobs")
    else:
        jobs += seed_jobs(worker)
    try:
        from .migrate_jobs import migrate_jobs
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.migrate_jobs":
            raise
        log.debug("no migrate jobs")
    else:
        jobs += migrate_jobs(worker)
    try:
        from .export_jobs import export_jobs
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.export_jobs":
            raise
        log.debug("no export jobs")
    else:
        jobs += export_jobs(worker)
    try:
        from .usage_jobs import usage_jobs
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.usage_jobs":
            raise
        log.debug("no usage jobs")
    else:
        jobs += usage_jobs(worker)
    from .partition_jobs import partition_jobs

    jobs += partition_jobs(worker)
    # A `downloading` recall whose edge is gone or idle for good fails.
    from .recall_timeout import recall_timeout_jobs

    jobs += recall_timeout_jobs(worker)
    # What a dead letter would lose: recalls, read-backs and deletes are sent again.
    from .redrive import redrive_jobs

    jobs += redrive_jobs(worker)
    # Decision 50: the edge CA's CRL to the Roles Anywhere trust anchors.
    from .crl_jobs import crl_jobs

    jobs += crl_jobs(worker)
    # The portal's Settings test sends (a card, an email, an AWS role assumption).
    from ..settings.worker_jobs import settings_jobs

    jobs += settings_jobs(worker)
    # Connect AWS's fetch, Storage Mover deployment and source scans (cloud-connect.md).
    from ..settings.cloud_jobs import cloud_jobs

    jobs += cloud_jobs(worker)
    return jobs
