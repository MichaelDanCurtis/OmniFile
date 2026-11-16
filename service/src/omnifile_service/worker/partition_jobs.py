"""Time partitions of the feed, the audit log (migration 0003) and the annex
events (0038).

`catalog.feed_event` is partitioned by week, `catalog.audit_log` by month and
`catalog.annex_event` by day (14 days ahead; never dropped here).
Each has a DEFAULT partition, which should stay empty: a row there means its
partition was missing when it was written, and creating that partition then
fails until the rows are moved. So once a day the worker:

- creates partitions ahead (8 weeks of feed, 6 months of audit);
- drops feed partitions older than the feed's retention, recording the last
  cursor dropped (`catalog.feed_retention`): a cursor at or below it gets
  `reset_required` (edges) or `410` (partners). Audit partitions are never
  dropped here;
- logs rows found in a DEFAULT partition. The metrics collector reports them
  too (`omnifile.catalog.default_partition_rows`), and an alert fires on any.

All times are the database's, as `ensure_time_partitions` uses.

``OMNIFILE_FEED_RETENTION_DAYS`` sets the feed's retention (default 35, at
least 7: `edge-metadata.md` section 12, item 14).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import psycopg

from .jobs import Job, SweepJob

if TYPE_CHECKING:
    from .runner import Worker

log = logging.getLogger(__name__)

EVERY = timedelta(hours=24)
FEED_WEEKS_AHEAD = 8
AUDIT_MONTHS_AHEAD = 6
#: Annex events (migration 0038) are partitioned by day; never dropped here.
ANNEX_DAYS_AHEAD = 14
DEFAULT_FEED_RETENTION_DAYS = 35
MIN_FEED_RETENTION_DAYS = 7


def feed_retention_days(env: Mapping[str, str] = os.environ) -> int:
    raw = env.get("OMNIFILE_FEED_RETENTION_DAYS")
    if not raw:
        return DEFAULT_FEED_RETENTION_DAYS
    days = int(raw)
    if days < MIN_FEED_RETENTION_DAYS:
        raise ValueError(f"OMNIFILE_FEED_RETENTION_DAYS must be at least {MIN_FEED_RETENTION_DAYS}")
    return days


async def maintain_partitions(conn: psycopg.AsyncConnection, retention_days: int) -> int:
    """One pass. Returns the number of partitions created or dropped."""
    before = await _count(conn)
    async with conn.transaction():
        await conn.execute(
            "SELECT catalog.ensure_time_partitions('catalog.feed_event', 'week', %s)",
            (FEED_WEEKS_AHEAD,),
        )
        await conn.execute(
            "SELECT catalog.ensure_time_partitions('catalog.audit_log', 'month', %s)",
            (AUDIT_MONTHS_AHEAD,),
        )
        await conn.execute(
            "SELECT catalog.ensure_time_partitions('catalog.annex_event', 'day', %s)",
            (ANNEX_DAYS_AHEAD,),
        )
    created = await _count(conn) - before
    async with conn.transaction():
        row = await (
            await conn.execute(
                "SELECT catalog.feed_drop_before(now() - make_interval(days => %s))",
                (retention_days,),
            )
        ).fetchone()
    dropped = int(row[0]) if row else 0
    if created or dropped:
        log.info("partitions: %d created, %d feed partitions dropped", created, dropped)
    rows = await (
        await conn.execute(
            "SELECT parent::text, sampled_rows FROM catalog.default_partition_rows()"
        )
    ).fetchall()
    for parent, n in rows:
        log.error(
            "%s has rows in its DEFAULT partition (%d sampled): a partition was missing when"
            " they were written; move them before creating that range (runbook:"
            " deploy/azure/README.md)",
            parent,
            n,
        )
    return created + dropped


async def _count(conn: psycopg.AsyncConnection) -> int:
    row = await (await conn.execute("SELECT count(*) FROM catalog.time_partition")).fetchone()
    return int(row[0]) if row else 0


def partition_jobs(worker: Worker) -> list[Job]:
    days = feed_retention_days()

    async def run(conn: psycopg.AsyncConnection, _now: datetime) -> int:
        return await maintain_partitions(conn, days)

    return [SweepJob("partitions", EVERY, run)]
