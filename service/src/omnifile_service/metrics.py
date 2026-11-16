"""Service metrics (decision 37; docs/design/observability.md section 1).

Every instrument lives here, written against the vendor-neutral OpenTelemetry
API. Where they go is configuration (``OMNIFILE_METRICS_EXPORTER``):

- ``azuremonitor``: straight to Application Insights with the Azure Monitor
  exporter, authenticated with the app's managed identity
  (``APPLICATIONINSIGHTS_CONNECTION_STRING``; ``AZURE_CLIENT_ID`` picks a
  user-assigned identity). The Container Apps managed OpenTelemetry agent can't
  carry metrics to Application Insights (it accepts only logs and traces
  there), so metrics don't go through it. Over private networking the
  ingestion endpoint is reached through the Azure Monitor Private Link Scope
  the Bicep creates.
- ``otlp``: OTLP over gRPC to the managed agent (``OTEL_EXPORTER_OTLP_ENDPOINT``,
  which Container Apps injects), for a customer whose metrics destination is an
  OTLP endpoint of their own. Needs the ``otlp`` extra.
- unset or ``none``: off. Every call below is then a no-op.

Instruments:

- the worker's queue handlers: duration by queue and outcome;
- the edge and portal APIs: requests and their duration by route template and
  status;
- gauges read from the catalog by :class:`Collector` (worker only): queue
  depth and oldest message age, change-feed lag, migration prefixes by state,
  restores open by trigger, and each edge's heartbeat (age, guard, outbox,
  free space);
- recalls (requested to available) and uploads (committed to verified), as
  histograms of the ones that finished since the last collection;
- provider throttling (S3 ``SlowDown`` and its family);
- tier moves held back on a shared blob, at most one a day per blob, by file.

No global provider is installed: this module keeps its own, so tests can swap
in an in-memory reader.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, NoOpMeterProvider, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from .catalog.restricted import UNANSWERED_SHARES
from .policy.overdue import ARCHIVE_OVERDUE_SQL, OWN_COPY_STUCK_SQL
from .usage.share_map import age_s as share_map_age_s

log = logging.getLogger(__name__)

EXPORTERS = ("none", "azuremonitor", "otlp")
DEFAULT_INTERVAL_S = 60


@dataclass
class Snapshot:
    """What the collector read last; the gauges report it."""

    queues: dict[str, tuple[int, int]] = field(default_factory=dict)  # depth, oldest age
    changefeed_lag: dict[str, float] = field(default_factory=dict)  # container -> seconds
    prefixes: dict[str, int] = field(default_factory=dict)
    staging_left: int = 0
    restores_open: dict[str, int] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_partition_rows: dict[str, int] = field(default_factory=dict)  # parent -> sampled
    #: Host-trusted shares over restricted folders, no override (threat model T7).
    shares_restricted: dict[str, int] = field(default_factory=dict)  # share name -> 1
    #: Decision 64: filer name -> 1 while its NAS shows no ACLs, unacknowledged.
    filers_acls_hidden: dict[str, int] = field(default_factory=dict)
    #: Annex name -> files whose copy was refused `MAX_REFUSALS` times in a row, or whose
    #: copying an ArchiveAdmin stopped, and that nobody acknowledged (readback.py).
    annex_copies_refused: dict[str, int] = field(default_factory=dict)
    #: Roles Anywhere trust anchor -> seconds its CRL upload has failed (0: fine).
    crl_failing: dict[str, float] = field(default_factory=dict)
    #: Files past the end of their archive date and not in Archive (`policy.overdue`).
    archive_overdue: int = 0
    #: Seconds since the oldest change the share map's roll-up hasn't counted yet.
    share_map_age_s: float = 0.0
    #: Blobs with a file waiting for its own copy in the last day (`policy.overdue`).
    own_copy_stuck: int = 0


class _Instruments:
    def __init__(self, meter: Meter, snapshot: Callable[[], Snapshot]) -> None:
        self.handler = meter.create_histogram(
            "omnifile.queue.handler.duration", unit="s", description="Queue handler run time"
        )
        self.requests = meter.create_counter(
            "omnifile.api.requests", description="API requests by route and status"
        )
        self.request_duration = meter.create_histogram(
            "omnifile.api.request.duration", unit="s", description="API request time"
        )
        self.recall = meter.create_histogram(
            "omnifile.recall.duration", unit="s", description="Recall: requested to available"
        )
        self.upload = meter.create_histogram(
            "omnifile.upload.verify_delay", unit="s", description="Upload: committed to verified"
        )
        self.throttled = meter.create_counter(
            "omnifile.backend.throttled", description="Provider throttling answers (SlowDown, 503)"
        )
        self.tier_held_back = meter.create_counter(
            "omnifile.tier.held_back",
            description="A warmer tier move held back on a blob with other references",
        )
        self.tier_forced_archive = meter.create_counter(
            "omnifile.tier.forced_archive",
            description="A file went to Archive before its date, for another file's date",
        )
        self.tier_archive_late = meter.create_counter(
            "omnifile.tier.archive_late",
            description="A file reached Archive after its archive date",
        )
        self.tier_own_copy = meter.create_counter(
            "omnifile.tier.own_copy",
            description="A file that wanted a different tier got its own blob (decision 66)",
        )

        def gauge(name: str, unit: str, desc: str, read: Callable[[Snapshot], Iterable]):
            def cb(_: CallbackOptions) -> Iterable[Observation]:
                return [Observation(v, a) for v, a in read(snapshot())]

            meter.create_observable_gauge(name, [cb], unit=unit, description=desc)

        gauge(
            "omnifile.queue.depth",
            "{message}",
            "Messages in a queue (dead-letter queues end in _dlq)",
            lambda s: [(d, {"queue": q}) for q, (d, _) in s.queues.items()],
        )
        gauge(
            "omnifile.queue.oldest_age",
            "s",
            "Age of a queue's oldest message",
            lambda s: [(a, {"queue": q}) for q, (_, a) in s.queues.items()],
        )
        gauge(
            "omnifile.changefeed.lag",
            "s",
            "Blob change feed: latest max lag per container",
            lambda s: [(v, {"container": c}) for c, v in s.changefeed_lag.items()],
        )
        gauge(
            "omnifile.migration.prefixes",
            "{prefix}",
            "Migration prefixes by state",
            lambda s: [(n, {"state": st}) for st, n in s.prefixes.items()],
        )
        gauge(
            "omnifile.migration.staging_left",
            "{prefix}",
            "Prefixes cut over in the last week with staging objects they could not delete",
            lambda s: [(s.staging_left, {})],
        )
        gauge(
            "omnifile.catalog.default_partition_rows",
            "{row}",
            "Rows in a DEFAULT time partition (sampled up to 1,000): a partition was missing",
            lambda s: [(n, {"table": t}) for t, n in s.default_partition_rows.items()],
        )
        gauge(
            "omnifile.share_map.age",
            "s",
            "Seconds since the oldest change the share map's roll-up hasn't counted yet",
            lambda s: [(s.share_map_age_s, {})],
        )
        gauge(
            "omnifile.tier.archive_overdue",
            "{file}",
            "Files past the end of their archive date and not yet in Archive",
            lambda s: [(s.archive_overdue, {})],
        )
        gauge(
            "omnifile.tier.own_copy_stuck",
            "{report}",
            "Files waiting for their own copy of a shared blob, reported in the last day",
            lambda s: [(s.own_copy_stuck, {})],
        )
        gauge(
            "omnifile.shares.host_trusted_restricted",
            "{share}",
            "A sec=sys share over folders restricted to AD groups, with no override recorded",
            lambda s: [(n, {"share": name}) for name, n in s.shares_restricted.items()],
        )
        gauge(
            "omnifile.annex.filers_acls_hidden",
            "{filer}",
            "Filers whose NAS shows no ACLs, and no ArchiveAdmin has acknowledged it",
            lambda s: [(n, {"filer": f}) for f, n in s.filers_acls_hidden.items()],
        )
        gauge(
            "omnifile.annex.copies_refused",
            "{file}",
            "Annexed files whose copy Azure's read-back refused too many times in a row",
            lambda s: [(n, {"annex": a}) for a, n in s.annex_copies_refused.items()],
        )
        gauge(
            "omnifile.roles_anywhere.crl_failing",
            "s",
            "Seconds the edge CA's CRL upload to a Roles Anywhere trust anchor has failed",
            lambda s: [(v, {"trust_anchor": a}) for a, v in s.crl_failing.items()],
        )
        gauge(
            "omnifile.restores.open",
            "{restore}",
            "Restores not yet completed, by trigger",
            lambda s: [(n, {"trigger": t}) for t, n in s.restores_open.items()],
        )
        for key, name, unit, desc in (
            (
                "heartbeat_age_s",
                "omnifile.edge.heartbeat_age",
                "s",
                "Seconds since an edge's heartbeat",
            ),
            ("guard_armed", "omnifile.edge.guard_armed", "1", "1 if the edge's smb-guard is armed"),
            ("outbox_depth", "omnifile.edge.outbox_depth", "{op}", "Edge ops not yet acknowledged"),
            (
                "data_free_ratio",
                "omnifile.edge.data_free_ratio",
                "1",
                "Free share of the data volume",
            ),
            (
                "recovery_used",
                "omnifile.edge.recovery_used",
                "1",
                "1 while the break-glass account was used and not sealed again",
            ),
            (
                "quiet_stale_s",
                "omnifile.edge.quiet_stale",
                "s",
                "Seconds the edge has failed to read its shares' quiet periods",
            ),
            (
                "recalls_waiting",
                "omnifile.edge.recalls_waiting",
                "{recall}",
                "Recalls readers wait on",
            ),
        ):
            gauge(
                name,
                unit,
                desc,
                lambda s, k=key: [
                    (e[k], {"edge": n}) for n, e in s.edges.items() if e.get(k) is not None
                ],
            )


_provider: MeterProvider | None = None
_instruments: _Instruments | None = None
_snapshot = Snapshot()


def _set_snapshot(s: Snapshot) -> None:
    global _snapshot
    _snapshot = s


def configure(
    service: str,
    env: dict[str, str] | None = None,
    *,
    reader: MetricReader | None = None,
) -> bool:
    """Set up metrics for this process (``service``: api, web or worker).
    ``reader``: tests pass an in-memory reader instead of an exporter.
    Returns whether metrics are on. Raises ``ValueError`` on bad settings."""
    global _provider, _instruments
    env = dict(os.environ) if env is None else env
    if reader is None:
        kind = (env.get("OMNIFILE_METRICS_EXPORTER") or "none").strip().lower()
        if kind not in EXPORTERS:
            raise ValueError(f"OMNIFILE_METRICS_EXPORTER={kind!r}: one of {', '.join(EXPORTERS)}")
        if kind == "none":
            shutdown()
            return False
        interval = float(env.get("OMNIFILE_METRICS_INTERVAL_S") or DEFAULT_INTERVAL_S)
        reader = PeriodicExportingMetricReader(
            _exporter(kind, env), export_interval_millis=interval * 1000
        )
    shutdown()
    resource = Resource.create({"service.name": f"omnifile-{service}"})
    _provider = MeterProvider(resource=resource, metric_readers=[reader])
    _instruments = _Instruments(_provider.get_meter("omnifile"), lambda: _snapshot)
    return True


def _exporter(kind: str, env: dict[str, str]):
    if kind == "azuremonitor":
        from azure.identity import ManagedIdentityCredential
        from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter

        conn = env.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        if not conn:
            raise ValueError(
                "OMNIFILE_METRICS_EXPORTER=azuremonitor needs APPLICATIONINSIGHTS_CONNECTION_STRING"
            )
        cred = ManagedIdentityCredential(client_id=env.get("AZURE_CLIENT_ID") or None)
        return AzureMonitorMetricExporter(connection_string=conn, credential=cred)
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    except ModuleNotFoundError:
        raise ValueError(
            "OMNIFILE_METRICS_EXPORTER=otlp needs the otlp extra (omnifile-service[otlp])"
        ) from None
    if not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        raise ValueError("OMNIFILE_METRICS_EXPORTER=otlp needs OTEL_EXPORTER_OTLP_ENDPOINT")
    return OTLPMetricExporter()


def shutdown() -> None:
    """Flush and stop (process exit, or before reconfiguring)."""
    global _provider, _instruments
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as e:  # noqa: BLE001 - never fail a shutdown over metrics
            log.warning("metrics shutdown: %s", e)
    _provider = None
    _instruments = None


def enabled() -> bool:
    return _instruments is not None


# ------------------------------------------------------------------ recording


def handler_done(queue: str, outcome: str, seconds: float) -> None:
    """outcome: ok, retry, failed (to be retried) or dead (dead-lettered)."""
    if _instruments is not None:
        _instruments.handler.record(seconds, {"queue": queue, "outcome": outcome})


def request_done(app: str, route: str, method: str, status: int, seconds: float) -> None:
    if _instruments is not None:
        a = {"app": app, "route": route, "method": method, "status": status}
        _instruments.requests.add(1, a)
        _instruments.request_duration.record(seconds, a)


def throttled(provider: str, what: str) -> None:
    if _instruments is not None:
        _instruments.throttled.add(1, {"provider": provider, "operation": what})


def tier_held_back(dataset_id: int, file_id: int) -> None:
    if _instruments is not None:
        _instruments.tier_held_back.add(1, {"dataset_id": str(dataset_id), "file_id": str(file_id)})


def tier_own_copy(dataset_id: int, file_id: int, outcome: str) -> None:
    """`outcome`: `copied` (one more blob), `joined` (a copy already there), or
    `failed` (the move fell back to the blob everyone shares)."""
    if _instruments is not None:
        _instruments.tier_own_copy.add(
            1, {"dataset_id": str(dataset_id), "file_id": str(file_id), "outcome": outcome}
        )


def tier_forced_archive(dataset_id: int, file_id: int) -> None:
    if _instruments is not None:
        _instruments.tier_forced_archive.add(
            1, {"dataset_id": str(dataset_id), "file_id": str(file_id)}
        )


def tier_archive_late(dataset_id: int, file_id: int) -> None:
    if _instruments is not None:
        _instruments.tier_archive_late.add(
            1, {"dataset_id": str(dataset_id), "file_id": str(file_id)}
        )


class RequestMetrics:
    """ASGI middleware: one count and one duration per HTTP request, by route
    template (``/v0/edges/{edge_id}/feed``, never the raw path)."""

    def __init__(self, app, name: str) -> None:
        self.app = app
        self.name = name

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or _instruments is None:
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        status = 500

        async def send_wrapper(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            request_done(
                self.name, template, scope.get("method", ""), status, time.monotonic() - start
            )


# ------------------------------------------------------------------ collector


class Collector:
    """Reads the gauges from the catalog every ``interval`` (worker only, so
    one process does it). Each pass also records the recalls and uploads that
    finished since the last pass."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[Any]],
        *,
        interval: timedelta = timedelta(seconds=DEFAULT_INTERVAL_S),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.connect = connect
        self.interval = interval
        self.now = now
        self.since: datetime | None = None

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.collect_once()
            except Exception as e:  # noqa: BLE001 - metrics never stop the worker
                log.warning("metrics collection failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval.total_seconds())
            except TimeoutError:
                pass

    async def collect_once(self) -> Snapshot:
        conn = await self.connect()
        try:
            snap, finished = await self._read(conn)
        finally:
            await conn.close()
        _set_snapshot(snap)
        if _instruments is not None:
            for secs, attrs in finished["recalls"]:
                _instruments.recall.record(secs, attrs)
            for secs, attrs in finished["uploads"]:
                _instruments.upload.record(secs, attrs)
        return snap

    async def _read(self, conn) -> tuple[Snapshot, dict[str, list]]:
        now = self.now()
        since = self.since or now
        s = Snapshot()
        rows = await (
            await conn.execute(
                "SELECT m.queue_name, m.queue_length, coalesce(m.oldest_msg_age_sec, 0)"
                "  FROM pgmq.list_queues() q, LATERAL pgmq.metrics(q.queue_name) m"
            )
        ).fetchall()
        s.queues = {r[0]: (int(r[1]), int(r[2])) for r in rows}
        rows = await (
            await conn.execute(
                "SELECT DISTINCT ON (backend_id, container) container, max_lag_seconds"
                "  FROM catalog.changefeed_lag WHERE at > %s - interval '1 hour'"
                " ORDER BY backend_id, container, at DESC",
                (now,),
            )
        ).fetchall()
        s.changefeed_lag = {r[0]: float(r[1]) for r in rows}
        rows = await (
            await conn.execute(
                "SELECT state, count(*) FROM catalog.migration_prefix GROUP BY state"
            )
        ).fetchall()
        s.prefixes = {r[0]: int(r[1]) for r in rows}
        from .migrate.staging import LEFT_BEHIND  # it imports the queue, which imports this

        row = await (
            await conn.execute(
                "SELECT count(*) FROM catalog.migration_prefix WHERE state = 'cut_over'"
                " AND cut_over_at > %s - interval '7 days' AND strpos(last_error, %s) > 0",
                (now, LEFT_BEHIND),
            )
        ).fetchone()
        s.staging_left = int(row[0]) if row else 0
        rows = await (
            await conn.execute(
                "SELECT triggered_by, count(*) FROM catalog.restore"
                " WHERE state IN ('requested', 'in_progress') GROUP BY triggered_by"
            )
        ).fetchall()
        s.restores_open = {r[0]: int(r[1]) for r in rows}
        rows = await (
            await conn.execute(
                "SELECT parent::text, sampled_rows FROM catalog.default_partition_rows()"
            )
        ).fetchall()
        s.default_partition_rows = {r[0]: int(r[1]) for r in rows}
        row = await (
            await conn.execute(ARCHIVE_OVERDUE_SQL, {"now": now, "datasets": None})
        ).fetchone()
        s.archive_overdue = int(row[0]) if row else 0
        s.share_map_age_s = await share_map_age_s(conn, now)
        # The wait is a day, so a report just over a day old means the next is on its way.
        since = now - timedelta(hours=25)
        row = await (await conn.execute(OWN_COPY_STUCK_SQL, {"since": since})).fetchone()
        s.own_copy_stuck = int(row[0]) if row else 0
        rows = await (await conn.execute(UNANSWERED_SHARES, ())).fetchall()
        s.shares_restricted = {r[1]: 1 for r in rows}
        from .annex.readback import AT_BOUND_SQL, MAX_REFUSALS

        rows = await (
            await conn.execute(
                "SELECT name FROM catalog.filer"
                " WHERE acls_hidden_since IS NOT NULL AND acls_hidden_ack_at IS NULL"
            )
        ).fetchall()
        s.filers_acls_hidden = {r[0]: 1 for r in rows}
        rows = await (await conn.execute(AT_BOUND_SQL, (MAX_REFUSALS,))).fetchall()
        s.annex_copies_refused = {r[0]: int(r[1]) for r in rows}
        rows = await (
            await conn.execute(
                "SELECT name, extract(epoch FROM %s - last_heartbeat_at), guard_state, health"
                "  FROM catalog.edge WHERE state = 'active'",
                (now,),
            )
        ).fetchall()
        for name, age, guard, h in rows:
            h = h or {}
            free, cap = h.get("data_free_bytes"), h.get("data_capacity_bytes")
            s.edges[name] = {
                "heartbeat_age_s": None if age is None else float(age),
                "guard_armed": None if guard is None else int(guard == "armed"),
                "outbox_depth": h.get("outbox_depth"),
                "data_free_ratio": free / cap if free is not None and cap else None,
                "recalls_waiting": h.get("recalls_waiting"),
                "recovery_used": h.get("recovery_used"),
                "quiet_stale_s": h.get("quiet_stale_s"),
            }
        # Decision 50: how long each trust anchor's CRL upload has failed.
        rows = await (
            await conn.execute(
                "SELECT trust_anchor_arn, extract(epoch FROM %s - failing_since)"
                "  FROM catalog.roles_anywhere_crl",
                (now,),
            )
        ).fetchall()
        s.crl_failing = {r[0]: max(0.0, float(r[1])) if r[1] is not None else 0.0 for r in rows}
        rows = await (
            await conn.execute(
                "SELECT extract(epoch FROM r.completed_at - r.requested_at), r.priority,"
                "       coalesce(b.kind, 'unknown')"
                "  FROM catalog.recall r"
                "  JOIN catalog.file f USING (file_id)"
                "  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id"
                "  LEFT JOIN catalog.backend b ON b.backend_id = l.backend_id"
                " WHERE r.state = 'available' AND r.completed_at > %s AND r.completed_at <= %s",
                (since, now),
            )
        ).fetchall()
        recalls = [(float(r[0]), {"priority": r[1], "backend": r[2]}) for r in rows]
        rows = await (
            await conn.execute(
                "SELECT extract(epoch FROM l.verified_at - v.committed_at), b.kind"
                "  FROM catalog.location l"
                "  JOIN catalog.file_version v USING (version_id)"
                "  JOIN catalog.backend b ON b.backend_id = l.backend_id"
                " WHERE l.role = 'primary' AND l.verified_at > %s AND l.verified_at <= %s"
                "   AND v.committed_at IS NOT NULL",
                (since, now),
            )
        ).fetchall()
        uploads = [(max(0.0, float(r[0])), {"backend": r[1]}) for r in rows]
        self.since = now
        return s, {"recalls": recalls, "uploads": uploads}


__all__ = [
    "Collector",
    "NoOpMeterProvider",
    "RequestMetrics",
    "Snapshot",
    "configure",
    "enabled",
    "handler_done",
    "request_done",
    "shutdown",
    "throttled",
    "tier_archive_late",
    "tier_forced_archive",
    "tier_held_back",
    "tier_own_copy",
]
