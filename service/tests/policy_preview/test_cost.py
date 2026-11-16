"""First-year cost per TB, against keeping it Hot."""

from __future__ import annotations

from decimal import Decimal

from omnifile_service.policy import Policy
from omnifile_service.policy.cost import first_year_cost
from omnifile_service.policy.preview import CostAssumptions

from .support import SCHEDULED, SMART, WORM, ctx


def items(est) -> dict[tuple[str, str | None], dict]:
    return {(i.item, i.tier and i.tier.value): i.amount.to_json() for i in est.breakdown}


def cost(body: dict, **kw):
    c = ctx(**kw)
    return first_year_cost(Policy.from_body(body), c)


def test_keep_hot_is_a_year_of_hot_in_the_live_account() -> None:
    est = cost(SCHEDULED)
    # eastus Hot ZRS 0.026 /GB-month x 1,024 GB x 12 months.
    assert est.keep_hot_per_tb_first_year.to_json()["amount"] == "319.49"
    single = cost(SCHEDULED, caps=WORM, layout="single_account")
    assert single.keep_hot_per_tb_first_year.to_json()["amount"] == "255.59"  # LRS 0.0208


def test_scheduled_storage_by_tier() -> None:
    got = items(cost(SCHEDULED))
    # 30 days Hot ZRS, 90 days Cool ZRS (0.019), 245 days Cold ZRS (0.004).
    assert got[("storage", "hot")]["amount"] == "26.26"
    assert got[("storage", "warm")]["amount"] == "57.57"
    assert got[("storage", "cold")]["amount"] == "32.99"
    assert ("storage", "archive") not in got  # day 365 is outside the first year
    est = cost(SCHEDULED)
    assert est.per_tb_first_year.high is None
    assert est.saving.amount == est.keep_hot_per_tb_first_year.amount - est.per_tb_first_year.amount


def test_smart_is_a_range_from_all_cold_to_all_hot() -> None:
    est = cost(SMART)
    got = items(est)
    assert got[("storage", "smart")] == {"amount": "24.24", "high": "157.56", "currency": "USD"}
    assert ("smart_monitoring", "smart") in got
    assert est.per_tb_first_year.high is not None
    assert est.per_tb_first_year.amount < est.per_tb_first_year.high
    # The saving is the other way round.
    keep = est.keep_hot_per_tb_first_year.amount
    assert est.saving.amount == keep - est.per_tb_first_year.high
    assert est.saving.high == keep - est.per_tb_first_year.amount


def test_smart_monitoring_skips_small_objects() -> None:
    small = cost(SMART, assumptions=CostAssumptions(mean_file_bytes=64 * 1024))
    assert ("smart_monitoring", "smart") not in items(small)


def test_two_account_archive_copy_and_read_back() -> None:
    body = {**SCHEDULED, "archive_day": 200}
    got = items(cost(body))
    assert ("archive_copy", "archive") in got and ("read_back", "archive") in got
    # 165 days in Archive LRS 0.00099.
    assert got[("storage", "archive")]["amount"] == "5.50"
    # Cold left after 80 of 90 days: an early-deletion fee for 10 days of Cold ZRS.
    early = Decimal(got[("early_deletion", None)]["amount"])
    assert early == (Decimal("0.004") * 1024 * 10 / (Decimal(365) / 12)).quantize(Decimal("0.01"))


def test_worm_tiers_in_place_without_a_copy() -> None:
    body = {**SCHEDULED, "archive_day": 200}
    got = items(cost(body, caps=WORM, layout="single_account"))
    assert ("archive_copy", "archive") not in got and ("read_back", "archive") not in got
    assert ("transitions", None) in got


def test_recall_per_tb_at_each_speed() -> None:
    est = cost(SCHEDULED)
    by = {r.priority: r for r in est.recall_per_tb}
    assert set(by) == {"standard", "high"}
    # 1,024 GB x 0.02 + 1,024 objects x (5 read + 0.05 write) / 10,000.
    assert by["standard"].per_tb.to_json()["amount"] == "21.00"
    assert by["high"].per_tb.amount > by["standard"].per_tb.amount
    assert by["standard"].hours == (0.0, 15.0) and by["high"].hours == (0.0, 1.0)


def test_recall_fraction_adds_expected_recalls() -> None:
    body = {**SCHEDULED, "archive_day": 200}
    base = cost(body)
    more = cost(body, assumptions=CostAssumptions(recall_fraction_per_year=0.5))
    rec = [i for i in more.breakdown if i.item == "recall"]
    assert len(rec) == 1
    std = next(r for r in more.recall_per_tb if r.priority == "standard").per_tb.amount
    assert rec[0].amount.amount == Decimal("0.5") * Decimal(165) / 365 * std
    assert more.per_tb_first_year.amount == base.per_tb_first_year.amount + rec[0].amount.amount
    assert not [
        i for i in cost(SCHEDULED, assumptions=more.assumptions).breakdown if i.item == "recall"
    ]  # archived after the first year


def test_more_objects_cost_more_per_operation() -> None:
    few = cost(SMART)
    many = cost(SMART, assumptions=CostAssumptions(mean_file_bytes=1 << 20))
    assert many.per_tb_first_year.amount > few.per_tb_first_year.amount


def test_override_prices_are_used() -> None:
    body = load_override_with_hot_zrs("0.052")
    est = cost(SCHEDULED, override=body)
    assert est.keep_hot_per_tb_first_year.to_json()["amount"] == "638.98"
    assert est.prices.overridden


def load_override_with_hot_zrs(price: str) -> dict:
    from omnifile_service.policy.cost import load_price_table

    body = load_price_table("sim", None).to_json()
    body["classes"]["Hot:ZRS"]["gb_month"] = price
    return body
