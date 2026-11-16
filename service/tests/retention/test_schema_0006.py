"""Migration 0006 and the worker's job registry."""

from __future__ import annotations

from datetime import timedelta

import psycopg

from omnifile_service.worker.jobs import QueueJob, SweepJob
from omnifile_service.worker.runner import Worker

from .support import add_file, clock_days_ago, seed

QUEUES = ("restores", "storage_mover", "verify", "notify", "edge_instructions", "retention",
          "reconcile", "tiering")  # fmt: skip


def test_every_queue_and_dead_letter_queue_is_created_by_migrations(
    db: psycopg.Connection,
) -> None:
    names = {r[0] for r in db.execute("SELECT queue_name FROM pgmq.list_queues()")}
    for q in QUEUES:
        assert q in names and f"{q}_dlq" in names


def test_scheduler_clocks_keep_the_entity_version(db: psycopg.Connection) -> None:
    w = seed(db, clock_days_ago(1))
    f = add_file(db, w, "a")
    (v0,) = db.execute(
        "SELECT entity_version FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone()
    db.execute(
        "UPDATE catalog.file SET next_action = 'archive', next_action_at = now(),"
        " archive_at = current_date WHERE file_id = %s",
        (f,),
    )
    db.execute("UPDATE catalog.file SET last_read_at = now() WHERE file_id = %s", (f,))
    (v1,) = db.execute(
        "SELECT entity_version FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone()
    assert v1 == v0
    db.execute("UPDATE catalog.file SET mode = 420 WHERE file_id = %s", (f,))
    (v2,) = db.execute(
        "SELECT entity_version FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone()
    assert v2 == v0 + 1


def test_track_jobs_register_and_replace_builtin_consumers(connect) -> None:
    async def handler(msg, conn) -> None:  # pragma: no cover - never run here
        return None

    wk = Worker(connect, registry=None)  # type: ignore[arg-type]
    assert {"retention", "reconcile", "restores", "edge_instructions"} <= set(wk.consumers)
    assert {"worm_autolock", "worm_stamp", "prune_versions", "changefeed_sync",
            "reconcile_nightly"} <= set(wk.sweeps)  # fmt: skip
    builtin = wk.restores
    wk2 = Worker(
        connect,
        registry=None,  # type: ignore[arg-type]
        extra_jobs=[QueueJob("restores", handler), SweepJob("x", timedelta(1), None)],  # type: ignore[arg-type]
    )
    assert wk2.restores is wk2.consumers["restores"] and wk2.restores is not builtin
    assert wk2.restores._handler is handler
    assert "x" in wk2.sweeps
    bare = Worker(connect, registry=None, track_jobs=False)  # type: ignore[arg-type]
    # M1's built-ins: restores, stub instructions and the upload read-back.
    assert set(bare.consumers) == {"restores", "edge_instructions", "verify"} and not bare.sweeps
