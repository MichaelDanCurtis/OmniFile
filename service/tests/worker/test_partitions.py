"""The worker keeps the feed's and the audit log's time partitions
(worker/partition_jobs.py; migration 0003 said it did, and nothing did).

Each test gets its own database: dropping feed partitions moves the feed's
retention horizon, which would reset every other test's cursors."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service import metrics
from omnifile_service.catalog.migrate import migrate
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.partition_jobs import (
    DEFAULT_FEED_RETENTION_DAYS,
    feed_retention_days,
    maintain_partitions,
)
from omnifile_service.worker.runner import Worker

from .conftest import ENV, run


@pytest.fixture
def url() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set")
    name = f"omnifile_part_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(server)
    params["dbname"] = name
    u = make_conninfo(**params)
    migrate(u)
    yield u
    with psycopg.connect(server, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _pass(url: str, days: int = DEFAULT_FEED_RETENTION_DAYS) -> int:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        return await maintain_partitions(conn, days)
    finally:
        await conn.close()


def _covers(c: psycopg.Connection, parent: str, ahead: str) -> bool:
    row = c.execute(
        "SELECT count(*) FROM catalog.time_partition WHERE parent = %s::regclass"
        " AND from_ts <= now() + %s::interval AND to_ts > now() + %s::interval",
        (parent, ahead, ahead),
    ).fetchone()
    return bool(row and row[0] == 1)


def test_partitions_are_kept_ahead_and_old_feed_partitions_dropped(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        # Months later: the feed's partitions ran out (as if nothing ran since).
        c.execute("SELECT catalog.feed_drop_before(now() + interval '400 days')")
        assert not _covers(c, "catalog.feed_event", "0 days")
        # And an old one, past retention, still there. Made by the catalog's
        # owner (the migrator, tests/conftest.py), as every partition is.
        c.execute("SET ROLE omnifile_migrator")
        c.execute(
            "CREATE TABLE catalog.feed_event_p20250106 PARTITION OF catalog.feed_event"
            " FOR VALUES FROM ('2025-01-06') TO ('2025-01-13')"
        )
        c.execute(
            "INSERT INTO catalog.time_partition (parent, partition, from_ts, to_ts) VALUES"
            " ('catalog.feed_event', 'catalog.feed_event_p20250106', '2025-01-06', '2025-01-13')"
        )
    assert run(_pass(url)) > 0
    with psycopg.connect(url, autocommit=True) as c:
        assert _covers(c, "catalog.feed_event", "0 days")
        assert _covers(c, "catalog.feed_event", "7 weeks")
        assert _covers(c, "catalog.audit_log", "5 months")
        assert c.execute("SELECT to_regclass('catalog.feed_event_p20250106')").fetchone()[0] is None
        assert c.execute("SELECT count(*) FROM catalog.default_partition_rows()").fetchone()[0] == 0
    # A second pass changes nothing.
    assert run(_pass(url)) == 0


def test_rows_in_a_default_partition_are_reported(url: str, caplog) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(
            "INSERT INTO catalog.audit_log (at, actor, action, decision)"
            " VALUES (now() + interval '3 years', 'test', 'x', 'info')"
        )
    with caplog.at_level("ERROR"):
        run(_pass(url))
    assert any(
        "catalog.audit_log has rows in its DEFAULT partition" in r.message for r in caplog.records
    )

    # And the collector reports them, for the alert.
    reader = InMemoryMetricReader()
    metrics.configure("test", reader=reader)
    try:

        async def connect() -> psycopg.AsyncConnection:
            return await psycopg.AsyncConnection.connect(url, autocommit=True)

        run(metrics.Collector(connect).collect_once())
        data = reader.get_metrics_data()
        got = {
            p.attributes["table"]: p.value
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "omnifile.catalog.default_partition_rows"
            for p in m.data.data_points
        }
        assert got == {"catalog.audit_log": 1}
    finally:
        metrics.shutdown()


def test_the_worker_runs_it_daily_and_retention_has_a_floor() -> None:
    w = Worker(connect=None, registry=BackendRegistry())  # type: ignore[arg-type]
    assert "partitions" in w.sweeps
    assert w.sweeps["partitions"].every.total_seconds() == 86400
    assert feed_retention_days({}) == 35
    assert feed_retention_days({"OMNIFILE_FEED_RETENTION_DAYS": "14"}) == 14
    with pytest.raises(ValueError, match="at least 7"):
        feed_retention_days({"OMNIFILE_FEED_RETENTION_DAYS": "3"})
