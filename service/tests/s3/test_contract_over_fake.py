"""The shared contract suite, run against the S3 backend over the in-memory
fake. The fake is a test double, so this checks the backend's logic and its
requests through the SDK, not S3's behaviour (that is the real-AWS lane)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from backend_contract.test_contract import *  # noqa: F403
from backend_contract.test_contract_rules import *  # noqa: F403

from omnifile_service.backends.azure.testing import LoopThreadBackend
from omnifile_service.backends.base import StorageBackend
from omnifile_service.backends.s3 import S3Backend


@pytest.fixture(params=["direct", "loop_thread"])
def backend(
    request: pytest.FixtureRequest, make_backend: Callable[..., S3Backend]
) -> StorageBackend:
    # loop_thread: one event loop and one SDK client for every call, as in
    # production; direct: a fresh loop per call (the clients follow the loop).
    return LoopThreadBackend(make_backend) if request.param == "loop_thread" else make_backend()
