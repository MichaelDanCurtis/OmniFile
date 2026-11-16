"""Change-feed records touch only the version they name, never a newer
state, and one bad record can't stop a container's feed."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import psycopg

from omnifile_service.backends.base import ChangeKind, ChangePage, ChangeRecord, ObjectRef
from omnifile_service.reconcile import open_drift, reconcile_container
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.changefeed_sync import (
    apply_record,
    lag_report,
    register_container,
    sync_all,
    sync_container,
)

from .support import add_file, clock_days_ago, commit_content, location, run, seed, with_conn

H = timedelta(hours=1)


def _apply(connect, w, rec, now):
    async def go(c):
        async with c.transaction():
            return await apply_record(c, w.sim, rec, now)

    return with_conn(connect, go)


def test_a_record_touches_only_the_version_it_names(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    f = add_file(db, w, "a")
    old = commit_content(db, w, f, b"old", clock.now(), key="k/a")
    new = commit_content(db, w, f, b"new", clock.now() + H, key="k/a")
    db.execute("UPDATE catalog.location SET storage_class = 'Archive', logical_tier = 'archive'"
               " WHERE location_id = %s", (old.location_id,))  # fmt: skip
    at = clock.now() + 2 * H
    # A record without a version id can't say which version moved.
    rec = ChangeRecord(ChangeKind.TIER_CHANGED, ObjectRef(w.backend_id, w.container, "k/a"), at,
                       native_class="Hot")  # fmt: skip
    _apply(connect, w, rec, at)
    assert location(db, old.location_id)["storage_class"] == "Archive"
    # A location without a version id isn't matched by a versioned record.
    db.execute("UPDATE catalog.location SET backend_version_id = NULL WHERE location_id = %s",
               (old.location_id,))  # fmt: skip
    _apply(connect, w, replace(rec, ref=new.ref), at)
    assert location(db, old.location_id)["storage_class"] == "Archive"
    assert location(db, new.location_id)["storage_class"] == "Hot"


def test_a_record_older_than_the_last_tier_move_is_ignored(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    s = commit_content(db, w, add_file(db, w, "a"), b"x", clock.now())
    moved = clock.now() + 2 * H
    # The tiering worker archived it (tier_changed_at), with no feed record yet.
    db.execute("UPDATE catalog.location SET storage_class = 'Archive', logical_tier = 'archive',"
               " tier_changed_at = %s WHERE location_id = %s", (moved, s.location_id))  # fmt: skip
    stale = ChangeRecord(ChangeKind.TIER_CHANGED, s.ref, moved - H, native_class="Cool")
    _apply(connect, w, stale, moved)
    assert location(db, s.location_id)["storage_class"] == "Archive"


class _PoisonFeed:
    """sim, but its feed starts with a record the catalog can't apply."""

    def __init__(self, sim, bad: ChangeRecord, good: ChangeRecord) -> None:
        self._sim, self._bad, self._good = sim, bad, good

    def __getattr__(self, name):
        return getattr(self._sim, name)

    async def read_changes(self, container, cursor):
        if cursor in (None, "0"):
            return ChangePage([self._bad, self._good], "2", caught_up=True)
        return ChangePage([], "2", caught_up=True)


def test_one_bad_record_is_retried_then_skipped_as_drift(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    s = commit_content(db, w, add_file(db, w, "a"), b"x", clock.now())
    lid = s.location_id
    db.execute("INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed,"
               " provider_ref, state, triggered_by) VALUES (%s, %s, 'azure_copy_rehydrate',"
               " 'standard', 'poison', 'in_progress', 'user_recall')",
               (lid, w.backend_id))  # fmt: skip
    at = clock.now()
    bad = ChangeRecord(ChangeKind.RESTORE_COMPLETED, s.ref, at,
                       detail={"provider_ref": "poison", "expires_at": "not a time"})  # fmt: skip
    good = ChangeRecord(ChangeKind.TIER_CHANGED, s.ref, at, native_class="Cold")
    feed = _PoisonFeed(w.sim, bad, good)
    registry = BackendRegistry.from_mapping({w.backend_id: feed})
    db.execute("INSERT INTO catalog.changefeed_cursor (backend_id, container, cursor)"
               " VALUES (%s, %s, '0')", (w.backend_id, w.container))  # fmt: skip

    for attempt in range(1, 4):
        with_conn(connect, lambda c: sync_all(c, registry, clock.now()))  # never raises
        row = next(r for r in with_conn(connect, lambda c: lag_report(c, clock.now()))
                   if r["backend_id"] == w.backend_id)  # fmt: skip
        if attempt < 3:
            assert row["consecutive_failures"] == attempt and row["last_error"]
            assert location(db, lid)["storage_class"] == "Smart"  # the page didn't apply
    # Third failure: the bad record is skipped as drift, the good one applies.
    assert location(db, lid)["storage_class"] == "Cold"
    assert row["consecutive_failures"] == 0 and row["skipped_records"] == 1
    kinds = {d["kind"] for d in with_conn(connect, lambda c: open_drift(c, w.backend_id))}
    assert kinds == {"feed_record_failed"}


def test_lag_report_shows_a_stalled_feed(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    with_conn(connect, lambda c: register_container(c, w.backend_id, w.container))
    with_conn(connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container,
                                                clock.now()))  # fmt: skip
    commit_content(db, w, add_file(db, w, "a"), b"x", clock.now())
    with_conn(connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container,
                                                clock.now()))  # fmt: skip
    clock.advance(3 * H)  # nothing new arrives
    with_conn(connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container,
                                                clock.now()))  # fmt: skip
    row = next(r for r in with_conn(connect, lambda c: lag_report(c, clock.now()))
               if r["backend_id"] == w.backend_id)  # fmt: skip
    assert row["last_record_age_seconds"] >= 3 * 3600


def test_reconcile_resolves_change_feed_drift_once_fixed(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    with_conn(connect, lambda c: register_container(c, w.backend_id, w.container))
    with_conn(connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container,
                                                clock.now()))  # fmt: skip
    s = commit_content(db, w, add_file(db, w, "a"), b"x", clock.now())
    run(w.sim.delete(s.ref))
    with_conn(connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container,
                                                clock.now()))  # fmt: skip
    assert {d["kind"] for d in with_conn(connect, lambda c: open_drift(c, w.backend_id))} == {
        "deleted_externally"}  # fmt: skip
    # An operator retires the lost copy; the next reconcile closes the row.
    db.execute("UPDATE catalog.location SET state = 'deleted', verified = false"
               " WHERE location_id = %s", (s.location_id,))  # fmt: skip
    with_conn(connect, lambda c: reconcile_container(c, w.registry, w.backend_id, w.container,
                                                     clock.now()))  # fmt: skip
    assert with_conn(connect, lambda c: open_drift(c, w.backend_id)) == []
