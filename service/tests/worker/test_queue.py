"""The PGMQ consumer framework against a real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from omnifile_service.worker.queue import (
    THROTTLE_LIMIT,
    THROTTLE_RETRY_SECONDS,
    THROTTLED_SINCE_KEY,
    Consumer,
    ConsumerConfig,
    Defer,
    Extend,
    Message,
    Permanent,
    Retry,
    dlq_name,
    outside,
    send,
    throttled,
)

from .conftest import run

Q = "verify"  # an M0 queue the worker doesn't use otherwise
FAST = ConsumerConfig(backoff_base=0, max_failures=3, max_reads=50, visibility_timeout=30)


@pytest.fixture(autouse=True)
def clean(db: psycopg.Connection) -> None:
    db.execute(f"DELETE FROM pgmq.q_{Q}")
    db.execute(f"DELETE FROM pgmq.a_{Q}")
    db.execute("SELECT pgmq.create(%s)", (dlq_name(Q),))
    db.execute(f"DELETE FROM pgmq.q_{dlq_name(Q)}")
    db.execute("CREATE TABLE IF NOT EXISTS public.worker_probe (k text PRIMARY KEY)")
    db.execute("DELETE FROM public.worker_probe")
    db.execute("GRANT ALL ON public.worker_probe TO omnifile_worker")


def _enqueue(db: psycopg.Connection, body: dict) -> None:
    db.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (Q, psycopg.types.json.Jsonb(body)))


def _probe(db: psycopg.Connection) -> list[str]:
    return [r[0] for r in db.execute("SELECT k FROM public.worker_probe ORDER BY k")]


async def _write(conn: psycopg.AsyncConnection, k: str) -> None:
    await conn.execute("INSERT INTO public.worker_probe (k) VALUES (%s)", (k,))


def test_success_commits_the_work_and_archives_the_message(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await _write(conn, msg.body["k"])

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.drain()) == 1
    run(c.close())
    assert _probe(db) == ["a"]
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{Q}").fetchone() == (0,)
    archived = db.execute(f"SELECT message FROM pgmq.a_{Q}").fetchall()
    assert archived == [({"k": "a"},)]


def test_failure_rolls_back_retries_then_dead_letters(db, connect) -> None:
    _enqueue(db, {"k": "b"})
    seen: list[int] = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        seen.append(msg.failures)
        await _write(conn, f"attempt{msg.failures}")
        raise RuntimeError("backend down")

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert seen == [0, 1, 2]  # max_failures=3
    assert _probe(db) == []  # every attempt rolled back
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{Q}").fetchone() == (0,)
    (dead,) = db.execute(f"SELECT message FROM pgmq.q_{dlq_name(Q)}").fetchone()
    assert dead["failures"] == 3 and "backend down" in dead["error"]
    assert dead["message"]["k"] == "b" and dead["queue"] == Q
    assert c.stats.failed == 2 and c.stats.dead == 1


def test_failure_backs_off(db, connect) -> None:
    _enqueue(db, {"k": "c"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        raise RuntimeError("nope")

    c = Consumer(connect, Q, handler, ConsumerConfig(backoff_base=60))
    assert run(c.run_once()) == 1
    assert run(c.run_once()) == 0  # re-sent 60 s in the future: invisible now
    run(c.close())
    ((body, delay),) = db.execute(
        f"SELECT message, vt - now() > interval '50 seconds' FROM pgmq.q_{Q}"
    ).fetchall()
    assert body["_failures"] == 1 and "nope" in body["_last_error"] and delay


def test_permanent_errors_dead_letter_at_once(db, connect) -> None:
    _enqueue(db, {"k": "d"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        raise Permanent("malformed")

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert c.stats.dead == 1 and c.stats.failed == 0
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{dlq_name(Q)}").fetchone() == (1,)


def test_defer_commits_and_keeps_the_message_and_its_read_count(db, connect) -> None:
    _enqueue(db, {"k": "a"})
    (mid,) = db.execute(f"SELECT msg_id FROM pgmq.q_{Q}").fetchone()

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await _write(conn, f"seen-{msg.read_ct}")
        raise Defer(0)

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.run_once()) == 1
    assert run(c.run_once()) == 1
    run(c.close())
    assert _probe(db) == ["seen-1", "seen-2"]
    assert db.execute(f"SELECT msg_id, read_ct FROM pgmq.q_{Q}").fetchall() == [(mid, 2)]
    assert c.stats.retried == 2 and c.stats.failed == 0


def test_defer_hides_the_message_for_its_delay(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        raise Defer(300)

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.run_once()) == 1
    assert run(c.run_once()) == 0
    run(c.close())
    (left,) = db.execute(f"SELECT vt - now() > interval '4 minutes' FROM pgmq.q_{Q}").fetchone()
    assert left


def test_defer_of_a_message_someone_else_finished_rolls_back(db, connect) -> None:
    """Another holder took it while this handler ran: its work isn't kept, as
    with any handler that loses its message."""
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await _write(conn, "lost")
        await conn.execute(f"SELECT pgmq.delete('{Q}'::text, %s::bigint)", (msg.msg_id,))
        raise Defer(0)

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.run_once()) == 1
    run(c.close())
    assert _probe(db) == []
    assert c.stats.lost == 1 and c.stats.retried == 0


def test_extend_commits_more_time_then_runs_the_handler_again(db, connect) -> None:
    """The new timeout is visible to others before the long work starts, and the
    read count doesn't move."""
    _enqueue(db, {"k": "a"})
    seen: list[tuple[bool, bool, int]] = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        (long,) = db.execute(
            f"SELECT vt - now() > interval '50 minutes' FROM pgmq.q_{Q}"
        ).fetchone()
        seen.append((msg.extended, long, msg.read_ct))
        if not msg.extended:
            raise Extend(3600)

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.run_once()) == 1
    run(c.close())
    assert seen == [(False, False, 1), (True, True, 1)]
    assert c.stats.acked == 1
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{Q}").fetchone() == (0,)


def test_extend_of_a_message_someone_else_finished_rolls_back(db, connect) -> None:
    _enqueue(db, {"k": "a"})
    runs = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        runs.append(msg.extended)
        await conn.execute(f"SELECT pgmq.delete('{Q}'::text, %s::bigint)", (msg.msg_id,))
        raise Extend(3600)

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.run_once()) == 1
    run(c.close())
    assert runs == [False]
    assert c.stats.lost == 1


CONFLICTS = (
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
    psycopg.errors.LockNotAvailable,
)


@pytest.mark.parametrize("conflict", CONFLICTS)
def test_a_conflict_between_transactions_backs_off_and_never_dead_letters(
    db, connect, conflict
) -> None:
    """Nothing is wrong with the message. A recall dead-lettered for this would
    sit for ever: nothing sends it again, so it never counts towards the bound."""
    _enqueue(db, {"k": "a"})
    runs = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        runs.append(msg.read_ct)
        if len(runs) <= 8:
            raise conflict("a conflict")
        await _write(conn, "done")

    config = ConsumerConfig(backoff_base=0, max_failures=3, max_reads=3)
    c = Consumer(connect, Q, handler, config)
    for _ in range(9):
        assert run(c.run_once()) == 1
    run(c.close())
    assert c.stats.dead == 0 and c.stats.failed == 0 and c.stats.retried == 8
    assert c.stats.acked == 1
    assert _probe(db) == ["done"]
    assert set(runs) == {1}  # sent again each time, so the read count can't build up
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{dlq_name(Q)}").fetchone() == (0,)


def test_a_conflict_on_a_message_someone_else_finished_is_lost_not_raised(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        db.execute(f"SELECT pgmq.delete('{Q}'::text, %s::bigint)", (msg.msg_id,))
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.run_once()) == 1
    run(c.close())
    assert c.stats.lost == 1 and c.stats.retried == 0


def test_a_conflict_backs_off_further_each_time(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    c = Consumer(connect, Q, handler, ConsumerConfig(backoff_base=100, visibility_timeout=3600))
    assert run(c.run_once()) == 1
    (first,) = db.execute(f"SELECT vt - now() FROM pgmq.q_{Q}").fetchone()
    db.execute(f"UPDATE pgmq.q_{Q} SET vt = now()")
    assert run(c.run_once()) == 1
    run(c.close())
    (second,) = db.execute(f"SELECT vt - now() FROM pgmq.q_{Q}").fetchone()
    assert 60 < first.total_seconds() <= 100
    assert 160 < second.total_seconds() <= 200


def test_a_query_canceled_goes_up_and_isnt_counted(db, connect) -> None:
    """Any other `OperationalError` is left as it was: up to the run loop, and the
    message shows again in its own time. Five of them must not dead-letter it: on
    `restores` that leaves a recall `requested` for ever."""
    _enqueue(db, {"k": "a"})
    (mid,) = db.execute(f"SELECT msg_id FROM pgmq.q_{Q}").fetchone()

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        raise psycopg.errors.QueryCanceled("canceling statement")

    config = ConsumerConfig(backoff_base=0, max_failures=2, max_reads=2)
    c = Consumer(connect, Q, handler, config)
    for _ in range(5):
        with pytest.raises(psycopg.errors.QueryCanceled):
            run(c.run_once())
        db.execute(f"UPDATE pgmq.q_{Q} SET vt = now()")
    run(c.close())
    assert c.stats.failed == 0 and c.stats.dead == 0
    assert db.execute(f"SELECT msg_id, read_ct FROM pgmq.q_{Q}").fetchall() == [(mid, 5)]
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{dlq_name(Q)}").fetchone() == (0,)


def test_a_lost_connection_goes_up(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await conn.close()
        raise psycopg.OperationalError("the connection is gone")

    c = Consumer(connect, Q, handler, FAST)
    with pytest.raises(psycopg.OperationalError):
        run(c.run_once())
    run(c.close())
    assert c.stats.failed == 0


def test_retry_commits_and_requeues_with_a_fresh_read_count(db, connect) -> None:
    _enqueue(db, {"k": "e", "n": 0})
    reads: list[tuple[int, int]] = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        n = msg.body["n"]
        reads.append((n, msg.read_ct))
        await _write(conn, f"poll{n}")
        if n < 2:
            raise Retry(0, {**msg.body, "n": n + 1})

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert reads == [(0, 1), (1, 1), (2, 1)]
    assert _probe(db) == ["poll0", "poll1", "poll2"]  # writes before Retry are kept
    assert c.stats.retried == 2 and c.stats.acked == 1


def test_a_message_that_keeps_crashing_workers_is_dead_lettered(db, connect) -> None:
    db.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (Q, psycopg.types.json.Jsonb({"k": "f"})))
    # Simulate earlier crashed reads: read_ct already at the cap.
    db.execute(f"UPDATE pgmq.q_{Q} SET read_ct = 49")

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        raise RuntimeError("crash")

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert c.stats.dead == 1


def test_send_joins_the_callers_transaction(db, connect) -> None:
    async def body() -> None:
        conn = await connect()
        try:
            with pytest.raises(RuntimeError):
                async with conn.transaction():
                    await send(conn, Q, {"k": "never"})
                    raise RuntimeError("abort")
        finally:
            await conn.close()

    run(body())
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{Q}").fetchone() == (0,)


# --- a provider call, with no transaction open ---------------------------------------------


def _open_transactions(db: psycopg.Connection, consumer_pid: list[int]) -> str:
    (state,) = db.execute(
        "SELECT state FROM pg_stat_activity WHERE pid = %s", (consumer_pid[0],)
    ).fetchone()
    return state


def test_outside_commits_first_calls_with_nothing_open_then_runs_again(db, connect) -> None:
    _enqueue(db, {"k": "a"})
    runs: list[object] = []
    states: list[str] = []
    probed: list[list[str]] = []
    pid: list[int] = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        pid[:] = [conn.info.backend_pid]
        if not runs:
            await _write(conn, "before the call")

        async def call() -> str:
            states.append(_open_transactions(db, pid))
            probed.append(_probe(db))
            return "the answer"

        runs.append(None)
        got = await outside(msg, "call", call)
        runs[-1] = got
        await _write(conn, f"after: {got}")

    c = Consumer(connect, Q, handler, FAST)
    assert run(c.drain()) == 1
    run(c.close())
    assert states == ["idle"], "a transaction was open during the call"
    assert probed == [["before the call"]], "what it had done was not committed"
    assert runs == [None, "the answer"]
    assert _probe(db) == ["after: the answer", "before the call"]
    assert c.stats.acked == 1
    assert db.execute(f"SELECT count(*) FROM pgmq.q_{Q}").fetchone() == (0,)


def test_a_handlers_except_exception_cant_swallow_the_signal(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        async def call() -> str:
            return "the answer"

        try:
            got = await outside(msg, "call", call)
        except Exception as e:  # noqa: BLE001 - as `settings/cloud_jobs.py` does
            got = f"failed: {type(e).__name__}"
        await _write(conn, got)

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert _probe(db) == ["the answer"]


def test_each_call_outside_is_made_once_for_a_message(db, connect) -> None:
    _enqueue(db, {"k": "a"})
    made: list[str] = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        async def call(name: str) -> str:
            made.append(name)
            return name

        assert await outside(msg, "one", lambda: call("one")) == "one"
        assert await outside(msg, "two", lambda: call("two")) == "two"

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert made == ["one", "two"]
    assert c.stats.acked == 1


def test_a_calls_error_is_raised_on_the_next_run_and_counts_like_any_other(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        async def call() -> None:
            raise RuntimeError("from the provider")

        try:
            await outside(msg, "call", call)
        except RuntimeError:
            await _write(conn, "rolled back with the failure")
            raise

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert c.stats.dead == 1 and "from the provider" in c.stats.errors[0]
    assert _probe(db) == []


def test_a_long_call_hides_its_message_first(db, connect) -> None:
    """A row lock used to make a second holder wait. Now the message stays hidden."""
    _enqueue(db, {"k": "a"})
    hidden: list[float] = []

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        async def call() -> None:
            (left,) = db.execute(
                f"SELECT extract(epoch FROM vt - now()) FROM pgmq.q_{Q}"
            ).fetchone()
            hidden.append(float(left))

        await outside(msg, "call", call, hide_for=3600)

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert len(hidden) == 1 and 3500 < hidden[0] <= 3600


def test_a_holder_that_lost_its_message_during_the_call_writes_nothing(db, connect) -> None:
    _enqueue(db, {"k": "a"})

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        async def call() -> None:
            db.execute(f"DELETE FROM pgmq.q_{Q}")  # another holder finished it

        await outside(msg, "call", call)
        await _write(conn, "after")

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    assert c.stats.lost == 1 and c.stats.acked == 0
    assert _probe(db) == []


def test_a_message_no_consumer_read_calls_in_place(db, connect) -> None:
    """A caller with a transaction of its own (a CLI, a test) can't be run again."""

    async def go() -> list[str]:
        async def call() -> str:
            return "answer"

        now = datetime.now(UTC)
        return [
            await outside(Message(1, 1, now, now, {}), "call", call),
            await outside(None, "call", call),
        ]

    assert run(go()) == ["answer", "answer"]


def test_a_throttled_message_is_a_retry_until_its_limit_then_the_error_itself() -> None:
    now = datetime.now(UTC)
    error = RuntimeError("503")

    def message(since: datetime | None) -> Message:
        body = {} if since is None else {THROTTLED_SINCE_KEY: since.isoformat()}
        return Message(1, 1, now, now, body)

    first = throttled(message(None), error)
    assert isinstance(first, Retry) and THROTTLED_SINCE_KEY in first.message
    assert first.delay_seconds == THROTTLE_RETRY_SECONDS
    # The start is kept, or the limit would never come.
    start = now - timedelta(minutes=30)
    again = throttled(message(start), error, timedelta(seconds=7))
    assert isinstance(again, Retry) and again.delay_seconds == 7
    assert again.message[THROTTLED_SINCE_KEY] == start.isoformat()
    assert throttled(message(now - THROTTLE_LIMIT), error) is error
