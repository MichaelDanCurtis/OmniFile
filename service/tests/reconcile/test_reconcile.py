"""Nightly reconcile: an (emulated) Blob Inventory on non-WORM accounts, the
change feed plus listings on WORM ones; drift injected behind the catalog's
back is found, reported, and resolved once fixed."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb
from retention.support import (
    _one,
    add_file,
    clock_days_ago,
    commit_content,
    run,
    run_sweep,
    seed,
    with_conn,
    worker,
)
from watched import FakeWorker, Watched, drain_job

from omnifile_service.backends.base import LogicalTier
from omnifile_service.backends.sim import InjectedError, Op
from omnifile_service.reconcile import known_containers, open_drift, reconcile_container
from omnifile_service.reconcile import run as reconcile_run
from omnifile_service.retention import enable_worm
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.retention_jobs import retention_jobs

H = timedelta(hours=1)


def _reconcile(connect, w, clock, container=None):
    return with_conn(
        connect,
        lambda c: reconcile_container(c, w.registry, w.backend_id, container or w.container,
                                      clock.now()),
    )  # fmt: skip


def _kinds(connect, w, container=None) -> dict[str, str]:
    rows = with_conn(connect, lambda c: open_drift(c, w.backend_id, container or w.container))
    return {f"{r['object_key']}:{r['kind']}": r["source"] for r in rows}


def test_inventory_reconcile_finds_injected_drift(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(2)
    w = seed(db, clock)
    ok = commit_content(db, w, add_file(db, w, "ok"), b"fine", clock.now())
    gone = commit_content(db, w, add_file(db, w, "gone"), b"lost", clock.now())
    tier = commit_content(db, w, add_file(db, w, "tier"), b"moved", clock.now())
    size = commit_content(db, w, add_file(db, w, "size"), b"12345", clock.now(), catalog_size=9)
    clock.advance(H)
    # Drift behind the catalog's back.
    run(w.sim.delete(gone.ref))
    run(w.sim.set_class(tier.ref, LogicalTier.COLD))
    stray = run(w.sim.put(w.container, "stray/object", _one(b"?"), None, {}))

    report = _reconcile(connect, w, clock)
    # No Blob Inventory is configured for sim: the run listed, and says so.
    assert (report.method, report.method_detail) == ("listing", "no inventory configured")
    assert db.execute(
        "SELECT method, method_detail FROM catalog.reconcile_run WHERE run_id = %s",
        (report.run_id,),
    ).fetchone() == ("listing", "no inventory configured")
    assert report.objects_seen == 4  # ok, tier, size, stray
    assert _kinds(connect, w) == {
        gone.ref.key + ":missing": "reconcile",
        tier.ref.key + ":tier_mismatch": "reconcile",
        size.ref.key + ":size_mismatch": "reconcile",
        "stray/object:unexpected": "reconcile",
    }
    assert ok.ref.key not in {k.split(":")[0] for k in _kinds(connect, w)}
    (state, found) = db.execute(
        "SELECT state, drift_found FROM catalog.reconcile_run WHERE run_id = %s",
        (report.run_id,),
    ).fetchone()
    assert (state, found) == ("completed", 4)

    # Running again refreshes the same rows instead of duplicating them.
    clock.advance(H)
    again = _reconcile(connect, w, clock)
    assert again.drift_found == 0 and len(again.drift_open) == 4
    # Fix two; the next run resolves them.
    db.execute("UPDATE catalog.location SET storage_class = 'Cold', logical_tier = 'cold'"
               " WHERE location_id = %s", (tier.location_id,))  # fmt: skip
    # By version id: a plain delete on a versioned container keeps the bytes.
    run(w.sim.delete(stray))
    clock.advance(H)
    third = _reconcile(connect, w, clock)
    assert third.drift_resolved == 2
    assert set(_kinds(connect, w)) == {gone.ref.key + ":missing", size.ref.key + ":size_mismatch"}


def test_a_failed_listing_marks_the_run_failed(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    commit_content(db, w, add_file(db, w, "a"), b"a", clock.now())
    w.sim.faults.fail_nth(Op.LIST, 1)
    with pytest.raises(InjectedError):
        _reconcile(connect, w, clock)
    (state, failure) = db.execute(
        "SELECT state, failure FROM catalog.reconcile_run WHERE backend_id = %s"
        " ORDER BY run_id DESC LIMIT 1",
        (w.backend_id,),
    ).fetchone()
    assert state == "failed" and "InjectedError" in failure


def test_worm_reconcile_uses_the_change_feed_and_listings(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(2)
    w = seed(db, clock, layout="azure_worm")
    with_conn(connect, lambda c: enable_worm(c, w.share_id, retention_days=30,
                                             confirmation=w.share_name, actor="user:1",
                                             now=clock.now(), test_window=H))  # fmt: skip
    wk = worker(connect, w, clock)
    clock.advance(2 * H)
    run_sweep(wk, "worm_autolock")
    a = commit_content(db, w, add_file(db, w, "a"), b"aaa", clock.now())
    b = commit_content(db, w, add_file(db, w, "b"), b"bbb", clock.now())
    run_sweep(wk, "worm_stamp")
    clock.advance(H)
    report = _reconcile(connect, w, clock)
    assert report.method == "listing_changefeed"
    assert report.drift_open == []
    (cursor,) = db.execute(
        "SELECT cursor FROM catalog.changefeed_cursor WHERE backend_id = %s AND container = %s",
        (w.backend_id, w.container),
    ).fetchone()
    assert cursor is not None  # the change feed was followed first

    # A legal hold placed at the provider, outside the catalog, is drift. A
    # tier change is not: the change feed, caught up first, carries it into
    # the catalog.
    run(w.sim.set_legal_hold(a.ref, True))
    run(w.sim.set_class(b.ref, LogicalTier.WARM))
    clock.advance(H)
    _reconcile(connect, w, clock)
    assert _kinds(connect, w) == {a.ref.key + ":hold_mismatch": "reconcile"}
    (cls,) = db.execute(
        "SELECT storage_class FROM catalog.location WHERE location_id = %s", (b.location_id,)
    ).fetchone()
    assert cls == "Cool"


def test_known_containers_follow_the_layout() -> None:
    assert known_containers({"layout": "azure_two_account"}) == [
        "live/data", "archive/data", "live/recall"]  # fmt: skip
    assert known_containers({"layout": "azure_worm", "container": "worm/data"}) == [
        "worm/data", "worm/recall"]  # fmt: skip
    assert known_containers({"container": "acct/c", "archive_account": "arch",
                             "recall_container": "acct/recall"}) == [
        "acct/c", "arch/c", "acct/recall"]  # fmt: skip


def test_a_whole_listing_holds_nothing_while_it_reads(
    db: psycopg.Connection, connect, retention_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run: it held a transaction id for the whole read, from its temporary table: hours
    for a big bucket, with every edge's feed waiting. Now each batch is written on its own."""
    clock = clock_days_ago(1)
    w = seed(db, clock)
    for name in ("a", "b", "c"):
        commit_content(db, w, add_file(db, w, name), name.encode() * 10, clock.now())
    provider = Watched(w.sim, retention_db_url)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]
    monkeypatch.setattr(reconcile_run, "LOAD_BATCH", 2)  # more than one batch
    report = with_conn(
        connect,
        lambda c: reconcile_container(c, registry, w.backend_id, w.container, clock.now()),
    )
    assert report.objects_seen == 3 and report.drift_found == 0
    assert provider.seen, "it read the backend"
    assert provider.with_ids() == [] and provider.with_locks() == []


def test_a_reconcile_from_the_queue_holds_nothing_either(
    db: psycopg.Connection, connect, retention_db_url: str
) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock)
    commit_content(db, w, add_file(db, w, "a"), b"a" * 10, clock.now())
    db.execute("DELETE FROM pgmq.q_reconcile")
    db.execute(
        "SELECT pgmq.send('reconcile'::text, %s::jsonb)",
        (Jsonb({"backend_id": str(w.backend_id), "container": w.container}),),
    )
    provider = Watched(w.sim, retention_db_url)
    registry = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]
    jobs = retention_jobs(FakeWorker(registry, clock))
    stats = drain_job(retention_db_url, jobs, "reconcile")
    assert stats.acked == 1 and stats.dead == 0, stats.errors
    assert provider.seen and provider.with_ids() == [] and provider.with_locks() == []
    (n,) = db.execute(
        "SELECT count(*) FROM catalog.reconcile_run WHERE backend_id = %s AND state = 'completed'",
        (w.backend_id,),
    ).fetchone()
    assert n == 1


def test_a_failed_read_leaves_no_temporary_table_on_the_connection(
    db: psycopg.Connection, connect
) -> None:
    """Pooled connections go on being used, and the next run makes its own."""
    clock = clock_days_ago(1)
    w = seed(db, clock)
    commit_content(db, w, add_file(db, w, "a"), b"a" * 10, clock.now())

    class Fails:
        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(w.sim, name)

        async def list(self, *_a, **_k):  # type: ignore[no-untyped-def]
            raise RuntimeError("the listing broke")

    registry = BackendRegistry.from_mapping({w.backend_id: Fails()})  # type: ignore[dict-item]

    async def go(c):  # type: ignore[no-untyped-def]
        with pytest.raises(RuntimeError, match="the listing broke"):
            await reconcile_container(c, registry, w.backend_id, w.container, clock.now())
        cur = await c.execute("SELECT to_regclass('pg_temp.recon_obj')")
        assert (await cur.fetchone())[0] is None
        return await reconcile_container(c, w.registry, w.backend_id, w.container, clock.now())

    assert with_conn(connect, go).objects_seen >= 1
