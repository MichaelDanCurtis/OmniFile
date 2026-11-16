"""The shared contract suite, run against the Azure backend over the fake
transport. The fake is a test double, so this checks the backend's logic and
request plumbing, not Azure's behaviour (that is the opt-in real lane)."""

from __future__ import annotations

import pytest
from azure_fakes import FakeAzure, ManualClock
from backend_contract.test_contract import *  # noqa: F403
from backend_contract.test_contract_rules import *  # noqa: F403
from conftest import FakeCredential, _no_sleep, two_account_config, worm_config

from omnifile_service.backends.azure import AzureBlobBackend
from omnifile_service.backends.azure.testing import LoopThreadBackend
from omnifile_service.backends.base import StorageBackend


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture(params=["two_account", "worm", "loop_thread"])
def backend(request: pytest.FixtureRequest, clock: ManualClock) -> StorageBackend:
    fake = FakeAzure(clock.now)
    fake.add_account("live", smart=True)
    fake.add_account("arch")
    fake.add_account("worm")
    cfg = (worm_config if request.param == "worm" else two_account_config)(
        auto_create_containers=True, extra_client_kwargs={"retry_total": 0}
    )

    def build() -> AzureBlobBackend:
        return AzureBlobBackend(
            1, cfg, credential=FakeCredential(), clock=clock, transport=fake, sleep=_no_sleep
        )

    # loop_thread: the proxy the real-Azure lane uses (testing.py).
    return LoopThreadBackend(build) if request.param == "loop_thread" else build()


# The fake writes no change feed (test_changefeed.py covers the reader), so
# the feed tests are shadowed here; this does not touch the shared suite.
def test_change_feed_reports_creates_and_deletes() -> None:
    pytest.skip("the fake has no change feed")


def test_restore_events_are_ordered_in_the_change_feed() -> None:
    pytest.skip("the fake has no change feed")
