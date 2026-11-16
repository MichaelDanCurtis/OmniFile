"""The `snapshot_export` queue: one part of a bulk snapshot export per
message, each queueing the next, and the sweep that expires old exports
(`exports.py`; control-api.md section 6)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import psycopg

from .. import exports
from .jobs import Job, QueueJob, SweepJob
from .queue import ConsumerConfig, Message, Permanent

if TYPE_CHECKING:
    from .runner import Worker

#: A part of 1M rows takes about a minute to read, compress and upload.
STEP_VT_S = 900
EXPIRE_EVERY = timedelta(hours=1)


def export_jobs(worker: Worker) -> list[Job]:
    async def part(msg: Message, conn: psycopg.AsyncConnection) -> None:
        try:
            export_id = int(msg.body["export_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise Permanent(f"malformed snapshot_export message {dict(msg.body)!r}: {e}") from None
        await exports.step(conn, worker.registry.get, export_id)

    async def expire(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await exports.expire(conn, now, worker.registry.get)

    return [
        QueueJob(exports.QUEUE, part, ConsumerConfig(visibility_timeout=STEP_VT_S, batch=1)),
        SweepJob("snapshot_export_expiry", EXPIRE_EVERY, expire),
    ]
