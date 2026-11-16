"""Service metrics (decision 37, metrics.py), read back through an in-memory reader."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from omnifile_service import metrics
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Message, Retry, dlq_name

from .conftest import run

Q = "verify"
FAST = ConsumerConfig(backoff_base=0, max_failures=2, max_reads=50, visibility_timeout=30)


@pytest.fixture
def reader():
    r = InMemoryMetricReader()
    metrics.configure("test", reader=r)
    yield r
    metrics.shutdown()


def points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    out = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def test_configure_refuses_what_it_cannot_do() -> None:
    assert metrics.configure("test", {}) is False
    assert not metrics.enabled()
    with pytest.raises(ValueError, match="one of"):
        metrics.configure("test", {"OMNIFILE_METRICS_EXPORTER": "prometheus"})
    with pytest.raises(ValueError, match="APPLICATIONINSIGHTS_CONNECTION_STRING"):
        metrics.configure("test", {"OMNIFILE_METRICS_EXPORTER": "azuremonitor"})
    # Off: recording is a no-op, never an error.
    metrics.handler_done("q", "ok", 1.0)
    metrics.throttled("s3", "restore_object")
    metrics.tier_held_back(1, 2)
    metrics.tier_own_copy(1, 2, "copied")
    metrics.tier_archive_late(1, 2)
    metrics.tier_forced_archive(1, 2)


def test_a_held_back_tier_move_names_its_dataset_and_file(reader) -> None:
    metrics.tier_held_back(7, 42)
    [point] = points(reader, "omnifile.tier.held_back")
    assert point.value == 1
    assert dict(point.attributes) == {"dataset_id": "7", "file_id": "42"}


def test_a_files_own_copy_names_its_dataset_file_and_outcome(reader) -> None:
    metrics.tier_own_copy(7, 42, "joined")
    [point] = points(reader, "omnifile.tier.own_copy")
    assert point.value == 1
    assert dict(point.attributes) == {"dataset_id": "7", "file_id": "42", "outcome": "joined"}


def test_a_late_archive_names_its_dataset_and_file(reader) -> None:
    metrics.tier_forced_archive(7, 42)
    [forced] = points(reader, "omnifile.tier.forced_archive")
    assert forced.value == 1
    assert forced.attributes == {"dataset_id": "7", "file_id": "42"}
    metrics.tier_archive_late(7, 42)
    [point] = points(reader, "omnifile.tier.archive_late")
    assert point.value == 1
    assert dict(point.attributes) == {"dataset_id": "7", "file_id": "42"}


def test_the_azure_monitor_exporter_is_built_with_a_managed_identity() -> None:
    conn = "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://x.in.applicationinsights.azure.com/"
    try:
        assert metrics.configure(
            "test",
            {
                "OMNIFILE_METRICS_EXPORTER": "azuremonitor",
                "APPLICATIONINSIGHTS_CONNECTION_STRING": conn,
            },
        )
    finally:
        metrics.shutdown()


@pytest.fixture
def clean_queue(db: psycopg.Connection) -> None:
    db.execute(f"DELETE FROM pgmq.q_{Q}")
    db.execute("SELECT pgmq.create(%s)", (dlq_name(Q),))
    db.execute(f"DELETE FROM pgmq.q_{dlq_name(Q)}")


def test_queue_handlers_are_timed_by_outcome(db, connect, reader, clean_queue) -> None:
    for k in ("ok", "retry", "boom"):
        db.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (Q, json.dumps({"k": k})))
    retried = set()

    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        k = msg.body["k"]
        if k == "retry" and k not in retried:
            retried.add(k)
            raise Retry(0)
        if k == "boom":
            raise RuntimeError("down")

    c = Consumer(connect, Q, handler, FAST)
    run(c.drain())
    run(c.close())
    got = {
        p.attributes["outcome"]: p.count
        for p in points(reader, "omnifile.queue.handler.duration")
        if p.attributes["queue"] == Q
    }
    # ok twice (ok, then retry's second run), retry once, boom failed then dead-lettered.
    assert got == {"ok": 2, "retry": 1, "failed": 1, "dead": 1}


def test_requests_are_counted_by_route_template(reader) -> None:
    async def item(request):
        return PlainTextResponse("x", status_code=201)

    app = Starlette(routes=[Route("/v0/items/{item_id}", item, methods=["POST"])])
    app.add_middleware(metrics.RequestMetrics, name="edge_api")
    with TestClient(app) as c:
        c.post("/v0/items/1")
        c.post("/v0/items/2")
        c.get("/nowhere")
    got = {
        (p.attributes["route"], p.attributes["status"]): p.value
        for p in points(reader, "omnifile.api.requests")
    }
    # Never the raw path: ids would make every request its own series.
    assert got == {("/v0/items/{item_id}", 201): 2, ("unmatched", 404): 1}


def test_the_collector_reads_gauges_and_new_completions_once(db, connect, reader) -> None:
    db.execute("DELETE FROM pgmq.q_verify")
    db.execute("SELECT pgmq.send('verify', '{}'::jsonb)")
    now = datetime.now(UTC)
    name = f"metrics-{os.getpid()}"
    db.execute("DELETE FROM catalog.edge WHERE name = %s", (name,))
    db.execute(
        "INSERT INTO catalog.edge (name, state, enrolled_at, cert_sha256, last_heartbeat_at,"
        " guard_state, health) VALUES (%s, 'active', now(), %s, %s, 'down', %s)",
        (
            name,
            os.urandom(32),
            now - timedelta(minutes=20),
            json.dumps(
                {
                    "outbox_depth": 7,
                    "data_free_bytes": 25,
                    "data_capacity_bytes": 100,
                    "recovery_used": 1,
                    "quiet_stale_s": 400,
                }
            ),
        ),
    )
    t = [now]
    col = metrics.Collector(connect, now=lambda: t[0])
    run(col.collect_once())
    q = {p.attributes["queue"]: p.value for p in points(reader, "omnifile.queue.depth")}
    assert q["verify"] >= 1
    assert len(points(reader, "omnifile.tier.archive_overdue")) == 1
    assert len(points(reader, "omnifile.tier.own_copy_stuck")) == 1
    edge = {
        m: {p.attributes["edge"]: p.value for p in points(reader, f"omnifile.edge.{m}")}
        for m in (
            "heartbeat_age",
            "guard_armed",
            "outbox_depth",
            "data_free_ratio",
            "recovery_used",
            "quiet_stale",
        )
    }
    assert 1190 < edge["heartbeat_age"][name] < 1210
    assert edge["guard_armed"][name] == 0 and edge["outbox_depth"][name] == 7
    assert edge["data_free_ratio"][name] == pytest.approx(0.25)
    assert edge["recovery_used"][name] == 1
    assert edge["quiet_stale"][name] == 400
    db.execute("DELETE FROM catalog.edge WHERE name = %s", (name,))


def test_files_refused_too_often_keep_a_gauge_up(connect, reader, monkeypatch) -> None:
    """Review of #81: at the bound the service stops asking for a copy again, so an alert
    must stay up while the file is there (its rows: tests/api/test_annex.py)."""
    from omnifile_service.annex import readback

    monkeypatch.setattr(readback, "AT_BOUND_SQL", "SELECT 'geo-annex', 2 WHERE %s = 3")
    col = metrics.Collector(connect, now=lambda: datetime.now(UTC))
    run(col.collect_once())
    got = {p.attributes["annex"]: p.value for p in points(reader, "omnifile.annex.copies_refused")}
    assert got == {"geo-annex": 2}


def test_a_flagged_filer_keeps_a_gauge_up_until_acknowledged(db, connect, reader) -> None:
    """Decision 64: the alert stays until an ArchiveAdmin acknowledges it."""
    name = f"flagged-{os.getpid()}"
    db.execute("DELETE FROM catalog.filer WHERE name = %s", (name,))
    (nas,) = db.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'nas', '{}', false) RETURNING backend_id",
        (f"nas-{name}",),
    ).fetchone()
    (edge,) = db.execute(
        "INSERT INTO catalog.edge (name, state, enrolled_at, cert_sha256)"
        " VALUES (%s, 'active', now(), %s) RETURNING edge_id",
        (f"edge-{name}", os.urandom(32)),
    ).fetchone()
    db.execute(
        "INSERT INTO catalog.filer (name, host, protocol, export, edge_id, backend_id,"
        " created_by, acls_hidden_since) VALUES (%s, 'nas1', 'nfs3', '/x', %s, %s, 't', now())",
        (name, edge, nas),
    )
    col = metrics.Collector(connect, now=lambda: datetime.now(UTC))
    run(col.collect_once())
    got = {
        p.attributes["filer"]: p.value for p in points(reader, "omnifile.annex.filers_acls_hidden")
    }
    assert got.get(name) == 1
