from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.backends.sim_http import BlobFacade, create_app

from .client import Blob


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def facade(clock: VirtualClock) -> BlobFacade:
    return BlobFacade(SimBackend.azure_two_account(clock))


@pytest.fixture
def client(facade: BlobFacade) -> Iterator[TestClient]:
    with TestClient(create_app(facade, admin=True), client=("127.0.0.1", 50000)) as c:
        yield c


@pytest.fixture
def blob(client: TestClient, facade: BlobFacade) -> Blob:
    return Blob(client, facade, "live", "data", "dir/line_1042.segy")
