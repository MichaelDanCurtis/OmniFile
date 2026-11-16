"""The policy and tiering track's worker jobs (registered by `runner.Worker`).

- ``tiering``: `tiering.handle_move` runs the scheduler's `set_class` moves.
- ``restores``: `tiering.handle_restore_budgeted` replaces the M1 consumer,
  so every restore passes the per-account 10 GiB/h High budget first.
- ``tiering_sweep``: `policy.scheduler.sweep` every minute.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import psycopg

from ..policy import scheduler
from .jobs import Job, QueueJob, SweepJob
from .queue import Message
from .recalls import VISIBILITY_SECONDS
from .tiering import MOVE_VISIBILITY_SECONDS, QUEUE, handle_move, handle_restore_budgeted

if TYPE_CHECKING:
    from .runner import Worker

SWEEP_EVERY = timedelta(minutes=1)


def tiering_jobs(worker: Worker) -> list[Job]:
    async def moves(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_move(msg, conn, registry=worker.registry, now=worker.clock.now())

    async def restores(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(
            msg, conn, registry=worker.registry, now=worker.clock.now(),
            config=worker.recall_config,
        )  # fmt: skip

    async def sweep(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return int(await scheduler.sweep(conn, now))

    return [
        # One at a time: messages waiting in a batch behind a long copy would
        # use up their own visibility timeout there.
        QueueJob(
            QUEUE,
            moves,
            replace(worker.consumer_config, visibility_timeout=MOVE_VISIBILITY_SECONDS, batch=1),
        ),
        QueueJob(
            scheduler.RESTORES,
            restores,
            replace(worker.consumer_config, visibility_timeout=VISIBILITY_SECONDS),
        ),
        SweepJob("tiering_sweep", SWEEP_EVERY, sweep),
    ]
