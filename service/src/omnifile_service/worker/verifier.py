"""The verifier job: one pass over the `verify` queue, then exit.

    python -m omnifile_service.worker.verifier

Container Apps runs it as the `verifier` job (deploy/azure/main.bicep), on a schedule by default
(`verifierTrigger = 'Schedule'`, every 15 minutes), one replica per execution. It reads committed
uploads back, one message at a time, and checks their CRC64-NVME (`verify.handle_verify`, the
decision-24 read inside Azure) until the queue shows nothing visible or its time budget is
spent, then exits. A pass holds a Postgres advisory lock (`PASS_LOCK`), so a new execution
that starts while one still reads a big file does nothing. PGMQ's visibility timeout (2 hours,
`verify.VISIBILITY_S`, the worker's too) gives each message to one reader, and a reader that
finds the message already finished by another rolls back and re-sends nothing
(`queue.LostOwnership`). The worker consumes the same queue, so the job is extra read
capacity for a backlog, never the only reader. Its audit rows say `system:verifier`.

Exit status:
  0  every message it read was handled: verified, or put back for later by its own rules (a
     version not readable yet)
  1  a message failed (rolled back; retried with backoff, or dead-lettered), or the catalog was
     unreachable
Configuration errors end it like the worker's (a message and a non-zero status).

Only the `verify` queue. Its handler reads blobs and writes the catalog, which the verifier's
identity may do (Storage Blob Data Reader, database role omnifile_worker). A migration copy's
verification (`migrate_verify`) also stamps the copy's POSIX metadata, a blob write, so it stays
with the worker.

Environment: the worker's catalog settings (`__main__.py`), and
  OMNIFILE_VERIFIER_MAX_SECONDS  the pass's time budget (default 840: it ends before the next
                                 15-minute run; a message being read is finished first)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import psycopg

from .backends import BackendRegistry
from .queue import Consumer
from .verify import QUEUE, verify_jobs

log = logging.getLogger("omnifile_service.worker.verifier")

DEFAULT_MAX_SECONDS = 840.0
#: Its audit rows' actor (a failed read-back), so the verifier's work is told from the worker's.
ACTOR = "system:verifier"


@dataclass(frozen=True)
class PassResult:
    read: int
    verified: int
    retried: int
    failed: int
    dead: int
    out_of_time: bool
    #: Another holder finished the message first (queue.LostOwnership): nothing re-sent.
    lost: int = 0
    #: Another pass held PASS_LOCK.
    skipped: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failed or self.dead else 0


#: The session advisory lock a pass holds: one pass at a time, across executions (a schedule
#: can start one while the last still reads a big file) and replicas.
PASS_LOCK = 0x6F6D6E6976657269  # "omniveri"


async def run_pass(
    connect: Callable[[], Awaitable[psycopg.AsyncConnection]],
    registry: BackendRegistry,
    *,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> PassResult:
    """Drain the `verify` queue once, within `max_seconds`, one message at a time, and only
    while no other pass runs (`PASS_LOCK`; a pass that finds it held does nothing). The
    deadline is checked before each message, so a pass overruns it by one read at most."""
    (job,) = verify_jobs(registry, actor=ACTOR)
    assert job.config is not None and job.config.batch == 1
    lock = await connect()
    try:
        row = await (await lock.execute("SELECT pg_try_advisory_lock(%s)", (PASS_LOCK,))).fetchone()
        if not (row and row[0]):
            log.info("verifier: another pass is running; nothing to do")
            return PassResult(0, 0, 0, 0, 0, False, skipped=True)
        consumer = Consumer(connect, QUEUE, job.handler, job.config)
        deadline = clock() + max_seconds
        read, out_of_time = 0, False
        try:
            while True:
                if clock() >= deadline:
                    out_of_time = True
                    break
                n = await consumer.run_once()
                read += n
                if n == 0:
                    break
        finally:
            await consumer.close()
        s = consumer.stats
        return PassResult(read, s.acked, s.retried, s.failed, s.dead, out_of_time, lost=s.lost)
    finally:
        await lock.close()  # ends the session, and the lock with it


async def amain() -> int:
    from ..backends.sim.clock import DevClockRefused, dev_clock
    from ..catalog.connect import aconnect, from_env_or_exit
    from .backends import sim_refusal

    db = from_env_or_exit("worker")
    raw = os.environ.get("OMNIFILE_VERIFIER_MAX_SECONDS")
    try:
        max_seconds = float(raw) if raw else DEFAULT_MAX_SECONDS
    except ValueError:
        raise SystemExit("OMNIFILE_VERIFIER_MAX_SECONDS must be a number of seconds") from None

    async def connect() -> psycopg.AsyncConnection:
        return await aconnect(db, autocommit=True)

    try:
        clock = dev_clock()
    except DevClockRefused as e:
        raise SystemExit(str(e)) from None
    try:
        conn = await connect()
    except psycopg.OperationalError as e:
        log.error("verifier: the catalog is unreachable: %s", e)
        return 1
    try:
        registry = await BackendRegistry.load(conn, clock=clock)
    finally:
        await conn.close()
    if (why := sim_refusal(registry.kinds(), os.environ)) is not None:
        raise SystemExit(why)
    try:
        result = await run_pass(connect, registry, max_seconds=max_seconds)
    except (psycopg.OperationalError, psycopg.InterfaceError) as e:
        log.error("verifier: lost the catalog: %s", e)
        return 1
    finally:
        await registry.close()
    log.info(
        "verifier pass: %d read, %d verified, %d put back, %d failed, %d dead-lettered%s",
        result.read,
        result.verified,
        result.retried,
        result.failed,
        result.dead,
        " (out of time; the rest waits for the next run)" if result.out_of_time else "",
    )
    return result.exit_code


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
