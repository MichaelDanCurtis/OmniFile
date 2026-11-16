"""An async PGMQ consumer (decision 27: queues live in the catalog Postgres).

Semantics, per message:

- **Read** with a visibility timeout (`pgmq.read`); the message is invisible
  to other consumers until it is acked or the timeout lapses (a crashed
  worker's message comes back by itself).
- **Success:** the handler runs inside one transaction, and the message is
  archived (kept in ``pgmq.a_<queue>`` for audit) in that same transaction.
  The catalog change and the ack commit together or not at all.
- **`Retry(delay)`** ("not yet", e.g. a rehydrate still running): the
  handler's writes are committed and the message is re-sent with the delay,
  in the same transaction. Re-sending (rather than `set_vt`) gives the message
  a fresh ``read_ct``, so long polls never look like poison messages.
- **Failure** (any other exception): the handler's writes are rolled back.
  The message is re-sent after an exponential backoff, with
  ``"_failures"`` counted in its body. After `max_failures`, or when
  ``read_ct`` reaches `max_reads` (a worker crashing on it again and again),
  it is **dead-lettered**: moved to ``<queue>_dlq`` with the error, and
  deleted from the queue.
- **Lost ownership:** acking or re-sending first archives or deletes the message, and
  checks that it did. If `pgmq.archive` or `pgmq.delete` answers false, another holder (one
  that read it after its visibility timeout lapsed) has already finished it: the
  transaction rolls back, nothing is re-sent, and the message is not counted twice, so a slow
  handler never multiplies a message (review of #34).
- **A provider call:** a handler's transaction must not be open across one. A
  transaction id held there stops the edge feed for every edge (its horizon is the
  oldest open id), and a read lock held there stops an ``ALTER``. `outside` commits
  what the handler has done so far, makes the call with no transaction open, then
  runs the handler again from the top with the result, the way `Retry` runs it again
  later. So the handler reads and locks its rows afresh, and looks again at anything
  that may have moved during the call, as it must anyway after a `Retry`.
- **A session advisory lock:** a handler may take one to hold through its `outside`
  calls, since it needs no transaction. The consumer lets go of them all when the
  message ends, however it ended, and a crash lets go with the connection.

The ``*_dlq`` queues are created by catalog migrations (0008 and later). The
consumer creates one at start only if it is missing, as a guard for a queue
added before its migration: that needs the schema owner, and the worker's own
role (`catalog/privileges.py`) can't create tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import metrics

log = logging.getLogger(__name__)

FAILURES_KEY = "_failures"
ERROR_KEY = "_last_error"
CONFLICTS_KEY = "_conflicts"
THROTTLED_SINCE_KEY = "_throttled_since"
#: How long a message goes on being throttled before it counts as failing.
#: Real throttling lasts seconds or minutes, and no burst should dead-letter
#: anything. Azure also answers a gateway's 503 as `Throttled`, though, and one
#: that lasts for days must reach the alert in the hour, not retry unseen for ever.
THROTTLE_LIMIT = timedelta(hours=1)
THROTTLE_RETRY_SECONDS = 30.0
#: Two transactions got in each other's way. Nothing is wrong with the message.
CONFLICTS = (
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
    psycopg.errors.LockNotAvailable,
)


class Retry(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Commit the handler's writes and come back to this message later.

    `message`, when given, replaces the message body for the next attempt.
    """

    def __init__(self, delay_seconds: float, message: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"retry in {delay_seconds:.0f}s")
        self.delay_seconds = max(0.0, delay_seconds)
        self.message = message


class Defer(Exception):  # noqa: N818 - a control-flow signal
    """Not now: leave this message where it is and show it again later.

    Unlike `Retry`, the message stays, with its id and its `read_ct`, so a handler
    can bound how often it comes back, and a holder of the same message loses
    nothing: `Retry` deletes the message and sends a new one."""

    def __init__(self, delay_seconds: float) -> None:
        super().__init__(f"defer {delay_seconds:.0f}s")
        self.delay_seconds = max(0.0, delay_seconds)


class Extend(Exception):  # noqa: N818 - a control-flow signal
    """This attempt will take long: keep the message hidden this much longer, then
    run the handler again at once, with `Message.extended` set.

    The new timeout is committed before the long work starts, so nothing has to
    stay open to protect it, and only this message waits that long if the worker
    dies."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"extend {seconds:.0f}s")
        self.seconds = seconds


class Outside(BaseException):  # noqa: N818 - a control-flow signal
    """Raised by `outside`: commit, make `call` with no transaction open, run again.

    A BaseException, so a handler's `except Exception` around a call (every failure
    is the task's answer, say) can't take it for the call's own failure."""

    def __init__(
        self, tag: str, call: Callable[[], Awaitable[Any]], hide_for: float | None = None
    ) -> None:
        super().__init__(f"outside {tag}")
        self.tag = tag
        self.call = call
        self.hide_for = hide_for


@dataclass(frozen=True)
class Called:
    """What came of a call made `outside` the transaction."""

    tag: str
    value: Any = None
    error: Exception | None = None

    def result(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.value


class LostOwnership(Exception):  # noqa: N818 - a control-flow signal
    """Another holder already archived or deleted the message."""


class Permanent(Exception):  # noqa: N818
    """The message can never succeed (malformed, unknown kind): dead-letter
    it now instead of retrying."""


@dataclass(frozen=True)
class Message:
    msg_id: int
    read_ct: int
    enqueued_at: datetime
    vt: datetime
    body: Mapping[str, Any]
    #: A handler asked for more time (`Extend`), and has it.
    extended: bool = False
    #: The calls made `outside` so far for this message, by tag.
    called: Mapping[str, Called] = field(default_factory=dict)
    #: Read by a `Consumer`, which can run the handler again. A message a caller
    #: built itself is run in that caller's transaction, so `outside` calls in place.
    consumed: bool = False

    @property
    def failures(self) -> int:
        return int(self.body.get(FAILURES_KEY, 0))


Handler = Callable[[Message, psycopg.AsyncConnection], Awaitable[None]]


def throttled(msg: Message, exc: Exception, retry_after: timedelta | None = None) -> Exception:
    """What a handler raises for the provider's `Throttled`. A `Retry` that uses up no
    attempt, until the message has been throttled for `THROTTLE_LIMIT`. Each `Retry` is a
    fresh message, so without a limit this would loop for ever with no alert: even the
    queue-age alert stays quiet. Past it, `exc` itself: a failure like any other, which
    dead-letters, so `redrive.py`'s rules apply. The clock is real time, like a dead
    letter's. An answer from the provider ends it: a handler's next `Retry` leaves the
    key out."""
    now = datetime.now(UTC)
    raw = msg.body.get(THROTTLED_SINCE_KEY)
    since = datetime.fromisoformat(raw) if raw else now
    if now - since >= THROTTLE_LIMIT:
        return exc
    wait = retry_after.total_seconds() if retry_after is not None else THROTTLE_RETRY_SECONDS
    return Retry(wait, {**msg.body, THROTTLED_SINCE_KEY: since.isoformat()})


async def outside[T](
    msg: Message | None,
    tag: str,
    call: Callable[[], Awaitable[T]],
    *,
    hide_for: float | None = None,
) -> T:
    """The result of `call`, made with no transaction open (see the module's
    docstring). The first time, this raises `Outside`: what the handler wrote so far
    is committed, and everything it read or locked is let go. The call's own error
    is raised here, on the handler's next run, so it is handled like any other.

    `tag` names the call, so each call is made once for this message however many
    runs the handler takes. Its result is as old as that call: anything that may have
    moved since is the handler's to look at again. Called with no message, or one no
    consumer read, it calls in place, in the caller's transaction.

    `hide_for`: for a call that can outlast the message's visibility timeout. A row
    lock used to make a second holder wait; now the message stays hidden instead."""
    if msg is None or not msg.consumed:
        return await call()
    done = msg.called.get(tag)
    if done is None:
        raise Outside(tag, call, hide_for)
    return done.result()


@dataclass(frozen=True)
class ConsumerConfig:
    visibility_timeout: int = 60  # seconds a read message stays invisible
    batch: int = 10
    max_failures: int = 5
    max_reads: int = 20
    backoff_base: float = 5.0
    backoff_max: float = 900.0
    idle_poll: float = 2.0  # seconds between reads of an empty queue

    def backoff(self, failures: int) -> float:
        return min(self.backoff_max, self.backoff_base * 2 ** max(0, failures - 1))


@dataclass
class ConsumerStats:
    acked: int = 0
    retried: int = 0
    failed: int = 0
    dead: int = 0
    #: Finished by another holder first (`LostOwnership`): rolled back, not re-sent.
    lost: int = 0
    errors: list[str] = field(default_factory=list)


async def _take(conn: psycopg.AsyncConnection, op: str, queue: str, msg_id: int) -> None:
    """`pgmq.archive` or `pgmq.delete` the message; LostOwnership if it was already gone."""
    assert op in ("archive", "delete")
    cur = await conn.execute(f"SELECT pgmq.{op}(%s::text, %s::bigint)", (queue, msg_id))
    row = await cur.fetchone()
    if not (row and row[0]):
        raise LostOwnership(f"{queue} msg {msg_id}")


def dlq_name(queue: str) -> str:
    return f"{queue}_dlq"


async def send(
    conn: psycopg.AsyncConnection, queue: str, body: Mapping[str, Any], delay: float = 0
) -> int:
    """Enqueue on `conn`'s current transaction (the decision-27 pattern)."""
    cur = await conn.execute(
        "SELECT pgmq.send(%s::text, %s::jsonb, %s::integer)",
        (queue, Jsonb(dict(body)), int(round(delay))),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


class Consumer:
    """Reads one queue and runs `handler` on each message."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[psycopg.AsyncConnection]],
        queue: str,
        handler: Handler,
        config: ConsumerConfig | None = None,
    ) -> None:
        self._connect = connect
        self.queue = queue
        self._handler = handler
        self.config = config or ConsumerConfig()
        self.stats = ConsumerStats()
        self._conn: psycopg.AsyncConnection | None = None
        #: The worker's pooled connect (`worker.pool`, under OMNIFILE_PG_POOL_MAX): hold the
        #: connection for one batch only, so every queue's consumer shares the pool.
        self._lend = bool(getattr(connect, "lends_connections", False))
        if self._lend and hasattr(connect, "lane"):
            self._connect = connect.lane(queue)  # this queue's slice of the budget
        self._dlq_ready = False

    async def _connection(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            if self._conn is not None:
                await self._conn.close()  # a pooled one goes back (and is checked) there
            self._conn = await self._connect()
            # Every unit of work below is an explicit transaction() block.
            await self._conn.set_autocommit(True)
            if not self._dlq_ready:
                dlq = dlq_name(self.queue)
                row = await (
                    await self._conn.execute(
                        "SELECT 1 FROM pgmq.meta WHERE queue_name = %s", (dlq,)
                    )
                ).fetchone()
                if row is None:
                    async with self._conn.transaction():
                        await self._conn.execute("SELECT pgmq.create(%s::text)", (dlq,))
                self._dlq_ready = True
        return self._conn

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None and (self._lend or not conn.closed):
            await conn.close()

    async def _read(self, conn: psycopg.AsyncConnection) -> list[Message]:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT msg_id, read_ct, enqueued_at, vt, message"
                " FROM pgmq.read(%s::text, %s::integer, %s::integer)",
                (self.queue, self.config.visibility_timeout, self.config.batch),
            )
            rows = await cur.fetchall()
        return [Message(r[0], r[1], r[2], r[3], r[4] or {}, consumed=True) for r in rows]

    async def run_once(self) -> int:
        """Process one batch. Returns how many messages were read."""
        conn = await self._connection()
        try:
            messages = await self._read(conn)
            for msg in messages:
                try:
                    await self._process(conn, msg)
                finally:
                    await self._let_go(conn)
        finally:
            if self._lend:
                await self.close()
        return len(messages)

    async def _let_go(self, conn: psycopg.AsyncConnection) -> None:
        """Session advisory locks the handler took: the connection outlives the message,
        and may go back to the pool."""
        if conn.closed:
            return
        try:
            await conn.execute("SELECT pg_advisory_unlock_all()")
        except psycopg.Error as e:
            log.warning("queue %s: session locks not let go: %s", self.queue, e)
            await self.close()

    async def drain(self, limit: int = 1000) -> int:
        """Process until the queue shows nothing visible (tests, one-shot jobs)."""
        total = 0
        while total < limit:
            n = await self.run_once()
            if n == 0:
                break
            total += n
        return total

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                n = await self.run_once()
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                log.warning("queue %s: database unavailable: %s", self.queue, e)
                await self.close()
                n = 0
            if n == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.config.idle_poll)
                except TimeoutError:
                    pass

    async def _process(self, conn: psycopg.AsyncConnection, msg: Message) -> None:
        q = self.queue
        start = time.monotonic()
        extend = False
        call: Outside | None = None
        try:
            async with conn.transaction():
                try:
                    await self._handler(msg, conn)
                except Retry as r:
                    body = dict(r.message if r.message is not None else msg.body)
                    body.pop(FAILURES_KEY, None)
                    body.pop(ERROR_KEY, None)
                    body.pop(CONFLICTS_KEY, None)
                    await _take(conn, "delete", q, msg.msg_id)
                    await send(conn, q, body, r.delay_seconds)
                    self.stats.retried += 1
                    metrics.handler_done(q, "retry", time.monotonic() - start)
                    return
                except Defer as d:
                    cur = await conn.execute(
                        "SELECT msg_id FROM pgmq.set_vt(%s::text, %s::bigint, %s::integer)",
                        (q, msg.msg_id, int(round(d.delay_seconds))),
                    )
                    if await cur.fetchone() is None:
                        raise LostOwnership(f"{q} msg {msg.msg_id}") from None
                    self.stats.retried += 1
                    metrics.handler_done(q, "retry", time.monotonic() - start)
                    return
                except Extend as x:
                    cur = await conn.execute(
                        "SELECT msg_id FROM pgmq.set_vt(%s::text, %s::bigint, %s::integer)",
                        (q, msg.msg_id, int(round(x.seconds))),
                    )
                    if await cur.fetchone() is None:
                        raise LostOwnership(f"{q} msg {msg.msg_id}") from None
                    extend = True
                except Outside as o:
                    call = o
                    if o.hide_for is not None:
                        cur = await conn.execute(
                            "SELECT msg_id FROM pgmq.set_vt(%s::text, %s::bigint, %s::integer)",
                            (q, msg.msg_id, int(round(o.hide_for))),
                        )
                        if await cur.fetchone() is None:
                            raise LostOwnership(f"{q} msg {msg.msg_id}") from None
                else:
                    await _take(conn, "archive", q, msg.msg_id)
                    self.stats.acked += 1
                    metrics.handler_done(q, "ok", time.monotonic() - start)
                    return
        except LostOwnership:
            self.stats.lost += 1
            log.warning("queue %s msg %s: another holder finished it first", q, msg.msg_id)
            metrics.handler_done(q, "lost", time.monotonic() - start)
            return
        except Exception as e:  # noqa: BLE001 - every failure is accounted for below
            error = f"{type(e).__name__}: {e}"
            log.warning("queue %s msg %s failed: %s", q, msg.msg_id, error)
            self.stats.errors.append(error)
            # A lost connection, or trouble with the database, goes up, and the
            # message shows again in its own time. A conflict between transactions
            # backs off and isn't counted: a recall dead-lettered for one would
            # sit for ever, since nothing sends it again.
            if conn.closed or isinstance(e, psycopg.InterfaceError):
                raise
            if isinstance(e, CONFLICTS):
                try:
                    await self._back_off(conn, msg, error)
                except LostOwnership:
                    self.stats.lost += 1
                    log.warning("queue %s msg %s: another holder finished it first", q, msg.msg_id)
                    return
                metrics.handler_done(q, "retry", time.monotonic() - start)
                return
            if isinstance(e, psycopg.OperationalError):
                raise
            try:
                dead = await self._fail(
                    conn,
                    msg,
                    error,
                    traceback.format_exc(limit=5),
                    permanent=isinstance(e, Permanent),
                )
            except LostOwnership:
                self.stats.lost += 1
                log.warning("queue %s msg %s: another holder finished it first", q, msg.msg_id)
                return
            metrics.handler_done(q, "dead" if dead else "failed", time.monotonic() - start)
            return
        if extend:
            await self._process(conn, replace(msg, extended=True))
        elif call is not None:
            # No transaction is open here: the block above has committed.
            try:
                called = Called(call.tag, await call.call())
            except Exception as e:  # noqa: BLE001 - the handler's next run raises it
                called = Called(call.tag, error=e)
            await self._process(conn, replace(msg, called={**msg.called, call.tag: called}))

    async def _back_off(self, conn: psycopg.AsyncConnection, msg: Message, error: str) -> None:
        """Send the message again later: further off each time, up to `backoff_max`,
        and with a fresh read count, so a conflict never counts towards
        dead-lettering. A message that always conflicts shows in the queue-age alert."""
        conflicts = int(msg.body.get(CONFLICTS_KEY, 0)) + 1
        body = {**msg.body, CONFLICTS_KEY: conflicts, ERROR_KEY: error[:2000]}
        async with conn.transaction():
            await _take(conn, "delete", self.queue, msg.msg_id)
            await send(conn, self.queue, body, self.config.backoff(conflicts))
        self.stats.retried += 1

    async def _fail(
        self,
        conn: psycopg.AsyncConnection,
        msg: Message,
        error: str,
        tb: str,
        *,
        permanent: bool = False,
    ) -> bool:
        """Retry later, or dead-letter; True if dead-lettered."""
        q = self.queue
        failures = msg.failures + 1
        body = dict(msg.body)
        body[FAILURES_KEY] = failures
        body[ERROR_KEY] = error[:2000]
        async with conn.transaction():
            if (
                permanent
                or failures >= self.config.max_failures
                or msg.read_ct >= self.config.max_reads
            ):
                dead = {
                    "queue": q,
                    "msg_id": msg.msg_id,
                    "read_ct": msg.read_ct,
                    "failures": failures,
                    "error": error[:2000],
                    "traceback": tb[-4000:],
                    "message": body,
                }
                await _take(conn, "delete", q, msg.msg_id)
                await send(conn, dlq_name(q), dead)
                self.stats.dead += 1
                log.error(
                    "queue %s msg %s dead-lettered: %s", q, msg.msg_id, json.dumps(dead)[:500]
                )
                dead_lettered = True
            else:
                await _take(conn, "delete", q, msg.msg_id)
                await send(conn, q, body, self.config.backoff(failures))
                self.stats.failed += 1
                dead_lettered = False
        return dead_lettered
