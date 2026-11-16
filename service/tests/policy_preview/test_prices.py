"""The built-in price table and the admin override shape."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from omnifile_service.policy.cost import PriceTableError, builtin_table, load_price_table

from .support import schema_errors


def test_builtin_is_dated_and_every_number_marked_verify() -> None:
    raw = builtin_table("azure_blob")
    assert raw["verify"] is True
    assert date.fromisoformat(raw["as_of"])
    assert raw["default_region"] in raw["regions"]
    for region, r in raw["regions"].items():
        for name, c in r["classes"].items():
            assert c["verify"] is True, (region, name)
            for k, v in c.items():
                if k not in ("verify", "minimum_days"):
                    assert isinstance(v, str) and Decimal(v) >= 0, (region, name, k)


def test_every_region_has_what_the_model_needs() -> None:
    for region in builtin_table("azure_blob")["regions"]:
        t = load_price_table("azure_blob", None, region=region)
        for cls in ("Hot", "Cool", "Cold"):
            for acct in ("live", "single"):
                p = t.price(cls, acct)
                assert p is not None and p.gb_month is not None and p.write_per_10k is not None
        arch = t.price("Archive", "archive")
        assert arch is not None
        assert arch.retrieval_per_gb_high is not None and arch.read_per_10k_high is not None
        assert t.price("Smart", "live").monitoring_per_10k_objects_month is not None  # type: ignore[union-attr]


def test_redundancy_lookup() -> None:
    t = load_price_table("azure_blob", None, region="eastus")
    assert t.price("Hot", "live").gb_month == Decimal("0.026")  # type: ignore[union-attr]  # ZRS
    assert t.price("Hot", "single").gb_month == Decimal("0.0208")  # type: ignore[union-attr]  # LRS
    assert t.price("Archive", "archive").gb_month == Decimal("0.00099")  # type: ignore[union-attr]
    assert t.price("Nope", "live") is None


def test_default_region_and_ids() -> None:
    t = load_price_table("azure_blob", None)
    assert (t.region, t.verify, t.overridden) == ("eastus", True, False)
    assert t.id == "azure_blob/eastus/2026-10-06"
    sim = load_price_table("sim", None)
    assert sim.backend_kind == "sim" and sim.classes == t.classes  # sim plays Azure


def test_unknown_region_and_kind() -> None:
    with pytest.raises(PriceTableError, match="region"):
        load_price_table("azure_blob", None, region="moon-1")
    with pytest.raises(PriceTableError, match="unknown backend kind"):
        load_price_table("gcs", None)


def test_s3_has_no_builtin_table_yet() -> None:
    t = load_price_table("s3", None)
    assert t.classes == {} and t.notes


def test_table_json_matches_the_api_schema() -> None:
    body = load_price_table("azure_blob", None).to_json()
    assert schema_errors("PriceTable", body) == []
    assert schema_errors("PriceTableRef", load_price_table("sim", None).ref()) == []


def test_override_round_trips_through_the_api_shape() -> None:
    builtin = load_price_table("azure_blob", None).to_json()
    edited = json.loads(json.dumps(builtin))
    edited["classes"]["Hot:ZRS"]["gb_month"] = "0.02"
    edited["verify"] = False
    for c in edited["classes"].values():
        c["verify"] = False
    edited["as_of"] = "2026-11-01"
    t = load_price_table("azure_blob", edited)
    assert t.overridden and not t.verify
    assert t.price("Hot", "live").gb_month == Decimal("0.02")  # type: ignore[union-attr]
    assert t.id == "azure_blob/eastus/2026-10-06"  # the id it was copied from
    assert schema_errors("PriceTable", t.to_json()) == []


def test_override_keeps_verify_while_any_class_is_unchecked() -> None:
    body = {
        "backend_kind": "azure_blob",
        "currency": "EUR",
        "as_of": "2026-11-01",
        "verify": False,
        "classes": {"Hot": {"gb_month": "0.02", "verify": True}},
    }
    t = load_price_table("azure_blob", body)
    assert t.verify and t.currency == "EUR" and t.id.startswith("azure_blob/")


@pytest.mark.parametrize(
    "patch, match",
    [
        ({"backend_kind": "s3"}, "override is for"),
        ({"as_of": "yesterday"}, "as_of"),
        ({"currency": "usd"}, "currency"),
        ({"verify": "yes"}, "verify"),
        ({"classes": []}, "classes"),
        ({"classes": {"Hot": {"gb_month": "-1"}}}, ">= 0"),
        ({"classes": {"Hot": {"gb_month": "cheap"}}}, "decimal"),
        ({"classes": {"Hot": {"minimum_days": -3}}}, "minimum_days"),
    ],
)
def test_bad_overrides_are_refused(patch: dict, match: str) -> None:
    body = {
        "backend_kind": "azure_blob",
        "currency": "USD",
        "as_of": "2026-11-01",
        "verify": True,
        "classes": {"Hot": {"gb_month": "0.02"}},
        **patch,
    }
    with pytest.raises(PriceTableError, match=match):
        load_price_table("azure_blob", body)
