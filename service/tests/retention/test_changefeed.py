"""Change-feed sync: provider records into the catalog, idempotently, with
the cursor persisted and the lag measured (sim lag is configurable)."""

from __future__ import annotations

from datetime import timedelta

import psycopg

from omnifile_service.backends.base import LogicalTier
from omnifile_service.backends.sim import Op
from omnifile_service.reconcile import open_drift
from omnifile_service.worker.changefeed_sync import lag_report, register_container, sync_container

from .support import add_file, clock_days_ago, commit_content, location, run, seed, with_conn

LAG = timedelta(minutes=5)
MIN = timedelta(minutes=1)


def _cursor(db: psycopg.Connection, w) -> dict:
    cur = db.execute(
        "SELECT * FROM catalog.changefeed_cursor WHERE backend_id = %s AND container = %s",
        (w.backend_id, w.container),
    )
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def _sync(connect, w, clock, **kw):
    return with_conn(
        connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container, clock.now(),
                                          **kw)
    )  # fmt: skip


def test_sync_converges_idempotently_and_reports_lag(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, feed_lag=LAG)
    with_conn(connect, lambda c: register_container(c, w.backend_id, w.container))
    _sync(connect, w, clock)  # first read starts "from now"
    assert _cursor(db, w)["cursor"] is not None

    files = [commit_content(db, w, add_file(db, w, f"f{i}"), b"x" * 10, clock.now())
             for i in range(3)]  # fmt: skip
    # Tiers change at the provider (a lifecycle rule, an admin, a policy move).
    run(w.sim.set_class(files[0].ref, LogicalTier.WARM))
    run(w.sim.set_class(files[1].ref, LogicalTier.COLD))

    # Within the lag nothing is visible yet.
    stats = _sync(connect, w, clock)
    assert stats.records == 0 and stats.caught_up[(w.backend_id, w.container)]
    assert location(db, files[0].location_id)["storage_class"] == "Smart"

    clock.advance(LAG + MIN)
    before = _cursor(db, w)["cursor"]
    stats = _sync(connect, w, clock)
    assert stats.applied >= 2
    l0, l1 = location(db, files[0].location_id), location(db, files[1].location_id)
    assert (l0["storage_class"], l0["logical_tier"]) == ("Cool", "warm")
    assert (l1["storage_class"], l1["logical_tier"]) == ("Cold", "cold")
    assert location(db, files[2].location_id)["storage_class"] == "Smart"
    # The file summary followed (trigger), so the edge learns it from the feed.
    (tier,) = db.execute(
        "SELECT remote_tier FROM catalog.file WHERE file_id = %s", (files[0].file_id,)
    ).fetchone()
    assert tier == "warm"

    # Lag: at least the provider's lag, recorded on the cursor and sampled.
    c = _cursor(db, w)
    assert c["caught_up"] and c["last_lag_seconds"] >= LAG.total_seconds()
    assert stats.max_lag is not None and stats.max_lag >= LAG
    (samples,) = db.execute(
        "SELECT count(*) FROM catalog.changefeed_lag WHERE backend_id = %s", (w.backend_id,)
    ).fetchone()
    assert samples >= 1
    report = [r for r in with_conn(connect, lag_report) if r["backend_id"] == w.backend_id]
    assert report[0]["records_applied"] >= 2

    # Replaying the same page (a crash before commit) changes nothing.
    versions = db.execute(
        "SELECT entity_version FROM catalog.file WHERE file_id = ANY(%s) ORDER BY file_id",
        ([f.file_id for f in files],),
    ).fetchall()
    db.execute(
        "UPDATE catalog.changefeed_cursor SET cursor = %s WHERE backend_id = %s",
        (before, w.backend_id),
    )
    _sync(connect, w, clock)
    assert (
        db.execute(
            "SELECT entity_version FROM catalog.file WHERE file_id = ANY(%s) ORDER BY file_id",
            ([f.file_id for f in files],),
        ).fetchall()
        == versions
    )
    # An older record never overwrites a newer one: move it back to Smart's
    # neighbour and replay only the old page again.
    run(w.sim.set_class(files[0].ref, LogicalTier.HOT))
    clock.advance(LAG + MIN)
    _sync(connect, w, clock)
    assert location(db, files[0].location_id)["storage_class"] == "Hot"
    db.execute(
        "UPDATE catalog.changefeed_cursor SET cursor = %s WHERE backend_id = %s",
        (before, w.backend_id),
    )
    _sync(connect, w, clock)
    assert location(db, files[0].location_id)["storage_class"] == "Hot"


def test_feed_outage_is_recorded_and_recovers(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    with_conn(connect, lambda c: register_container(c, w.backend_id, w.container))
    _sync(connect, w, clock)
    f = commit_content(db, w, add_file(db, w, "a"), b"abc", clock.now())
    run(w.sim.set_class(f.ref, LogicalTier.COLD))
    w.sim.faults.fail_nth(Op.READ_CHANGES, 1)
    stats = _sync(connect, w, clock)
    assert not stats.caught_up[(w.backend_id, w.container)]
    assert "InjectedError" in _cursor(db, w)["last_error"]
    _sync(connect, w, clock)
    assert _cursor(db, w)["last_error"] is None
    assert location(db, f.location_id)["storage_class"] == "Cold"


def test_an_external_delete_is_reported_as_drift(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    with_conn(connect, lambda c: register_container(c, w.backend_id, w.container))
    _sync(connect, w, clock)
    f = commit_content(db, w, add_file(db, w, "victim"), b"abc", clock.now())
    run(w.sim.delete(f.ref))  # by version id: the bytes are gone
    _sync(connect, w, clock)
    drift = with_conn(connect, lambda c: open_drift(c, w.backend_id, w.container))
    assert [(d["kind"], d["location_id"], d["source"]) for d in drift] == [
        ("deleted_externally", f.location_id, "changefeed")]  # fmt: skip
    assert location(db, f.location_id)["state"] == "committed"  # reported, not repaired
    _sync(connect, w, clock)  # idempotent: still one open row
    assert len(with_conn(connect, lambda c: open_drift(c, w.backend_id, w.container))) == 1
