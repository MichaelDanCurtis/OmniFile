"""The `seed` queue: one batch of a source per message, each queueing the next
(docs/design/catalog-seeding.md section 6). And the journal sweep that keeps
seeded `s3_metadata` sources current (section 8, `journal_sync.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import psycopg

from ..sources.model import SourceError
from ..sources.run import SEED_QUEUE, seed_step
from .jobs import Job, QueueJob, SweepJob
from .queue import Message, Permanent

if TYPE_CHECKING:
    from .runner import Worker

#: S3 Metadata journal rows are "near real time" [D]; the EventBridge feed
#: is read every minute too.
JOURNAL_EVERY = timedelta(minutes=1)


def seed_jobs(worker: Worker) -> list[Job]:
    async def seed(msg: Message, conn: psycopg.AsyncConnection) -> None:
        try:
            source_id = int(msg.body["source_id"])
        except (KeyError, TypeError, ValueError) as e:
            raise Permanent(f"malformed seed message {dict(msg.body)!r}: {e}") from None
        try:
            await seed_step(conn, worker.registry.get, source_id, worker.clock.now(), msg=msg)
        except SourceError as e:  # a source that doesn't exist
            raise Permanent(str(e)) from None

    async def journal(conn: psycopg.AsyncConnection, now: datetime) -> int:
        from .journal_sync import journal_all

        return await journal_all(conn, worker.registry, now)

    return [QueueJob(SEED_QUEUE, seed), SweepJob("metadata_journal", JOURNAL_EVERY, journal)]
