from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from azure.core.credentials import AccessToken, AccessTokenInfo

# The sibling test packages (backend_contract) are top-level packages under
# tests/; put tests/ on the path when only this directory is collected.
_TESTS = str(Path(__file__).resolve().parent.parent)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from azure_fakes import FakeAzure, ManualClock  # noqa: E402

from omnifile_service.backends.azure import (  # noqa: E402,E501
    AzureAccount,
    AzureBlobBackend,
    AzureBlobConfig,
)


class FakeCredential:
    """An Entra token credential that never talks to Entra."""

    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        return AccessToken("fake-entra-token", int(time.time()) + 3600)

    async def get_token_info(self, *scopes: str, **kwargs: object) -> AccessTokenInfo:
        return AccessTokenInfo("fake-entra-token", int(time.time()) + 3600)

    async def close(self) -> None:
        return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "floci: smoke tests against a floci-az container (OMNIFILE_TEST_FLOCI_AZ)"
    )
    config.addinivalue_line(
        "markers", "azure: tests against a real Azure account (OMNIFILE_TEST_AZURE=1)"
    )


def url(name: str) -> str:
    return f"https://{name}.blob.core.windows.net"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def fake(clock: ManualClock) -> FakeAzure:
    f = FakeAzure(clock.now)
    f.add_account("live", smart=True)
    f.add_account("arch")
    f.add_account("worm")
    for acct in ("live", "arch", "worm"):
        f.container(acct, "data")
        f.container(acct, "recall")
    return f


def two_account_config(**kw: object) -> AzureBlobConfig:
    return AzureBlobConfig(
        accounts={
            "live": AzureAccount(
                "live", url("live"), sas_url="https://live.privatelink.blob.core.windows.net"
            ),
            "arch": AzureAccount("arch", url("arch")),
        },
        layout="two_account",
        primary_account="live",
        archive_account="arch",
        smart=True,
        **kw,  # type: ignore[arg-type]
    )


def worm_config(**kw: object) -> AzureBlobConfig:
    return AzureBlobConfig(
        accounts={"worm": AzureAccount("worm", url("worm"))},
        layout="single_account",
        primary_account="worm",
        worm=True,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def two(fake: FakeAzure, clock: ManualClock) -> Iterator[AzureBlobBackend]:
    yield AzureBlobBackend(
        7,
        two_account_config(block_size=4, extra_client_kwargs={"retry_total": 0}),
        credential=FakeCredential(),
        clock=clock,
        transport=fake,
        sleep=_no_sleep,
    )


@pytest.fixture
def worm(fake: FakeAzure, clock: ManualClock) -> Iterator[AzureBlobBackend]:
    yield AzureBlobBackend(
        8,
        worm_config(extra_client_kwargs={"retry_total": 0}),
        credential=FakeCredential(),
        clock=clock,
        transport=fake,
        sleep=_no_sleep,
    )
