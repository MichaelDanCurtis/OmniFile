"""Seeding tests: a real Postgres (the retention tests' fixtures) and the S3
backend over the in-memory S3 fake (tests/s3)."""

import pytest
from retention.conftest import connect, db, retention_db_url  # noqa: F401

from omnifile_service.sources import inventory_s3


@pytest.fixture(autouse=True)
def _no_kept_streams():  # type: ignore[no-untyped-def]
    """Each test runs its own event loop; a stream another test left open
    can't be continued (or closed) from this one."""
    inventory_s3._streams.clear()
    yield
    inventory_s3._streams.clear()
