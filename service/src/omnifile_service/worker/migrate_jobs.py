"""The `migrate` and `migrate_verify` queues (docs/design/migration-engine.md).

`migrate` steps one migration prefix per message, each step queueing the
next; `migrate_verify` checks one copy. The copier is the worker's `mover`
(Azure Storage Mover in production; None: `copy_by = omnifile` prefixes
wait with an error, `external` ones still verify and cut over).

No transaction is open during a provider call (`migrate/calls.py`): the step
runs again from the top after each one, and only its last pass writes. A
savepoint rolls back each earlier pass, and a provider error too, before the
message is retried, so a step never leaves half its work behind (a job row
with no job, say); what it asked the provider for is idempotent or found
again by the next step. The error goes on the prefix's `last_error`.

With no row locked through a call, each step holds its prefix's lease, a
session advisory lock, so two messages for a prefix never both call. It costs
no transaction, a crash frees it, and the consumer lets it go at the end of
the message.

Both queues hold a message longer than the default minute, and each call hides
it for longer than the call's bound. A verification reads a whole copy (hundreds
of GB) back. Both read one message at a time: a step can copy for hours, and a
message read with it would come back to another worker meanwhile.

A copy that can't be checked says so on its prefix at the first error, and is tried
further apart each time. Before its last try the storage is asked for the object: no answer
is an outage, so the try waits, as long as `redrive.RedriveConfig.outage_limit`. An answer is
the provider up, so the last try goes ahead. Past that it dead-letters, where the alert sees
it, and the progress sweep counts the letter for as long as it is there (`migrate/progress.py`).
Its copy stays pending, so the prefix doesn't cut over around it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from ..backends.base import BackendError
from ..migrate import QUEUE, VERIFY_QUEUE, migrate_step, verify_copy
from ..migrate.calls import STEP_VT_S
from ..migrate.plans import plan_sweep
from ..migrate.progress import progress_sweep
from ..migrate.waves import close_abandoned
from .jobs import Job, QueueJob, SweepJob
from .queue import Defer, Message, Permanent, Retry, outside
from .redrive import HELD_SINCE_KEY, Probe, RedriveConfig
from .verify import READ_ERRORS_KEY

#: How long a read message stays hidden: one verification's full read.
VERIFY_VT_S = 2 * 60 * 60
#: Another message's step holds the prefix: look again this soon. The step may be hours
#: into a copy, and each look counts as a read, so not often.
LEASE_BUSY_S = 5 * 60
#: Tries at a copy's check, a minute apart at first and an hour at most: about 6 hours to the
#: last one. A read too slow for its bound costs a whole read each time, so not many.
VERIFY_ERRORS = 12
VERIFY_ERROR_DELAY_S = 60.0
VERIFY_ERROR_MAX_DELAY_S = 3600.0
#: How a prefix's `last_error` from here starts. It names the copy, which clears only its own.
CHECK_FAILED = "checking a copy failed"
#: How often an outage's hold looks again, and how long it can last.
OUTAGE = RedriveConfig()
ABANDONED_WAVES_EVERY = timedelta(hours=1)

if TYPE_CHECKING:
    from .runner import Worker


def migrate_jobs(worker: Worker) -> list[Job]:
    def _id(msg: Message, name: str) -> int:
        try:
            return int(msg.body[name])
        except (KeyError, TypeError, ValueError) as e:
            raise Permanent(f"malformed {name} message {dict(msg.body)!r}: {e}") from None

    async def step(msg: Message, conn: psycopg.AsyncConnection) -> None:
        mid = _id(msg, "migration_id")
        if msg.consumed:
            # Run by a test or a CLI otherwise, in its own transaction, with nothing to
            # release it. A session lock: it lasts through the commit before each call,
            # and each pass of this message takes it again, on the same connection.
            cur = await conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (f"omnifile.migration-step:{mid}",),
            )
            row = await cur.fetchone()
            if not (row and row[0]):
                raise Defer(LEASE_BUSY_S)
        try:
            async with conn.transaction():
                await migrate_step(
                    conn, worker.registry.get, worker.mover, mid, worker.clock.now(), msg
                )
        except BackendError as e:
            await conn.execute(
                "UPDATE catalog.migration_prefix SET last_error = %s WHERE migration_id = %s",
                (f"provider error, retrying: {e}"[:1000], mid),
            )
            raise Retry(60) from e

    async def note(conn: psycopg.AsyncConnection, mid: Any, error: str) -> None:
        """On the prefix, while its state can use it. A cut-over prefix's `last_error` is its
        record of what it left behind, and the `staging-left` alert reads that."""
        await conn.execute(
            "UPDATE catalog.migration_prefix SET last_error = %s WHERE migration_id = %s"
            " AND state IN ('pending', 'copying', 'verifying', 'verified')",
            (error[:1000], mid),
        )

    def check_error(lid: int) -> str:
        return f"{CHECK_FAILED} (location {lid}):"

    async def clear_check_error(conn: psycopg.AsyncConnection, mid: Any, lid: int) -> None:
        await conn.execute(
            "UPDATE catalog.migration_prefix SET last_error = NULL"
            " WHERE migration_id = %s AND starts_with(last_error, %s)",
            (mid, check_error(lid)),
        )

    async def hold_in_an_outage(
        msg: Message, conn: psycopg.AsyncConnection, lid: int, mid: Any, errors: int
    ) -> None:
        """Before the last try: if the storage holding this copy, or the object its checksum
        must come from, gives no answer, this is an outage and not the copy's fault, so wait.
        It spends no read. An answer, even "not found", or the limit passing: on to the try."""
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT backend_id, container, object_key, backend_version_id FROM catalog.location"
            " WHERE location_id = %(l)s"
            " UNION ALL SELECT s.backend_id, s.container, s.object_key, s.backend_version_id"
            "  FROM catalog.location t JOIN catalog.location s ON s.version_id = t.version_id"
            " WHERE t.location_id = %(l)s AND s.location_id <> t.location_id"
            "   AND s.state = 'committed' AND s.crc64nvme IS NULL",
            {"l": lid},
        )
        rows = await cur.fetchall()
        probe = Probe(worker.registry, OUTAGE)

        async def what_came_back() -> str | None:
            for r in rows:
                if await probe.no_answer(r):
                    return probe.why.get(r["backend_id"], "no answer")
            return None

        came_back = await outside(msg, "outage_probe", what_came_back)
        if came_back is None:
            return
        now = worker.clock.now()
        raw = msg.body.get(HELD_SINCE_KEY)
        since = datetime.fromisoformat(raw) if raw else now
        if now - since >= OUTAGE.outage_limit:
            return
        await note(
            conn,
            mid,
            f"{check_error(lid)} {errors} of {VERIFY_ERRORS} times. Asking the storage for it"
            f" gave {came_back}: the last try waits for it",
        )
        raise Retry(VERIFY_ERROR_MAX_DELAY_S, {**msg.body, HELD_SINCE_KEY: since.isoformat()})

    async def verify(msg: Message, conn: psycopg.AsyncConnection) -> None:
        lid = _id(msg, "location_id")
        errors = int(msg.body.get(READ_ERRORS_KEY, 0))
        if errors >= VERIFY_ERRORS:
            raise Permanent(f"the check of location {lid} failed {errors} times")
        mid = msg.body.get("migration_id")
        if errors == VERIFY_ERRORS - 1:
            await hold_in_an_outage(msg, conn, lid, mid, errors)
        try:
            async with conn.transaction():
                await verify_copy(conn, worker.registry.get, lid, worker.clock.now(), msg)
        except BackendError as e:
            errors += 1
            done = errors >= VERIFY_ERRORS
            if done:
                # The dead letter says it from here, for as long as it is there.
                await clear_check_error(conn, mid, lid)
            else:
                await note(
                    conn,
                    mid,
                    f"{check_error(lid)} {errors} of {VERIFY_ERRORS} times, retrying: {e}",
                )
            delay = VERIFY_ERROR_DELAY_S * 2 ** (errors - 1)
            raise Retry(
                0 if done else min(VERIFY_ERROR_MAX_DELAY_S, delay),
                {**msg.body, READ_ERRORS_KEY: errors},
            ) from e
        if errors:
            await clear_check_error(conn, mid, lid)

    def has_batch(backend_id: int) -> bool:
        try:
            backend = worker.registry.get(backend_id)
        except KeyError:
            return False
        return getattr(getattr(backend, "config", None), "batch", None) is not None

    async def progress(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await progress_sweep(conn, now, has_batch)

    async def agents(conn: psycopg.AsyncConnection, now: datetime) -> int:
        """The Storage Mover agents, as ARM lists them, for the console
        (web reads only the catalog). No mover, or one without agents (sim):
        nothing to read."""
        lister = getattr(worker.mover, "list_agents", None)
        if lister is None:
            return 0
        found = await lister()
        async with conn.transaction():
            for a in found:
                await conn.execute(
                    "INSERT INTO catalog.mover_agent (name, status, version, host, arc_resource_id,"
                    " last_status_at, error, polled_at) VALUES (%(name)s, %(status)s, %(version)s,"
                    " %(host)s, %(arc_resource_id)s, %(last_status_at)s, %(error)s, %(now)s)"
                    " ON CONFLICT (name) DO UPDATE SET status = EXCLUDED.status,"
                    " version = EXCLUDED.version, host = EXCLUDED.host,"
                    " arc_resource_id = EXCLUDED.arc_resource_id,"
                    " last_status_at = EXCLUDED.last_status_at, error = EXCLUDED.error,"
                    " polled_at = EXCLUDED.polled_at",
                    {**a, "now": now},
                )
            # An agent ARM no longer lists is gone.
            await conn.execute(
                "DELETE FROM catalog.mover_agent WHERE NOT (name = ANY(%s))",
                ([a["name"] for a in found],),
            )
        return len(found)

    base = worker.consumer_config
    return [
        QueueJob(QUEUE, step, replace(base, visibility_timeout=STEP_VT_S, batch=1)),
        QueueJob(VERIFY_QUEUE, verify, replace(base, visibility_timeout=VERIFY_VT_S, batch=1)),
        # Staged plans start their waves on time (migrate/plans.py).
        SweepJob("migration_plans", timedelta(minutes=1), plan_sweep),
        SweepJob("mover_agents", timedelta(minutes=5), agents),
        # The console's rolled-up progress (migrate/progress.py).
        SweepJob("migration_progress", timedelta(minutes=5), progress),
        # Waves under a prefix no step will come back to.
        SweepJob("abandoned_waves", ABANDONED_WAVES_EVERY, close_abandoned),
    ]
