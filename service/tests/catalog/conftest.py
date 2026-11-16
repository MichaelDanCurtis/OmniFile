"""Fixtures for tests that need a real Postgres.

Set OMNIFILE_TEST_DATABASE_URL to a server where the user may CREATE DATABASE,
for example:

    docker run -d --rm --name omnifile-pg -e POSTGRES_PASSWORD=omnifile \\
        -p 127.0.0.1:55432:5432 postgres:17
    export OMNIFILE_TEST_DATABASE_URL=postgresql://postgres:omnifile@127.0.0.1:55432/postgres

Each test session gets its own throwaway databases, dropped at the end.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.catalog.migrate import migrate

ENV = "OMNIFILE_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def server_url() -> str:
    url = os.environ.get(ENV)
    if not url:
        pytest.skip(f"{ENV} is not set; catalog tests need a Postgres 16+ server")
    return url


@pytest.fixture(scope="session")
def make_database(server_url: str) -> Iterator[Callable[[], str]]:
    """Factory: create an empty database and return its URL."""
    created: list[str] = []

    def make() -> str:
        name = f"omnifile_t_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(server_url, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        created.append(name)
        params = conninfo_to_dict(server_url)
        params["dbname"] = name
        return make_conninfo(**params)

    yield make
    with psycopg.connect(server_url, autocommit=True) as conn:
        for name in created:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migrated_url(make_database: Callable[[], str]) -> str:
    """One migrated database shared by the schema tests. Tests create their
    own rows (fresh datasets), so they don't interfere."""
    url = make_database()
    migrate(url)
    return url


@pytest.fixture
def conn(migrated_url: str) -> Iterator[psycopg.Connection]:
    """A connection whose work is rolled back after the test."""
    with psycopg.connect(migrated_url) as c:
        yield c
        c.rollback()
