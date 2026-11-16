"""Backend selection from catalog rows (no database needed)."""

from __future__ import annotations

import json

import pytest

from omnifile_service.backends.base import BackendKind
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.worker.backends import (
    BackendRegistry,
    BackendRow,
    backend_for,
    capabilities_for,
)


class _Cred:
    async def get_token(self, *a: object, **k: object) -> None:  # never called here
        raise AssertionError


def test_sim_rows_pick_their_layout() -> None:
    clock = VirtualClock()
    b = backend_for(4, "sim", {"layout": "azure_worm"}, clock=clock)
    assert isinstance(b, SimBackend) and b.backend_id == 4 and b.clock is clock
    assert b.capabilities().worm
    with pytest.raises(ValueError):
        backend_for(4, "sim", {"layout": "gcs"})


def test_azure_rows_build_the_azure_backend() -> None:
    config = {
        "accounts": {
            "live": "https://live.blob.core.windows.net",
            "arch": {"url": "https://arch.blob.core.windows.net"},
        },
        "layout": "two_account",
        "primary_account": "live",
        "archive_account": "arch",
        "smart": True,
    }
    b = backend_for(5, "azure_blob", config, credential=_Cred())
    assert b.kind is BackendKind.AZURE_BLOB and b.backend_id == 5
    assert b.capabilities().archive_needs_separate_account
    with pytest.raises(ValueError, match="account_id"):
        backend_for(6, "s3", {})


ACCT = "123456789012"
AWS_ACCOUNTS = {
    ACCT: {
        "region": "eu-west-1",
        "endpoints": {
            "s3": "https://bucket.vpce-0a1b2c3d.s3.eu-west-1.vpce.amazonaws.com",
            "sts": "https://vpce-0a1b2c3d.sts.eu-west-1.vpce.amazonaws.com",
        },
        "role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-service",
    }
}


def test_s3_rows_build_the_s3_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoints, the role and the audience come from the operator's
    settings (OMNIFILE_AWS_ACCOUNTS); the row names the account and buckets."""
    monkeypatch.setenv("OMNIFILE_AWS_ACCOUNTS", json.dumps(AWS_ACCOUNTS))
    monkeypatch.setenv("OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE", "api://omnifile-aws")
    config = {
        "account_id": ACCT,
        "region": "eu-west-1",
        "buckets": ["seismic-raw"],
        "versioning": False,
    }
    b = backend_for(7, "s3", config, credential=_Cred())
    assert b.kind is BackendKind.S3 and b.backend_id == 7
    assert not b.capabilities().versioning
    assert b.config.credentials == "web_identity"  # type: ignore[attr-defined]
    # The row's private_network column can only switch private networking on.
    reg = BackendRegistry([BackendRow(8, "s3", config, private_network=False)])
    assert reg.get(8).config.private_network  # type: ignore[attr-defined]
    # A row naming an endpoint or a key is refused, whatever it says.
    for bad in (
        {**config, "endpoint_url": "https://attacker.example"},
        {**config, "roles_anywhere": {"private_key": "/etc/key.pem"}},
        {**config, "credentials": "default"},
    ):
        with pytest.raises(ValueError):
            BackendRegistry([BackendRow(9, "s3", bad)]).get(9)
    caps = capabilities_for("s3", {})
    assert caps.kind is BackendKind.S3 and caps.versioning and not caps.worm


def test_registry_builds_once_and_accepts_injected_backends() -> None:
    reg = BackendRegistry([BackendRow(1, "sim", {"layout": "s3"})], clock=VirtualClock())
    assert reg.get(1) is reg.get(1)
    injected = SimBackend.azure_two_account(VirtualClock())
    reg2 = BackendRegistry.from_mapping({9: injected})
    assert reg2.get(9) is injected
    with pytest.raises(KeyError):
        reg2.get(10)
