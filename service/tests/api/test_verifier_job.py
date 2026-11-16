"""The verifier job's pass (worker/verifier.py): drain the `verify` queue once, then exit.

Before, the job ran the worker itself (`python -m omnifile_service.worker`), which never
exits: a scheduled or manual execution would have run until its 4-hour replica timeout and
been marked failed, and nothing started one anyway.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker import verifier

from .conftest import E, World, complete_body, create_file, upload_blocks
from .test_upload_flow import DATA, intent


class _AnyId:
    """Every backend id is the facade's sim (as conftest.run_verify_queue)."""

    def __init__(self, facade) -> None:
        self.facade = facade

    def get(self, backend_id: int):
        return self.facade.sim


def _committed(w: World, facade_client, name: str) -> int:
    """A file uploaded and completed: its location waits on the `verify` queue."""
    file_id = create_file(w, name)
    resp = w.edge.post(f"{E}/uploads", intent(w, file_id, DATA))
    assert resp.status_code == 201, resp.text
    up = resp.json()
    upload_blocks(facade_client, up["target"]["url"], DATA, 4096)
    done = w.edge.post(
        f"{E}/uploads/{{upload_id}}/complete", complete_body(DATA), upload_id=up["upload_id"]
    )
    assert done.status_code == 200, done.text
    return file_id


def _pass(db_url: str, facade, **kw) -> verifier.PassResult:
    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(as_app(db_url, "worker"), autocommit=True)

    return asyncio.run(verifier.run_pass(connect, _AnyId(facade), **kw))  # type: ignore[arg-type]


@pytest.fixture
def empty_verify_queue(sql):
    """Messages other tests left on the session database's queue aren't this test's."""
    sql.execute("SELECT pgmq.purge_queue('verify')")
    yield
    sql.execute("SELECT pgmq.purge_queue('verify')")


def _verified(sql, file_id: int) -> bool:
    (ok,) = sql.execute(
        "SELECT bool_and(l.verified) FROM catalog.location l"
        " JOIN catalog.file f ON f.content_version_id = l.version_id WHERE f.file_id = %s",
        (file_id,),
    ).fetchone()
    return bool(ok)


def test_one_pass_verifies_the_queue_and_exits_0(
    make_world, facade_client, facade, db_url, sql, empty_verify_queue
) -> None:
    w = make_world()
    files = [_committed(w, facade_client, f"v-{i}.bin") for i in range(3)]
    assert not any(_verified(sql, f) for f in files)
    result = _pass(db_url, facade)
    assert (result.read, result.verified, result.failed, result.dead) == (3, 3, 0, 0)
    assert result.exit_code == 0 and not result.out_of_time
    assert all(_verified(sql, f) for f in files)
    (left,) = sql.execute("SELECT count(*) FROM pgmq.q_verify").fetchone()
    assert left == 0
    # An empty queue is a pass too: nothing read, exit 0.
    again = _pass(db_url, facade)
    assert (again.read, again.exit_code) == (0, 0)


def test_a_failed_message_makes_the_pass_exit_1(
    make_world, facade_client, facade, db_url, sql, empty_verify_queue
) -> None:
    w = make_world()
    _committed(w, facade_client, "ok.bin")
    # A message naming no location is malformed for good: dead-lettered, not retried forever.
    sql.execute("SELECT pgmq.send('verify', %s::jsonb)", ('{"location_id": "nope"}',))
    result = _pass(db_url, facade)
    assert result.verified == 1
    assert result.failed + result.dead == 1
    assert result.exit_code == 1


def test_the_time_budget_ends_the_pass(
    make_world, facade_client, facade, db_url, sql, empty_verify_queue
) -> None:
    w = make_world()
    _committed(w, facade_client, f"late-{uuid.uuid4().hex[:6]}.bin")
    result = _pass(db_url, facade, max_seconds=0)
    assert result.out_of_time and result.read == 0 and result.exit_code == 0
    (left,) = sql.execute("SELECT count(*) FROM pgmq.q_verify").fetchone()
    assert left == 1  # the next run takes it


# --- review of #34, round 2: one message at a time, one pass at a time, no duplicates -----


def test_the_deadline_is_checked_before_each_message(
    make_world, facade_client, facade, db_url, sql, empty_verify_queue
) -> None:
    """A batch of 10 was read before the budget was looked at again: with big files that ran
    for hours. Now one message, then the clock."""
    w = make_world()
    for i in range(3):
        _committed(w, facade_client, f"d-{i}.bin")
    ticks = iter(range(0, 10_000, 100))
    result = _pass(db_url, facade, max_seconds=150, clock=lambda: next(ticks))
    assert (result.read, result.out_of_time) == (1, True)
    (left,) = sql.execute("SELECT count(*) FROM pgmq.q_verify").fetchone()
    assert left == 2


def test_the_worker_reads_verify_as_the_verifier_does() -> None:
    """The worker read the same queue with a 60 s visibility timeout, so a long read-back's
    message came back to another reader mid-read."""
    from omnifile_service.worker.queue import ConsumerConfig
    from omnifile_service.worker.verify import VISIBILITY_S, verify_jobs

    (job,) = verify_jobs(None, ConsumerConfig(idle_poll=0.25))  # type: ignore[arg-type]
    assert job.config == ConsumerConfig(idle_poll=0.25, visibility_timeout=VISIBILITY_S, batch=1)
    assert VISIBILITY_S >= 2 * 60 * 60


def test_only_one_pass_runs_at_a_time(make_world, facade_client, facade, db_url, sql,
                                      empty_verify_queue) -> None:  # fmt: skip
    w = make_world()
    _committed(w, facade_client, "locked.bin")
    with psycopg.connect(as_app(db_url, "worker"), autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (verifier.PASS_LOCK,))
        result = _pass(db_url, facade)
    assert result.skipped and result.read == 0 and result.exit_code == 0
    assert _pass(db_url, facade).verified == 1  # once the other pass is gone


def test_a_failed_read_back_is_audited_as_the_verifier(
    make_world, facade_client, facade, db_url, sql, empty_verify_queue
) -> None:
    w = make_world()
    file_id = _committed(w, facade_client, "gone.bin")
    sql.execute(
        "UPDATE catalog.location SET object_key = object_key || '-missing'"
        " WHERE version_id = (SELECT content_version_id FROM catalog.file WHERE file_id = %s)",
        (file_id,),
    )
    _pass(db_url, facade)
    (actor,) = sql.execute(
        "SELECT actor FROM catalog.audit_log WHERE action = 'location.verify' AND file_id = %s"
        " ORDER BY audit_id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    assert actor == "system:verifier"


@pytest.mark.parametrize("outcome", ["ack", "retry", "fail"])
def test_a_message_another_holder_finished_is_not_multiplied(db_url, sql, empty_verify_queue,
                                                             outcome) -> None:  # fmt: skip
    """Two holders of one message (the first's visibility lapsed mid-read): the second to
    finish found pgmq.archive or delete false, yet acked, re-sent or retried it anyway, so
    the message multiplied. Now it rolls back and sends nothing."""
    from omnifile_service.worker.queue import Consumer, ConsumerConfig, Retry

    (msg_id,) = sql.execute(
        "SELECT pgmq.send('verify', %s::jsonb)", ('{"location_id": "0"}',)
    ).fetchone()

    async def handler(msg, conn) -> None:
        # The other holder finishes the message while this one still works on it.
        with psycopg.connect(as_app(db_url, "worker"), autocommit=True) as other:
            other.execute("SELECT pgmq.archive('verify', %s::bigint)", (msg.msg_id,))
        await conn.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:test', 'wp1.lost', 'allow', '{}'::jsonb)"
        )
        if outcome == "retry":
            raise Retry(0)
        if outcome == "fail":
            raise RuntimeError("the handler failed")

    async def run() -> Consumer:
        async def connect():
            return await psycopg.AsyncConnection.connect(as_app(db_url, "worker"), autocommit=True)

        c = Consumer(connect, "verify", handler, ConsumerConfig(batch=1))
        await c.run_once()
        await c.close()
        return c

    before = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'wp1.lost'"
    ).fetchone()[0]
    c = asyncio.run(run())
    assert c.stats.lost == 1 and (c.stats.acked, c.stats.retried, c.stats.failed) == (0, 0, 0)
    (queued,) = sql.execute("SELECT count(*) FROM pgmq.q_verify").fetchone()
    assert queued == 0, "nothing re-sent"
    after = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'wp1.lost'"
    ).fetchone()[0]
    assert after == before, "the handler's writes rolled back"
    assert msg_id
