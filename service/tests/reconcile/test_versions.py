"""Reconcile on versioned containers: each catalog location is matched with
the exact backend version it names, so overwrites and tombstones don't look
like drift, and a version that is really gone is found."""

from __future__ import annotations

from datetime import timedelta

import psycopg
from retention.support import (
    add_file,
    clock_days_ago,
    commit_content,
    run,
    run_sweep,
    seed,
    with_conn,
    worker,
)

from omnifile_service.backends.base import ObjectRef, VersionListing
from omnifile_service.reconcile import open_drift, reconcile_container
from omnifile_service.retention import enable_worm
from omnifile_service.worker.backends import BackendRegistry

H = timedelta(hours=1)


def _reconcile(connect, w, clock):
    return with_conn(
        connect,
        lambda c: reconcile_container(c, w.registry, w.backend_id, w.container, clock.now()),
    )


def _drift(connect, w) -> set[tuple[str, str | None, str]]:
    rows = with_conn(connect, lambda c: open_drift(c, w.backend_id, w.container))
    return {(r["object_key"], r["backend_version_id"], r["kind"]) for r in rows}


def test_overwritten_versions_match_and_a_lost_version_is_found(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(2)
    w = seed(db, clock)  # live/data: versioned, no WORM
    f = add_file(db, w, "line.segy")
    v1 = commit_content(db, w, f, b"short", clock.now(), key="k/line")
    v2 = commit_content(db, w, f, b"a much longer body", clock.now() + H, key="k/line")
    assert v1.ref.version_id != v2.ref.version_id
    clock.advance(2 * H)
    assert _reconcile(connect, w, clock).drift_open == []  # no false size mismatch

    run(w.sim.delete(v1.ref))  # the old version, by id: really gone
    clock.advance(H)
    _reconcile(connect, w, clock)
    assert _drift(connect, w) == {("k/line", v1.ref.version_id, "missing")}


def test_versions_of_a_tombstoned_worm_file_are_not_missing(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(2)
    w = seed(db, clock, layout="azure_worm")
    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=30,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip
    wk = worker(connect, w, clock)
    clock.advance(2 * H)
    run_sweep(wk, "worm_autolock")
    f = add_file(db, w, "evidence")
    v1 = commit_content(db, w, f, b"one", clock.now(), key="k/ev")
    v2 = commit_content(db, w, f, b"two!", clock.now() + H, key="k/ev")
    run_sweep(wk, "worm_stamp")
    # The blob is deleted (versioned): no current version, both retained.
    run(w.sim.delete(ObjectRef(w.backend_id, w.container, "k/ev")))
    clock.advance(2 * H)
    report = _reconcile(connect, w, clock)
    assert report.drift_open == []
    assert {v1.location_id, v2.location_id}  # both still committed in the catalog


class _NoVersionListing:
    """sim without `list_all_versions`: the contract-only fallback."""

    def __init__(self, sim) -> None:
        self._sim = sim

    def __getattr__(self, name):
        if name == "list_all_versions":
            raise AttributeError(name)
        return getattr(self._sim, name)


def test_the_fallback_listing_sees_old_and_tombstoned_versions(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(2)
    w = seed(db, clock)
    plain = _NoVersionListing(w.sim)
    assert not isinstance(plain, VersionListing)
    w.registry = BackendRegistry.from_mapping({w.backend_id: plain})
    f = add_file(db, w, "a")
    v1 = commit_content(db, w, f, b"short", clock.now(), key="k/a")
    commit_content(db, w, f, b"a longer body", clock.now() + H, key="k/a")
    g = add_file(db, w, "b")
    g1 = commit_content(db, w, g, b"bee", clock.now(), key="k/b")
    run(w.sim.delete(ObjectRef(w.backend_id, w.container, "k/b")))  # tombstone, kept
    clock.advance(2 * H)
    assert _reconcile(connect, w, clock).drift_open == []
    run(w.sim.delete(v1.ref))
    run(w.sim.delete(g1.ref))
    clock.advance(H)
    _reconcile(connect, w, clock)
    assert _drift(connect, w) == {("k/a", v1.ref.version_id, "missing"),
                                  ("k/b", g1.ref.version_id, "missing")}  # fmt: skip


def test_version_pages_cross_the_sim_rpc() -> None:
    from omnifile_service.backends.base import (
        ListedVersion,
        LogicalTier,
        ObjectHead,
        RestoreState,
        VersionPage,
    )
    from omnifile_service.backends.sim_http import rpc

    head = ObjectHead(ObjectRef(1, "live/data", "k", "v1"), 3, "Hot", LogicalTier.HOT,
                      RestoreState.NOT_NEEDED)  # fmt: skip
    page = VersionPage((ListedVersion(head, False),), None)
    assert rpc.decode(rpc.encode(page)) == page
