"""Reconcile of a bucket with sources: only the source prefixes are
OmniFile's (catalog-seeding.md section 9; reconcile/run.py)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from omnifile_service.backends.base import ObjectRef
from omnifile_service.reconcile import SourcesSeeding, reconcile_container, reconcile_due
from omnifile_service.sources import register_source, seed_step

from .support import BUCKET, GROUPS, put, world


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _with(connect, fn):  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


def _register(connect, w, prefix: str) -> int:  # type: ignore[no-untyped-def]
    return run(
        _with(
            connect,
            lambda c: register_source(
                c, w.s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, prefix=prefix
            ),
        )
    )


def _seed(connect, w, sid: int) -> None:  # type: ignore[no-untyped-def]
    async def go(conn) -> None:  # type: ignore[no-untyped-def]
        while True:
            async with conn.transaction():
                if await seed_step(conn, w.registry.get, sid, w.clock.now()) == "seeded":
                    return

    run(_with(connect, go))


def _reconcile(connect, w):  # type: ignore[no-untyped-def]
    w.clock.advance(timedelta(minutes=5))
    return run(
        _with(connect, lambda c: reconcile_container(c, w.registry, w.s3_id, BUCKET, w.clock.now()))
    )


def test_only_the_source_prefixes_are_reconciled(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db, versioning=False)
    for key in ("r/a", "r/b", "s/c", "other/x", "other/y"):
        run(put(w, key))
    for prefix in ("r/", "s/"):
        _seed(connect, w, _register(connect, w, prefix))
    report = _reconcile(connect, w)
    assert report.objects_seen == 3 and report.drift_open == []  # other/ is not ours
    assert report.method == "listing" and report.method_detail is not None
    assert report.method_detail.startswith("source prefixes: ")
    # Inside the prefixes, drift is still found: a delete nobody applied,
    # and an object no location knows.
    run(w.client.delete(ObjectRef(w.s3_id, BUCKET, "r/a")))
    run(put(w, "s/new"))
    run(put(w, "other/z"))
    report = _reconcile(connect, w)
    assert {(d["key"], d["kind"]) for d in report.drift_open} == {
        ("r/a", "missing"),
        ("s/new", "unexpected"),
    }


def test_a_bucket_still_seeding_is_not_reconciled(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    run(put(w, "q/a"))
    sid = _register(connect, w, "q/")
    with pytest.raises(SourcesSeeding, match="q/ is registered"):
        _reconcile(connect, w)
    due = run(_with(connect, lambda c: reconcile_due(c, w.registry, w.clock.now())))
    assert all(r.run_id is not None for r in due)
    (runs,) = db.execute(
        "SELECT count(*) FROM catalog.reconcile_run WHERE backend_id = %s", (w.s3_id,)
    ).fetchone()
    assert runs == 0  # the sweep skipped it
    _seed(connect, w, sid)
    assert _reconcile(connect, w).drift_open == []


def test_versions_from_before_adoption_are_not_unexpected(db, connect) -> None:  # type: ignore[no-untyped-def]
    w = world(db)  # versioned
    old = run(put(w, "v/a", b"old"))
    run(put(w, "v/a", b"current"))
    _seed(connect, w, _register(connect, w, "v/"))
    report = _reconcile(connect, w)
    assert report.drift_open == []
    # A current object nobody adopted is still unexpected; the old version
    # stays listed, so it could still match a location naming it.
    run(put(w, "v/new"))
    report = _reconcile(connect, w)
    assert {(d["key"], d["kind"]) for d in report.drift_open} == {("v/new", "unexpected")}
    assert report.objects_seen == 3 and old.version_id is not None
