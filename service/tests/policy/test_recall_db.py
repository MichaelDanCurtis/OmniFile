"""Recalls through the budgeted restores handler: the per-account 10 GiB/h
High budget queues what doesn't fit, downgrades High where it can't be
faster, and no recall above 10 GB carries an ETA."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker.queue import Message, Retry
from omnifile_service.worker.recalls import RecallConfig
from omnifile_service.worker.tiering import handle_restore_budgeted

from .support import GB, GiB, Stored, World, make_world, row, run, set_tier, store

NOW_POLL = RecallConfig(poll_min=timedelta(0), poll_max=timedelta(0), poll_no_eta=timedelta(0))


@pytest.fixture
def w(db: psycopg.Connection, policy_db_url: str) -> World:
    return make_world(db, policy_db_url, policy={"archive_day": 365})


def archived(db: psycopg.Connection, w: World, name: str, size: int) -> Stored:
    s = store(db, w, name, size=size)
    ref = set_tier(db, w, s, T.ARCHIVE)
    assert ref.container == "archive/data"
    return s


def recall(db: psycopg.Connection, w: World, s: Stored, priority: str) -> int:
    (rid,) = db.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority, requested_at)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING recall_id",
        (s.file_id, s.version_id, w.edge_id, priority, w.clock.now()),
    ).fetchone()
    return rid


async def _handle(w: World, rid: int) -> float | None:
    """Run the handler once in one transaction; the Retry delay, if any."""
    conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
    try:
        msg = Message(1, 1, w.clock.now(), w.clock.now(), {"recall_id": str(rid)})
        async with conn.transaction():
            try:
                await handle_restore_budgeted(
                    msg, conn, registry=w.registry, now=w.clock.now(), config=NOW_POLL
                )
            except Retry as r:
                return r.delay_seconds
        return None
    finally:
        await conn.close()


def handle(w: World, rid: int) -> float | None:
    return run(_handle(w, rid))


def recall_row(db: psycopg.Connection, rid: int) -> dict:
    r = row(
        db,
        "SELECT r.*, s.speed FROM catalog.recall r LEFT JOIN catalog.restore s"
        " ON s.restore_id = r.restore_id WHERE r.recall_id = %s",
        rid,
    )
    assert r is not None
    return r


def test_priority_budget_queues_beyond_10_gib_per_hour(db: psycopg.Connection, w: World) -> None:
    a = archived(db, w, "a", 6 * GiB)
    b = archived(db, w, "b", 6 * GiB)
    c = archived(db, w, "c", 1 * GiB)
    t0 = w.clock.now()
    ra, rb, rc = (recall(db, w, s, "high") for s in (a, b, c))

    handle(w, ra)
    got = recall_row(db, ra)
    assert (got["state"], got["speed"]) == ("rehydrating", "high")
    assert got["eta"] == t0 + timedelta(hours=1)

    # 6 GiB in the pipeline + 6 GiB > 10 GiB: B waits in OmniFile's queue.
    delay = handle(w, rb)
    got = recall_row(db, rb)
    assert got["state"] == "requested" and got["restore_id"] is None
    assert delay == pytest.approx(0.2 * 3600)
    assert got["eta"] == t0 + timedelta(hours=1.6)
    # C is small but behind B (first come, first served).
    assert handle(w, rc) is not None and recall_row(db, rc)["restore_id"] is None

    w.clock.set(t0 + timedelta(hours=0.2))
    assert handle(w, rb) is not None  # admitted and started: now polling
    got = recall_row(db, rb)
    assert (got["state"], got["speed"]) == ("rehydrating", "high")
    assert got["eta"] == t0 + timedelta(hours=1.6)  # sim's own pipeline agrees
    assert w.sim.priority_queue_clear_at("archive/data") == t0 + timedelta(hours=1.2)

    w.clock.set(t0 + timedelta(hours=1.2))  # pipeline drained
    handle(w, rc)
    assert recall_row(db, rc)["speed"] == "high"


def test_high_downgraded_when_not_faster(db: psycopg.Connection, w: World) -> None:
    big = archived(db, w, "big", 150 * GiB)
    small = archived(db, w, "small", 1 * GiB)
    t0 = w.clock.now()
    rbig = recall(db, w, big, "high")
    handle(w, rbig)
    got = recall_row(db, rbig)
    assert got["speed"] == "high" and got["eta"] is None  # over 10 GB: no promise
    rsmall = recall(db, w, small, "high")
    handle(w, rsmall)
    got = recall_row(db, rsmall)
    # 15 h of High backlog ahead: Standard is as fast, so High isn't offered.
    assert (got["priority"], got["speed"]) == ("standard", "standard")
    assert got["eta"] == t0 + timedelta(hours=15)


def test_no_eta_above_10_gb(db: psycopg.Connection, w: World) -> None:
    s = archived(db, w, "huge", 20 * GB)
    rid = recall(db, w, s, "standard")
    handle(w, rid)
    got = recall_row(db, rid)
    assert got["state"] == "rehydrating" and got["eta"] is None
    ok = archived(db, w, "ten", 10 * GB)
    rid = recall(db, w, ok, "standard")
    handle(w, rid)
    assert recall_row(db, rid)["eta"] == w.clock.now() + timedelta(hours=15)
