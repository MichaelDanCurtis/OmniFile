"""preview(): the timeline and the PolicyPreview shape."""

from __future__ import annotations

import pytest

from omnifile_service.policy import Policy
from omnifile_service.policy.preview import (
    CostAssumptions,
    apply_share_overrides,
    preview,
    timeline,
)

from .support import SCHEDULED, SMART, TWO, WORM, codes, ctx, schema_errors, share


def spans(body: dict, caps=TWO, layout: str = "two_account") -> list[tuple]:
    t = timeline(Policy.from_body(body), caps, layout)
    return [
        (s.from_day, s.to_day, s.logical_tier.value, s.native_class, s.account, s.local)
        for s in t.segments
    ]


def test_scheduled_timeline_two_accounts() -> None:
    assert spans(SCHEDULED) == [
        (0, 30, "hot", "Hot", "live", "resident"),
        (30, 120, "warm", "Cool", "live", "resident"),
        (120, 365, "cold", "Cold", "live", "resident"),
        (365, None, "archive", "Archive", "archive", "stub"),
    ]


def test_smart_timeline_and_its_clock() -> None:
    t = timeline(Policy.from_body({**SMART, "clock": "last_read"}), TWO, "two_account")
    assert [(s.from_day, s.to_day, s.logical_tier.value) for s in t.segments] == [
        (0, 180, "smart"),
        (180, None, "archive"),
    ]
    assert t.clock == "written"  # Smart's archive day counts from the write
    assert "Microsoft moves it" in (t.segments[0].note or "")
    assert t.horizon_days == 400


def test_stub_points_split_segments() -> None:
    leaves = {**SCHEDULED, "stub_at": "leaves_hot", "resident_mb": 4}
    assert [s[5] for s in spans(leaves)] == ["resident", "stub", "stub", "stub"]
    t = timeline(Policy.from_body(leaves), TWO, "two_account")
    assert "first 4 MB" in (t.segments[1].note or "")
    assert {s[5] for s in spans({**SCHEDULED, "stub_at": "never"})} == {"resident"}
    # Smart counts as hot: leaves_hot stubs at the archive day.
    assert [s[5] for s in spans({**SMART, "stub_at": "leaves_hot"})] == ["resident", "stub"]


def test_day_zero_and_single_account() -> None:
    body = {"warm_day": 0, "archive_day": 500}
    assert spans(body, WORM, "single_account") == [
        (0, 500, "warm", "Cool", "single", "resident"),
        (500, None, "archive", "Archive", "single", "stub"),
    ]
    t = timeline(Policy.from_body(body), WORM, "single_account")
    assert t.horizon_days == 530 and "in place" in (t.segments[-1].note or "")


def test_preview_json_is_the_policy_preview_schema() -> None:
    c = ctx(
        shares=(share(on_open="wait"),), assumptions=CostAssumptions(recall_fraction_per_year=0.1)
    )
    for body in (SCHEDULED, SMART, {**SCHEDULED, "archive_day": 200}, {"warm_day": 5}, {}):
        out = preview(body, c).to_json()
        assert schema_errors("PolicyPreview", out) == [], body


def test_invalid_body_is_not_an_exception() -> None:
    r = preview({"mode": "smart", "warm_day": 10, "archive_day": 100}, ctx())
    assert not r.valid
    assert r.timeline.segments == ()
    assert r.cost.per_tb_first_year == r.cost.keep_hot_per_tb_first_year
    assert codes(r.checks)[0] == "smart_has_no_warm_cold"
    r = preview("not an object", ctx())  # type: ignore[arg-type]
    assert not r.valid and codes(r.checks) == ["invalid_body"]
    assert r.to_json()["body"] == {}


def test_m1_body_needs_converting() -> None:
    r = preview({"stub_after_minutes": 60}, ctx())
    assert not r.valid
    (c,) = [c for c in r.checks if c.code == "archive_day_required"]
    assert "M1 policy" in c.message and c.field == "archive_day"


def test_valid_means_no_error_checks() -> None:
    r = preview(SCHEDULED, ctx())
    assert r.valid and all(c.severity != "error" for c in r.checks)
    sev = [c.severity for c in r.checks]
    assert sev == sorted(sev, key=["error", "warning", "info"].index)


def test_s3_without_prices_names_what_is_missing() -> None:
    from .support import S3

    r = preview(SCHEDULED, ctx(S3, "single_account", kind="s3"))
    assert r.valid
    (c,) = [c for c in r.checks if c.code == "price_missing"]
    assert "STANDARD" in c.message


def test_share_overrides() -> None:
    shares = (share(share_id=1), share(share_id=2))
    out = apply_share_overrides(
        shares, [{"share_id": "2", "on_open": "auto", "wait_threshold_minutes": 90}]
    )
    assert out[0] == shares[0]
    assert (out[1].on_open, out[1].wait_threshold_minutes) == ("auto", 90)
    assert apply_share_overrides(shares, None) == shares


@pytest.mark.parametrize(
    "d",
    [
        {"tb": 0},
        {"tb": True},
        {"mean_file_bytes": 0},
        {"mean_file_bytes": 1.5},
        {"recall_fraction_per_year": 2},
    ],
)
def test_bad_assumptions(d: dict) -> None:
    with pytest.raises(ValueError):
        CostAssumptions.from_json(d)


def test_assumptions_defaults() -> None:
    assert CostAssumptions.from_json(None) == CostAssumptions()
    a = CostAssumptions.from_json({"tb": 40, "mean_file_bytes": 1 << 20})
    assert a.to_json() == {"tb": 40.0, "mean_file_bytes": 1 << 20, "recall_fraction_per_year": 0.0}
