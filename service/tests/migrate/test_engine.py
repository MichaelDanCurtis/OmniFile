"""The migration engine end to end on sim: copy, verify, cut over
(docs/design/migration-engine.md)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sources.support import BUCKET, GROUPS, put, world
from watched import FakeWorker, Watched, drain_job

from omnifile_service.backends.base import BackendError, LogicalTier, ObjectRef, RestoreSpeed
from omnifile_service.backends.sim import SimBackend
from omnifile_service.migrate import (
    MigrationError,
    SimMover,
    migrate_step,
    request_location,
    verify_copy,
)
from omnifile_service.migrate.engine import RERUN_EVERY
from omnifile_service.sources import register_source, seed_step
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.changefeed_sync import sync_container
from omnifile_service.worker.migrate_jobs import migrate_jobs
from omnifile_service.worker.queue import Consumer, Retry


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _with(connect, fn):  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


class Rig:
    """A seeded S3 source, a sim Azure home, and the engine's copier.

    `through_consumer` (conftest runs each test both ways): steps and verifications go
    through the real consumer, so each provider call is made outside the transaction and
    the step runs again, instead of in place in the caller's transaction."""

    through_consumer = False

    def __init__(self, db, connect, *, versioning: bool = True, batch: bool = False,
                 staging: bool = False) -> None:  # type: ignore[no-untyped-def]  # fmt: skip
        self.db, self.connect = db, connect
        self.w = w = world(db, versioning=versioning, batch=batch, staging=staging)
        self.home = SimBackend.azure_two_account(w.clock, backend_id=w.home_id)
        self.registry = BackendRegistry.from_mapping({w.s3_id: w.s3, w.home_id: self.home})
        self.w.registry = self.registry
        self.mover = SimMover(self.registry)

    def seed(self, prefix: str = "m/") -> int:
        w = self.w

        async def go(conn) -> int:  # type: ignore[no-untyped-def]
            sid = await register_source(
                conn, w.s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, prefix=prefix
            )
            while True:
                async with conn.transaction():
                    if await seed_step(conn, self.registry.get, sid, w.clock.now()) == "seeded":
                        return sid

        sid = run(_with(self.connect, go))
        self.sync()
        return sid

    def sync(self) -> None:
        self.w.clock.advance(timedelta(seconds=5))
        run(_with(self.connect, lambda c: sync_container(
            c, self.registry, self.w.s3_id, BUCKET, self.w.clock.now())))  # fmt: skip

    def request(self, prefix: str = "m/", **kw: Any) -> int:
        async def go(conn) -> int:  # type: ignore[no-untyped-def]
            async with conn.transaction():
                return await request_location(
                    conn, dataset_id=self.w.dataset_id, prefix=prefix,
                    to_backend_id=kw.pop("to", self.w.home_id), **kw,
                )  # fmt: skip

        return run(_with(self.connect, go))

    def _queue_one(self, queue: str, key: str, value: int) -> None:
        """This queue holds one message, the newest for `value`, shown now. Other tests
        leave theirs on this shared queue."""
        self.db.execute(f"DELETE FROM pgmq.q_{queue} WHERE message->>%s IS DISTINCT FROM %s",
                        (key, str(value)))  # fmt: skip
        self.db.execute(
            f"DELETE FROM pgmq.q_{queue} WHERE msg_id < (SELECT max(msg_id) FROM pgmq.q_{queue})"
        )
        self.db.execute(f"UPDATE pgmq.q_{queue} SET vt = now()")
        (n,) = self.db.execute(f"SELECT count(*) FROM pgmq.q_{queue}").fetchone()
        if not n:
            self.db.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (queue, Jsonb({key: value})))

    def _step_through_consumer(self, mid: int) -> str:
        worker = FakeWorker(self.registry, self.w.clock, self.mover)
        job = migrate_jobs(worker)[0]

        async def go() -> None:
            c = Consumer(self.connect, "migrate", job.handler, job.config)
            try:
                while True:
                    before = self.mig(mid)["state"]
                    self._queue_one("migrate", "migration_id", mid)
                    await c.run_once()
                    assert not c.stats.errors, c.stats.errors
                    # A page of a listing, or a call's answer put on record: the same
                    # step goes on in the next message, as it does in place in one.
                    (due,) = self.db.execute(
                        "SELECT count(*) FROM pgmq.q_migrate WHERE vt <= now()"
                    ).fetchone()
                    if not due or self.mig(mid)["state"] != before:
                        return
            finally:
                await c.close()

        run(go())
        return str(self.mig(mid)["state"])

    def _verify_through_consumer(self, ids: list[int]) -> list[str]:
        out: list[str] = []

        async def handler(msg, conn) -> None:  # type: ignore[no-untyped-def]
            # worker/migrate_jobs.py's `verify`, keeping what verify_copy returned.
            lid = int(msg.body["location_id"])
            try:
                async with conn.transaction():
                    out.append(
                        await verify_copy(conn, self.registry.get, lid, self.w.clock.now(), msg)
                    )
            except BackendError as e:
                raise Retry(60) from e

        async def go() -> None:
            c = Consumer(self.connect, "migrate_verify", handler)
            try:
                for lid in ids:
                    self._queue_one("migrate_verify", "location_id", lid)
                    await c.run_once()
                    assert not c.stats.errors, c.stats.errors
            finally:
                await c.close()

        run(go())
        return out

    def step(self, mid: int) -> str:
        if self.through_consumer:
            return self._step_through_consumer(mid)

        async def go(conn) -> str:  # type: ignore[no-untyped-def]
            async with conn.transaction():
                s = await migrate_step(conn, self.registry.get, self.mover, mid, self.w.clock.now())
            return s.state

        return run(_with(self.connect, go))

    def verify_pending(self) -> list[str]:
        ids = [r[0] for r in self.db.execute(
            "SELECT location_id FROM catalog.location WHERE backend_id = %s AND state = 'pending'"
            " ORDER BY location_id", (self.w.home_id,)).fetchall()]  # fmt: skip
        if self.through_consumer:
            return self._verify_through_consumer(ids)

        async def go(conn) -> list[str]:  # type: ignore[no-untyped-def]
            out = []
            for lid in ids:
                async with conn.transaction():
                    out.append(await verify_copy(conn, self.registry.get, lid, self.w.clock.now()))
            return out

        return run(_with(self.connect, go))

    def drive(self, mid: int, *, until: str = "cut_over", steps: int = 12) -> str:
        """Step, verify copies and read the feed until `until` (or give up)."""
        state = ""
        for _ in range(steps):
            state = self.step(mid)
            self.verify_pending()
            self.sync()
            if state == until:
                break
        return state

    def mig(self, mid: int) -> dict:
        cur = self.db.cursor(row_factory=dict_row)
        cur.execute("SELECT * FROM catalog.migration_prefix WHERE migration_id = %s", (mid,))
        return cur.fetchone()  # type: ignore[return-value]

    def primary(self, name: str) -> dict:
        cur = self.db.cursor(row_factory=dict_row)
        cur.execute(
            "SELECT l.* FROM catalog.file f JOIN catalog.location l"
            "  ON l.location_id = f.primary_location_id"
            " WHERE f.dataset_id = %s AND f.name = %s AND f.deleted_at IS NULL",
            (self.w.dataset_id, name),
        )
        return cur.fetchone()  # type: ignore[return-value]

    def dataset_state(self) -> str:
        return self.db.execute(
            "SELECT migration_state FROM catalog.dataset WHERE dataset_id = %s",
            (self.w.dataset_id,),
        ).fetchone()[0]


def _file_crc(db, version_id: int) -> bytes | None:  # type: ignore[no-untyped-def]
    row = db.execute(
        "SELECT crc64nvme FROM catalog.file WHERE content_version_id = %s", (version_id,)
    ).fetchone()
    return None if row is None or row[0] is None else bytes(row[0])


def test_a_prefix_is_copied_verified_and_cut_over(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic" * 100))
    run(put(r.w, "m/deep/b.segy", b"more"))
    r.seed()
    mid = r.request()
    assert r.mig(mid)["state"] == "pending"
    assert r.drive(mid) == "cut_over"
    m = r.mig(mid)
    assert (m["files_total"], m["files_verified"], m["files_failed"]) == (2, 2, 0)
    assert m["run_counters"]["transferred"] == 2 and m["cut_over_at"] is not None
    a = r.primary("a.segy")
    assert (a["backend_id"], a["object_key"]) == (r.w.home_id, f"migrated/{BUCKET}/m/a.segy")
    assert a["verified"] and a["verified_by"] == "azure_read_crc" and a["crc64nvme"] is not None
    # The seeded file had no CRC; the verified one is now the file's, which
    # every edge fill is checked against.
    assert _file_crc(db, a["version_id"]) == a["crc64nvme"]
    # The S3 object stays, as a secondary copy; nothing is deleted in S3.
    (role,) = db.execute(
        "SELECT role FROM catalog.location WHERE backend_id = %s AND object_key = 'm/a.segy'",
        (r.w.s3_id,),
    ).fetchone()
    assert role == "secondary"
    # POSIX metadata stamped on the copy.
    head = run(r.home.head(ObjectRef(r.w.home_id, a["container"], a["object_key"])))
    assert head.metadata.get("file-owner") == "5000"
    # Nothing of the dataset comes from S3 any more.
    assert r.dataset_state() == "cut_over"


def test_a_bad_copy_is_caught_removed_and_copied_again(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic" * 100))
    r.seed()
    r.mover.corrupt.add("m/a.segy")
    mid = r.request()
    assert r.drive(mid, until="verifying", steps=3) == "verifying"
    assert r.drive(mid, steps=3) == "verifying"  # the copy failed its check
    m = r.mig(mid)
    assert m["files_failed"] == 1 and m["files_verified"] == 0
    (kind,) = db.execute(
        "SELECT kind FROM catalog.reconcile_drift WHERE backend_id = %s AND source = 'migrate'",
        (r.w.home_id,),
    ).fetchone()
    assert kind == "checksum_mismatch"
    # The next Additive run copies it again, and this time it's right.
    r.w.clock.advance(RERUN_EVERY)
    assert r.drive(mid) == "cut_over"
    assert r.primary("a.segy")["backend_id"] == r.w.home_id


def test_archived_objects_wait_for_their_restore(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/hot.segy", b"hot"))
    cold = run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=4) == "verifying"
    m = r.mig(mid)
    assert (m["files_total"], m["files_verified"]) == (2, 1)
    assert m["run_counters"]["unsupported"] == 1
    # Restored (a wave would do this), then the next run picks it up.
    run(r.w.s3.restore(cold, RestoreSpeed.STANDARD, 7))
    r.w.clock.advance(RERUN_EVERY)
    r.sync()
    assert r.drive(mid) == "cut_over"
    assert r.primary("cold.segy")["backend_id"] == r.w.home_id


def test_a_change_in_s3_after_the_copy_waits_for_the_next_run(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"one"))
    r.seed()
    mid = r.request()
    assert r.step(mid) == "copying"  # the run copied "one"
    r.w.clock.advance(timedelta(minutes=1))
    two = run(put(r.w, "m/a.segy", b"two"))  # same size: only the time tells them apart
    r.sync()  # the follower applies the overwrite
    assert r.drive(mid, steps=4) == "verifying"  # the copy is of the old version
    r.w.clock.advance(RERUN_EVERY)
    assert r.drive(mid) == "cut_over"
    a = r.primary("a.segy")
    assert a["backend_id"] == r.w.home_id
    (cv,) = db.execute(
        "SELECT backend_version_id FROM catalog.location WHERE backend_id = %s"
        "   AND object_key = 'm/a.segy' AND version_id = %s",
        (r.w.s3_id, a["version_id"]),
    ).fetchone()
    assert cv == two.version_id  # the copy is of the new version
    # The stale copy was never taken for the new version: no false alarm.
    assert (
        db.execute(
            "SELECT count(*) FROM catalog.reconcile_drift"
            " WHERE backend_id = %s AND source = 'migrate'",
            (r.w.home_id,),
        ).fetchone()[0]
        == 0
    )


def test_an_external_copy_is_verified_and_cut_over(db, connect) -> None:  # type: ignore[no-untyped-def]
    from omnifile_service.migrate import MoverJob

    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"by prizmanyx"))
    r.seed()
    mid = r.request(copy_by="external")
    # Prizmanyx's own Storage Mover job, to the same target root.
    other = SimMover(r.registry)
    job = run(other.ensure_job(MoverJob("prizmanyx", r.w.s3_id, BUCKET, "m/", r.w.home_id,
                                        "live/data", f"migrated/{BUCKET}/m/")))  # fmt: skip
    r.mover = None  # the engine starts no copies itself
    assert r.drive(mid, until="verifying", steps=2) == "verifying"
    run(other.start_run(job))
    assert r.drive(mid) == "cut_over"
    assert r.primary("a.segy")["backend_id"] == r.w.home_id


def test_an_unversioned_overwrite_is_not_checked_against_the_old_version(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review finding: with no version id, the checksum HEAD read the key's
    # current object, and a copy of new bytes was verified as the old version.
    r = Rig(db, connect, versioning=False)
    run(put(r.w, "m/a.segy", b"one"))
    r.w.fake.forget_checksum(BUCKET, "m/a.segy")
    r.seed()
    mid = r.request()
    run(put(r.w, "m/a.segy", b"two"))  # same size; the follower hasn't seen it yet
    assert r.step(mid) == "copying"  # the run copies "two"
    r.step(mid)  # discovered as a copy of the catalog's "one"
    assert r.verify_pending() == ["stale"]
    assert _s3_location(r, "m/a.segy")["crc64nvme"] is None
    # Once the follower applies the overwrite, the copy is found for "two".
    r.sync()
    r.w.clock.advance(RERUN_EVERY)
    assert r.drive(mid) == "cut_over"
    a = r.primary("a.segy")
    ref = ObjectRef(a["backend_id"], a["container"], a["object_key"], a["backend_version_id"])
    assert run(_read(r, ref, 3)) == b"two"


def test_a_file_written_after_verification_is_copied_before_cut_over(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review finding: the last flip cut the prefix over without counting
    # again, leaving a file written since then on S3 for good.
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"a"))
    r.seed()
    mid = r.request()
    assert r.drive(mid, until="verified") == "verified"
    run(put(r.w, "m/late.segy", b"late"))
    r.sync()
    assert r.step(mid) == "verifying"
    r.w.clock.advance(RERUN_EVERY)
    assert r.drive(mid) == "cut_over"
    assert r.primary("late.segy")["backend_id"] == r.w.home_id
    assert r.dataset_state() == "cut_over"


def test_an_external_copy_after_an_overwrite_is_verified(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review finding: an external copier's copy of an overwritten object was
    # skipped as "written after the copy started" every time.
    from omnifile_service.migrate import MoverJob

    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"one"))
    r.seed()
    mid = r.request(copy_by="external")
    r.mover = None
    assert r.drive(mid, until="verifying", steps=2) == "verifying"
    r.w.clock.advance(timedelta(minutes=5))
    run(put(r.w, "m/a.segy", b"two"))
    r.sync()
    other = SimMover(r.registry)
    job = run(other.ensure_job(MoverJob("prizmanyx", r.w.s3_id, BUCKET, "m/", r.w.home_id,
                                        "live/data", f"migrated/{BUCKET}/m/")))  # fmt: skip
    run(other.start_run(job))
    assert r.drive(mid) == "cut_over"
    assert r.primary("a.segy")["backend_id"] == r.w.home_id


def test_a_bad_copy_never_deletes_a_key_another_location_uses(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review finding: with no blob version id, removing a late, bad copy of
    # an older version deleted the blob the file now reads from.
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"current"))
    r.seed()
    mid = r.request()
    assert r.drive(mid) == "cut_over"
    a = r.primary("a.segy")
    old = db.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, seq, name, kind, state, size)"
        " SELECT file_id, dataset_id, seq + 100, name, 'content', 'pending', 3"
        "   FROM catalog.file_version WHERE version_id = %s RETURNING version_id",
        (a["version_id"],),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, size, crc64nvme, committed_at) VALUES (%s, %s, %s, %s, 'm/a.segy',"
        " 'secondary', 'committed', 3, '\\x0102030405060708'::bytea, now())",
        (a["file_id"], old, r.w.s3_id, BUCKET),
    )
    (late,) = db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, size) VALUES (%s, %s, %s, %s, %s, 'secondary', 'pending', 3)"
        " RETURNING location_id",
        (a["file_id"], old, r.w.home_id, a["container"], a["object_key"]),
    ).fetchone()
    assert run(_with(connect, lambda c: _verify_one(c, r, late))) == "mismatch"
    # The key still reads (no delete, not even a delete marker).
    assert run(_read(r, ObjectRef(a["backend_id"], a["container"], a["object_key"]), 7)) == (
        b"current"
    )


async def _read(r: Rig, ref: ObjectRef, n: int) -> bytes:
    return b"".join([c async for c in r.home.get_range(ref, 0, n)])


async def _verify_one(conn, r: Rig, lid: int) -> str:  # type: ignore[no-untyped-def]
    async with conn.transaction():
        return await verify_copy(conn, r.registry.get, lid, r.w.clock.now())


def test_requests_are_checked(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a"))
    r.seed()
    for kw, code in (
        ({"prefix": "elsewhere/"}, "invalid_request"),
        ({"prefix": "m/x"}, "invalid_request"),
        ({"mode": "hand_off"}, "invalid_request"),
        ({"to": r.w.s3_id}, "invalid_request"),
    ):
        with pytest.raises(MigrationError) as e:
            r.request(**kw)
        assert e.value.code == code, kw
    r.request("m/sub/")
    with pytest.raises(MigrationError) as e:
        r.request("m/")
    assert e.value.code == "migration_conflict"


def test_the_worker_steps_a_migration_from_its_queue(db, connect) -> None:  # type: ignore[no-untyped-def]
    from omnifile_service.worker.queue import ConsumerConfig
    from omnifile_service.worker.runner import Worker

    r = Rig(db, connect)
    run(put(r.w, "m/a.segy"))
    r.seed()
    db.execute("DELETE FROM pgmq.q_migrate")
    mid = r.request()
    wk = Worker(connect, r.registry, clock=r.w.clock, mover=r.mover,
                consumer_config=ConsumerConfig(backoff_base=0))  # fmt: skip
    assert {"migrate", "migrate_verify"} <= set(wk.consumers)

    async def drain() -> int:
        try:
            return await wk.consumers["migrate"].drain()
        finally:
            await wk.consumers["migrate"].close()

    assert run(drain()) >= 1
    m = r.mig(mid)
    assert m["state"] == "copying" and m["run_state"] == "running"


def test_a_restore_wave_brings_archived_objects_in(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/hot.segy", b"hot"))
    run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    wave = db.execute(
        "SELECT wave_id, state, objects, job_id FROM catalog.restore_wave WHERE migration_id = %s",
        (mid,),
    ).fetchone()
    assert wave is not None and wave[1:3] == ("running", 1) and wave[3]
    (mech, trig, migrate) = db.execute(
        "SELECT mechanism, triggered_by, migrate_on_complete FROM catalog.restore"
        " WHERE wave_id = %s",
        (wave[0],),
    ).fetchone()
    assert (mech, trig, migrate) == ("s3_batch_restore", "migration_wave", True)
    # The job ran one RestoreObject (Bulk); no second wave while it runs.
    assert [c for c, _ in r.w.fake.calls].count("CreateJob") == 1
    r.drive(mid, steps=2)
    assert [c for c, _ in r.w.fake.calls].count("CreateJob") == 1
    # Bulk from Glacier: 8 h in the fake. Its completion event closes the
    # wave, and the restored object is copied, verified and cut over.
    r.w.clock.advance(timedelta(hours=9))
    r.sync()
    assert r.drive(mid) == "cut_over"
    assert r.primary("cold.segy")["backend_id"] == r.w.home_id
    (state,) = db.execute(
        "SELECT state FROM catalog.restore_wave WHERE wave_id = %s", (wave[0],)
    ).fetchone()
    assert state == "done"


def test_a_users_recall_is_copied_without_waiting_for_the_schedule(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)  # no Batch Operations: no waves
    run(put(r.w, "m/hot.segy", b"hot"))
    cold = run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    # A user's recall restores it (decision 15: the restore serves the move too).
    run(r.w.s3.restore(cold, RestoreSpeed.EXPEDITED, 7))
    r.w.clock.advance(timedelta(minutes=10))
    r.sync()
    (loc,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s"
        "   AND object_key = 'm/cold.segy'", (r.w.s3_id,)).fetchone()  # fmt: skip
    db.execute(
        "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, state,"
        " triggered_by, completed_at, provider_ref) VALUES (%s, %s, 's3_restore_object',"
        " 'expedited', 'completed', 'user_recall', %s, 'x')",
        (loc, r.w.s3_id, r.w.clock.now()),
    )
    # Not yet an hour since the run: wait. After it, copy again, long before
    # the 6-hour schedule.
    assert r.drive(mid, steps=2) == "verifying"
    r.w.clock.advance(timedelta(hours=1))
    assert r.drive(mid) == "cut_over"


def _checksum_jobs(db, mid: int) -> list[tuple]:  # type: ignore[no-untyped-def]
    return db.execute(
        "SELECT state, objects, found FROM catalog.checksum_job WHERE migration_id = %s"
        " ORDER BY checksum_job_id",
        (mid,),
    ).fetchall()


def _s3_location(r: Rig, key: str) -> dict:
    cur = r.db.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT * FROM catalog.location WHERE backend_id = %s AND object_key = %s",
        (r.w.s3_id, key),
    )
    return cur.fetchone()  # type: ignore[return-value]


def test_an_object_with_no_stored_checksum_gets_one_from_a_checksum_job(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old line,1+2 é.segy", b"written before S3 kept checksums"))
    run(put(r.w, "m/new.segy", b"has one"))
    r.w.fake.forget_checksum(BUCKET, "m/old line,1+2 é.segy")
    r.seed()
    assert _s3_location(r, "m/old line,1+2 é.segy")["crc64nvme"] is None
    mid = r.request()
    assert r.drive(mid, until="verifying", steps=3) == "verifying"
    # The copy waits; when the job's values land, it is queued to be checked.
    (copy,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s AND state = 'pending'",
        (r.w.home_id,),
    ).fetchone()
    db.execute("DELETE FROM pgmq.q_migrate_verify")
    r.step(mid)
    r.step(mid)
    queued = [m["location_id"] for (m,) in db.execute(
        "SELECT message FROM pgmq.q_migrate_verify").fetchall()]  # fmt: skip
    assert queued == [copy]
    # The job's value is the file's CRC too, before any copy is verified: the
    # edge can read the S3 object meanwhile.
    old = _s3_location(r, "m/old line,1+2 é.segy")
    assert old["crc64nvme"] is not None and _file_crc(db, old["version_id"]) == old["crc64nvme"]
    assert r.drive(mid) == "cut_over"
    # One job, for the one object with no checksum; S3 computed it at rest.
    assert _checksum_jobs(db, mid) == [("done", 1, 1)]
    assert [c for c, _ in r.w.fake.calls].count("CreateJob") == 1
    old = _s3_location(r, "m/old line,1+2 é.segy")
    assert old["verified_by"] == "s3_batch_checksum" and old["crc64nvme"] is not None
    a = r.primary("old line,1+2 é.segy")
    assert a["backend_id"] == r.w.home_id and a["crc64nvme"] == old["crc64nvme"]
    assert r.mig(mid)["last_error"] is None


def test_a_bad_copy_of_an_object_with_no_stored_checksum_is_caught(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    r.mover.corrupt.add("m/old.segy")
    mid = r.request()
    assert r.drive(mid, steps=4) == "verifying"
    (kind,) = db.execute(
        "SELECT kind FROM catalog.reconcile_drift WHERE backend_id = %s AND source = 'migrate'",
        (r.w.home_id,),
    ).fetchone()
    assert kind == "checksum_mismatch"
    assert r.mig(mid)["files_failed"] == 1
    # The next run copies it again, checked against the value already computed.
    r.w.clock.advance(RERUN_EVERY)
    assert r.drive(mid) == "cut_over"
    assert r.primary("old.segy")["backend_id"] == r.w.home_id
    assert len(_checksum_jobs(db, mid)) == 1


def test_objects_s3_cannot_checksum_stay_unverified(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/ssec.segy", b"customer key"))
    run(put(r.w, "m/fine.segy", b"fine"))
    r.w.fake.forget_checksum(BUCKET, "m/ssec.segy")
    r.w.fake.checksum_refused.add("m/ssec.segy")
    r.seed()
    mid = r.request()
    assert r.drive(mid) == "verifying"
    assert _checksum_jobs(db, mid) == [("done", 1, 0)]
    m = r.mig(mid)
    assert "1 objects have no CRC64NVME" in m["last_error"]
    assert (m["files_total"], m["files_verified"]) == (2, 1)
    # It isn't asked about again, and nothing is cut over unverified.
    assert len(_checksum_jobs(db, mid)) == 1
    assert r.primary("ssec.segy")["backend_id"] == r.w.s3_id


def test_a_failed_checksum_job_is_tried_again_then_given_up(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old.segy", b"old"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    r.w.fake.checksum_jobs_to_fail = 1
    mid = r.request()
    assert r.drive(mid) == "cut_over"
    assert [s for s, *_ in _checksum_jobs(db, mid)] == ["failed", "done"]

    r2 = Rig(db, connect, batch=True)
    run(put(r2.w, "n/old.segy", b"old"))
    r2.w.fake.forget_checksum(BUCKET, "n/old.segy")
    r2.seed("n/")
    r2.w.fake.checksum_jobs_to_fail = 5
    mid2 = r2.request("n/")
    assert r2.drive(mid2, steps=8) == "verifying"
    assert [s for s, *_ in _checksum_jobs(db, mid2)] == ["failed"] * 3
    assert "(3 of 3)" in r2.mig(mid2)["last_error"]


def _verified_copy(r: Rig, key: str) -> bool:
    return r.db.execute(
        "SELECT EXISTS (SELECT 1 FROM catalog.location t JOIN catalog.location s"
        "   ON s.version_id = t.version_id AND s.backend_id = %s AND s.object_key = %s"
        " WHERE t.backend_id = %s AND t.state = 'committed' AND t.verified)",
        (r.w.s3_id, key, r.w.home_id),
    ).fetchone()[0]


def test_a_copy_that_can_never_be_checked_does_not_hold_up_the_waves(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review finding: the copy of an object S3 can't checksum stays pending
    # for good, and used to stop every wave and rerun of its prefix.
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/ssec.segy", b"customer key"))
    run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.w.fake.forget_checksum(BUCKET, "m/ssec.segy")
    r.w.fake.checksum_refused.add("m/ssec.segy")
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=4) == "verifying"
    (waves,) = db.execute(
        "SELECT count(*) FROM catalog.restore_wave WHERE migration_id = %s", (mid,)
    ).fetchone()
    assert waves == 1
    r.w.clock.advance(timedelta(hours=9))
    r.sync()
    r.w.clock.advance(RERUN_EVERY)
    assert r.drive(mid, steps=6) == "verifying"
    # The archived object is copied and verified; nothing is cut over while
    # one file under the prefix has no verified copy.
    assert _verified_copy(r, "m/cold.segy")
    assert not _verified_copy(r, "m/ssec.segy")
    assert r.primary("cold.segy")["backend_id"] == r.w.s3_id


def test_with_no_batch_an_unchecked_copy_does_not_hold_up_the_reruns(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)  # no Batch Operations: nothing can compute the checksum
    run(put(r.w, "m/old.segy", b"written before S3 kept checksums"))
    cold = run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    first = r.mig(mid)["last_copy_started_at"]
    run(r.w.s3.restore(cold, RestoreSpeed.EXPEDITED, 7))
    r.w.clock.advance(timedelta(minutes=10))
    r.sync()
    r.w.clock.advance(RERUN_EVERY)
    r.drive(mid, steps=4)
    assert r.mig(mid)["last_copy_started_at"] > first
    assert _verified_copy(r, "m/cold.segy")


def _worker_step(r: Rig, mid: int) -> str:
    """One `migrate` message through the worker's handler, committed the way
    the consumer commits it (a Retry keeps the handler's writes)."""
    from omnifile_service.worker.migrate_jobs import migrate_jobs
    from omnifile_service.worker.queue import ConsumerConfig, Message, Retry

    class _W:
        registry, mover, clock = r.registry, r.mover, r.w.clock
        consumer_config = ConsumerConfig()

    handler = migrate_jobs(_W())[0].handler  # type: ignore[arg-type]
    now = r.w.clock.now()
    msg = Message(0, 1, now, now, {"migration_id": mid})

    async def go(conn) -> str:  # type: ignore[no-untyped-def]
        async with conn.transaction():
            try:
                await handler(msg, conn)
            except Retry:
                return "retry"
        return "ok"

    return run(_with(r.connect, go))


def test_a_provider_error_leaves_no_half_made_checksum_job(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review finding: the job row was written before CreateJob; a failed call
    # left a running job with no job id, and no checksum job ever ran again.
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/old.segy", b"old"))
    r.w.fake.forget_checksum(BUCKET, "m/old.segy")
    r.seed()
    mid = r.request()
    r.w.fake.fail_next("CreateJob", "InternalError", 500)
    outcomes = []
    for _ in range(8):
        outcomes.append(_worker_step(r, mid))
        r.verify_pending()
        r.sync()
        if r.mig(mid)["state"] == "cut_over":
            break
    assert "retry" in outcomes
    assert r.mig(mid)["state"] == "cut_over"
    assert _checksum_jobs(db, mid) == [("done", 1, 1)]


def _recall(r: Rig, ref: ObjectRef, key: str) -> None:
    """A user's recall of an archived object, completed."""
    run(r.w.s3.restore(ref, RestoreSpeed.EXPEDITED, 7))
    r.w.clock.advance(timedelta(minutes=10))
    r.sync()
    (loc,) = r.db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s AND object_key = %s",
        (r.w.s3_id, key),
    ).fetchone()
    r.db.execute(
        "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, state,"
        " triggered_by, completed_at, expires_at, provider_ref) VALUES (%s, %s,"
        " 's3_restore_object', 'expedited', 'completed', 'user_recall', %s, %s, 'x')",
        (loc, r.w.s3_id, r.w.clock.now(), r.w.clock.now() + timedelta(days=7)),
    )


def _staged(r: Rig, mid: int) -> list[tuple]:
    return r.db.execute(
        "SELECT object_key, state, attempts FROM catalog.staging_object WHERE migration_id = %s"
        " ORDER BY object_key",
        (mid,),
    ).fetchall()


def test_an_urgent_recall_is_copied_through_the_staging_bucket(db, connect) -> None:  # type: ignore[no-untyped-def]
    from sources.support import INV

    r = Rig(db, connect, staging=True)
    run(put(r.w, "m/hot.segy", b"hot"))
    cold = run(put(r.w, "m/deep/cold.segy", b"cold" * 50, tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    last_run = r.mig(mid)["last_run_id"]
    _recall(r, cold, "m/deep/cold.segy")
    # Copied inside S3 to the staging bucket, and a small run moves it onto
    # the real target path: no wait for the hour, nor a rerun of the prefix.
    r.step(mid)
    key = f"omnifile-staging/{mid}/deep/cold.segy"
    assert _staged(r, mid) == [(key, "moving", 1)]
    staged = run(r.w.s3.head(ObjectRef(r.w.s3_id, INV, key)))
    assert staged.native_class == "STANDARD"
    assert r.drive(mid) == "cut_over"
    assert r.mig(mid)["last_run_id"] == last_run
    c = r.primary("cold.segy")
    assert c["backend_id"] == r.w.home_id
    assert c["object_key"] == f"migrated/{BUCKET}/m/deep/cold.segy"
    assert c["verified_by"] == "azure_read_crc"
    # The staging object is gone once the copy is verified.
    assert _staged(r, mid) == [(key, "removed", 1)]
    with pytest.raises(Exception):  # noqa: B017, PT011 - ObjectNotFound or a 404
        run(r.w.s3.head(ObjectRef(r.w.s3_id, INV, key)))
    (runs,) = db.execute(
        "SELECT count(*) FROM catalog.staging_run WHERE migration_id = %s AND state = 'done'",
        (mid,),
    ).fetchone()
    assert runs == 1


def test_a_staging_object_of_an_overwritten_file_is_removed(db, connect) -> None:  # type: ignore[no-untyped-def]
    # Review nit: a staged object whose file changed since was never deleted.
    r = Rig(db, connect, staging=True)
    run(put(r.w, "m/hot.segy", b"hot"))
    cold = run(put(r.w, "m/cold.segy", b"cold" * 50, tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    _recall(r, cold, "m/cold.segy")
    r.step(mid)
    key = f"omnifile-staging/{mid}/cold.segy"
    assert _staged(r, mid) == [(key, "moving", 1)]
    run(put(r.w, "m/cold.segy", b"rewritten"))
    r.sync()
    for _ in range(3):
        r.step(mid)
        if _staged(r, mid)[0][1] == "removed":
            break
    assert _staged(r, mid) == [(key, "removed", 1)]
    assert r.mig(mid)["state"] != "cut_over"


def test_a_failing_staging_run_is_tried_again_then_left_to_the_prefixs_run(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, staging=True)
    cold = run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    _recall(r, cold, "m/cold.segy")
    r.mover.fail_runs[f"omnifile-staging-{mid}"] = 3
    assert r.drive(mid, steps=6) == "verifying"
    assert _staged(r, mid) == [(f"omnifile-staging/{mid}/cold.segy", "removed", 3)]
    (failed,) = db.execute(
        "SELECT count(*) FROM catalog.staging_run WHERE migration_id = %s AND state = 'failed'",
        (mid,),
    ).fetchone()
    assert failed == 3
    # The prefix's own run, an hour after the restore, still copies it.
    r.w.clock.advance(timedelta(hours=1))
    assert r.drive(mid) == "cut_over"
    assert r.primary("cold.segy")["backend_id"] == r.w.home_id


def test_a_wave_restore_is_not_staged(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect, batch=True, staging=True)
    run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    r.drive(mid, steps=3)
    r.w.clock.advance(timedelta(hours=9))
    r.sync()
    assert r.drive(mid) == "cut_over"
    assert _staged(r, mid) == []


def test_a_users_recall_through_the_recall_worker_serves_the_move(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Decision 15: the recall worker's restore of an object under a
    migrating prefix is marked to migrate, and the engine stages it."""
    from omnifile_service.worker.queue import Message, Retry
    from omnifile_service.worker.recalls import RecallConfig, handle_restore

    r = Rig(db, connect, staging=True)
    run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    run(put(r.w, "elsewhere/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed("")
    mid = r.request("m/")
    assert r.drive(mid, steps=3) == "verifying"
    cfg = RecallConfig(poll_min=timedelta(0), poll_max=timedelta(0), poll_no_eta=timedelta(0))

    def recall(name: str) -> int:
        (fid, vid) = db.execute(
            "SELECT f.file_id, f.content_version_id FROM catalog.file f JOIN catalog.file p"
            "  ON p.file_id = f.parent_id WHERE f.dataset_id = %s AND f.name = %s AND p.name = %s",
            (r.w.dataset_id, "cold.segy", name),
        ).fetchone()
        (rid,) = db.execute(
            "INSERT INTO catalog.recall (file_id, version_id) VALUES (%s, %s) RETURNING recall_id",
            (fid, vid),
        ).fetchone()
        return int(rid)

    def handle(rid: int) -> None:
        async def go(conn) -> None:  # type: ignore[no-untyped-def]
            msg = Message(1, 1, r.w.clock.now(), r.w.clock.now(), {"recall_id": str(rid)})
            async with conn.transaction():
                try:
                    await handle_restore(msg, conn, registry=r.registry, now=r.w.clock.now(),
                                         config=cfg)  # fmt: skip
                except Retry:
                    pass

        run(_with(r.connect, go))

    inside, outside = recall("m"), recall("elsewhere")
    handle(inside)
    handle(outside)
    flags = dict(db.execute(
        "SELECT l.object_key, s.migrate_on_complete FROM catalog.restore s"
        "  JOIN catalog.location l USING (location_id) WHERE s.triggered_by = 'user_recall'"
        "   AND s.backend_id = %s", (r.w.s3_id,)).fetchall())  # fmt: skip
    assert flags == {"m/cold.segy": True, "elsewhere/cold.segy": False}
    # Standard from Glacier: about 4 h in the fake. The completion event
    # lands, and the file is staged, copied, verified and cut over without
    # waiting for the prefix's next run.
    r.w.clock.advance(timedelta(hours=5))
    r.sync()
    handle(inside)
    last_run = r.mig(mid)["last_run_id"]
    assert r.drive(mid) == "cut_over"
    assert r.mig(mid)["last_run_id"] == last_run
    assert _staged(r, mid)[0][1] == "removed"


def test_a_staging_bucket_that_is_a_source_is_never_used(db, connect) -> None:  # type: ignore[no-untyped-def]
    from sources.support import INV

    r = Rig(db, connect, staging=True)
    cold = run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    # Another dataset reads the staging bucket.
    other = db.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id)"
        " SELECT 'other-' || gen_random_uuid(), backend_id, edge_id FROM catalog.dataset"
        " WHERE dataset_id = %s RETURNING dataset_id",
        (r.w.dataset_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO catalog.source (dataset_id, backend_id, container, prefix, path, state,"
        " owners_sid, writers_sid, readers_sid, kind) SELECT %s, backend_id, %s, '', '', state,"
        " owners_sid, writers_sid, readers_sid, kind FROM catalog.source WHERE dataset_id = %s",
        (other, INV, r.w.dataset_id),
    )
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    _recall(r, cold, "m/cold.segy")
    r.step(mid)
    assert _staged(r, mid) == []
    assert "is a source of a dataset" in r.mig(mid)["last_error"]


def test_a_copys_read_back_holds_nothing_across_its_calls(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """It read hundreds of GB with a read lock held, and stamped its metadata with an id."""
    r = Rig(db, connect)
    run(put(r.w, "m/a", b"seismic"))
    r.seed()
    mid = r.request()
    r.step(mid)  # starts the run
    r.step(mid)  # finds the copies
    (pending,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE backend_id = %s AND state = 'pending'",
        (r.w.home_id,),
    ).fetchone()
    # Other tests leave their messages on this shared queue.
    db.execute(
        "DELETE FROM pgmq.q_migrate_verify WHERE message->>'location_id' <> %s", (str(pending),)
    )
    home = Watched(r.home, retention_db_url)
    s3 = Watched(r.w.s3, retention_db_url)
    registry = BackendRegistry.from_mapping({r.w.s3_id: s3, r.w.home_id: home})  # type: ignore[dict-item]
    jobs = migrate_jobs(FakeWorker(registry, r.w.clock))
    stats = drain_job(retention_db_url, jobs, "migrate_verify")
    assert stats.dead == 0, stats.errors
    assert {"get_range", "set_metadata"} <= set(home.calls())
    for p in (home, s3):
        assert p.with_ids() == [] and p.with_locks() == []
    (verified,) = db.execute(
        "SELECT count(*) FROM catalog.location WHERE backend_id = %s AND state = 'committed'"
        " AND verified",
        (r.w.home_id,),
    ).fetchone()
    assert verified == 1
