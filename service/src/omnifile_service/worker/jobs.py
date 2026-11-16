"""Units of work the worker runs, contributed by each track.

- `QueueJob`: a PGMQ consumer for one queue (`worker.queue.Consumer`).
- `SweepJob`: a periodic function, ``async fn(conn, now) -> int``, on an
  autocommit connection (the function opens its own transactions). It
  returns how much it did, for logs and tests.

`runner.Worker` collects jobs from `worker.retention_jobs.retention_jobs`
and `worker.tiering_jobs.tiering_jobs`. A `QueueJob` for a queue the worker
already consumes replaces the built-in consumer, so a queue never has two
handlers with different rules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from .queue import ConsumerConfig, Handler

SweepFn = Callable[[psycopg.AsyncConnection, datetime], Awaitable[int]]


@dataclass(frozen=True)
class QueueJob:
    queue: str
    handler: Handler
    config: ConsumerConfig | None = None  # None: the worker's consumer_config


@dataclass(frozen=True)
class SweepJob:
    name: str
    every: timedelta
    fn: SweepFn


Job = QueueJob | SweepJob

__all__ = ["Job", "QueueJob", "SweepFn", "SweepJob"]
