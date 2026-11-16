"""Fixtures for the worker tests: a real Postgres 17 (decision 27: the
queues are PGMQ in the catalog database) and sim on a virtual clock.

Set OMNIFILE_TEST_DATABASE_URL to a server where the user may CREATE
DATABASE (see tests/catalog/conftest.py); without it these tests skip.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pooled
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker.backends import BackendRegistry

ENV = "OMNIFILE_TEST_DATABASE_URL"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return pooled.run(coro)  # asyncio.run, closing the worker pools it made (tests/pooled.py)


@pytest.fixture(scope="session")
def worker_db_url() -> Iterator[str]:
    """One migrated throwaway database for the worker tests."""
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set; worker tests need a Postgres 16+ server")
    name = f"omnifile_w_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(server)
    params["dbname"] = name
    url = make_conninfo(**params)
    migrate(url)
    yield url
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(worker_db_url: str) -> Iterator[psycopg.Connection]:
    """An autocommit connection; every test seeds its own rows and empties
    the queues it uses first."""
    with psycopg.connect(worker_db_url, autocommit=True) as c:
        for q in ("restores", "edge_instructions", "notify"):
            c.execute(f"DELETE FROM pgmq.q_{q}")
        yield c


@pytest.fixture
def connect(worker_db_url: str) -> Callable[[], Coroutine[Any, Any, psycopg.AsyncConnection]]:
    async def make() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            as_app(worker_db_url, "worker"), autocommit=True
        )

    return pooled.connect_for(as_app(worker_db_url, "worker"), make)


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def sim(clock: VirtualClock) -> SimBackend:
    return SimBackend.azure_two_account(clock, backend_id=0)


@pytest.fixture
def registry() -> BackendRegistry:
    return BackendRegistry()
