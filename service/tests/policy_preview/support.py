"""Contexts for the preview tests, and a validator for portal-api.yaml schemas."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.policy.cost import load_price_table
from omnifile_service.policy.preview import CostAssumptions, PreviewContext, ShareFacts

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "portal-api.yaml"

TWO = SimBackend.azure_two_account(VirtualClock()).capabilities()
WORM = SimBackend.azure_worm(VirtualClock()).capabilities()
S3 = SimBackend.s3(VirtualClock()).capabilities()

SCHEDULED = {"mode": "scheduled", "warm_day": 30, "cold_day": 120, "archive_day": 365}
SMART = {"mode": "smart", "archive_day": 180}


def share(**kw: Any) -> ShareFacts:
    base: dict[str, Any] = {
        "share_id": 7,
        "name": "seismic",
        "protocols": ("nfs4",),
        "nfs_sec": ("krb5",),
        "trust_level": "proven",
        "on_open": "fail",
        "wait_threshold_minutes": None,
        "worm_enabled": False,
    }
    return ShareFacts(**{**base, **kw})


def ctx(
    caps=TWO,
    layout: str = "two_account",
    shares: tuple[ShareFacts, ...] = (),
    kind: str = "sim",
    assumptions: CostAssumptions | None = None,
    override: dict[str, Any] | None = None,
) -> PreviewContext:
    return PreviewContext(
        caps,
        layout,
        shares,
        load_price_table(kind, override),
        assumptions or CostAssumptions(),
    )


@cache
def _spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC_PATH.read_text())


def schema_errors(schema: str, body: Any) -> list[str]:
    v = Draft202012Validator({**_spec(), "$ref": f"#/components/schemas/{schema}"})
    return [f"{list(e.absolute_path)}: {e.message}" for e in v.iter_errors(body)]


def codes(checks) -> list[str]:
    return [c.code for c in checks]
