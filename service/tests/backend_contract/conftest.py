from __future__ import annotations

import pytest

from omnifile_service.backends.base import StorageBackend

from .clock import ManualClock
from .registry import FACTORIES


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture(params=sorted(FACTORIES), ids=sorted(FACTORIES))
def backend(request: pytest.FixtureRequest, clock: ManualClock) -> StorageBackend:
    return FACTORIES[request.param](clock)
