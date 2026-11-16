"""Staged plans (migration 0037; migrate/plans.py; portal-api.md 5.22): the
scheduler starts waves on time, inside their windows and outside
blackouts, after their dependencies and once their sources are catalogued,
never cuts over an unverified file, and survives a crash mid-start. Also
the console's Copy again and Restore again in the engine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.rows import dict_row
from sources.support import BUCKET, GROUPS, put

from omnifile_service.backends.base import LogicalTier
from omnifile_service.migrate import plans
from omnifile_service.migrate.plans import in_blackout, in_window, next_open, plan_sweep
from omnifile_service.sources import register_source

from .test_engine import Rig, _with, run

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)  # a Tuesday, 12:00 UTC


def _owner(db) -> int:  # type: ignore[no-untyped-def]
    (oid,) = db.execute(
        "INSERT INTO catalog.identity (sid, kind, display_name)"
        " VALUES ('S-1-5-21-9-9-9-' || (floor(random() * 1e6))::int, 'user', 'Olga Owner')"
        " RETURNING identity_id"
    ).fetchone()
    return oid


def _plan(db, r: Rig, waves: list[dict[str, Any]], *, state: str = "approved") -> int:  # type: ignore[no-untyped-def]
    (pid,) = db.execute(
        "INSERT INTO catalog.migration_plan (dataset_id, name, state, owner_identity_id,"
        " proposed_by) VALUES (%s, 'test plan', %s, %s, 'user:1') RETURNING plan_id",
        (r.w.dataset_id, state, _owner(db)),
    ).fetchone()
    for i, w in enumerate(waves, 1):
        db.execute(
            "INSERT INTO catalog.migration_wave (plan_id, seq, name, prefixes, start_at,"
            " window_start_min, window_end_min, blackouts, after_seq, cutover)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
            (pid, i, f"Wave {i}", w["prefixes"], w.get("start_at", T0),
             w.get("window", (None, None))[0], w.get("window", (None, None))[1],
             json.dumps(w.get("blackouts", [])), w.get("after"), w.get("cutover", "auto")),
        )  # fmt: skip
    return pid


def _sweep(r: Rig, now: datetime) -> int:
    async def go(conn):  # type: ignore[no-untyped-def]
        await conn.set_autocommit(True)
        return await plan_sweep(conn, now)

    return run(_with(r.connect, go))


def _wave(db, pid: int, seq: int = 1) -> dict:  # type: ignore[no-untyped-def]
    cur = db.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM catalog.migration_wave WHERE plan_id = %s AND seq = %s", (pid, seq))
    return cur.fetchone()  # type: ignore[return-value]


def _mids(db, pid: int, seq: int = 1) -> list[int]:  # type: ignore[no-untyped-def]
    return [x[0] for x in db.execute(
        "SELECT x.migration_id FROM catalog.migration_wave_prefix x"
        " JOIN catalog.migration_wave w USING (wave_id) WHERE w.plan_id = %s AND w.seq = %s"
        " ORDER BY x.prefix", (pid, seq)).fetchall()]  # fmt: skip


def _two_prefixes(r: Rig) -> None:
    run(put(r.w, "m/a/1.segy", b"seismic" * 50))
    run(put(r.w, "m/b/2.segy", b"more data"))
    r.seed()


def test_windows_and_blackouts() -> None:
    at = T0.replace(hour=23, minute=30)
    assert in_window(22 * 60, 6 * 60, at) and not in_window(22 * 60, 6 * 60, T0)
    assert in_window(None, None, T0) and in_window(9 * 60, 17 * 60, T0)
    b = [{"from": "2026-11-03T11:00:00Z", "to": "2026-11-03T13:00:00Z"}]
    assert in_blackout(b, T0) and not in_blackout(b, T0 + timedelta(hours=2))
    # The first minute inside 22:00-06:00, after the blackout.
    assert next_open(22 * 60, 6 * 60, b, T0) == T0.replace(hour=22)
    always = [{"from": "2026-11-01T00:00:00Z", "to": "2027-03-01T00:00:00Z"}]
    assert next_open(None, None, always, T0) is None


def test_a_wave_starts_on_time_inside_its_window_and_not_in_a_blackout(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    blackout = [{"from": "2026-11-03T22:00:00Z", "to": "2026-11-03T23:00:00Z"}]
    pid = _plan(db, r, [{"prefixes": ["m/a/", "m/b/"], "start_at": T0 + timedelta(hours=1),
                         "window": (22 * 60, 6 * 60), "blackouts": blackout}])  # fmt: skip
    _sweep(r, T0)
    w = _wave(db, pid)
    assert (w["state"], w["waiting_reason"]) == ("waiting", "its start time hasn't come")
    _sweep(r, T0 + timedelta(hours=2))  # 14:00: outside 22:00-06:00
    assert _wave(db, pid)["waiting_reason"] == "outside its daily window"
    _sweep(r, T0.replace(hour=22, minute=30))  # in the blackout
    assert _wave(db, pid)["waiting_reason"] == "inside a blackout"
    assert _mids(db, pid) == []
    assert _sweep(r, T0.replace(hour=23, minute=5)) == 1
    w = _wave(db, pid)
    assert w["state"] == "started" and w["waiting_reason"] is None
    mids = _mids(db, pid)
    assert len(mids) == 2
    # Each held at verified: the scheduler asks for the cutover (decision 24).
    assert {r.mig(m)["cutover"] for m in mids} == {"manual"}
    # The plan's, not its proposer's (review of #45).
    assert {r.mig(m)["requested_by"] for m in mids} == {"system:plans"}
    # A second sweep starts nothing twice.
    _sweep(r, T0.replace(hour=23, minute=6))
    assert len(_mids(db, pid)) == 2


def test_a_wave_waits_for_the_wave_it_depends_on(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}, {"prefixes": ["m/b/"], "after": 1}])
    _sweep(r, T0)
    assert _wave(db, pid, 1)["state"] == "started"
    assert _wave(db, pid, 2)["waiting_reason"] == "waiting for wave 1 to be verified"
    (m1,) = _mids(db, pid, 1)
    assert r.drive(m1, until="verified") == "verified"
    _sweep(r, T0 + timedelta(minutes=1))  # wave 1 becomes verified (and asks its cutover)
    assert _wave(db, pid, 1)["state"] == "verified"
    _sweep(r, T0 + timedelta(minutes=2))
    assert _wave(db, pid, 2)["state"] == "started"


def test_a_wave_waits_for_its_source_to_be_catalogued(db, connect) -> None:  # type: ignore[no-untyped-def]
    """The wizard's L item: a migration asked for while its source is still
    being catalogued starts by itself once it is."""
    r = Rig(db, connect)
    run(put(r.w, "m/a/1.segy", b"x" * 64))

    async def register(conn):  # type: ignore[no-untyped-def]
        async with conn.transaction():
            return await register_source(conn, r.w.s3, dataset_id=r.w.dataset_id,
                                         container=BUCKET, groups=GROUPS, prefix="m/")  # fmt: skip

    sid = run(_with(connect, register))
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}])
    _sweep(r, T0)
    w = _wave(db, pid)
    assert w["state"] == "waiting" and "still being catalogued" in w["waiting_reason"]
    db.execute("UPDATE catalog.source SET state = 'seeded' WHERE source_id = %s", (sid,))
    _sweep(r, T0 + timedelta(minutes=1))
    assert _wave(db, pid)["state"] == "started" and len(_mids(db, pid)) == 1


def test_an_auto_wave_never_cuts_over_an_unverified_file(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"], "window": (12 * 60, 13 * 60)}])
    _sweep(r, T0)
    (mid,) = _mids(db, pid)
    # Copying and verifying: the scheduler asks for nothing.
    r.step(mid)
    _sweep(r, T0 + timedelta(minutes=1))
    assert r.mig(mid)["state"] != "verified" and r.mig(mid)["cutover_requested_at"] is None
    assert r.drive(mid, until="verified") == "verified"
    # Verified, but outside the window now: cutover waits for it.
    _sweep(r, T0 + timedelta(hours=2))
    assert r.mig(mid)["cutover_requested_at"] is None
    assert _wave(db, pid)["waiting_reason"] == "cutover waits for its window"
    _sweep(r, T0 + timedelta(minutes=30))
    assert r.mig(mid)["cutover_requested_at"] is not None
    assert r.drive(mid) == "cut_over"
    _sweep(r, T0 + timedelta(minutes=31))
    w = _wave(db, pid)
    assert w["state"] == "cut_over" and w["cut_over_at"] is not None
    (state,) = db.execute(
        "SELECT state FROM catalog.migration_plan WHERE plan_id = %s", (pid,)
    ).fetchone()
    assert state == "done"


def test_a_manual_wave_waits_for_an_admin(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"], "cutover": "manual"}])
    _sweep(r, T0)
    (mid,) = _mids(db, pid)
    assert r.drive(mid, until="verified") == "verified"
    _sweep(r, T0 + timedelta(minutes=1))
    assert _wave(db, pid)["state"] == "verified" and r.mig(mid)["cutover_requested_at"] is None


def test_a_crash_mid_start_leaves_the_wave_whole(db, connect, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The worker dies after the first prefix's migration: the wave's
    transaction rolls back, so nothing half-started is left, and the next
    sweep starts both."""
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/", "m/b/"]}])
    real = plans.request_location
    calls = {"n": 0}

    async def dies(*a, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("worker killed")
        return await real(*a, **kw)

    monkeypatch.setattr(plans, "request_location", dies)
    with pytest.raises(RuntimeError):
        _sweep(r, T0)
    assert _mids(db, pid) == [] and _wave(db, pid)["state"] == "planned"
    (n,) = db.execute(
        "SELECT count(*) FROM catalog.migration_prefix WHERE dataset_id = %s", (r.w.dataset_id,)
    ).fetchone()
    assert n == 0
    monkeypatch.setattr(plans, "request_location", real)
    _sweep(r, T0 + timedelta(minutes=1))
    assert len(_mids(db, pid)) == 2 and _wave(db, pid)["state"] == "started"


def test_a_refused_prefix_fails_the_wave_with_the_reason(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    r.request(prefix="m/a/")  # already migrating outside the plan
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}])
    _sweep(r, T0)
    w = _wave(db, pid)
    assert w["state"] == "failed" and "overlaps" in w["waiting_reason"]


def test_a_proposed_plan_starts_nothing(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}], state="proposed")
    _sweep(r, T0)
    assert _wave(db, pid)["state"] == "planned" and _mids(db, pid) == []


def test_copy_again_starts_a_new_run(db, connect) -> None:  # type: ignore[no-untyped-def]
    """The console's Copy again: the engine's next step in verifying starts
    a new run (rerun_requested_at), and clears the request."""
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request(cutover="manual")
    r.step(mid)  # pending -> copying
    r.step(mid)  # copying -> verifying
    first = r.mig(mid)["last_run_id"]
    assert r.mig(mid)["state"] == "verifying"
    r.w.clock.advance(timedelta(minutes=5))
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'verifying', rerun_requested_at = %s"
        " WHERE migration_id = %s",
        (r.w.clock.now(), mid),
    )
    assert r.step(mid) == "copying"
    m = r.mig(mid)
    assert m["last_run_id"] != first and m["rerun_requested_at"] is None


def test_an_all_cancelled_wave_is_cancelled_and_blocks_its_dependents(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Review of #45: a wave whose migrations were all cancelled verified
    nothing, so it isn't `verified`, and the wave after it never starts."""
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}, {"prefixes": ["m/b/"], "after": 1}])
    _sweep(r, T0)
    (m1,) = _mids(db, pid, 1)
    db.execute("UPDATE catalog.migration_prefix SET state = 'cancelled', cancelled_at = now()"
               " WHERE migration_id = %s",
               (m1,))  # fmt: skip
    _sweep(r, T0 + timedelta(minutes=1))
    assert _wave(db, pid, 1)["state"] == "cancelled"
    _sweep(r, T0 + timedelta(minutes=2))
    w2 = _wave(db, pid, 2)
    assert w2["state"] == "blocked" and "was cancelled" in w2["waiting_reason"]
    assert _mids(db, pid, 2) == []
    (state,) = db.execute(
        "SELECT state FROM catalog.migration_plan WHERE plan_id = %s", (pid,)
    ).fetchone()
    assert state == "approved"  # still open, showing the blocked wave


def test_a_cancelled_plans_started_wave_still_cuts_over(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Review of #45: cancelling a plan stops the waves not started; one under
    way is still followed, and an auto wave cuts over in its window."""
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}, {"prefixes": ["m/b/"], "after": 1}])
    _sweep(r, T0)
    (m1,) = _mids(db, pid, 1)
    db.execute("UPDATE catalog.migration_plan SET state = 'cancelled' WHERE plan_id = %s", (pid,))
    db.execute("UPDATE catalog.migration_wave SET state = 'cancelled' WHERE plan_id = %s"
               " AND seq = 2", (pid,))  # fmt: skip
    assert r.drive(m1, until="verified") == "verified"
    _sweep(r, T0 + timedelta(minutes=1))
    assert r.mig(m1)["cutover_requested_at"] is not None
    assert r.drive(m1) == "cut_over"
    _sweep(r, T0 + timedelta(minutes=2))
    assert _wave(db, pid, 1)["state"] == "cut_over"


def test_a_failed_wave_goes_back_to_started_once_retried(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}])
    _sweep(r, T0)
    (m1,) = _mids(db, pid)
    db.execute("UPDATE catalog.migration_prefix SET state = 'failed' WHERE migration_id = %s",
               (m1,))  # fmt: skip
    _sweep(r, T0 + timedelta(minutes=1))
    assert _wave(db, pid)["state"] == "failed"
    db.execute("UPDATE catalog.migration_prefix SET state = 'pending' WHERE migration_id = %s",
               (m1,))  # fmt: skip
    _sweep(r, T0 + timedelta(minutes=2))
    w = _wave(db, pid)
    assert w["state"] == "started" and w["waiting_reason"] is None


def test_leaving_verified_drops_the_plans_cutover_request(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Review of #45: a file changed in S3 after the sweep asked for the
    cutover sends the migration back to verifying; the request goes, so the
    next one is asked inside the window again."""
    r = Rig(db, connect)
    _two_prefixes(r)
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}])
    _sweep(r, T0)
    (m1,) = _mids(db, pid)
    assert r.drive(m1, until="verified") == "verified"
    _sweep(r, T0 + timedelta(minutes=1))
    assert r.mig(m1)["cutover_requested_by"] == "system:plans"
    run(put(r.w, "m/a/new.segy", b"written after the check"))
    r.sync()
    assert r.step(m1) == "verifying"
    m = r.mig(m1)
    assert m["cutover_requested_at"] is None and m["cutover_requested_by"] is None


def test_the_window_gates_new_runs_for_a_waves_migration(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Review of #45: Copy again (and a scheduled rerun) for a wave's
    migration waits for the wave's window."""
    r = Rig(db, connect)
    _two_prefixes(r)
    now = r.w.clock.now()
    start_min = (now.hour * 60 + now.minute + 5) % 1440  # opens in 5 minutes
    pid = _plan(db, r, [{"prefixes": ["m/a/"], "start_at": now - timedelta(days=1),
                         "window": (start_min, (start_min + 60) % 1440)}])  # fmt: skip
    db.execute("UPDATE catalog.migration_wave SET window_start_min = NULL, window_end_min = NULL"
               " WHERE plan_id = %s", (pid,))  # fmt: skip
    _sweep(r, now)  # starts: no window yet
    db.execute("UPDATE catalog.migration_wave SET window_start_min = %s, window_end_min = %s"
               " WHERE plan_id = %s", (start_min, (start_min + 60) % 1440, pid))  # fmt: skip
    (m1,) = _mids(db, pid)
    r.step(m1)  # pending -> copying (it started inside its window)
    r.step(m1)  # copying -> verifying
    first = r.mig(m1)["last_run_id"]
    r.w.clock.advance(timedelta(minutes=1))
    db.execute("UPDATE catalog.migration_prefix SET rerun_requested_at = %s"
               " WHERE migration_id = %s", (r.w.clock.now(), m1))  # fmt: skip
    assert r.step(m1) == "verifying" and r.mig(m1)["last_run_id"] == first  # gated
    r.w.clock.advance(timedelta(minutes=10))  # inside the window now
    assert r.step(m1) == "copying" and r.mig(m1)["last_run_id"] != first


def test_restore_again_renews_copies_about_to_expire(db, connect) -> None:  # type: ignore[no-untyped-def]
    """The console's Restore again: the next restore wave also takes the
    restored copies that expire within 24 hours unverified; without it they
    wait for their expiry."""
    r = Rig(db, connect, batch=True)
    run(put(r.w, "m/hot.segy", b"hot"))
    run(put(r.w, "m/cold.segy", b"cold", tier=LogicalTier.ARCHIVE))
    r.seed()
    mid = r.request()
    assert r.drive(mid, steps=3) == "verifying"
    r.w.clock.advance(timedelta(hours=9))  # the Bulk restore completes
    r.sync()
    r.step(mid)  # the wave closes
    db.execute("UPDATE catalog.restore SET expires_at = %s WHERE state = 'completed'",
               (r.w.clock.now() + timedelta(hours=12),))  # fmt: skip
    jobs = [c for c, _ in r.w.fake.calls].count("CreateJob")
    db.execute("UPDATE catalog.migration_prefix SET state = 'verifying',"
               " last_copy_started_at = %s WHERE migration_id = %s",
               (r.w.clock.now(), mid))  # fmt: skip
    r.step(mid)
    assert [c for c, _ in r.w.fake.calls].count("CreateJob") == jobs  # nothing to renew yet
    db.execute("UPDATE catalog.migration_prefix SET restore_requested_at = %s"
               " WHERE migration_id = %s", (r.w.clock.now(), mid))  # fmt: skip
    r.step(mid)
    assert [c for c, _ in r.w.fake.calls].count("CreateJob") == jobs + 1
    assert r.mig(mid)["restore_requested_at"] is None


def test_a_blocked_wave_blocks_the_waves_after_it(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Re-review of #45: wave 3 after a blocked wave 2 is blocked too."""
    r = Rig(db, connect)
    run(put(r.w, "m/a/1.segy", b"a"))
    run(put(r.w, "m/b/2.segy", b"b"))
    run(put(r.w, "m/c/3.segy", b"c"))
    r.seed()
    pid = _plan(db, r, [{"prefixes": ["m/a/"]}, {"prefixes": ["m/b/"], "after": 1},
                        {"prefixes": ["m/c/"], "after": 2}])  # fmt: skip
    db.execute("UPDATE catalog.migration_wave SET state = 'cancelled' WHERE plan_id = %s"
               " AND seq = 1", (pid,))  # fmt: skip
    _sweep(r, T0)
    assert _wave(db, pid, 2)["state"] == "blocked"
    _sweep(r, T0 + timedelta(minutes=1))
    w3 = _wave(db, pid, 3)
    assert w3["state"] == "blocked" and "wave 2 is blocked" in w3["waiting_reason"]
