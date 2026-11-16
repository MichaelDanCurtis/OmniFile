"""Policy bodies and the editor's checks."""

from __future__ import annotations

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.policy import Policy, problems

TWO = SimBackend.azure_two_account(VirtualClock()).capabilities()
WORM = SimBackend.azure_worm(VirtualClock()).capabilities()


def codes(body: dict, caps=TWO, worm: bool = False) -> set[str]:
    return {p.code for p in problems(Policy.from_body(body), caps, worm=worm)}


def test_defaults() -> None:
    p = Policy.from_body({"archive_day": 365})
    assert (p.mode, p.clock, p.stub_at) == ("scheduled", "written", "archive")
    assert Policy.is_tiering_body({"archive_day": 1})
    assert not Policy.is_tiering_body({"stub_after_minutes": 60})


def test_minimum_durations_warned_in_azure_terms() -> None:
    ps = problems(Policy.from_body({"warm_day": 1, "cold_day": 10, "archive_day": 50}), TWO)
    msgs = [p.message for p in ps if p.code == "minimum_duration"]
    assert any("Cool has a 30-day minimum" in m for m in msgs)
    assert any("Cold has a 90-day minimum" in m for m in msgs)
    assert "minimum_duration" not in codes({"warm_day": 30, "cold_day": 60, "archive_day": 150})


def test_smart_not_on_worm() -> None:
    assert "smart_on_worm" in codes({"mode": "smart", "archive_day": 365}, WORM, worm=True)
    assert "smart_unavailable" in codes({"mode": "smart", "archive_day": 365}, WORM)


def test_recall_warning_mentions_no_promise() -> None:
    ps = problems(Policy.from_body({"archive_day": 365}), TWO)
    assert any("10 GB" in p.message for p in ps if p.code == "archive_recall_time")
