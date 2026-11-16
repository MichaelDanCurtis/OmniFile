"""A migration step holds no transaction id across a provider call.

The edge feed's horizon is the oldest open id, for every edge, so an id held during a
storage call keeps every edge's ACL and share events unread until the call ends. These
run the real `migrate` handler through the real consumer, over real Postgres, sim and the
S3 fake, and they look at what the step used to protect with its one transaction: a call
made twice after a crash or a lapsed timeout, a change during a call, and what it records."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg.types.json import Jsonb
from sources.support import BUCKET, GROUPS, INV, put
from watched import FakeWorker, Watched, drain_job

from omnifile_service import metrics
from omnifile_service.backends.base import BackendError, LogicalTier, ObjectNotFound, ObjectRef
from omnifile_service.catalog.privileges import as_app
from omnifile_service.migrate import SimMover
from omnifile_service.migrate import calls as migrate_calls
from omnifile_service.migrate import staging as migrate_staging
from omnifile_service.migrate.checksums import job_name
from omnifile_service.migrate.engine import DISCOVER, READ_SECONDS, RERUN_EVERY, _resuming
from omnifile_service.sources import register_source
from omnifile_service.web.routes_console import _health
from omnifile_service.worker import migrate_jobs as migrate_jobs_module
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.migrate_jobs import migrate_jobs
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Message, outside

from .test_engine import Rig, _recall, _with, run


def blocking(fn: Callable[[], Any]) -> Any:
    """Run `fn` to its end on another thread, with its own event loop, while the worker's
    is stopped in the call that `during` was called from."""
    out: list[Any] = []
    errors: list[BaseException] = []

    def go() -> None:
        try:
            out.append(fn())
        except BaseException as e:  # noqa: BLE001 - raised again below
            errors.append(e)

    t = threading.Thread(target=go)
    t.start()
    t.join(60)
    assert not t.is_alive(), "what ran during the call didn't finish"
    if errors:
        raise errors[0]
    return out[0]


def once(name: str, fn: Callable[[], Any]) -> Callable[[str], None]:
    """A `during` that runs `fn` at the first call of `name`."""
    done: list[bool] = []

    def during(call: str) -> None:
        if call == name and not done:
            done.append(True)
            blocking(fn)

    return during


def _feed(r: Rig, url: str, file_id: int) -> list[int]:
    """What an edge could read now, below a horizon worked out from this database's
    sessions alone (the real one spans the server, as in test_tiering_shared_blob)."""
    with psycopg.connect(url, autocommit=True) as c:
        (edge,) = c.execute(
            "SELECT edge_id FROM catalog.dataset WHERE dataset_id = %s", (r.w.dataset_id,)
        ).fetchone()
        (horizon,) = c.execute(
            """
            SELECT coalesce(
                (SELECT min(backend_xid::text::bigint) FROM pg_stat_activity
                  WHERE datname = current_database() AND backend_xid IS NOT NULL
                    AND pid <> pg_backend_pid()),
                pg_snapshot_xmax(pg_current_snapshot())::text::bigint)::text
            """
        ).fetchone()
        rows = c.execute(
            "SELECT entity_id FROM catalog.feed_read('0'::xid8, 0, %s::xid8, %s, %s, 100000)"
            " WHERE entity = 'file'",
            (horizon, edge, [r.w.dataset_id]),
        ).fetchall()
    return [x[0] for x in rows if x[0] == file_id]


def feed_checker(r: Rig, url: str) -> tuple[Callable[[str], None], list[tuple[str, bool]]]:
    """Run at each provider call: commit a change to a file, as an ACL change would, and
    see if an edge can read it."""
    (fid,) = r.db.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
        (r.w.dataset_id,),
    ).fetchone()
    seen: list[tuple[str, bool]] = []

    def during(call: str) -> None:
        before = len(_feed(r, url, fid))
        with psycopg.connect(url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute(
                "UPDATE catalog.file SET mode = %s WHERE file_id = %s",
                (0o600 + len(seen) % 64, fid),
            )
        seen.append((call, len(_feed(r, url, fid)) > before))

    return during, seen


def registry_of(r: Rig, s3: Any = None, home: Any = None) -> BackendRegistry:
    return BackendRegistry.from_mapping({r.w.s3_id: s3 or r.w.s3, r.w.home_id: home or r.home})


def drain(r: Rig, url: str, registry: Any = None, mover: Any = None, queue: str = "migrate") -> Any:
    """What's on the queue, shown now, through the worker's own handler."""
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(f"UPDATE pgmq.q_{queue} SET vt = now()")
    worker = FakeWorker(registry or r.registry, r.w.clock, mover or r.mover)
    return drain_job(url, migrate_jobs(worker), queue)


def only(db: psycopg.Connection, mid: int) -> None:
    """This migration's one message on the queue: other tests leave theirs."""
    db.execute("DELETE FROM pgmq.q_migrate")
    db.execute("SELECT pgmq.send('migrate', %s::jsonb)", (Jsonb({"migration_id": mid}),))


def state(r: Rig, mid: int) -> str:
    return str(r.mig(mid)["state"])


class CrashAfter:
    """`target`, whose worker dies the first time the call `name` returns, before anything
    is recorded: every session holding an advisory lock in this database is ended."""

    def __init__(self, target: Any, url: str, name: str, nth: int = 1) -> None:
        self._target, self._url, self._name, self._left = target, url, name, nth

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._target, attr)
        if attr != self._name:
            return value

        async def call(*a: Any, **k: Any) -> Any:
            out = await value(*a, **k)
            self._left -= 1
            if self._left == 0:
                with psycopg.connect(self._url, autocommit=True) as c:
                    c.execute(
                        "SELECT pg_terminate_backend(l.pid) FROM pg_locks l"
                        "  JOIN pg_database d ON d.oid = l.database"
                        " WHERE l.locktype = 'advisory' AND d.datname = current_database()"
                    )
            return out

        return call


def crash(r: Rig, url: str, registry: Any = None, mover: Any = None) -> None:
    with pytest.raises(psycopg.OperationalError):
        drain(r, url, registry, mover)


class Hung:
    """`target`, whose call `name` never answers."""

    def __init__(self, target: Any, name: str) -> None:
        self._target, self._name = target, name
        self.asked = 0

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._target, attr)
        if attr != self._name:
            return value

        async def call(*a: Any, **k: Any) -> Any:
            self.asked += 1
            await asyncio.sleep(60)  # long past the bound; a failing test still ends

        return call


def _staging_rig(db: Any, connect: Any, **kw: Any) -> tuple[Rig, int, ObjectRef]:
    """A prefix at `verifying`, with a user's recall of an archived object completed."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, staging=True, **kw)
    run(put(r.w, "m/hot.segy", b"hot"))
    cold = run(put(r.w, "m/deep/cold.segy", b"cold" * 50, tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    _recall(r, cold, "m/deep/cold.segy")
    only(db, mid)
    return r, mid, cold


def _staged(r: Rig, mid: int) -> list[tuple[str, str]]:
    return r.db.execute(
        "SELECT object_key, state FROM catalog.staging_object WHERE migration_id = %s"
        " ORDER BY object_key",
        (mid,),
    ).fetchall()


# --- the feed ---------------------------------------------------------------------------


def test_a_feed_event_during_a_staging_copy_can_be_read_at_once(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    r, _mid, _ = _staging_rig(db, connect)
    during, seen = feed_checker(r, retention_db_url)
    s3 = Watched(r.w.s3, retention_db_url, during)
    stats = drain(r, retention_db_url, registry_of(r, s3))
    assert stats.dead == 0, stats.errors
    assert "copy_object" in s3.calls()
    assert seen and all(ok for _, ok in seen), seen
    assert s3.with_ids() == []


def test_a_whole_migration_holds_no_id_across_any_call(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Every kind of call the engine makes, from `pending` to `cut_over`, each with a feed
    event committed during it. No write to the source bucket, in the whole run."""
    db.execute("DELETE FROM pgmq.q_migrate")
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    r = Rig(db, connect, staging=True, batch=True)
    run(put(r.w, "m/hot.segy", b"hot"))
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    run(put(r.w, "m/wave.segy", b"wave" * 10, tier=LogicalTier.ARCHIVE))
    urgent = run(put(r.w, "m/urgent.segy", b"urgent" * 10, tier=LogicalTier.ARCHIVE))
    r.seed()
    before = len(r.w.fake.calls)
    mid = r.request()
    only(db, mid)
    during, seen = feed_checker(r, retention_db_url)
    url = retention_db_url
    s3, home = Watched(r.w.s3, url, during), Watched(r.home, url, during)
    mover = Watched(r.mover, url, during)
    registry = registry_of(r, s3, home)
    recalled = False
    for _ in range(40):
        assert drain(r, url, registry, mover).dead == 0
        assert drain(r, url, registry, mover, "migrate_verify").dead == 0
        if state(r, mid) == "cut_over":
            break
        if state(r, mid) == "verifying" and not recalled:
            recalled = True
            _recall(r, urgent, "m/urgent.segy")
        r.sync()
        r.w.clock.advance(timedelta(hours=2))
        r.sync()
    assert state(r, mid) == "cut_over", r.mig(mid)["last_error"]
    made = set(s3.calls()) | set(home.calls()) | set(mover.calls())
    assert {
        "ensure_job", "running_run", "start_run", "run_status", "list", "copy_object", "head",
        "delete", "batch_restore", "batch_checksum", "batch_checksums", "batch_job", "get_range",
        "set_metadata",
    } <= made  # fmt: skip
    assert s3.with_ids() == [] and home.with_ids() == [] and mover.with_ids() == []
    assert seen and all(ok for _, ok in seen), [s for s in seen if not s[1]]
    # Decision 40: nothing was written to the source, and nothing deleted from it.
    writes = {"PutObject", "DeleteObject", "CopyObject", "CreateMultipartUpload",
              "UploadPartCopy", "CompleteMultipartUpload", "PutObjectTagging"}  # fmt: skip
    assert [
        (op, p.get("Key")) for op, p in r.w.fake.calls[before:]
        if op in writes and p.get("Bucket") == BUCKET
    ] == []  # fmt: skip


def test_removing_a_bad_copy_holds_no_id(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    r.mover.corrupt.add("m/a.segy")
    r.step(mid)
    r.step(mid)
    during, seen = feed_checker(r, retention_db_url)
    home = Watched(r.home, retention_db_url, during)
    stats = drain(r, retention_db_url, registry_of(r, None, home), queue="migrate_verify")
    assert stats.dead == 0, stats.errors
    assert "delete" in home.calls()
    assert home.with_ids() == []
    assert seen and all(ok for _, ok in seen), seen
    # And what it records is recorded once, for all its passes.
    assert r.mig(mid)["files_failed"] == 1
    (drift,) = db.execute(
        "SELECT count(*) FROM catalog.reconcile_drift WHERE backend_id = %s AND source = 'migrate'",
        (r.w.home_id,),
    ).fetchone()
    assert drift == 1


# --- bounds, and the message staying hidden -----------------------------------------------


def test_a_hung_staging_copy_is_cut_off(db, connect, retention_db_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    r, mid, _ = _staging_rig(db, connect)
    second = run(put(r.w, "m/deep/z.segy", b"z" * 50, tier=LogicalTier.ARCHIVE))
    r.sync()
    _recall(r, second, "m/deep/z.segy")
    only(db, mid)
    monkeypatch.setattr(migrate_staging, "STAGE_SECONDS", 0.2)
    s3 = Hung(r.w.s3, "copy_object")
    stats = run(asyncio.wait_for(asyncio.to_thread(
        drain, r, retention_db_url, registry_of(r, s3)), 30))  # fmt: skip
    assert stats.dead == 0, stats.errors
    # Each had its own step: past the deadline, the second has no row, and goes in the next.
    assert s3.asked == 2
    assert stats.acked == 3
    # Left to the prefix's next run, and whatever got there is deleted in the same step.
    key = f"omnifile-staging/{mid}/deep/cold.segy"
    assert _staged(r, mid) == [(key, "removed"), (f"omnifile-staging/{mid}/deep/z.segy", "removed")]
    assert ("HeadObject", INV, key) in [
        (op, p.get("Bucket"), p.get("Key")) for op, p in r.w.fake.calls
    ]
    for (error,) in db.execute(
        "SELECT error FROM catalog.staging_object WHERE migration_id = %s", (mid,)
    ).fetchall():
        assert "not copied in" in error
    assert state(r, mid) == "verifying"


def test_a_hung_status_call_is_retried_with_its_reason(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    r.step(mid)
    only(db, mid)
    monkeypatch.setattr(migrate_calls, "CALL_SECONDS", 0.2)
    mover = Hung(r.mover, "run_status")
    stats = run(asyncio.wait_for(asyncio.to_thread(
        drain, r, retention_db_url, None, mover), 30))  # fmt: skip
    assert stats.dead == 0 and stats.failed == 0, stats.errors
    assert stats.retried == 1
    assert mover.asked == 1
    assert "no answer from the provider" in r.mig(mid)["last_error"]
    assert state(r, mid) == "copying"


def test_a_long_copy_keeps_its_message_hidden(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """The row lock used to make a second holder wait. Now the message stays hidden, for
    longer than the call's bound."""
    r, _mid, _ = _staging_rig(db, connect)
    hidden: list[timedelta] = []

    def look() -> None:
        with psycopg.connect(retention_db_url, autocommit=True) as c:
            hidden.append(c.execute("SELECT max(vt) - now() FROM pgmq.q_migrate").fetchone()[0])

    s3 = Watched(r.w.s3, retention_db_url, once("copy_object", look))
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert hidden and hidden[0] > timedelta(seconds=migrate_staging.STAGE_SECONDS)


def test_a_long_read_back_keeps_its_message_hidden(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    r.step(mid)
    r.step(mid)
    hidden: list[timedelta] = []

    def look() -> None:
        with psycopg.connect(retention_db_url, autocommit=True) as c:
            hidden.append(
                c.execute("SELECT max(vt) - now() FROM pgmq.q_migrate_verify").fetchone()[0]
            )

    home = Watched(r.home, retention_db_url, once("get_range", look))
    assert drain(r, retention_db_url, registry_of(r, None, home), queue="migrate_verify").dead == 0
    assert hidden and hidden[0] > timedelta(seconds=READ_SECONDS)


def test_a_cutover_removes_every_staging_object_before_it_ends(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A step deletes a limited number, so the cutover waits for the rest."""
    r, mid = _held_at_verified(db, connect)
    for name in ("b", "c"):
        (loc,) = db.execute(
            "SELECT location_id FROM catalog.location WHERE backend_id = %s AND object_key = %s",
            (r.w.s3_id, f"m/{name}.segy"),
        ).fetchone()
        db.execute(
            "INSERT INTO catalog.staging_object (migration_id, location_id, bucket, object_key,"
            " state, staged_at) VALUES (%s, %s, %s, %s, 'failed', now())",
            (mid, loc, INV, f"omnifile-staging/{mid}/{name}.segy"),
        )
    monkeypatch.setattr(migrate_staging, "UNSTAGE_PER_STEP", 1)
    during, seen = feed_checker(r, retention_db_url)
    s3 = Watched(r.w.s3, retention_db_url, during)
    stats = drain(r, retention_db_url, registry_of(r, s3))
    assert s3.calls().count("delete") >= 3
    assert s3.with_ids() == []
    assert seen and all(ok for _, ok in seen), seen
    assert stats.dead == 0, stats.errors
    assert state(r, mid) == "cut_over"
    assert stats.acked == 3  # one a step, and the cutover waits for them all
    (left,) = db.execute(
        "SELECT count(*) FROM catalog.staging_object"
        " WHERE migration_id = %s AND state <> 'removed'",
        (mid,),
    ).fetchone()
    assert left == 0


def test_migrate_reads_one_message_at_a_time() -> None:
    """One read with a long copy would come back to another worker while it waits."""
    (step,) = [j for j in migrate_jobs(FakeWorker(None)) if getattr(j, "queue", "") == "migrate"]
    assert step.config.batch == 1


# --- a change during a call ---------------------------------------------------------------


def test_a_cancel_during_the_start_of_a_run_stands(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Design note section 9.1: a run already started finishes, and nothing is revived."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    only(db, mid)

    def cancel() -> None:
        with psycopg.connect(retention_db_url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute(
                "UPDATE catalog.migration_prefix SET state = 'cancelled', cancelled_at = now()"
                " WHERE migration_id = %s",
                (mid,),
            )

    mover = Watched(r.mover, retention_db_url, once("start_run", cancel))
    assert drain(r, retention_db_url, None, mover).dead == 0
    assert "start_run" in mover.calls()
    m = r.mig(mid)
    assert m["state"] == "cancelled" and m["last_run_id"] is None
    (left,) = db.execute("SELECT count(*) FROM pgmq.q_migrate").fetchone()
    assert left == 0


def _held_at_verified(db: Any, connect: Any) -> tuple[Rig, int]:
    """A verified prefix with a staging object to delete, and its cutover asked for."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, staging=True)
    run(put(r.w, "m/a.segy", b"seismic"))
    run(put(r.w, "m/b.segy", b"seismic 2"))
    run(put(r.w, "m/c.segy", b"seismic 3"))
    r.seed()
    mid = r.request(cutover="manual")
    assert r.drive(mid, until="verified") == "verified"
    (loc,) = db.execute(
        "SELECT location_id FROM catalog.location"
        " WHERE backend_id = %s AND object_key = 'm/a.segy'",
        (r.w.s3_id,),
    ).fetchone()
    db.execute(
        "INSERT INTO catalog.staging_object (migration_id, location_id, bucket, object_key,"
        " state, staged_at) VALUES (%s, %s, %s, %s, 'failed', now())",
        (mid, loc, INV, f"omnifile-staging/{mid}/a.segy"),
    )
    db.execute(
        "UPDATE catalog.migration_prefix SET cutover_requested_at = now(),"
        " cutover_requested_by = 'system:test' WHERE migration_id = %s",
        (mid,),
    )
    only(db, mid)
    return r, mid


def test_a_file_overwritten_during_a_call_is_not_cut_over_on_its_old_copy(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """Decision 24. The cutover's checks run after the step's calls, on what is true then."""
    r, mid = _held_at_verified(db, connect)

    def overwrite() -> None:
        run(put(r.w, "m/b.segy", b"new bytes, after the copy"))
        r.sync()

    s3 = Watched(r.w.s3, retention_db_url, once("delete", overwrite))
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert "delete" in s3.calls()
    assert state(r, mid) == "verifying"
    b = r.primary("b.segy")
    assert (b["backend_id"], b["object_key"]) == (r.w.s3_id, "m/b.segy")
    # The copy that was verified belongs to the old version, and only it.
    (copies,) = db.execute(
        "SELECT count(*) FROM catalog.location WHERE backend_id = %s AND version_id = %s",
        (r.w.home_id, b["version_id"]),
    ).fetchone()
    assert copies == 0


def test_a_hold_placed_during_a_call_waits_for_its_stamp(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Decision 31. The hold counts before the flip, however late in the step it came."""
    from .test_held_cutover import _hold, _src_dest

    r, mid = _held_at_verified(db, connect)
    s3 = Watched(r.w.s3, retention_db_url, once("delete", lambda: _hold(r, "a.segy")))
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert "delete" in s3.calls()
    m = r.mig(mid)
    assert m["state"] == "verified"
    assert "wait for their retention or legal hold" in m["last_error"]
    source, copy = _src_dest(r, "a.segy")
    assert source["role"] == "primary" and copy["role"] == "secondary"


def test_a_recall_joining_during_the_staging_copy_shares_it(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """A second user asks for the file while its staging copy runs. It is copied once, and
    the prefix's copy serves both."""
    r, mid, _ = _staging_rig(db, connect)
    (loc,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s"
        "   AND object_key = 'm/deep/cold.segy'",
        (r.w.s3_id,),
    ).fetchone()

    def join() -> None:
        with psycopg.connect(retention_db_url, autocommit=True) as c:
            c.execute(
                "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, state,"
                " triggered_by, completed_at, expires_at, provider_ref) VALUES (%s, %s,"
                " 's3_restore_object', 'expedited', 'completed', 'user_recall', now(),"
                " now() + interval '7 days', 'y')",
                (loc, r.w.s3_id),
            )

    s3 = Watched(r.w.s3, retention_db_url, once("copy_object", join))
    registry = registry_of(r, s3)
    assert drain(r, retention_db_url, registry).dead == 0
    key = f"omnifile-staging/{mid}/deep/cold.segy"
    assert _staged(r, mid) == [(key, "moving")]
    assert drain(r, retention_db_url, registry).dead == 0
    assert r.drive(mid) == "cut_over"
    assert s3.calls().count("copy_object") == 1
    assert _staged(r, mid) == [(key, "removed")]
    assert r.primary("cold.segy")["backend_id"] == r.w.home_id


def test_a_file_deleted_during_its_staging_copy_leaves_nothing_behind(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """What was copied is recorded even though the catalog no longer asks for it, or
    nothing would ever delete it."""
    r, mid, cold = _staging_rig(db, connect)

    def delete() -> None:
        run(r.w.client.delete(ObjectRef(cold.backend_id, cold.container, cold.key)))
        r.sync()

    s3 = Watched(r.w.s3, retention_db_url, once("copy_object", delete))
    registry = registry_of(r, s3)
    assert drain(r, retention_db_url, registry).dead == 0
    assert "copy_object" in s3.calls()
    key = f"omnifile-staging/{mid}/deep/cold.segy"
    assert _staged(r, mid)[0][0] == key
    r.mover.fail_runs[f"omnifile-staging-{mid}"] = 3
    for _ in range(5):
        assert drain(r, retention_db_url, registry).dead == 0
    assert _staged(r, mid) == [(key, "removed")]
    with pytest.raises(ObjectNotFound):
        run(r.w.s3.head(ObjectRef(r.w.s3_id, INV, key)))


def test_a_staging_bucket_that_becomes_a_source_during_a_copy_is_left_alone(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """Decision 40. Its object is on record, and never deleted."""
    r, mid, _ = _staging_rig(db, connect)

    def register() -> None:
        run(_with(connect, lambda c: register_source(
            c, r.w.s3, dataset_id=r.w.dataset_id, container=INV, groups=GROUPS, prefix="other/"
        )))  # fmt: skip

    s3 = Watched(r.w.s3, retention_db_url, once("copy_object", register))
    registry = registry_of(r, s3)
    before = len(r.w.fake.calls)
    for _ in range(3):
        assert drain(r, retention_db_url, registry).dead == 0
    assert "copy_object" in s3.calls()
    assert _staged(r, mid)[0][1] != "removed"
    assert [
        p.get("Key") for op, p in r.w.fake.calls[before:]
        if op == "DeleteObject" and p.get("Bucket") == INV
    ] == []  # fmt: skip


def test_a_runs_start_is_when_it_was_asked_for(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Not when its answer was recorded: a file written in S3 in between must not pass
    for one the run was sure to see, nor the journal gate be later than it should be."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    only(db, mid)
    asked: datetime = r.w.clock.now()

    def later() -> None:
        r.w.clock.advance(timedelta(hours=1))

    mover = Watched(r.mover, retention_db_url, once("start_run", later))
    assert drain(r, retention_db_url, None, mover).dead == 0
    assert r.mig(mid)["last_copy_started_at"] == asked


# --- a call made twice --------------------------------------------------------------------


def test_a_crash_after_starting_a_run_follows_that_run(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    only(db, mid)
    job = f"omnifile-migration-{mid}"
    r.mover.hold_runs.add(job)
    crash(r, retention_db_url, None, CrashAfter(r.mover, retention_db_url, "start_run"))
    assert r.mover.started == [job]
    m = r.mig(mid)
    assert m["state"] == "pending" and m["last_run_id"] is None
    assert drain(r, retention_db_url).dead == 0
    assert r.mover.started == [job]  # not started again
    m = r.mig(mid)
    assert m["state"] == "copying" and m["last_run_id"] == f"{job}-run-1"


def test_a_crash_after_starting_a_staging_run_follows_that_run(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    r, mid, _ = _staging_rig(db, connect)
    job = f"omnifile-staging-{mid}"
    r.mover.hold_runs.add(job)
    r.mover.started.clear()
    crash(r, retention_db_url, None, CrashAfter(r.mover, retention_db_url, "start_run"))
    assert r.mover.started == [job]
    assert drain(r, retention_db_url).dead == 0
    assert r.mover.started == [job]
    (runs,) = db.execute(
        "SELECT count(*) FROM catalog.staging_run WHERE migration_id = %s AND state = 'running'",
        (mid,),
    ).fetchone()
    assert runs == 1
    assert _staged(r, mid)[0][1] == "moving"


def _wave_rig(db: Any, connect: Any) -> tuple[Rig, int]:
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/hot.segy", b"hot"))
    run(put(r.w, "m/cold.segy", b"cold" * 50, tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    r.step(mid)
    r.step(mid)
    assert state(r, mid) == "verifying"
    only(db, mid)
    return r, mid


def _jobs(r: Rig, kind: str) -> set[str]:
    """The Batch jobs S3 has of this kind, by id: a request token finds a job again."""
    return {v for k, v in r.w.fake.batch_tokens.items() if k.startswith(f"omnifile-{kind}-")}


def test_a_crash_after_creating_a_wave_finds_the_same_job(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """No second job, and no second billable restore."""
    r, mid = _wave_rig(db, connect)
    s3 = CrashAfter(r.w.s3, retention_db_url, "batch_restore")
    crash(r, retention_db_url, registry_of(r, s3))
    jobs = _jobs(r, "wave")
    assert len(jobs) == 1
    restores = r.w.fake.ops().count("RestoreObject")
    (waves,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert waves == 0  # the crash took the record with it
    assert drain(r, retention_db_url).dead == 0
    assert _jobs(r, "wave") == jobs
    assert r.w.fake.ops().count("RestoreObject") == restores
    (job, objects) = db.execute(
        "SELECT job_id, objects FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert job in jobs and objects == 1
    (opened,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE wave_id IS NOT NULL AND backend_id = %s",
        (r.w.s3_id,),
    ).fetchone()
    assert opened == 1


def test_a_crash_after_creating_a_wave_survives_a_changed_catalog(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """The retry asks for other objects. S3 refuses a token used with other parameters, so
    a name that didn't say which objects would retry for ever, with nothing restored."""
    r, mid = _wave_rig(db, connect)
    s3 = CrashAfter(r.w.s3, retention_db_url, "batch_restore")
    crash(r, retention_db_url, registry_of(r, s3))
    run(put(r.w, "m/cold2.segy", b"cold2" * 50, tier=LogicalTier.ARCHIVE))
    r.sync()
    only(db, mid)
    assert drain(r, retention_db_url).dead == 0
    (objects,) = db.execute(
        "SELECT objects FROM catalog.restore_wave WHERE migration_id = %s AND state = 'running'",
        (mid,),
    ).fetchone()
    assert objects == 2
    assert len(_jobs(r, "wave")) == 2
    # S3 answers a restore of an object already being restored with a refusal, not a second
    # retrieval. So each object is restored once, whatever was asked.
    (running,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE state IN ('requested', 'in_progress')"
        " AND wave_id IS NOT NULL AND backend_id = %s",
        (r.w.s3_id,),
    ).fetchone()
    assert running == 2


def test_a_crash_after_creating_a_checksum_job_finds_the_same_job(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    mid = r.request()
    r.step(mid)
    r.step(mid)
    only(db, mid)
    s3 = CrashAfter(r.w.s3, retention_db_url, "batch_checksum")
    crash(r, retention_db_url, registry_of(r, s3))
    jobs = _jobs(r, "checksum")
    assert len(jobs) == 1
    assert drain(r, retention_db_url).dead == 0
    assert _jobs(r, "checksum") == jobs
    (rows,) = db.execute(
        "SELECT count(*) FROM catalog.checksum_job WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert rows == 1


def test_a_crash_after_a_staging_copy_records_one_object(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid, _ = _staging_rig(db, connect)
    crash(r, retention_db_url, registry_of(r, CrashAfter(r.w.s3, retention_db_url, "copy_object")))
    assert _staged(r, mid) == []
    assert drain(r, retention_db_url).dead == 0
    assert _staged(r, mid) == [(f"omnifile-staging/{mid}/deep/cold.segy", "moving")]


@pytest.mark.parametrize("second", ["lapsed", "another_message"])
def test_two_holders_never_both_call(db, connect, retention_db_url, second) -> None:  # type: ignore[no-untyped-def]
    """A second consumer gets the prefix while the first is in a call: its message's
    timeout lapsed, or someone sent another (the console's Copy again). No row lock makes
    it wait now. It leaves the message, and starts nothing."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    only(db, mid)
    stats: list[Any] = []

    def other() -> None:
        with psycopg.connect(retention_db_url, autocommit=True) as c:
            if second == "another_message":
                c.execute("SELECT pgmq.send('migrate', %s::jsonb)", (Jsonb({"migration_id": mid}),))
            c.execute("UPDATE pgmq.q_migrate SET vt = now()")
        stats.append(drain(r, retention_db_url))

    mover = Watched(r.mover, retention_db_url, once("start_run", other))
    assert drain(r, retention_db_url, None, mover).dead == 0
    assert r.mover.started == [f"omnifile-migration-{mid}"]
    # It saw the first holder's message, shown again, and the other one if sent. It took neither.
    assert stats[0].acked == 0 and stats[0].dead == 0
    assert stats[0].retried == (2 if second == "another_message" else 1)
    m = r.mig(mid)
    assert m["state"] == "copying" and m["last_run_id"] == f"omnifile-migration-{mid}-run-1"
    # The first holder finished its message, with no lost ownership.
    (kept,) = db.execute("SELECT count(*) FROM pgmq.a_migrate WHERE message->>'migration_id' = %s",
                         (str(mid),)).fetchone()  # fmt: skip
    assert kept >= 1
    # And the other message runs when it shows again, with nothing to start.
    if second == "another_message":
        assert drain(r, retention_db_url).dead == 0
        assert r.mover.started == [f"omnifile-migration-{mid}"]


def _advisory_locks(url: str) -> int:
    with psycopg.connect(url, autocommit=True) as c:
        (n,) = c.execute(
            "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database"
            " WHERE l.locktype = 'advisory' AND d.datname = current_database()"
        ).fetchone()
    return n


@pytest.mark.parametrize("ending", ["done", "failed", "taken_by_another"])
def test_the_consumer_lets_go_of_session_locks_however_a_message_ends(
    db, retention_db_url, ending
) -> None:  # type: ignore[no-untyped-def]
    """Otherwise a kept connection holds the prefix's lease for good, and its migration
    waits for ever."""
    db.execute("DELETE FROM pgmq.q_migrate")
    db.execute("SELECT pgmq.send('migrate', %s::jsonb)", (Jsonb({"test": ending}),))

    async def nothing() -> None:
        return None

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await conn.execute("SELECT pg_try_advisory_lock(hashtextextended('wp27-test', 0))")
        if ending == "taken_by_another":
            db.execute("DELETE FROM pgmq.q_migrate")  # the message is gone before the call
        await outside(msg, "call", nothing, hide_for=30)
        if ending == "failed":
            raise RuntimeError("the handler failed")

    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            as_app(retention_db_url, "worker"), autocommit=True
        )

    async def go() -> None:
        c = Consumer(connect, "migrate", handler, ConsumerConfig(backoff_base=0))
        try:
            await c.run_once()
            # Its connection is still open, and holds nothing.
            assert _advisory_locks(retention_db_url) == 0
        finally:
            await c.close()

    run(go())


# --- a listing of the target ---------------------------------------------------------------


def test_a_listing_goes_a_page_a_message_and_resumes(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """On 10^8 blobs it took hours in one transaction. Each page now commits, and a crash
    goes back only as far as the last page."""
    db.execute("DELETE FROM pgmq.q_migrate")
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    r = Rig(db, connect)
    for n in range(4):
        run(put(r.w, f"m/{n}.segy", b"seismic"))
    r.seed()
    mid = r.request()
    r.step(mid)
    r.home.list_page_size = 1
    only(db, mid)

    def pending() -> int:
        (n,) = db.execute(
            "SELECT count(*) FROM catalog.location WHERE backend_id = %s AND state = 'pending'",
            (r.w.home_id,),
        ).fetchone()
        return n

    crash(
        r, retention_db_url, registry_of(r, None, CrashAfter(r.home, retention_db_url, "list", 3))
    )
    assert pending() == 2  # the first two pages stand
    assert state(r, mid) == "copying"
    (body,) = db.execute("SELECT message FROM pgmq.q_migrate").fetchone()
    assert body[DISCOVER]["after"]
    home = Watched(r.home, retention_db_url)
    mover = Watched(r.mover, retention_db_url)
    stats = drain(r, retention_db_url, registry_of(r, None, home), mover)
    assert stats.dead == 0, stats.errors
    assert stats.acked == 2  # the two pages left, a message each
    assert home.calls().count("list") == 2
    assert mover.calls() == []  # the run was seen to end before the first page
    assert pending() == 4
    assert state(r, mid) == "verifying"
    (queued,) = db.execute("SELECT count(*) FROM pgmq.q_migrate_verify").fetchone()
    assert queued == 4


def test_a_listing_starts_again_for_another_run() -> None:
    now = datetime(2026, 11, 7)
    m = {"state": "copying", "last_copy_started_at": now}
    body = {DISCOVER: {"state": "copying", "since": str(now), "after": "page-2"}}
    msg = Message(1, 1, now, now, body)
    assert _resuming(msg, m) == "page-2"
    later = {**m, "last_copy_started_at": now + timedelta(hours=6)}
    assert _resuming(msg, later) is None
    assert _resuming(msg, {**m, "state": "verifying"}) is None
    assert _resuming(None, m) is None


# --- the blob, while a bad copy is removed ---------------------------------------------------


def test_a_copy_is_not_verified_while_its_blob_is_being_removed(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """Two versions' copies name one unversioned key. The late, bad one's delete holds no
    row now, and the other can't be marked verified over the blob it is deleting."""
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"current"))
    r.seed()
    mid = r.request()
    r.step(mid)
    r.step(mid)
    (fresh, file_id, version, container, key) = db.execute(
        "SELECT location_id, file_id, version_id, container, object_key FROM catalog.location"
        " WHERE backend_id = %s AND state = 'pending'",
        (r.w.home_id,),
    ).fetchone()
    db.execute(
        "UPDATE catalog.location SET backend_version_id = NULL WHERE location_id = %s", (fresh,)
    )
    (old,) = db.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, seq, name, kind, state, size)"
        " SELECT file_id, dataset_id, seq + 100, name, 'content', 'pending', 3"
        "   FROM catalog.file_version WHERE version_id = %s RETURNING version_id",
        (version,),
    ).fetchone()
    db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, size, crc64nvme, committed_at) VALUES (%s, %s, %s, %s, 'm/a.segy',"
        " 'secondary', 'committed', 3, '\\x0102030405060708'::bytea, now())",
        (file_id, old, r.w.s3_id, BUCKET),
    )
    (late,) = db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, size) VALUES (%s, %s, %s, %s, %s, 'secondary', 'pending', 3)"
        " RETURNING location_id",
        (file_id, old, r.w.home_id, container, key),
    ).fetchone()

    def only_verify(location_id: int) -> None:
        db.execute("DELETE FROM pgmq.q_migrate_verify")
        db.execute("SELECT pgmq.send('migrate_verify', %s::jsonb)",
                   (Jsonb({"location_id": location_id, "migration_id": mid}),))  # fmt: skip

    def the_other_one() -> Any:
        with psycopg.connect(retention_db_url, autocommit=True) as c:
            c.execute("SELECT pgmq.send('migrate_verify', %s::jsonb)",
                      (Jsonb({"location_id": fresh, "migration_id": mid}),))  # fmt: skip
        return drain(r, retention_db_url, queue="migrate_verify")

    only_verify(late)
    home = Watched(r.home, retention_db_url, once("delete", the_other_one))
    assert drain(r, retention_db_url, registry_of(r, None, home), queue="migrate_verify").dead == 0
    assert "delete" in home.calls()
    (st, verified) = db.execute(
        "SELECT state, verified FROM catalog.location WHERE location_id = %s", (fresh,)
    ).fetchone()
    assert (st, verified) == ("pending", False)
    (gone,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (late,)
    ).fetchone()
    assert gone == "deleted"
    # Then it reads the blob again, and finds it gone: the next run copies the file.
    assert drain(r, retention_db_url, queue="migrate_verify").dead == 0
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (fresh,)
    ).fetchone()
    assert st == "deleted"


# --- names ---------------------------------------------------------------------------------


def test_a_jobs_name_says_which_objects() -> None:
    a = [ObjectRef(1, "b", "x", "v1"), ObjectRef(1, "b", "y", "v1")]
    assert job_name("wave", 5, 1, a) == job_name("wave", 5, 1, list(a))
    assert job_name("wave", 5, 1, a) != job_name("wave", 5, 1, a[:1])
    assert job_name("wave", 5, 1, a) != job_name("checksum", 5, 1, a)
    # S3's limit on a request token.
    assert len(job_name("checksum", 2**63 - 1, 2**31, a)) <= 64


# --- what the review of 0507f31 found -------------------------------------------------------


class FailOn:
    """`target`, whose call `name` always fails, for the keys `only` names if any."""

    def __init__(self, target: Any, name: str, only: str | None = None) -> None:
        self._target, self._name, self._only = target, name, only
        self.asked: list[Any] = []

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._target, attr)
        if attr != self._name:
            return value

        async def call(*a: Any, **k: Any) -> Any:
            self.asked.append(a[0] if a else None)
            if self._only is None or self._only in str(getattr(a[0], "key", a[0])):
                raise BackendError(f"{attr} failed")
            return await value(*a, **k)

        return call


class FailingRead:
    """`target`, whose reads fail."""

    def __init__(self, target: Any) -> None:
        self._target = target
        self.reads = 0
        self.failing = True

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._target, attr)

    def get_range(self, *a: Any, **k: Any) -> Any:
        self.reads += 1
        if not self.failing:
            return self._target.get_range(*a, **k)

        async def fails() -> Any:
            raise BackendError("the read took too long")
            yield b""  # a generator, as `get_range` is

        return fails()


def _three_to_remove(r: Rig, mid: int) -> None:
    """a (from `_held_at_verified`), b and c each have a staging object to delete."""
    for name in ("b", "c"):
        (loc,) = r.db.execute(
            "SELECT location_id FROM catalog.location WHERE backend_id = %s AND object_key = %s",
            (r.w.s3_id, f"m/{name}.segy"),
        ).fetchone()
        r.db.execute(
            "INSERT INTO catalog.staging_object (migration_id, location_id, bucket, object_key,"
            " state, staged_at) VALUES (%s, %s, %s, %s, 'failed', now())",
            (mid, loc, INV, f"omnifile-staging/{mid}/{name}.segy"),
        )


def _rows(r: Rig, mid: int) -> dict[str, tuple[str, str | None]]:
    rows = r.db.execute(
        "SELECT object_key, state, error FROM catalog.staging_object WHERE migration_id = %s",
        (mid,),
    ).fetchall()
    return {k.rsplit("/", 1)[1]: (st, err) for k, st, err in rows}


def test_one_failed_delete_does_not_end_a_cutovers_wait(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The step counted what it removed, so one failure sent it to `cut_over` with b and c
    never tried. It now asks what is left, and tries the rest first."""
    r, mid = _held_at_verified(db, connect)
    _three_to_remove(r, mid)
    monkeypatch.setattr(migrate_staging, "UNSTAGE_PER_STEP", 1)
    s3 = FailOn(r.w.s3, "delete", only="/a.segy")
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert state(r, mid) == "verified"
    rows = _rows(r, mid)
    assert rows["b.segy"][0] == "removed" and rows["c.segy"][0] == "removed"
    assert rows["a.segy"][0] != "removed"
    assert "delete failed" in rows["a.segy"][1]


def test_a_key_that_keeps_failing_ends_in_a_cutover_on_the_record(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """Leftover staged bytes cost money and risk no data, so they don't block for ever. The
    row stays to be seen, `last_error` says so, and the prefix shows as needing attention."""
    r, mid = _held_at_verified(db, connect)
    db.execute("UPDATE catalog.staging_object SET state = 'copied' WHERE migration_id = %s", (mid,))
    s3 = FailOn(r.w.s3, "delete", only="/a.segy")
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    # It failed, and nothing else waits for a try: time before the next one.
    (wait,) = db.execute("SELECT max(vt) - now() FROM pgmq.q_migrate").fetchone()
    assert wait > migrate_staging.DELETE_RETRY - timedelta(minutes=1)
    rounds = 1
    while state(r, mid) != "cut_over":
        rounds += 1
        assert rounds < 20, "it never gave up"
        assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    # It was tried more than once, with time between: it wasn't given up at the first error.
    assert rounds >= migrate_staging.DELETE_TRIES
    asked = [x for x in s3.asked if str(getattr(x, "key", "")).endswith("/a.segy")]
    assert len(asked) >= migrate_staging.DELETE_TRIES
    m = r.mig(mid)
    assert migrate_staging.LEFT_BEHIND in m["last_error"]
    assert INV in m["last_error"]
    st, err = _rows(r, mid)["a.segy"]
    assert st == "failed" and "delete failed" in err
    assert _health({**m, "failing_now": 0, "restores_expiring": 0}) == "attention"


def test_a_finished_prefixs_old_error_is_not_a_reason_for_attention() -> None:
    m = {"state": "cut_over", "run_state": "succeeded", "failing_now": 0, "restores_expiring": 0}
    assert _health({**m, "last_error": "3 restored objects expire within 24 h"}) == "done"
    assert _health({**m, "last_error": None}) == "done"


def test_a_cutover_doesnt_wait_for_a_hung_delete_for_ever(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Out of time on a row counts as a try on that row, or the step would stay on it."""
    r, mid = _held_at_verified(db, connect)
    monkeypatch.setattr(migrate_staging, "DELETES_SECONDS", 0.2)
    s3 = Hung(r.w.s3, "delete")
    for _ in range(migrate_staging.DELETE_TRIES + 2):
        stats = run(asyncio.wait_for(asyncio.to_thread(
            drain, r, retention_db_url, registry_of(r, s3)), 30))  # fmt: skip
        assert stats.dead == 0, stats.errors
        if state(r, mid) == "cut_over":
            break
    assert state(r, mid) == "cut_over"
    assert _rows(r, mid)["a.segy"][0] == "failed"


def test_an_open_staging_run_is_closed_only_at_the_cutover(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """The wait for a failing delete can end in a file changing and a return to verifying,
    where the run must still be open."""
    r, mid = _held_at_verified(db, connect)
    db.execute(
        "INSERT INTO catalog.staging_run (migration_id, job, run_id, started_at)"
        " VALUES (%s, 'j', 'r', now())",
        (mid,),
    )
    s3 = FailOn(r.w.s3, "delete", only="/a.segy")
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert state(r, mid) == "verified"
    (st,) = db.execute(
        "SELECT state FROM catalog.staging_run WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert st == "running"


def test_a_wave_is_on_record_before_a_later_call_can_fail(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """CreateJob, then an ARM 503 on the rerun, in one step: the wave's record went with the
    rollback. With a new object before the retry, the digest changed, and there were 2 jobs."""
    r, mid = _wave_rig(db, connect)
    r.w.clock.advance(RERUN_EVERY)  # so a run starts in the same step
    mover = FailOn(r.mover, "start_run")
    for _ in range(3):
        assert drain(r, retention_db_url, None, mover).dead == 0
    assert mover.asked, "the step never got as far as the run"
    (waves,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert waves == 1
    run(put(r.w, "m/cold2.segy", b"cold2" * 50, tier=LogicalTier.ARCHIVE))
    r.sync()
    for _ in range(3):
        assert drain(r, retention_db_url, None, mover).dead == 0
    assert len(_jobs(r, "wave")) == 1


def test_a_checksum_job_is_on_record_before_a_later_call_can_fail(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    mid = r.request()
    r.step(mid)
    r.step(mid)
    only(db, mid)
    r.w.clock.advance(RERUN_EVERY)
    mover = FailOn(r.mover, "start_run")
    for _ in range(3):
        assert drain(r, retention_db_url, None, mover).dead == 0
    assert mover.asked
    (jobs,) = db.execute(
        "SELECT count(*) FROM catalog.checksum_job WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert jobs == 1


def test_a_staging_copy_is_on_record_before_a_later_call_can_fail(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """Each retry copied the object again, with nothing on record."""
    r, mid, _ = _staging_rig(db, connect)
    r.w.clock.advance(RERUN_EVERY)  # the prefix's own run fails after the copy, in one step
    mover = FailOn(r.mover, "start_run")
    s3 = Watched(r.w.s3, retention_db_url)
    for _ in range(3):
        assert drain(r, retention_db_url, registry_of(r, s3), mover).dead == 0
    assert mover.asked, "the step never got as far as the staging run"
    assert s3.calls().count("copy_object") == 1
    assert _staged(r, mid) == [(f"omnifile-staging/{mid}/deep/cold.segy", "staged")]


def _cancel(url: str, mid: int) -> None:
    with psycopg.connect(url, autocommit=True) as c, c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute(
            "UPDATE catalog.migration_prefix SET state = 'cancelled', cancelled_at = now()"
            " WHERE migration_id = %s",
            (mid,),
        )


def test_a_cancel_during_a_waves_creation_keeps_the_wave_on_record(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """S3 is restoring them. A later migration of the prefix mustn't ask for them again."""
    r, mid = _wave_rig(db, connect)
    s3 = Watched(
        r.w.s3, retention_db_url, once("batch_restore", lambda: _cancel(retention_db_url, mid))
    )
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert "batch_restore" in s3.calls()
    assert state(r, mid) == "cancelled"
    (waves,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert waves == 1
    (opened,) = db.execute(
        "SELECT count(*) FROM catalog.restore WHERE wave_id IS NOT NULL AND backend_id = %s",
        (r.w.s3_id,),
    ).fetchone()
    assert opened == 1
    (left,) = db.execute("SELECT count(*) FROM pgmq.q_migrate").fetchone()
    assert left == 0  # and it stays cancelled


def test_a_cancel_during_a_staging_copy_keeps_the_copy_on_record(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    r, mid, _ = _staging_rig(db, connect)
    s3 = Watched(
        r.w.s3, retention_db_url, once("copy_object", lambda: _cancel(retention_db_url, mid))
    )
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert state(r, mid) == "cancelled"
    assert _staged(r, mid) == [(f"omnifile-staging/{mid}/deep/cold.segy", "staged")]


def test_a_cancel_during_a_staging_runs_start_keeps_the_run_on_record(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    r, mid, _ = _staging_rig(db, connect)
    r.mover.hold_runs.add(f"omnifile-staging-{mid}")
    mover = Watched(
        r.mover, retention_db_url, once("start_run", lambda: _cancel(retention_db_url, mid))
    )
    for _ in range(2):
        drain(r, retention_db_url, None, mover)
    assert state(r, mid) == "cancelled"
    assert "start_run" in mover.calls()
    (runs,) = db.execute(
        "SELECT count(*) FROM catalog.staging_run WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert runs == 1
    assert _staged(r, mid)[0][1] == "moving"


def test_a_later_migration_asks_again_about_a_cancelled_checksum_job(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """A job still `running` under a cancelled prefix has nobody to read its report. Its
    objects are asked about again, or their copies could never be checked."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    mid = r.request()
    r.step(mid)
    r.step(mid)
    only(db, mid)
    s3 = Watched(
        r.w.s3, retention_db_url, once("batch_checksum", lambda: _cancel(retention_db_url, mid))
    )
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert state(r, mid) == "cancelled"
    (running,) = db.execute(
        "SELECT count(*) FROM catalog.checksum_job WHERE migration_id = %s AND state = 'running'",
        (mid,),
    ).fetchone()
    assert running == 1
    again = r.request()
    assert again != mid
    assert r.drive(again) == "cut_over"
    (jobs,) = db.execute(
        "SELECT count(*) FROM catalog.checksum_job WHERE migration_id = %s", (again,)
    ).fetchone()
    assert jobs == 1


def _in_a_plan_wave(r: Rig, mid: int) -> None:
    (pid,) = r.db.execute(
        "INSERT INTO catalog.identity (sid, kind, display_name)"
        " VALUES ('S-1-5-21-9-9-8-' || (floor(random() * 1e6))::int, 'user', 'O')"
        " RETURNING identity_id"
    ).fetchone()
    (plan,) = r.db.execute(
        "INSERT INTO catalog.migration_plan (dataset_id, name, state, owner_identity_id,"
        " proposed_by) VALUES (%s, 'p', 'approved', %s, 'user:1') RETURNING plan_id",
        (r.w.dataset_id, pid),
    ).fetchone()
    (wave,) = r.db.execute(
        "INSERT INTO catalog.migration_wave (plan_id, seq, name, prefixes, start_at, cutover)"
        " VALUES (%s, 1, 'w', %s, now(), 'auto') RETURNING wave_id",
        (plan, ["m/"]),
    ).fetchone()
    r.db.execute(
        "INSERT INTO catalog.migration_wave_prefix (wave_id, prefix, migration_id)"
        " VALUES (%s, 'm/', %s)",
        (wave, mid),
    )


def _blackout_now(r: Rig, url: str, mid: int) -> None:
    at = r.w.clock.now()
    span = [
        {"from": (at - timedelta(days=1)).isoformat(), "to": (at + timedelta(days=1)).isoformat()}
    ]
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(
            "UPDATE catalog.migration_wave w SET blackouts = %s"
            " FROM catalog.migration_wave_prefix x"
            " WHERE x.wave_id = w.wave_id AND x.migration_id = %s",
            (Jsonb(span), mid),
        )


def test_a_window_closing_during_a_call_keeps_its_answer(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """The rerun reached the gate and returned, with the job or the run dropped."""
    r, mid = _wave_rig(db, connect)
    _in_a_plan_wave(r, mid)
    s3 = Watched(
        r.w.s3,
        retention_db_url,
        once("batch_restore", lambda: _blackout_now(r, retention_db_url, mid)),
    )
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    assert "batch_restore" in s3.calls()
    (waves,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert waves == 1


def test_a_window_closing_during_a_runs_start_keeps_the_run(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request(cutover="manual")
    r.step(mid)
    r.step(mid)
    _in_a_plan_wave(r, mid)
    first = r.mig(mid)["last_run_id"]
    r.w.clock.advance(RERUN_EVERY)
    only(db, mid)
    # A second file, so the prefix isn't verified and it goes on to its rerun.
    run(put(r.w, "m/b.segy", b"seismic 2"))
    r.sync()
    only(db, mid)
    mover = Watched(
        r.mover,
        retention_db_url,
        once("start_run", lambda: _blackout_now(r, retention_db_url, mid)),
    )
    assert drain(r, retention_db_url, None, mover).dead == 0
    assert "start_run" in mover.calls()
    assert r.mig(mid)["last_run_id"] != first


def test_migrate_verify_reads_one_message_at_a_time() -> None:
    """Verify has no lease, and a read can take hours: the ones read with it would come back
    to another worker while this one still holds them."""
    (verify,) = [
        j for j in migrate_jobs(FakeWorker(None)) if getattr(j, "queue", "") == "migrate_verify"
    ]
    assert verify.config.batch == 1


def _pending_copy(r: Rig, mid: int) -> None:
    r.db.execute("DELETE FROM pgmq.q_migrate_verify")
    r.step(mid)
    r.step(mid)


def test_a_read_back_that_keeps_failing_is_on_the_prefix_and_ends_in_an_alert(
    db, connect, retention_db_url, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """It retried in silence for ever, reading the whole file each time."""
    monkeypatch.setattr(migrate_jobs_module, "VERIFY_ERRORS", 3)
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    _pending_copy(r, mid)
    home = FailingRead(r.home)
    registry = registry_of(r, None, home)
    assert drain(r, retention_db_url, registry, queue="migrate_verify").dead == 0
    assert "1 of 3" in r.mig(mid)["last_error"]
    assert "the read took too long" in r.mig(mid)["last_error"]
    assert drain(r, retention_db_url, registry, queue="migrate_verify").dead == 0
    third = drain(r, retention_db_url, registry, queue="migrate_verify")
    # From here the dead letter says it, not `last_error` (test_read_back_hold.py).
    assert r.mig(mid)["last_error"] is None
    # It isn't read a fourth time: the message goes where the dead-letters alert sees it.
    assert third.dead == 1
    assert home.reads == 3
    (letters,) = db.execute("SELECT count(*) FROM pgmq.q_migrate_verify_dlq").fetchone()
    assert letters == 1


def test_a_read_back_that_recovers_clears_its_error(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    db.execute("DELETE FROM pgmq.q_migrate_verify_dlq")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    _pending_copy(r, mid)
    db.execute("UPDATE catalog.migration_prefix SET last_error = 'another reason'"
               " WHERE migration_id = %s", (mid,))  # fmt: skip
    home = FailingRead(r.home)
    registry = registry_of(r, None, home)
    assert drain(r, retention_db_url, registry, queue="migrate_verify").dead == 0
    assert "the read took too long" in r.mig(mid)["last_error"]
    home.failing = False
    assert drain(r, retention_db_url, registry, queue="migrate_verify").dead == 0
    assert r.mig(mid)["last_error"] is None
    (verified,) = db.execute(
        "SELECT count(*) FROM catalog.location WHERE backend_id = %s AND verified",
        (r.w.home_id,),
    ).fetchone()
    assert verified == 1


class RefusesPutDuringARun(SimMover):
    """A job definition can't be changed while it has a run going, as ARM may refuse
    (RA-14). The rest is `SimMover`."""

    async def ensure_job(self, job: Any) -> str:
        if await self.running_run(job.name) is not None:
            raise BackendError("Storage Mover refused PUT (409): a run is in progress")
        return await super().ensure_job(job)


def test_a_run_going_is_found_before_the_job_is_changed(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Ask first. A refused PUT would retry until the run ended, then start a second."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    only(db, mid)
    mover = RefusesPutDuringARun(r.registry)
    job = f"omnifile-migration-{mid}"
    mover.hold_runs.add(job)
    crash(r, retention_db_url, None, CrashAfter(mover, retention_db_url, "start_run"))
    assert drain(r, retention_db_url, None, mover).dead == 0
    assert mover.started == [job]
    m = r.mig(mid)
    assert m["state"] == "copying" and m["last_run_id"] == f"{job}-run-1"


def test_what_a_cutover_left_behind_is_a_gauge_for_a_week(db, connect) -> None:  # type: ignore[no-untyped-def]
    """The `staging-left` alert reads this, and it ends by itself: nothing retries a delete
    after a cutover, and an alert that can't clear soon stops being read."""
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'cut_over', cut_over_at = now(),"
        " verified_at = now(), files_total = 1, files_verified = 1, last_error = %s"
        " WHERE migration_id = %s",
        (f"1 {migrate_staging.LEFT_BEHIND} from x", mid),
    )
    reader = InMemoryMetricReader()
    metrics.configure("test", reader=reader)

    def gauge(at: datetime) -> float:
        run(metrics.Collector(connect, now=lambda: at).collect_once())
        data = reader.get_metrics_data()
        found = [
            p.value
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for g in sm.metrics
            if g.name == "omnifile.migration.staging_left"
            for p in g.data.data_points
        ]
        assert found
        return found[-1]

    try:
        now = datetime.now(UTC)
        assert gauge(now) >= 1
        before = gauge(now + timedelta(days=8))
        assert before == 0
    finally:
        metrics.shutdown()
    rules = (
        Path(__file__).parents[3] / "deploy" / "azure" / "modules" / "app-metrics.bicep"
    ).read_text()
    assert "name: 'staging-left'" in rules
    assert 'Name == "omnifile.migration.staging_left"' in rules


def test_a_cutover_closes_a_staging_run_still_open(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    r, mid = _held_at_verified(db, connect)
    db.execute(
        "INSERT INTO catalog.staging_run (migration_id, job, run_id, started_at)"
        " VALUES (%s, 'j', 'r', now())",
        (mid,),
    )
    assert drain(r, retention_db_url).dead == 0
    assert state(r, mid) == "cut_over"
    (st, error) = db.execute(
        "SELECT state, error FROM catalog.staging_run WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert st == "failed" and "cut over" in error


def test_a_staging_object_past_its_tries_is_not_tried_again(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Though another is still having its tries."""
    r, mid = _held_at_verified(db, connect)
    _three_to_remove(r, mid)
    db.execute(
        "UPDATE catalog.staging_object SET attempts = %s, state = 'copied'"
        " WHERE migration_id = %s AND object_key LIKE '%%/b.segy'",
        (migrate_staging.RUN_ATTEMPTS + migrate_staging.DELETE_TRIES, mid),
    )
    s3 = FailOn(r.w.s3, "delete")
    assert drain(r, retention_db_url, registry_of(r, s3)).dead == 0
    asked = [str(getattr(x, "key", "")) for x in s3.asked]
    assert any(k.endswith("/a.segy") for k in asked)
    assert not any(k.endswith("/b.segy") for k in asked)


def test_a_run_started_is_recorded_though_nothing_asks_for_it_any_more(
    db, connect, retention_db_url
) -> None:  # type: ignore[no-untyped-def]
    """The console's Copy again is taken back during the start. The step looks at why it
    wanted a run, finds no reason, and would forget the one it has."""
    db.execute("DELETE FROM pgmq.q_migrate")
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request(cutover="manual")
    r.step(mid)
    r.step(mid)
    first = r.mig(mid)["last_run_id"]
    run(put(r.w, "m/b.segy", b"seismic 2"))
    r.sync()
    r.w.clock.advance(timedelta(minutes=1))
    db.execute(
        "UPDATE catalog.migration_prefix SET rerun_requested_at = %s WHERE migration_id = %s",
        (r.w.clock.now(), mid),
    )
    only(db, mid)

    def taken_back() -> None:
        with psycopg.connect(retention_db_url, autocommit=True) as c, c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute(
                "UPDATE catalog.migration_prefix SET rerun_requested_at = NULL"
                " WHERE migration_id = %s",
                (mid,),
            )

    mover = Watched(r.mover, retention_db_url, once("start_run", taken_back))
    assert drain(r, retention_db_url, None, mover).dead == 0
    assert "start_run" in mover.calls()
    assert r.mig(mid)["last_run_id"] != first
