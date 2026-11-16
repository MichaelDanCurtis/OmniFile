from __future__ import annotations

import pytest

from omnifile_service.backends.sim import SimBackend, VirtualClock


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def azure(clock: VirtualClock) -> SimBackend:
    """Two-account layout: ZRS live account (Smart default) + LRS archive account."""
    return SimBackend.azure_two_account(clock)


@pytest.fixture
def worm(clock: VirtualClock) -> SimBackend:
    """Single LRS account with versioning and version-level WORM."""
    return SimBackend.azure_worm(clock)


@pytest.fixture
def s3(clock: VirtualClock) -> SimBackend:
    return SimBackend.s3(clock)
