"""A migration source is read-only to OmniFile (catalog-seeding.md
section 10; catalog/source_readonly.py).

Regression: once a share's rewind depth passed, pruning deleted old
versions of S3-resident files from the client's bucket (`prune_versions`
marked every location `deleting`, and the retention queue's `delete_blob`
called `S3Backend.delete`). The tiering scheduler and pins rewrote source
objects in place (an S3 class change is a CopyObject), and legal holds
tagged them. Every write the service sends to S3 is counted on the fake.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from s3.s3_fakes import ACCOUNT

from omnifile_service.backends.base import ObjectRef
from omnifile_service.policy import scheduler
from omnifile_service.retention import prune_versions
from omnifile_service.retention.worm import set_legal_hold
from omnifile_service.retention.worm_sync import delete_blob, stamp_worm
from omnifile_service.sources import register_source, seed_step
from omnifile_service.worker.changefeed_sync import sync_container
from omnifile_service.worker.queue import Message, Retry
from omnifile_service.worker.tiering import handle_move

from .support import BUCKET, GROUPS, World, put, world

#: S3 calls that change an object.
WRITES = frozenset({
    "PutObject", "CopyObject", "CreateMultipartUpload", "UploadPart", "UploadPartCopy",
    "CompleteMultipartUpload", "DeleteObject", "DeleteObjects", "PutObjectTagging",
    "DeleteObjectTagging", "PutObjectRetention", "PutObjectLegalHold", "PutObjectAcl",
})  # fmt: skip
POLICY = {"mode": "scheduled", "warm_day": 30, "cold_day": 120, "archive_day": 365}
D = timedelta(days=1)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _with(connect, fn):  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


def _ref(w: World, key: str) -> ObjectRef:
    return ObjectRef(w.s3_id, BUCKET, key)


def _mark(w: World) -> int:
    """Where the service's calls start: the client's own setup is before."""
    return len(w.fake.calls)


def _source_writes(w: World, since: int) -> list[tuple[str, Any]]:
    return [
        (op, p.get("Key"))
        for op, p in w.fake.calls[since:]
        if op in WRITES and p.get("Bucket") == BUCKET
    ]


def _seeded(connect, w: World) -> None:  # type: ignore[no-untyped-def]
    async def go(conn) -> None:  # type: ignore[no-untyped-def]
        sid = await register_source(
            conn, w.s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, prefix="f/"
        )
        while True:
            async with conn.transaction():
                if await seed_step(conn, w.registry.get, sid, w.clock.now()) == "seeded":
                    return

    run(_with(connect, go))
    _sync(connect, w)


def _sync(connect, w: World) -> None:  # type: ignore[no-untyped-def]
    w.clock.advance(timedelta(seconds=5))
    run(_with(connect, lambda c: sync_container(c, w.registry, w.s3_id, BUCKET, w.clock.now())))


def _file(db: psycopg.Connection, w: World, name: str) -> dict[str, Any]:
    row = (
        db.cursor(row_factory=dict_row)
        .execute(
            "SELECT * FROM catalog.file WHERE dataset_id = %s AND name = %s"
            " ORDER BY file_id DESC LIMIT 1",
            (w.dataset_id, name),
        )
        .fetchone()
    )
    assert row is not None
    return row


def _locations(db: psycopg.Connection, file_id: int) -> list[dict[str, Any]]:
    return list(
        db.cursor(row_factory=dict_row).execute(
            "SELECT l.*, v.state AS vstate FROM catalog.location l"
            "  JOIN catalog.file_version v USING (version_id)"
            " WHERE l.file_id = %s ORDER BY l.location_id",
            (file_id,),
        )
    )


def _identity(db: psycopg.Connection, w: World) -> int:
    (iid,) = db.execute(
        "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
        (f"S-1-5-21-7-7-{w.dataset_id}",),
    ).fetchone()  # type: ignore[misc]
    return int(iid)


def _policy(db: psycopg.Connection, w: World, iid: int) -> None:
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
            " VALUES (%s, 1, %s, %s)",
            (w.dataset_id, Jsonb(POLICY), iid),
        )
        db.execute("DELETE FROM pgmq.q_tiering")


def _drain_tiering(connect, w: World, now: datetime) -> int:  # type: ignore[no-untyped-def]
    """Run every queued move to the end, phase after phase."""

    async def go(conn) -> int:  # type: ignore[no-untyped-def]
        cur = await conn.execute("SELECT msg_id, message FROM pgmq.q_tiering ORDER BY msg_id")
        done = 0
        for msg_id, body in await cur.fetchall():
            await conn.execute("SELECT pgmq.delete('tiering', %s::bigint)", (msg_id,))
            for _ in range(10):
                msg = Message(msg_id, 1, now, now, body)
                try:
                    async with conn.transaction():
                        await handle_move(msg, conn, registry=w.registry, now=now)
                    break
                except Retry as r:
                    if r.message is None:
                        break
                    body = r.message
            done += 1
        return done

    return run(_with(connect, go))


def _drain_retention(connect, w: World, now: datetime) -> list[str]:  # type: ignore[no-untyped-def]
    async def go(conn) -> list[str]:  # type: ignore[no-untyped-def]
        cur = await conn.execute("SELECT msg_id, message FROM pgmq.q_retention ORDER BY msg_id")
        out = []
        for msg_id, body in await cur.fetchall():
            await conn.execute("SELECT pgmq.delete('retention', %s::bigint)", (msg_id,))
            if body.get("op") == "delete_blob":
                async with conn.transaction():
                    out.append(await delete_blob(conn, w.registry, int(body["location_id"]), now))
        return out

    return run(_with(connect, go))


# --- retention ----------------------------------------------------------------------


def test_pruning_a_version_never_deletes_it_from_the_source(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/line.segy", b"one"))
    run(put(w, "f/gone.segy", b"bye"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/line.segy", b"two"))  # the client overwrites one object...
    run(w.client.delete(_ref(w, "f/gone.segy")))  # ...and deletes another
    _sync(connect, w)
    line, gone = _file(db, w, "line.segy"), _file(db, w, "gone.segy")
    assert gone["deleted_at"] is not None
    since = _mark(w)

    now = w.clock.now() + 3 * D  # past the depth (0 days) and the grace
    result = run(_with(connect, lambda c: prune_versions(c, now, dataset_id=w.dataset_id)))
    outcomes = _drain_retention(connect, w, now)

    assert _source_writes(w, since) == []  # no DeleteObject on the source
    assert len(result.versions) == 2  # the old line.segy and gone.segy's content
    assert result.locations == [] and outcomes == []
    assert len(result.retained_at_source) == 2
    # The versions are pruned from rewind; their bytes are still the client's.
    for f in (line, gone):
        old = [loc for loc in _locations(db, f["file_id"]) if loc["vstate"] == "pruned"]
        assert [loc["state"] for loc in old] == ["committed"]
    (details,) = db.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'version.prune' AND dataset_id = %s",
        (w.dataset_id,),
    ).fetchone()  # type: ignore[misc]
    assert sorted(details["retained_at_source"]) == sorted(result.versions)


def test_the_catalog_refuses_to_queue_a_source_delete(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/a.segy", b"a"))
    _seeded(connect, w)
    (loc,) = _locations(db, _file(db, w, "a.segy")["file_id"])
    with pytest.raises(psycopg.Error) as e, db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET state = 'deleting' WHERE location_id = %s",
            (loc["location_id"],),
        )
    assert e.value.sqlstate == "OF008"


def test_a_delete_queued_for_a_source_object_is_refused(db, connect) -> None:  # type: ignore[no-untyped-def]
    """A delete queued before its bucket became a source: refused, audited,
    and the location is committed again."""
    w = world(db)
    run(put(w, "f/a.segy", b"a"))
    _seeded(connect, w)
    (loc,) = _locations(db, _file(db, w, "a.segy")["file_id"])
    lid = loc["location_id"]
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("ALTER TABLE catalog.location DISABLE TRIGGER location_source_guard")
        db.execute("UPDATE catalog.location SET state = 'deleting' WHERE location_id = %s", (lid,))
        db.execute("ALTER TABLE catalog.location ENABLE TRIGGER location_source_guard")
    since = _mark(w)
    got = run(_with(connect, lambda c: _delete(c, w, lid)))
    assert got == "retained_at_source"
    assert _source_writes(w, since) == []
    assert _locations(db, loc["file_id"])[0]["state"] == "committed"
    assert db.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'location.delete.refused'"
        " AND file_id = %s",
        (loc["file_id"],),
    ).fetchone() == (1,)


async def _delete(conn, w: World, lid: int) -> str:  # type: ignore[no-untyped-def]
    async with conn.transaction():
        return await delete_blob(conn, w.registry, lid, w.clock.now())


# --- tiering and pins ---------------------------------------------------------------


def test_the_schedule_never_rewrites_a_source_object(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/old.segy", b"one"))
    _seeded(connect, w)
    w.clock.advance(timedelta(minutes=1))
    run(put(w, "f/old.segy", b"two"))  # a superseded version, for sweep_versions
    _sync(connect, w)
    _policy(db, w, _identity(db, w))
    since = _mark(w)

    now = w.clock.now() + 400 * D  # every schedule date has passed: Archive
    res = run(_with(connect, lambda c: scheduler.sweep(c, now, dataset_ids=[w.dataset_id])))
    _drain_tiering(connect, w, now)

    assert _source_writes(w, since) == []  # no in-place CopyObject
    assert (res.moves, res.version_moves) == ([], [])
    f = _file(db, w, "old.segy")
    assert {loc["storage_class"] for loc in _locations(db, f["file_id"])} == {"STANDARD"}


def test_a_pin_never_rewrites_a_source_object(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/pinned.segy", b"p"))
    _seeded(connect, w)
    iid = _identity(db, w)
    _policy(db, w, iid)
    f = _file(db, w, "pinned.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, reason,"
            " set_by_identity_id, set_at) VALUES (%s, %s, 'cold', 'test', %s, %s)",
            (w.dataset_id, f["file_id"], iid, w.clock.now()),
        )
    since = _mark(w)

    now = w.clock.now() + D
    res = run(_with(connect, lambda c: scheduler.sweep(c, now, dataset_ids=[w.dataset_id])))
    _drain_tiering(connect, w, now)

    assert _source_writes(w, since) == []
    assert res.moves == []


def test_a_move_queued_for_a_source_object_is_refused(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Queued before its bucket became a source: every phase refuses it."""
    w = world(db)
    run(put(w, "f/a.segy", b"a"))
    _seeded(connect, w)
    _policy(db, w, _identity(db, w))
    f = _file(db, w, "a.segy")
    db.execute(
        "SELECT pgmq.send('tiering', %s)",
        (Jsonb({"kind": "move", "file_id": str(f["file_id"]),
                "version_id": str(f["content_version_id"]),
                "location_id": str(f["primary_location_id"]), "tier": "cold"}),),
    )  # fmt: skip
    since = _mark(w)
    assert _drain_tiering(connect, w, w.clock.now()) == 1
    assert _source_writes(w, since) == []
    assert db.execute(
        "SELECT details->>'why' FROM catalog.audit_log WHERE action = 'tier.move.refused'"
        " AND file_id = %s",
        (f["file_id"],),
    ).fetchone() == ("on_source",)


# --- WORM and legal holds -----------------------------------------------------------


def test_a_legal_hold_never_tags_a_source_object(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "f/evidence.segy", b"e"))
    _seeded(connect, w)
    iid = _identity(db, w)
    f = _file(db, w, "evidence.segy")
    run(_with(connect, lambda c: set_legal_hold(
        c, f["file_id"], reason="case 7", actor="user:test", identity_id=iid)))  # fmt: skip
    (loc,) = _locations(db, f["file_id"])
    assert loc["worm_sync_needed"]
    since = _mark(w)

    result = run(_with(connect, lambda c: stamp_worm(c, w.registry, w.clock.now())))

    assert _source_writes(w, since) == []
    assert result.stamped == 0
    (loc,) = _locations(db, f["file_id"])
    assert not loc["worm_sync_needed"]  # the catalog alone holds it
    assert loc["vstate"] == "committed"
    held = db.execute(
        "SELECT legal_hold FROM catalog.file_version WHERE version_id = %s", (loc["version_id"],)
    ).fetchone()
    assert held == (True,)


def test_a_source_can_not_be_an_operations_bucket(db, connect, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Review (#19): with `batch.manifest_bucket` (or the staging or report
    bucket) set to a source bucket, OmniFile would write into the client's
    bucket. Registration refuses such a source. The operations buckets are
    operator settings (the deployment's account); a backend row that names
    one is refused outright, as a partner could otherwise steer them."""
    from omnifile_service.sources.run import SourceError, create_source

    from .support import deployment

    w = world(db)

    async def register(conn):  # type: ignore[no-untyped-def]
        return await create_source(
            conn, dataset_id=w.dataset_id, backend_id=w.s3_id, container=BUCKET,
            groups=GROUPS,
        )  # fmt: skip

    batch = {"role_arn": f"arn:aws:iam::{ACCOUNT}:role/omnifile-batch"}
    for ops in ({"staging_bucket": BUCKET}, {"batch": {**batch, "manifest_bucket": BUCKET}},
                {"batch": {**batch, "manifest_bucket": "ops", "report_bucket": BUCKET}},
                {"owned_buckets": [BUCKET]}):  # fmt: skip
        deployment(monkeypatch, db, w, **ops)
        with pytest.raises(SourceError, match="operations bucket"):
            run(_with(connect, register))
        # The same settings in the row itself: never read, refused.
        db.execute(
            "UPDATE catalog.backend SET config = config || %s WHERE backend_id = %s",
            (Jsonb(ops), w.s3_id),
        )
        with pytest.raises(SourceError, match="not allowed"):
            run(_with(connect, register))


def test_no_batch_job_is_started_whose_manifest_bucket_is_a_source(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Review (#19): the runtime check behind registration's, for a config
    changed after a source was registered."""
    import dataclasses

    from omnifile_service.backends.s3.config import BatchOps
    from omnifile_service.migrate.opsbuckets import batch_writes_source

    w = world(db, batch=True)
    run(put(w, "f/a.segy", b"a"))
    _seeded(connect, w)  # BUCKET is now a registered source
    m = {"src_backend_id": w.s3_id, "migration_id": -1}
    now = w.clock.now()
    safe = run(_with(connect, lambda c: batch_writes_source(c, w.s3, m, now)))
    assert safe is False  # manifests in INV, an operations bucket
    bad = dataclasses.replace(
        w.s3.config, batch=BatchOps(w.s3.config.batch.account_id, w.s3.config.batch.role_arn,
                                    BUCKET),
    )  # fmt: skip
    backend = type("B", (), {"config": bad})()
    assert run(_with(connect, lambda c: batch_writes_source(c, backend, m, now))) is True
    assert BUCKET in bad.ops_buckets and BUCKET not in w.s3.config.ops_buckets
