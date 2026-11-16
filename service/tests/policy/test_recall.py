"""Recall planning: the 10 GiB/h High budget, ETAs, and the open decision."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from omnifile_service.backends.base import LogicalTier, RestoreSpeed
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.policy import decide_on_open, fsal_open_policy, plan_recall
from omnifile_service.policy.recall import PipelineEntry, account_of, pipeline_free_at

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
GiB = 1024**3
GB = 10**9
CAPS = SimBackend.azure_two_account(VirtualClock()).capabilities()


def test_standard_eta_comes_from_capabilities() -> None:
    p = plan_recall(CAPS, 1 * GB, "standard", NOW)
    assert p.speed is RestoreSpeed.STANDARD and p.eta == NOW + timedelta(hours=15)


def test_no_promise_above_10_gb() -> None:
    assert plan_recall(CAPS, 10 * GB + 1, "standard", NOW).eta is None
    big_high = plan_recall(CAPS, 20 * GB, "high", NOW)
    assert big_high.speed is RestoreSpeed.HIGH and big_high.eta is None
    assert plan_recall(CAPS, 10 * GB, "standard", NOW).eta is not None


def test_high_admitted_within_budget() -> None:
    p = plan_recall(CAPS, 4 * GiB, "high", NOW)
    assert p.speed is RestoreSpeed.HIGH and not p.queued and p.eta == NOW + timedelta(hours=1)
    backlog = [PipelineEntry(NOW, 5 * GiB)]
    p = plan_recall(CAPS, 4 * GiB, "high", NOW, high_backlog=backlog)
    assert not p.queued and p.submit_at == NOW
    assert p.eta == NOW + timedelta(hours=0.5) + timedelta(hours=1)


def test_high_beyond_budget_is_queued() -> None:
    backlog = [PipelineEntry(NOW, 8 * GiB)]
    p = plan_recall(CAPS, 4 * GiB, "high", NOW, high_backlog=backlog)
    assert p.queued and p.speed is RestoreSpeed.HIGH
    # Room for 4 GiB once the backlog is down to 6 GiB: 0.2 h from now.
    assert p.submit_at == NOW + timedelta(hours=0.2)
    assert p.eta == NOW + timedelta(hours=0.8) + timedelta(hours=1)
    # Recalls waiting ahead count too.
    q = plan_recall(CAPS, 4 * GiB, "high", NOW, waiting_ahead_bytes=8 * GiB)
    assert q.queued and q.submit_at == p.submit_at


def test_high_downgraded_where_it_cannot_be_faster() -> None:
    backlog = [PipelineEntry(NOW, 140 * GiB)]  # 14 h of pipeline
    p = plan_recall(CAPS, 1 * GiB, "high", NOW, high_backlog=backlog)
    assert p.downgraded and p.speed is RestoreSpeed.STANDARD and not p.queued
    assert p.eta == NOW + timedelta(hours=15)


def test_pipeline_matches_sim() -> None:
    clock = VirtualClock(NOW)
    sim = SimBackend.azure_two_account(clock)
    entries = [PipelineEntry(NOW, 6 * GiB), PipelineEntry(NOW + timedelta(minutes=10), 7 * GiB)]
    for i, e in enumerate(entries):
        clock.set(e.admitted_at)
        ref = sim.put_synthetic("archive/data", f"f{i}", e.size, LogicalTier.ARCHIVE)
        asyncio.run(sim.restore(ref, RestoreSpeed.HIGH))
    assert pipeline_free_at(entries, 10 * GiB) == sim.priority_queue_clear_at("archive/data")


def test_account_of() -> None:
    assert account_of("archive/data") == "archive"


def test_decide_on_open() -> None:
    soon, late = NOW + timedelta(minutes=50), NOW + timedelta(hours=15)
    assert decide_on_open("wait", None, None, NOW) == "wait"
    assert decide_on_open("fail", None, soon, NOW) == "fail"
    assert decide_on_open("auto", 60, soon, NOW) == "wait"
    assert decide_on_open("auto", 60, late, NOW) == "fail"
    assert decide_on_open("auto", 60, None, NOW) == "fail"  # >10 GB: no promise, fail fast
    assert fsal_open_policy("auto", 60) == {"policy": "waitquick", "quick_ms": 3_600_000}
    assert fsal_open_policy("wait", None) == {"policy": "wait"}
