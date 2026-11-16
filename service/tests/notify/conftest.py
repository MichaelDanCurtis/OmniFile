"""Fixtures for the notify tests. The database tests need OMNIFILE_TEST_DATABASE_URL (as for
tests/worker); the pure ones (intents, cards, Graph and Bot Connector clients) don't."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pooled
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app

from .support import Fakes, make_fakes

ENV = "OMNIFILE_TEST_DATABASE_URL"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return pooled.run(coro)  # asyncio.run, closing the worker pools it made (tests/pooled.py)


@pytest.fixture(scope="session")
def notify_db_url() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set; notify database tests need a Postgres 16+ server")
    name = f"omnifile_n_{uuid.uuid4().hex[:12]}"
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
def db(notify_db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(notify_db_url, autocommit=True) as c:
        for q in ("notify", "restores", "identity_resolve"):
            c.execute(f"DELETE FROM pgmq.q_{q}")
        yield c


@pytest.fixture
def connect(notify_db_url: str) -> Callable[[], Coroutine[Any, Any, psycopg.AsyncConnection]]:
    async def make() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            as_app(notify_db_url, "worker"), autocommit=True
        )

    return pooled.connect_for(as_app(notify_db_url, "worker"), make)


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return make_fakes(tmp_path)
