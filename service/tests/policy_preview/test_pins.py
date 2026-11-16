"""pin_checks() and pin_cost_change(): warnings before a tier pin (decision 29)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from omnifile_service.backends.base import LogicalTier
from omnifile_service.policy.checks import PinFacts, pin_checks
from omnifile_service.policy.cost import pin_cost_change

from .support import WORM, codes, ctx, schema_errors

NOW = datetime(2026, 10, 6, tzinfo=UTC)
GiB = 1024**3


def facts(**kw) -> PinFacts:
    base = dict(
        size=100 * GiB,
        logical_tier=LogicalTier.WARM,
        native_class="Cool",
        tier_changed_at=NOW - timedelta(days=10),
        explicit_tier_set=True,
        archived=False,
        smart_layout=True,
        archive_at=None,
    )
    return PinFacts(**{**base, **kw})


def test_early_deletion_fee() -> None:
    (c,) = pin_checks(LogicalTier.HOT, facts(), ctx(), NOW)
    assert c.code == "pin_early_deletion" and "30-day minimum" in c.message
    assert "20 more days" in c.message
    # 100 GiB of Cool ZRS (0.019) for 20 days.
    want = Decimal(100) * Decimal("0.019") * 20 / (Decimal(365) / 12)
    assert c.fee is not None and c.fee.amount == want
    assert (
        pin_checks(LogicalTier.HOT, facts(tier_changed_at=NOW - timedelta(days=31)), ctx(), NOW)
        == []
    )
    assert pin_checks(LogicalTier.WARM, facts(), ctx(), NOW) == []  # no move


def test_a_shared_blob_bills_a_read_and_a_new_blob_not_an_early_deletion() -> None:
    """Decision 66: the file leaves, and the blob it leaves stays where it is."""
    (c,) = pin_checks(LogicalTier.HOT, facts(shares_blob=True), ctx(), NOW)
    assert c.code == "pin_own_copy" and c.severity == "info"
    assert "own copy" in c.message and "one more blob" in c.message
    assert c.fee is not None and c.fee.amount == Decimal(100) * Decimal("0.01")
    # The whole new blob, not the difference between two tiers.
    change = pin_cost_change(LogicalTier.HOT, facts(shares_blob=True), ctx())
    alone = pin_cost_change(LogicalTier.HOT, facts(), ctx())
    assert change is not None and alone is not None and change.amount > alone.amount > 0
    assert change.amount == Decimal(100) * Decimal(ctx().prices.price("Hot", "live").gb_month)


def test_a_shared_blob_read_out_of_hot_or_smart_is_free() -> None:
    hot = facts(shares_blob=True, logical_tier=LogicalTier.HOT, native_class="Hot")
    (c,) = pin_checks(LogicalTier.COLD, hot, ctx(), NOW)
    assert c.code == "pin_own_copy" and c.fee is not None and c.fee.amount == 0
    smart = facts(shares_blob=True, logical_tier=LogicalTier.SMART, native_class="Smart",
                  explicit_tier_set=False)  # fmt: skip
    found = {c.code: c for c in pin_checks(LogicalTier.HOT, smart, ctx(), NOW)}
    assert found["pin_own_copy"].fee is not None and found["pin_own_copy"].fee.amount == 0


def test_a_shared_blob_staying_in_its_tier_or_archived_is_as_before() -> None:
    assert pin_checks(LogicalTier.WARM, facts(shares_blob=True), ctx(), NOW) == []
    change = pin_cost_change(LogicalTier.WARM, facts(shares_blob=True), ctx())
    assert change is not None and change.amount == 0
    archived = facts(shares_blob=True, archived=True, logical_tier=LogicalTier.ARCHIVE,
                     native_class="Archive")  # fmt: skip
    assert "pin_own_copy" not in [c.code for c in pin_checks(LogicalTier.HOT, archived, ctx(), NOW)]


def test_leaving_smart() -> None:
    smart = facts(logical_tier=LogicalTier.SMART, native_class="Smart", explicit_tier_set=False)
    (c,) = pin_checks(LogicalTier.COLD, smart, ctx(), NOW)
    assert c.code == "pin_leaves_smart" and "for good" in c.message and "Cold" in c.message
    # Not on a layout where an explicit tier doesn't leave Smart for good.
    assert pin_checks(LogicalTier.COLD, smart, ctx(caps=WORM, layout="single_account"), NOW) == []


def test_recall_time_and_no_promise_above_10_gb() -> None:
    archived = facts(logical_tier=LogicalTier.ARCHIVE, native_class="Archive", archived=True)
    (c,) = pin_checks(LogicalTier.HOT, archived, ctx(), NOW)
    assert c.code == "pin_recall_time" and "No recall time is promised above 10 GB" in c.message
    assert c.fee is not None and c.fee.amount > 0
    small = pin_checks(LogicalTier.HOT, facts(**{**archived.__dict__, "size": GiB}), ctx(), NOW)
    assert "standard up to 15 hours" in small[0].message and "high up to 1 hour" in small[0].message
    assert pin_checks(LogicalTier.ARCHIVE, archived, ctx(), NOW) == []


def test_archive_date() -> None:
    later = facts(tier_changed_at=None, archive_at=NOW + timedelta(days=40))
    (c,) = pin_checks(LogicalTier.HOT, later, ctx(), NOW)
    assert c.code == "pin_past_archive_date" and "2026-11-15" in c.message
    assert pin_checks(LogicalTier.HOT, later, ctx(), NOW, override_archive=True) == []
    assert pin_checks(LogicalTier.HOT, later, ctx(), NOW, until=NOW + timedelta(days=5)) == []
    past = facts(tier_changed_at=None, archive_at=NOW - timedelta(days=1))
    assert "past its archive date" in pin_checks(LogicalTier.HOT, past, ctx(), NOW)[0].message


def test_pin_checks_json() -> None:
    archived = facts(logical_tier=LogicalTier.ARCHIVE, archived=True, archive_at=NOW)
    out = pin_checks(LogicalTier.HOT, archived, ctx(), NOW)
    assert codes(out) == ["pin_recall_time", "pin_past_archive_date"]
    for c in out:
        assert schema_errors("Check", c.to_json()) == []


def test_monthly_cost_change() -> None:
    cool_to_hot = pin_cost_change(LogicalTier.HOT, facts(), ctx())
    # 100 GiB x (Hot ZRS 0.026 - Cool ZRS 0.019).
    assert cool_to_hot is not None and cool_to_hot.amount == Decimal(100) * Decimal("0.007")
    to_archive = pin_cost_change(LogicalTier.ARCHIVE, facts(), ctx())
    assert to_archive is not None and to_archive.amount < 0
    smart = facts(logical_tier=LogicalTier.SMART, native_class="Smart")
    rng = pin_cost_change(LogicalTier.COLD, smart, ctx())
    assert rng is not None and rng.high is not None and rng.amount < rng.high
    archived = facts(logical_tier=LogicalTier.ARCHIVE, archived=True)
    assert pin_cost_change(LogicalTier.HOT, archived, ctx()).amount == 0  # type: ignore[union-attr]
    assert pin_cost_change(LogicalTier.HOT, facts(), ctx(kind="s3")) is None
