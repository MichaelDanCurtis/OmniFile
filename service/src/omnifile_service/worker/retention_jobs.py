"""The versioning, WORM, change-feed and reconcile jobs (M3), for
`runner.Worker`.

Queues:
- ``retention``: ``{"op": "delete_blob", "location_id"}`` (a pruned
  version's bytes), ``{"op": "worm_policy", "share_id"}`` (the
  container policy) and ``{"op": "annex_purge", "annex_id", "file_id"}``
  (an ArchiveAdmin purges the copy of a file deleted on a NAS, annex/purge.py).
- ``reconcile``: ``{"backend_id", "container"}``, an on-demand reconcile.

Sweeps (intervals in `RetentionConfig`):
- ``worm_autolock``: lock shares whose validation window ended;
- ``worm_policy``: container policies that lag the catalog;
- ``worm_stamp``: per-version retention, legal hold and state tags;
- ``prune_versions``: versions beyond the rewind depth;
- ``replaced_secondaries``: once a day across replicas, however often they restart, queues
  one ``{"op": "retire_left_behind"}``. That message, on ``retention``, retires what a file's
  split left behind (decision 66): an old row whose retention has ended, or a copy whose
  message is gone. It works through them a page at a time, one straight after another, and
  its last page writes the audit row the next day's check reads. A message hidden for a day
  would read as a stuck queue, so nothing waits in the queue between passes;
- ``annex_deleted_retention``: copies of files deleted on a NAS, past their
  annex's optional retention (off by default);
- ``changefeed_sync``: provider change records into the catalog, with lag;
- ``recall_copy_expiry``: recall copies past the user's window;
- ``reconcile_nightly``: inventory or listing vs catalog, per container.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import psycopg
from psycopg.types.json import Jsonb

from ..annex.purge import purge_due, purge_file
from ..reconcile.run import (
    SourcesSeeding,
    reconcile_container,
    reconcile_due,
    register_known_containers,
)
from ..retention.common import set_actor
from ..retention.depth import RETENTION_QUEUE, prune_versions
from ..retention.worm import lock_due_shares
from ..retention.worm_sync import (
    delete_blob,
    expire_recall_copies,
    retire_replaced_secondaries,
    stale_policy_shares,
    stamp_worm,
    sync_share_policy,
)
from .changefeed_sync import sync_all
from .jobs import Job, QueueJob, SweepJob
from .queue import Message, Permanent, Retry, outside, send

if TYPE_CHECKING:
    from .runner import Worker

RECONCILE_QUEUE = "reconcile"
LEFT_BEHIND_OP = "retire_left_behind"
LEFT_BEHIND_EVERY = timedelta(days=1)
LEFT_BEHIND_SWEPT = "retention.left_behind.swept"
#: Did a pass finish in the last day? By the audit log's time index. `at` is the worker's `now`,
#: as `tiering` compares its reports; the real time only bounds what is read.
LEFT_BEHIND_SWEPT_SQL = f"""
SELECT 1 FROM catalog.audit_log
 WHERE at > now() - interval '2 days' AND action = '{LEFT_BEHIND_SWEPT}'
   AND (details->>'at')::timestamptz > %(now)s - %(every)s
 LIMIT 1
"""
#: A bucket still seeding is reconciled later.
SEEDING_RETRY_S = 900
#: Longer than a listing should take; only this message waits if its worker dies.
RECONCILE_HIDE_SECONDS = 12 * 3600


@dataclass(frozen=True)
class RetentionConfig:
    autolock_every: timedelta = timedelta(minutes=5)
    stamp_every: timedelta = timedelta(minutes=1)
    prune_every: timedelta = timedelta(hours=1)
    prune_grace: timedelta = timedelta(hours=24)
    replaced_every: timedelta = timedelta(hours=1)
    changefeed_every: timedelta = timedelta(seconds=30)
    recall_copy_window: timedelta = timedelta(days=7)
    reconcile_every: timedelta = timedelta(hours=20)
    reconcile_check_every: timedelta = timedelta(hours=1)


async def _left_behind_swept(conn: psycopg.AsyncConnection, now: datetime, retired: int) -> None:
    await set_actor(conn, "system:retention_delete")
    await conn.execute(
        "SELECT catalog.audit(%s, NULL, NULL, NULL, NULL, %s)",
        (LEFT_BEHIND_SWEPT, Jsonb({"at": now.isoformat(), "retired": retired})),
    )


def retention_jobs(worker: Worker, config: RetentionConfig | None = None) -> list[Job]:
    cfg = config or getattr(worker, "retention_config", None) or RetentionConfig()
    registry = worker.registry

    async def retention(msg: Message, conn: psycopg.AsyncConnection) -> None:
        now = worker.clock.now()
        op = msg.body.get("op")
        try:
            if op == "delete_blob":
                await delete_blob(conn, registry, int(msg.body["location_id"]), now)
            elif op == "worm_policy":
                await sync_share_policy(conn, registry, int(msg.body["share_id"]), now)
            elif op == LEFT_BEHIND_OP:
                retired, more = await retire_replaced_secondaries(
                    conn, now, after=int(msg.body.get("after", 0))
                )
                retired += int(msg.body.get("retired", 0))
                if more is not None:
                    raise Retry(0, {"op": op, "after": str(more), "retired": retired})
                await _left_behind_swept(conn, now, retired)
            elif op == "annex_purge":
                await purge_file(conn, int(msg.body["annex_id"]), int(msg.body["file_id"]), now)
            else:
                raise Permanent(f"unknown retention op {op!r}")
        except (KeyError, TypeError, ValueError) as e:
            raise Permanent(f"malformed retention message {dict(msg.body)!r}: {e}") from None

    async def reconcile(msg: Message, conn: psycopg.AsyncConnection) -> None:
        try:
            bid = int(msg.body["backend_id"])
            container = str(msg.body["container"])
        except (KeyError, TypeError, ValueError) as e:
            raise Permanent(f"malformed reconcile message {dict(msg.body)!r}: {e}") from None
        try:
            # A whole listing can take hours: on this connection, with the consumer's
            # transaction closed, as the nightly sweep runs it.
            await outside(
                msg,
                "reconcile",
                lambda: reconcile_container(conn, registry, bid, container, worker.clock.now()),
                hide_for=RECONCILE_HIDE_SECONDS,
            )
        except SourcesSeeding as e:
            raise Retry(SEEDING_RETRY_S) from e

    async def autolock(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return len(await lock_due_shares(conn, now))

    async def policies(conn: psycopg.AsyncConnection, now: datetime) -> int:
        done = 0
        for share_id in await stale_policy_shares(conn):
            try:
                await sync_share_policy(conn, registry, share_id, now)
                done += 1
            except (Retry, Permanent):
                pass
        return done

    async def stamp(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return (await stamp_worm(conn, registry, now)).stamped

    async def prune(conn: psycopg.AsyncConnection, now: datetime) -> int:
        total = 0
        while True:
            result = await prune_versions(conn, now, grace=cfg.prune_grace)
            total += len(result.versions)
            if not result.versions:
                return total

    async def replaced(conn: psycopg.AsyncConnection, now: datetime) -> int:
        """One message across replicas: the first to find none sends it."""
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext('omnifile:retire-left-behind'))"
            )
            (mine,) = await cur.fetchone()  # type: ignore[misc]
            if not mine:
                return 0
            cur = await conn.execute(
                "SELECT 1 FROM pgmq.q_retention WHERE message->>'op' = %s LIMIT 1",
                (LEFT_BEHIND_OP,),
            )
            if await cur.fetchone() is not None:
                return 0
            cur = await conn.execute(
                LEFT_BEHIND_SWEPT_SQL, {"now": now, "every": LEFT_BEHIND_EVERY}
            )
            if await cur.fetchone() is not None:
                return 0
            await send(conn, RETENTION_QUEUE, {"op": LEFT_BEHIND_OP})
        return 1

    async def changefeed(conn: psycopg.AsyncConnection, now: datetime) -> int:
        await register_known_containers(conn)
        return (await sync_all(conn, registry, now)).records

    async def recall_copies(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await expire_recall_copies(conn, now, window=cfg.recall_copy_window)

    async def annex_retention(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await purge_due(conn, now)

    async def nightly(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return len(await reconcile_due(conn, registry, now, every=cfg.reconcile_every))

    return [
        QueueJob(RETENTION_QUEUE, retention),
        QueueJob(RECONCILE_QUEUE, reconcile),
        SweepJob("worm_autolock", cfg.autolock_every, autolock),
        SweepJob("worm_policy", cfg.autolock_every, policies),
        SweepJob("worm_stamp", cfg.stamp_every, stamp),
        SweepJob("prune_versions", cfg.prune_every, prune),
        SweepJob("replaced_secondaries", cfg.replaced_every, replaced),
        SweepJob("annex_deleted_retention", cfg.prune_every, annex_retention),
        SweepJob("changefeed_sync", cfg.changefeed_every, changefeed),
        SweepJob("recall_copy_expiry", cfg.prune_every, recall_copies),
        SweepJob("reconcile_nightly", cfg.reconcile_check_every, nightly),
    ]
