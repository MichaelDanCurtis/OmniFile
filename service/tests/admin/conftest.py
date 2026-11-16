"""Fixtures for the admin commands: a fresh migrated catalog per test on the
shared Postgres (OMNIFILE_TEST_DATABASE_URL, as tests/catalog/conftest.py
describes), and a runner for `python -m omnifile_service.admin`."""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.admin.cli import main
from omnifile_service.catalog.migrate import migrate

ENV = "OMNIFILE_TEST_DATABASE_URL"


@dataclass
class Ran:
    code: int
    out: str
    err: str


Run = Callable[..., Ran]


def run_cli(*argv: str, env: Mapping[str, str]) -> Ran:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), env=env, out=out, err=err)
    return Ran(code, out.getvalue(), err.getvalue())


@pytest.fixture(scope="session")
def server_url() -> str:
    url = os.environ.get(ENV)
    if not url:
        pytest.skip(f"{ENV} is not set; the admin tests need a Postgres 16+ server")
    return url


@pytest.fixture(scope="session")
def make_database(server_url: str) -> Iterator[Callable[[], str]]:
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


@pytest.fixture
def catalog(make_database: Callable[[], str]) -> str:
    """A freshly migrated catalog: its URL, as its administrator (the owner)."""
    url = make_database()
    migrate(url)
    return url


@pytest.fixture
def run(catalog: str) -> Run:
    """Runs the CLI against `catalog`, with extra environment."""

    def go(*argv: str, **env: str) -> Ran:
        return run_cli(*argv, env={"OMNIFILE_DATABASE_URL": catalog, **env})

    return go
