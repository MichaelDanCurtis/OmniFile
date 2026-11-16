"""A copy's check through an outage, what it leaves on its prefix, and two small things
next to it: a cutover's tries at a delete, and waves nobody will close (wp27b.md)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sources.support import BUCKET, put
from watched import FakeWorker, Watched

from omnifile_service.backends.base import BackendError
from omnifile_service.catalog.privileges import as_app
from omnifile_service.migrate import progress as migrate_progress
from omnifile_service.migrate import staging as migrate_staging
from omnifile_service.migrate.progress import TO_COUNT, count_one
from omnifile_service.migrate.waves import WAVE_WINDOW, close_abandoned
from omnifile_service.web.routes_console import _ROWS, _attention, _health
from omnifile_service.worker import migrate_jobs as migrate_jobs_module
from omnifile_service.worker.migrate_jobs import migrate_jobs
from omnifile_service.worker.redrive import clean_body
from omnifile_service.worker.verify import READ_ERRORS_KEY

from .test_engine import Rig, _with, run
from .test_no_id_across_calls import (
    FailingRead,
    FailOn,
    _cancel,
    _held_at_verified,
    _pending_copy,
    _rows,
    _wave_rig,
    drain,
    only,
    registry_of,
    state,
)

#: For `_console`: this test's database and connections, and whether its source runs Batch.
_URL: list[str] = [""]
_CONNECT: list[Any] = [None]
_BATCH: list[bool] = [False]


@pytest.fixture(autouse=True)
def _remember(connect: Any) -> None:
    _CONNECT[0] = connect
    _BATCH[0] = False


class Outage(FailingRead):
    """`target`, where nothing answers: its reads fail, and so does a HEAD."""

    def __init__(self, target: Any) -> None:
        super().__init__(target)
        self.heads = 0
        self.head_error = "no answer"

    async def head(self, *a: Any, **k: Any) -> Any:
        self.heads += 1
        if self.failing:
            raise BackendError(self.head_error)
        return await self._target.head(*a, **k)


def _copy_rig(db: Any, connect: Any) -> tuple[Rig, int, int]:
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    _pending_copy(r, mid)
    (lid,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s AND state = 'pending'",
        (r.w.home_id,),
    ).fetchone()
    return r, mid, lid


def _verify(r: Rig, url: str, home: Any) -> Any:
    return drain(r, url, registry_of(r, None, home), queue="migrate_verify")


def _letters(db: Any) -> int:
    return db.execute("SELECT count(*) FROM pgmq.q_migrate_verify_dlq").fetchone()[0]


def _checksum_known(db: Any, r: Rig) -> None:
    """The S3 object's checksum is on record, so a copy's check has what it needs."""
    db.execute(
        "UPDATE catalog.location SET crc64nvme = '\\x0102030405060708'::bytea"
        " WHERE backend_id = %s",
        (r.w.s3_id,),
    )


def _location(db: Any, lid: int) -> str:
    return db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (lid,)
    ).fetchone()[0]


# --- an outage holds, a slow read ends ------------------------------------------------------


def test_an_outage_holds_the_last_try_and_a_slow_read_doesnt(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 3)
    r, mid, lid = _copy_rig(db, connect)
    home = Outage(r.home)
    for _ in range(2):
        assert _verify(r, retention_db_url, home).dead == 0
    assert home.reads == 2
    # No answer: no read is spent, and the prefix says why it waits.
    for _ in range(3):
        stats = _verify(r, retention_db_url, home)
        assert stats.dead == 0, stats.errors
    assert home.reads == 2
    assert home.heads >= 3
    assert _letters(db) == 0
    assert "the last try waits" in r.mig(mid)["last_error"]
    assert _location(db, lid) == "pending"


def test_asking_for_the_object_holds_nothing(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Held for days, asked each hour: no id, and no read lock an `ALTER` would wait for."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 1)
    r, _mid, _lid = _copy_rig(db, connect)
    home = Watched(Outage(r.home), retention_db_url)
    assert _verify(r, retention_db_url, home).dead == 0
    assert "head" in home.calls()
    assert [c for c in home.seen if c.call == "head" and (c.ids or c.tables)] == []


def test_a_hold_looks_again_each_hour(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 1)
    r, _mid, _lid = _copy_rig(db, connect)
    assert _verify(r, retention_db_url, Outage(r.home)).dead == 0
    (wait,) = db.execute("SELECT max(vt) - now() FROM pgmq.q_migrate_verify").fetchone()
    assert timedelta(minutes=50) < wait <= timedelta(hours=1)


def test_a_source_that_doesnt_answer_holds_the_last_try_too(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The copy's checksum must still come from S3, and S3 is the one that is out."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 1)
    r, _mid, lid = _copy_rig(db, connect)
    s3 = FailOn(r.w.s3, "head")
    stats = drain(r, retention_db_url, registry_of(r, s3), queue="migrate_verify")
    assert stats.dead == 0 and stats.retried == 1
    assert s3.asked
    assert _letters(db) == 0
    assert _location(db, lid) == "pending"


def test_a_source_it_has_the_checksum_of_isnt_asked(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Its outage can't be why this copy fails."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 1)
    r, _mid, _lid = _copy_rig(db, connect)
    db.execute(
        "UPDATE catalog.location SET crc64nvme = '\\x0102030405060708'::bytea"
        " WHERE backend_id = %s",
        (r.w.s3_id,),
    )
    s3 = FailOn(r.w.s3, "head")
    drain(r, retention_db_url, registry_of(r, s3, FailingRead(r.home)), queue="migrate_verify")
    assert s3.asked == []
    assert _letters(db) == 1


def test_a_lost_permission_is_called_what_it_is(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Any error but a 404 holds, as `redrive.Probe` decides. It said the storage service
    wasn't answering, for 3 days, when the service was answering 403."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 1)
    r, mid, _lid = _copy_rig(db, connect)
    home = Outage(r.home)
    home.head_error = "403 AuthorizationFailure: this request is not authorized"
    assert _verify(r, retention_db_url, home).dead == 0
    error = r.mig(mid)["last_error"]
    assert "isn't answering" not in error
    assert home.head_error in error
    assert "access" in _kinds(retention_db_url, mid)


def test_when_it_answers_again_the_last_try_can_succeed(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 3)
    r, mid, lid = _copy_rig(db, connect)
    home = Outage(r.home)
    for _ in range(3):
        _verify(r, retention_db_url, home)
    assert _location(db, lid) == "pending"
    home.failing = False
    assert _verify(r, retention_db_url, home).dead == 0
    assert _location(db, lid) == "committed"
    assert r.mig(mid)["last_error"] is None
    assert _letters(db) == 0


def test_a_hold_ends_after_its_limit_with_one_last_try(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 3)
    r, mid, _lid = _copy_rig(db, connect)
    home = Outage(r.home)
    for _ in range(3):
        _verify(r, retention_db_url, home)
    assert home.reads == 2
    r.w.clock.advance(timedelta(days=3, minutes=1))
    stats = _verify(r, retention_db_url, home)
    assert home.reads == 3
    assert stats.dead == 1
    assert _letters(db) == 1
    assert "check_given_up" in _kinds(retention_db_url, mid)


def test_a_slow_read_ends_at_its_tries_with_no_hold(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The provider answers, so it isn't an outage, and no extra read is spent."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 3)
    r, _mid, _lid = _copy_rig(db, connect)
    home = FailingRead(r.home)
    for _ in range(3):
        _verify(r, retention_db_url, home)
    assert home.reads == 3
    assert _letters(db) == 1


def test_a_held_message_starts_afresh_when_a_person_resends_it() -> None:
    body = {"location_id": 1, READ_ERRORS_KEY: 11, migrate_jobs_module.HELD_SINCE_KEY: "x"}
    assert clean_body(body) == {"location_id": 1}


# --- a give-up stays on the prefix ----------------------------------------------------------


def _given_up(db: Any, connect: Any, url: str, monkeypatch: Any) -> tuple[Rig, int, int, Any]:
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 2)
    r, mid, lid = _copy_rig(db, connect)
    home = FailingRead(r.home)
    for _ in range(2):
        _verify(r, url, home)
    assert _letters(db) == 1
    return r, mid, lid, home


def _count(connect: Any, mid: int, times: int = 2) -> None:
    """The worker's count, twice by default: a stuck copy is counted once a prefix made no
    progress since its last count."""

    async def go(conn: psycopg.AsyncConnection) -> None:
        await conn.set_autocommit(True)
        cur = conn.cursor(row_factory=dict_row)
        for n in range(times):
            await cur.execute(TO_COUNT + " WHERE m.migration_id = %s", (mid,))
            m = await cur.fetchone()
            async with conn.transaction():
                at = datetime.now(UTC) + timedelta(minutes=5 * (n + 1))
                await count_one(conn, m, at, lambda _: _BATCH[0])

    run(_with(connect, go))
    # Past the samples just taken, for the next count.
    with psycopg.connect(_URL[0], autocommit=True) as c:
        c.execute(
            "UPDATE catalog.migration_progress SET at = at - interval '1 hour'"
            " WHERE migration_id = %s",
            (mid,),
        )


def _console(url: str, mid: int, connect: Any = None) -> tuple[str, list[dict[str, Any]]]:
    """(health, attention) as the console reads them, as the app that reads them, after the
    worker's count."""
    _URL[0] = url
    _count(connect or _CONNECT[0], mid)
    with psycopg.connect(as_app(url, "web"), autocommit=True, row_factory=dict_row) as c:
        m = c.execute(_ROWS + " WHERE m.migration_id = %s", (mid,)).fetchone()
    row = {
        "migration_id": str(mid),
        "failing_now": int(m["failing_now"] or 0),
        "restores_expiring": int(m["restores_expiring"] or 0),
    }
    return _health(m), _attention(row, m)


def _kinds(url: str, mid: int) -> set[str]:
    return {a["kind"] for a in _console(url, mid)[1]}


def test_a_new_run_doesnt_hide_a_give_up(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Nothing revives the copy (`_record` skips a pending one), so the prefix waits on a
    person, and the alert alone doesn't say which prefix."""
    r, mid, lid, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    db.execute(
        "UPDATE catalog.migration_prefix SET rerun_requested_at = now() + interval '1 hour'"
        " WHERE migration_id = %s",
        (mid,),
    )
    only(db, mid)
    before = r.mig(mid)["last_run_id"]
    drain(r, retention_db_url)
    assert r.mig(mid)["last_run_id"] != before
    assert _location(db, lid) == "pending"
    health, items = _console(retention_db_url, mid)
    assert health == "attention"
    [item] = [a for a in items if a["kind"] == "check_given_up"]
    assert "1 copy" in item["message"] and "dead letters" in item["message"]
    assert "copy_stranded" not in {a["kind"] for a in items}
    assert item["actions"] == []


def test_a_give_up_is_seen_on_the_next_prefix_over_the_same_objects(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The letter names the cancelled prefix, and the copy holds up the next one."""
    r, mid, lid, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _cancel(retention_db_url, mid)
    again = r.request()
    only(db, again)
    for _ in range(4):
        assert drain(r, retention_db_url).dead == 0
        r.sync()
    assert state(r, again) == "verifying"
    assert _location(db, lid) == "pending"
    assert r.mig(again)["last_error"] is None
    health, items = _console(retention_db_url, again)
    assert health == "attention"
    assert "check_given_up" in {a["kind"] for a in items}
    # And not on the prefix that was cancelled.
    assert _console(retention_db_url, mid)[1] == []


def test_a_give_up_leaves_the_prefixs_other_errors_alone(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """It replaced `last_error`, so a failed run showed the wrong message, and an access
    problem or an expiring warning couldn't show at all."""
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    job = f"omnifile-migration-{mid}"
    r.mover.fail_runs[job] = 1
    db.execute(
        "UPDATE catalog.migration_prefix SET rerun_requested_at = now() + interval '1 hour'"
        " WHERE migration_id = %s",
        (mid,),
    )
    only(db, mid)
    drain(r, retention_db_url)  # starts the run
    drain(r, retention_db_url)  # sees it failed
    m = r.mig(mid)
    assert m["run_state"] == "failed" and m["last_error"] == "the run failed"
    by_kind = {a["kind"]: a for a in _console(retention_db_url, mid)[1]}
    assert by_kind["run_failed"]["message"] == "the run failed"
    assert "check_given_up" in by_kind
    # An access problem shows beside it.
    db.execute(
        "UPDATE catalog.migration_prefix SET last_error = 'AccessDenied: s3:GetObject',"
        " run_state = 'succeeded' WHERE migration_id = %s",
        (mid,),
    )
    assert {"access", "check_given_up"} <= _kinds(retention_db_url, mid)
    assert r.mig(mid)["last_error"] == "AccessDenied: s3:GetObject"


def test_giving_up_leaves_no_stale_error_behind(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The letter says it from here. A sent-again copy that works has nothing to clear."""
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    assert r.mig(mid)["last_error"] is None


def test_a_resend_that_works_ends_it(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    r, mid, lid, home = _given_up(db, connect, retention_db_url, monkeypatch)
    assert "check_given_up" in _kinds(retention_db_url, mid)
    (letter,) = db.execute(
        "SELECT message FROM pgmq.q_migrate_verify_dlq ORDER BY msg_id LIMIT 1"
    ).fetchone()
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    db.execute(
        "SELECT pgmq.send('migrate_verify', %s::jsonb)", (Jsonb(clean_body(letter["message"])),)
    )
    # Sent again and not yet read: neither given up nor stranded.
    assert _kinds(retention_db_url, mid) == set()
    home.failing = False
    assert _verify(r, retention_db_url, home).dead == 0
    assert _location(db, lid) == "committed"
    assert _kinds(retention_db_url, mid) == set()


def test_a_letter_counts_only_while_its_copy_is_pending(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A letter nobody cleared, for a copy dropped since, holds nothing up."""
    _r, mid, lid, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    assert "check_given_up" in _kinds(retention_db_url, mid)
    db.execute("UPDATE catalog.location SET state = 'deleted', committed_at = now()"
               " WHERE location_id = %s", (lid,))  # fmt: skip
    assert _letters(db) == 1
    assert "check_given_up" not in _kinds(retention_db_url, mid)


def test_a_letter_counts_only_for_the_prefix_holding_its_copy(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _cancel(retention_db_url, mid)
    elsewhere = r.request("m/sub/")
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'verifying' WHERE migration_id = %s",
        (elsewhere,),
    )
    assert "check_given_up" not in _kinds(retention_db_url, elsewhere)


def test_a_copy_with_a_letter_isnt_also_stranded(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    assert _kinds(retention_db_url, mid) == {"check_given_up"}


def test_a_malformed_letter_doesnt_break_the_console(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    _r, mid, _lid = _copy_rig(db, connect)
    for bad in ({"location_id": "abc"}, {}, {"location_id": "9" * 40}):
        db.execute("SELECT pgmq.send('migrate_verify_dlq', %s::jsonb)", (Jsonb({"message": bad}),))
    db.execute("SELECT pgmq.send('migrate_verify_dlq', %s::jsonb)", (Jsonb(["not", "an object"]),))
    assert "check_given_up" not in _kinds(retention_db_url, mid)


# --- a copy nothing will check ---------------------------------------------------------------


def test_a_force_dropped_letter_doesnt_look_healthy(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """`dead-letters drop --force` clears the alert, and the copy can never verify."""
    r, mid, lid, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _checksum_known(db, r)
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    assert _location(db, lid) == "pending"
    assert state(r, mid) == "verifying"
    health, items = _console(retention_db_url, mid)
    assert health == "attention"
    [item] = [a for a in items if a["kind"] == "copy_stranded"]
    assert "no check queued" in item["message"]
    # Only what is known: nothing says a letter was dropped.
    assert "dropped" not in item["message"]


def _no_checksum_rig(
    db: Any, connect: Any, url: str, *, batch: bool = True
) -> tuple[Rig, int, int]:
    """A copy whose check has ended for now: no message, and S3 has no checksum."""
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, batch=batch)
    _BATCH[0] = batch
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    mid = r.request()
    _pending_copy(r, mid)
    assert _verify(r, url, r.home).dead == 0
    (loc,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s AND state = 'committed'",
        (r.w.s3_id,),
    ).fetchone()
    return r, mid, loc


def _job(db: Any, r: Rig, mid: int, loc: int, state: str) -> None:
    (job,) = db.execute(
        "INSERT INTO catalog.checksum_job (migration_id, backend_id, name, job_id, objects,"
        " requested_at, state) VALUES (%s, %s, %s, 'j', 1, now(), %s) RETURNING checksum_job_id",
        (mid, r.w.s3_id, f"test-{mid}-{state}", state),
    ).fetchone()
    db.execute(
        "INSERT INTO catalog.checksum_job_object (checksum_job_id, location_id) VALUES (%s, %s)",
        (job, loc),
    )


def test_a_copy_waiting_for_its_checksum_job_to_be_asked_doesnt_flash_up(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """The next step asks within a minute, and the job queues the check again."""
    _r, mid, _loc = _no_checksum_rig(db, connect, retention_db_url)
    assert _kinds(retention_db_url, mid) == set()


def test_a_copy_on_a_running_checksum_job_isnt_stuck(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid, loc = _no_checksum_rig(db, connect, retention_db_url)
    _job(db, r, mid, loc, "running")
    assert _kinds(retention_db_url, mid) == set()


def test_an_object_later_than_the_running_job_isnt_stuck_either(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """Its turn comes after this job, which can take a week."""
    r, mid, _loc = _no_checksum_rig(db, connect, retention_db_url)
    db.execute(
        "INSERT INTO catalog.checksum_job (migration_id, backend_id, name, job_id, objects,"
        " requested_at) VALUES (%s, %s, %s, 'j', 1, now())",
        (mid, r.w.s3_id, f"test-{mid}-later"),
    )
    assert _kinds(retention_db_url, mid) == set()


def test_an_sse_c_copy_says_what_is_true(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Its job came and went with no value, so requeueing can't help, and there is no letter.
    It does hold up the cutover, and excluding it is what to do."""
    r, mid, loc = _no_checksum_rig(db, connect, retention_db_url)
    _job(db, r, mid, loc, "done")
    health, items = _console(retention_db_url, mid)
    assert health == "attention"
    [item] = items
    assert item["kind"] == "no_checksum" and item["actions"] == ["exclude"]
    assert "dropped" not in item["message"] and "dead letter" not in item["message"]
    assert "no checksum" in item["message"]


def test_a_copy_with_no_batch_to_ask_says_so_too(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    _r, mid, _loc = _no_checksum_rig(db, connect, retention_db_url, batch=False)
    assert _kinds(retention_db_url, mid) == {"no_checksum"}


def test_an_excluded_copy_isnt_stuck(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid, loc = _no_checksum_rig(db, connect, retention_db_url)
    _job(db, r, mid, loc, "done")
    assert _kinds(retention_db_url, mid) == {"no_checksum"}
    db.execute(
        "INSERT INTO catalog.migration_exclusion (migration_id, object_key, reason, excluded_by)"
        " VALUES (%s, 'm/old.segy', 'test', 'test')",
        (mid,),
    )
    assert _kinds(retention_db_url, mid) == set()


def test_a_copy_whose_source_is_gone_isnt_stuck(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """It isn't in the prefix's count, so it holds up nothing, and its check ends as gone."""
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _checksum_known(db, r)
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    assert "copy_stranded" in _kinds(retention_db_url, mid)
    db.execute(
        "UPDATE catalog.location SET state = 'deleted', committed_at = now() WHERE backend_id = %s",
        (r.w.s3_id,),
    )
    assert _kinds(retention_db_url, mid) == set()


def test_a_copy_of_an_old_version_isnt_stuck(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _r, mid, lid, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    assert "check_given_up" in _kinds(retention_db_url, mid)
    db.execute(
        "UPDATE catalog.file SET deleted_at = now() WHERE content_version_id ="
        " (SELECT version_id FROM catalog.location WHERE location_id = %s)",
        (lid,),
    )
    assert _kinds(retention_db_url, mid) == set()


def test_a_copy_with_its_message_queued_isnt_stranded(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    _r, mid, _lid = _copy_rig(db, connect)
    assert _kinds(retention_db_url, mid) == set()


# --- what else a check's error can touch ----------------------------------------------------


def test_a_late_checks_error_leaves_a_cut_over_prefixs_record_alone(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """It ended the `staging-left` alert early."""
    r, mid, _lid = _copy_rig(db, connect)
    left = f"1 {migrate_staging.LEFT_BEHIND} from x"
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'cut_over', cut_over_at = now(),"
        " verified_at = now(), files_total = 1, files_verified = 1, last_error = %s"
        " WHERE migration_id = %s",
        (left, mid),
    )
    assert _verify(r, retention_db_url, FailingRead(r.home)).dead == 0
    assert r.mig(mid)["last_error"] == left


def test_a_recovery_clears_only_its_own_error(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid, lid = _copy_rig(db, connect)
    home = FailingRead(r.home)
    assert _verify(r, retention_db_url, home).dead == 0
    assert r.mig(mid)["last_error"].startswith(
        f"{migrate_jobs_module.CHECK_FAILED} (location {lid})"
    )
    # Something else goes wrong on the prefix before the copy recovers.
    db.execute(
        "UPDATE catalog.migration_prefix SET last_error = 'another reason' WHERE migration_id = %s",
        (mid,),
    )
    home.failing = False
    assert _verify(r, retention_db_url, home).dead == 0
    assert _location(db, lid) == "committed"
    assert r.mig(mid)["last_error"] == "another reason"


def test_tries_are_at_most_an_hour_apart(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Without the cap the twelve tries stretch to about 34 hours."""
    r, _mid, _lid = _copy_rig(db, connect)
    db.execute(
        "UPDATE pgmq.q_migrate_verify SET message = message || %s", (Jsonb({READ_ERRORS_KEY: 9}),)
    )
    # Directly in the message, with the tenth about to fail: 2^9 minutes uncapped.
    (body,) = db.execute("SELECT message FROM pgmq.q_migrate_verify LIMIT 1").fetchone()
    assert body[READ_ERRORS_KEY] == 9
    assert _verify(r, retention_db_url, FailingRead(r.home)).dead == 0
    (wait,) = db.execute("SELECT max(vt) - now() FROM pgmq.q_migrate_verify").fetchone()
    assert timedelta(minutes=50) < wait <= timedelta(hours=1)


# --- a cutover's tries at a delete -----------------------------------------------------------


def test_a_cutover_makes_exactly_its_tries_at_a_delete(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """The note says 5, and it made 10: `unstage` tried too, at the top of each step, and
    nothing counted those."""
    r, mid = _held_at_verified(db, connect)
    s3 = FailOn(r.w.s3, "delete", only="/a.segy")
    for _ in range(migrate_staging.DELETE_TRIES + 2):
        assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
        if state(r, mid) == "cut_over":
            break
    assert state(r, mid) == "cut_over"
    assert _rows(r, mid)["a.segy"][0] == "failed"
    assert len(s3.asked) == migrate_staging.DELETE_TRIES


# --- waves nobody will close ------------------------------------------------------------------


def _sweep(r: Rig, connect: Any, at: datetime) -> int:
    (sweep,) = [
        j
        for j in migrate_jobs(FakeWorker(r.registry, r.w.clock, r.mover))
        if getattr(j, "name", "") == "abandoned_waves"
    ]

    async def go(conn: psycopg.AsyncConnection) -> int:
        await conn.set_autocommit(True)
        return await sweep.fn(conn, at)

    return run(_with(connect, go))


def _a_wave(db: Any, connect: Any, url: str) -> tuple[Rig, int]:
    r, mid = _wave_rig(db, connect)
    assert drain(r, url).dead == 0
    (n,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s AND state = 'running'",
        (mid,),
    ).fetchone()
    assert n == 1
    return r, mid


def _wave(db: Any, mid: int) -> tuple[str, list[str]]:
    (wid, st) = db.execute(
        "SELECT wave_id, state FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    rows = db.execute("SELECT state FROM catalog.restore WHERE wave_id = %s", (wid,)).fetchall()
    return st, [x[0] for x in rows]


def test_a_cancelled_prefixs_wave_is_closed_past_its_window(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A restore whose task failed stayed open for good, blocking that object in any later
    wave, and any recall that joined it."""
    r, mid = _a_wave(db, connect, retention_db_url)
    _cancel(retention_db_url, mid)
    now = datetime.now(UTC)
    # Counts below are at least: other tests leave their waves in this shared database.
    _sweep(r, connect, now)  # inside its window
    assert _wave(db, mid) == ("running", ["requested"])
    assert _sweep(r, connect, now + WAVE_WINDOW + timedelta(minutes=1)) >= 1
    assert _wave(db, mid) == ("done", ["failed"])
    (failure,) = db.execute(
        "SELECT failure FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert "cancelled" in failure
    # Nothing left to do.
    assert _sweep(r, connect, now + WAVE_WINDOW * 2) == 0
    # A later migration of the prefix may ask for the object again.
    again = r.request()
    assert again != mid
    r.step(again)
    r.step(again)
    r.step(again)
    (asked,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s", (again,)
    ).fetchone()
    assert asked == 1


def test_a_live_prefixs_wave_is_left_to_its_steps(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid = _a_wave(db, connect, retention_db_url)
    _sweep(r, connect, datetime.now(UTC) + WAVE_WINDOW * 2)
    assert _wave(db, mid) == ("running", ["requested"])


def test_a_restore_that_finished_stays_finished(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid = _a_wave(db, connect, retention_db_url)
    r.w.clock.advance(timedelta(hours=9))
    r.sync()
    assert _wave(db, mid) == ("running", ["completed"])
    _cancel(retention_db_url, mid)
    assert _sweep(r, connect, datetime.now(UTC) + WAVE_WINDOW * 2) >= 1
    assert _wave(db, mid) == ("done", ["completed"])


def test_a_cut_over_prefixs_wave_is_closed_too(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A prefix can verify before a step looks at its finished wave again."""
    r, mid = _a_wave(db, connect, retention_db_url)
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'cut_over', cut_over_at = now(),"
        " verified_at = now(), files_total = 1, files_verified = 1 WHERE migration_id = %s",
        (mid,),
    )
    assert _sweep(r, connect, datetime.now(UTC) + WAVE_WINDOW * 2) >= 1
    assert _wave(db, mid)[0] == "done"


def test_a_failed_prefixs_wave_is_left_alone(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A `retry` brings its steps back, and they close it."""
    r, mid = _a_wave(db, connect, retention_db_url)
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'failed' WHERE migration_id = %s", (mid,)
    )
    _sweep(r, connect, datetime.now(UTC) + WAVE_WINDOW * 2)
    assert _wave(db, mid) == ("running", ["requested"])


class SeesCommits:
    """`conn`, which at each close of a wave asks another session how many are closed."""

    def __init__(self, conn: psycopg.AsyncConnection, url: str, mids: list[int]) -> None:
        self._conn, self._url, self._mids = conn, url, mids
        self.closed_before: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        if "UPDATE catalog.restore_wave" in str(query):
            with psycopg.connect(self._url, autocommit=True) as c:
                (n,) = c.execute(
                    "SELECT count(*) FROM catalog.restore_wave"
                    " WHERE state = 'done' AND migration_id = ANY(%s)",
                    (self._mids,),
                ).fetchone()
            self.closed_before.append(n)
        return await self._conn.execute(query, *args, **kwargs)


def test_the_sweep_commits_each_wave_on_its_own(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A wave can have 100,000 restores. A hundred in one transaction is an id held for as
    long as that takes, with every edge's feed behind it."""
    _r1, one = _a_wave(db, connect, retention_db_url)
    _r2, two = _a_wave(db, connect, retention_db_url)
    _cancel(retention_db_url, one)
    _cancel(retention_db_url, two)

    async def go(conn: psycopg.AsyncConnection) -> list[int]:
        await conn.set_autocommit(True)
        seen = SeesCommits(conn, retention_db_url, [one, two])
        await close_abandoned(seen, datetime.now(UTC) + WAVE_WINDOW * 2)  # type: ignore[arg-type]
        return seen.closed_before

    closed_before = run(_with(connect, go))
    assert len(closed_before) >= 2
    assert closed_before[-1] > closed_before[0]


# --- what the sweep costs --------------------------------------------------------------------


class Watching:
    """`conn`, which records whether it holds an id as the count is about to be written."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn
        self.id_before_write: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        if "INSERT INTO catalog.migration_count" in str(query):
            cur = await self._conn.execute("SELECT pg_current_xact_id_if_assigned()")
            self.id_before_write.append((await cur.fetchone())[0])
        return await self._conn.execute(query, *args, **kwargs)


def test_counting_holds_no_id_until_it_writes(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Its scans can take minutes, and an id would hold back every edge's feed."""
    _r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)

    async def go(conn: psycopg.AsyncConnection) -> list[Any]:
        await conn.set_autocommit(True)
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(TO_COUNT + " WHERE m.migration_id = %s", (mid,))
        m = await cur.fetchone()
        seen = Watching(conn)
        async with conn.transaction():
            await count_one(seen, m, datetime.now(UTC), lambda _: False)  # type: ignore[arg-type]
        return seen.id_before_write

    assert run(_with(connect, go)) == [None]


def _stuck(db: Any, mid: int) -> tuple[int, int, int]:
    return db.execute(
        "SELECT copies_given_up, copies_stranded, copies_no_checksum"
        "  FROM catalog.migration_count WHERE migration_id = %s",
        (mid,),
    ).fetchone()


def _count_at(connect: Any, mid: int, at: datetime) -> None:
    """One count at `at`, as the sweep does it."""

    async def go(conn: psycopg.AsyncConnection) -> None:
        await conn.set_autocommit(True)
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(TO_COUNT + " WHERE m.migration_id = %s", (mid,))
        m = await cur.fetchone()
        async with conn.transaction():
            await count_one(conn, m, at, lambda _: _BATCH[0])

    run(_with(connect, go))


def _progressing(db: Any, mid: int) -> None:
    """The next count sees more files verified than the last one did."""
    db.execute("UPDATE catalog.migration_progress SET files_verified = files_verified + 1"
               " WHERE migration_id = %s", (mid,))  # fmt: skip


def _listed(db: Any, mid: int, kind: str) -> list[str]:
    rows = db.execute(
        "SELECT object_key FROM catalog.migration_stuck_copy"
        " WHERE migration_id = %s AND kind = %s ORDER BY object_key",
        (mid, kind),
    ).fetchall()
    return [k for (k,) in rows]


def test_a_source_that_keeps_progressing_doesnt_hide_a_stuck_copy(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A re-check found six sweeps with progress staying at nothing, for as long as it lasted.
    It reads the queues whole only hourly, though, not at every sweep."""
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _checksum_known(db, r)
    start = datetime.now(UTC)
    _count_at(connect, mid, start)
    assert _stuck(db, mid) == (1, 0, 0)
    # The copy is stuck from here on, and the prefix keeps making progress.
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    for minutes in range(5, 55, 5):
        _progressing(db, mid)
        _count_at(connect, mid, start + timedelta(minutes=minutes))
        assert _stuck(db, mid) == (0, 0, 0), minutes  # kept, not looked at
    _progressing(db, mid)
    _count_at(connect, mid, start + timedelta(minutes=55))
    assert _stuck(db, mid) == (0, 1, 0)
    assert _listed(db, mid, "stranded") == ["m/a.segy"]
    # And the look after that one is an hour off again.
    db.execute("UPDATE catalog.location SET state = 'deleted', committed_at = now()"
               " WHERE backend_id = %s", (r.w.s3_id,))  # fmt: skip
    _progressing(db, mid)
    _count_at(connect, mid, start + timedelta(minutes=60))
    assert _stuck(db, mid) == (0, 1, 0)
    _progressing(db, mid)
    _count_at(connect, mid, start + timedelta(minutes=110))
    assert _stuck(db, mid) == (0, 0, 0)
    assert _listed(db, mid, "stranded") == []


def test_a_prefix_that_stalls_is_looked_at_every_sweep(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _checksum_known(db, r)
    start = datetime.now(UTC)
    _count_at(connect, mid, start)
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    _count_at(connect, mid, start + timedelta(minutes=5))
    assert _stuck(db, mid) == (0, 1, 0)


def test_a_clock_set_back_doesnt_stop_it_looking(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A look dated ahead of the clock (the dev VMs move theirs) mustn't read as a recent one."""
    r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _checksum_known(db, r)
    start = datetime.now(UTC)
    _count_at(connect, mid, start + timedelta(days=30))
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    _progressing(db, mid)
    _count_at(connect, mid, start)
    assert _stuck(db, mid) == (0, 1, 0)


def test_an_exclude_lowers_the_number_while_the_prefix_progresses(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """It stayed high, in the console and on the page, after the fix had been used."""
    r, mid, loc = _no_checksum_rig(db, connect, retention_db_url)
    _job(db, r, mid, loc, "done")
    # Before the exclusion's own time: its `excluded_at` is the database's.
    start = datetime.now(UTC) - timedelta(minutes=2)
    _count_at(connect, mid, start)
    assert _stuck(db, mid) == (0, 0, 1)
    assert _listed(db, mid, "no_checksum") == ["m/old.segy"]
    _progressing(db, mid)
    _count_at(connect, mid, start + timedelta(minutes=1))
    assert _stuck(db, mid) == (0, 0, 1)
    db.execute(
        "INSERT INTO catalog.migration_exclusion (migration_id, object_key, reason, excluded_by)"
        " VALUES (%s, 'm/old.segy', 'test', 'test')",
        (mid,),
    )
    _progressing(db, mid)
    _count_at(connect, mid, start + timedelta(minutes=2))
    assert _stuck(db, mid) == (0, 0, 0)
    assert _listed(db, mid, "no_checksum") == []


def _copies_with_no_checksum(db: Any, connect: Any, url: str, keys: list[str]) -> tuple[Rig, int]:
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, batch=False)
    _BATCH[0] = False
    for key in keys:
        run(put(r.w, key, b"written before S3 kept checksums"))
        r.w.fake.forget_checksum(BUCKET, key)
    r.seed()
    mid = r.request()
    _pending_copy(r, mid)
    assert _verify(r, url, r.home).dead == 0
    return r, mid


def _source_location(key: str, r: Rig) -> str:
    return f"backend_id = {int(r.w.s3_id)} AND object_key = '{key}'"


def test_the_worker_lists_which_objects_they_are(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The Exclude dialog needs keys, and nothing else says which they are."""
    monkeypatch.setattr(migrate_progress, "KEEP_KEYS", 2)
    keys = ["m/a.segy", "m/b.segy", "m/c.segy", "m/d.segy"]
    r, mid = _copies_with_no_checksum(db, connect, retention_db_url, keys)
    db.execute("UPDATE catalog.location SET crc64nvme = '\\x0102030405060708'::bytea"
               " WHERE " + _source_location("m/a.segy", r))  # fmt: skip
    _count(connect, mid, times=1)
    # Bounded: the number says all of them, the list the first by key.
    assert _stuck(db, mid) == (0, 1, 3)
    assert _listed(db, mid, "stranded") == ["m/a.segy"]
    assert _listed(db, mid, "no_checksum") == ["m/b.segy", "m/c.segy"]
    # One that stops being stuck goes, and the next one shows.
    db.execute("UPDATE catalog.location SET state = 'deleted', committed_at = now()"
               " WHERE " + _source_location("m/b.segy", r))  # fmt: skip
    _count(connect, mid, times=1)
    assert _stuck(db, mid) == (0, 1, 2)
    assert _listed(db, mid, "no_checksum") == ["m/c.segy", "m/d.segy"]
    # A prefix that stops waiting on a check lists nothing.
    _cancel(retention_db_url, mid)
    _count(connect, mid, times=1)
    assert _stuck(db, mid) == (0, 0, 0)
    assert _listed(db, mid, "stranded") == [] and _listed(db, mid, "no_checksum") == []
    (looked,) = db.execute(
        "SELECT stuck_counted_at FROM catalog.migration_count WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert looked is None


def test_a_list_that_hasnt_changed_isnt_written_again(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A stalled prefix is looked at every sweep, and a rewrite would be dead rows each time."""
    _r, mid, _ = _no_checksum_rig(db, connect, retention_db_url, batch=False)
    _count(connect, mid, times=1)
    before = db.execute(
        "SELECT xmin::text FROM catalog.migration_stuck_copy WHERE migration_id = %s", (mid,)
    ).fetchall()
    assert len(before) == 1
    _count(connect, mid, times=1)
    assert (
        before
        == db.execute(
            "SELECT xmin::text FROM catalog.migration_stuck_copy WHERE migration_id = %s", (mid,)
        ).fetchall()
    )


def test_two_replicas_cant_mix_a_list(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """The second waited on the first's rows with its id held, and then added its own keys."""
    _r, mid, _ = _no_checksum_rig(db, connect, retention_db_url, batch=False)

    async def go() -> None:
        a = await psycopg.AsyncConnection.connect(retention_db_url, autocommit=True)
        b = await psycopg.AsyncConnection.connect(retention_db_url, autocommit=True)
        try:
            ca, cb = a.cursor(row_factory=dict_row), b.cursor(row_factory=dict_row)
            async with a.transaction():
                await migrate_progress._keep_keys(ca, mid, {"stranded": ["m/a", "m/b"]})
                async with b.transaction():
                    await asyncio.wait_for(
                        migrate_progress._keep_keys(cb, mid, {"stranded": ["m/b", "m/c"]}), 5
                    )
            assert _listed(db, mid, "stranded") == ["m/a", "m/b"]
            # It wasn't refused for good: the next look replaces the list whole.
            async with b.transaction():
                await migrate_progress._keep_keys(cb, mid, {"stranded": ["m/b", "m/c"]})
            assert _listed(db, mid, "stranded") == ["m/b", "m/c"]
        finally:
            await a.close()
            await b.close()

    run(go())


def test_a_key_being_written_elsewhere_doesnt_fail_the_count(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Run by the reviewer: `UniqueViolation`, once the other committed, out of the sweep."""
    _r, mid, _ = _no_checksum_rig(db, connect, retention_db_url, batch=False)

    async def go() -> None:
        a = await psycopg.AsyncConnection.connect(retention_db_url, autocommit=True)
        b = await psycopg.AsyncConnection.connect(retention_db_url, autocommit=True)
        try:
            cb = b.cursor(row_factory=dict_row)

            async def second() -> None:
                async with b.transaction():
                    await migrate_progress._keep_keys(cb, mid, {"stranded": ["m/b", "m/c"]})

            async with a.transaction():
                await a.execute(
                    "INSERT INTO catalog.migration_stuck_copy (migration_id, kind, object_key)"
                    " VALUES (%s, 'stranded', 'm/b')",
                    (mid,),
                )
                keep = asyncio.create_task(second())
                for _ in range(100):
                    (waiting,) = db.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                        (b.info.backend_pid,),
                    ).fetchone()
                    if waiting == "Lock":
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("the second connection never waited on the first")
            await asyncio.wait_for(keep, 5)
            assert _listed(db, mid, "stranded") == ["m/b", "m/c"]
        finally:
            await a.close()
            await b.close()

    run(go())


def _two_prefixes(db: Any, connect: Any) -> tuple[int, int]:
    mids = []
    for key in ("m/one.segy", "m/two.segy"):
        r = Rig(db, connect)
        run(put(r.w, key, b"seismic"))
        r.seed()
        mids.append(r.request())
    return mids[0], mids[1]


def _counted(db: Any, mid: int) -> bool:
    return db.execute(
        "SELECT EXISTS (SELECT 1 FROM catalog.migration_count WHERE migration_id = %s)", (mid,)
    ).fetchone()[0]


def _sweep_all(url: str) -> int:
    async def go() -> int:
        conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
        try:
            return await migrate_progress.progress_sweep(conn, datetime.now(UTC))
        finally:
            await conn.close()

    return run(go())


def test_one_prefix_failing_doesnt_stop_the_others_counts(
    db, connect, retention_db_url, monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    """The longest uncounted go first, so it would stop the same ones behind it every sweep."""
    bad, good = _two_prefixes(db, connect)
    real = migrate_progress.count_one

    async def poisoned(conn: Any, m: dict, now: datetime, has_batch: Any) -> None:
        if m["migration_id"] == bad:
            await conn.execute("SELECT 1 / 0")  # a real failure, in its own transaction
        await real(conn, m, now, has_batch)

    monkeypatch.setattr(migrate_progress, "count_one", poisoned)
    with caplog.at_level("ERROR", logger="omnifile_service.migrate"):
        _sweep_all(retention_db_url)
    assert _counted(db, good) and not _counted(db, bad)
    (record,) = [r for r in caplog.records if r.getMessage() == "a prefix's count failed"]
    assert record.migration_id == bad and record.exc_info[0] is psycopg.errors.DivisionByZero
    # And it is counted once it can be.
    monkeypatch.setattr(migrate_progress, "count_one", real)
    _sweep_all(retention_db_url)
    assert _counted(db, bad)


def test_a_connection_that_died_ends_the_sweep(
    db, connect, retention_db_url, monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    """Nothing after it can be counted, and each prefix's error would say the same thing."""
    _two_prefixes(db, connect)
    calls = []

    async def dead(conn: Any, m: dict, now: datetime, has_batch: Any) -> None:
        calls.append(m["migration_id"])
        await conn.close()
        raise psycopg.OperationalError("the server closed the connection")

    monkeypatch.setattr(migrate_progress, "count_one", dead)
    with caplog.at_level("ERROR", logger="omnifile_service.migrate"):
        with pytest.raises(psycopg.OperationalError):
            _sweep_all(retention_db_url)
    assert len(calls) == 1
    # Not blamed on each prefix in turn: the runner logs the one failure.
    assert not [r for r in caplog.records if r.getMessage() == "a prefix's count failed"]


def test_only_the_worker_writes_the_list(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    _r, mid, _ = _no_checksum_rig(db, connect, retention_db_url, batch=False)

    async def sweep() -> None:
        conn = await psycopg.AsyncConnection.connect(
            as_app(retention_db_url, "worker"), autocommit=True
        )
        try:
            await migrate_progress.progress_sweep(conn, datetime.now(UTC))
        finally:
            await conn.close()

    run(sweep())
    assert _listed(db, mid, "no_checksum") == ["m/old.segy"]
    with psycopg.connect(as_app(retention_db_url, "web"), autocommit=True) as c:
        assert c.execute(
            "SELECT object_key FROM catalog.migration_stuck_copy WHERE migration_id = %s", (mid,)
        ).fetchall() == [("m/old.segy",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM catalog.migration_stuck_copy")


def test_a_state_that_doesnt_wait_on_a_check_isnt_counted(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _r, mid, _, _ = _given_up(db, connect, retention_db_url, monkeypatch)
    _count(connect, mid)
    assert _stuck(db, mid)[0] == 1
    _cancel(retention_db_url, mid)
    _count(connect, mid)
    assert _stuck(db, mid) == (0, 0, 0)


def test_web_reads_no_queue_it_doesnt_send_to(retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A letter holds provider text and object keys, and nothing sanitises them."""
    with psycopg.connect(as_app(retention_db_url, "web"), autocommit=True) as c:
        for table in ("q_migrate_verify", "q_migrate_verify_dlq"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"SELECT 1 FROM pgmq.{table} LIMIT 1")


# --- one copy's error ------------------------------------------------------------------------


def test_one_copys_give_up_doesnt_clear_anothers_note(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Its retry note said so until its next failure, up to an hour away."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 2)
    r, mid, lid = _copy_rig(db, connect)
    home = FailingRead(r.home)
    assert _verify(r, retention_db_url, home).dead == 0  # its note: 1 of 2
    other = lid + 1000000
    db.execute(
        "UPDATE catalog.migration_prefix SET last_error = %s WHERE migration_id = %s",
        (f"{migrate_jobs_module.CHECK_FAILED} (location {other}): 1 of 2 times, retrying: x", mid),
    )
    assert _verify(r, retention_db_url, home).dead >= 0
    assert str(other) in r.mig(mid)["last_error"]


def test_the_sweep_asks_the_binding_whether_a_source_runs_batch(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A backend's row may not hold `batch`, so only the worker's binding knows. Without it, a
    copy waiting for the next step to ask for its job would show as one that can't be checked."""
    monkeypatch.setattr(migrate_progress, "PER_SWEEP", 100_000)
    r, mid, _ = _no_checksum_rig(db, connect, retention_db_url)
    (sweep,) = [
        j
        for j in migrate_jobs(FakeWorker(r.registry, r.w.clock, r.mover))
        if getattr(j, "name", "") == "migration_progress"
    ]

    async def go(conn: psycopg.AsyncConnection) -> None:
        await conn.set_autocommit(True)
        for n in range(2):
            await sweep.fn(conn, datetime.now(UTC) + timedelta(hours=n + 1))

    run(_with(connect, go))
    assert _stuck(db, mid) == (0, 0, 0)
