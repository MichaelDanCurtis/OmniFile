"""Fixtures for the versioning, WORM, change-feed and reconcile tests: a real
Postgres 17 and sim on a virtual clock.

Set OMNIFILE_TEST_DATABASE_URL to a server where the user may CREATE
DATABASE (see tests/catalog/conftest.py); without it these tests skip.

Time: the catalog's own WORM guards (0003) compare retention with the
database's now(), and sim compares it with the virtual clock. The tests
anchor the virtual clock in the past and move it up to (just short of) the
real now, so both clocks agree on what is retained.
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

from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app

ENV = "OMNIFILE_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def retention_db_url() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set; these tests need a Postgres 16+ server")
    name = f"omnifile_r_{uuid.uuid4().hex[:12]}"
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
def db(retention_db_url: str) -> Iterator[psycopg.Connection]:
    """Autocommit; each test seeds its own backend, dataset and shares, and
    empties the queues it reads."""
    with psycopg.connect(retention_db_url, autocommit=True) as c:
        for q in ("retention", "reconcile", "restores", "notify"):
            c.execute(f"DELETE FROM pgmq.q_{q}")
        yield c


@pytest.fixture
def connect(retention_db_url: str) -> Callable[[], Coroutine[Any, Any, psycopg.AsyncConnection]]:
    async def make() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            as_app(retention_db_url, "worker"), autocommit=True
        )

    return pooled.connect_for(as_app(retention_db_url, "worker"), make)
