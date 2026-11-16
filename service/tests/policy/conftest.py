"""Fixtures for the tiering tests that need Postgres 17 (decision 27).

Set OMNIFILE_TEST_DATABASE_URL to a server where the user may CREATE
DATABASE (see tests/catalog/conftest.py); without it those tests skip. The
pure engine tests (test_engine, test_recall, test_model) never need it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.catalog.migrate import migrate

ENV = "OMNIFILE_TEST_DATABASE_URL"
QUEUES = (
    "restores",
    "edge_instructions",
    "notify",
    "tiering",
    "tiering_dlq",
    "retention",
    "verify",
)


@pytest.fixture(scope="session")
def policy_db_url() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set; tiering tests need a Postgres 16+ server")
    name = f"omnifile_p_{uuid.uuid4().hex[:12]}"
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
def db(policy_db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(policy_db_url, autocommit=True) as c:
        for q in QUEUES:
            c.execute("SELECT pgmq.create(%s::text)", (q,))
            c.execute(f"DELETE FROM pgmq.q_{q}")
        yield c
