"""The `api` app: the M1 subset of the edge control API (control-api.md).

Served here (paths below /v0/edges/{edge_id}):
  POST certificate, aws-certificate, heartbeat, feed/ack, changes, uploads,
       uploads/{upload_id}/complete, uploads/{upload_id}/abort, credentials,
       files/{file_id}/stub/{prepare,commit,skip}, files/{file_id}/filled, recalls,
       recalls/{recall_id}/filled,
       recalls/{recall_id}/failed, reads, acls, acl-scans, acl-scans/{scan_id}/complete,
       snapshot-exports, audit, annex-events, annex-dry-runs/{run_id},
       annex-removal-tickets, annex-removals/{removal_id}, annex-placement-tickets,
       annex-placements/{placement_id}
  GET  feed, snapshot, snapshot-exports/{export_id}, digest, annexes,
       annexes/{annex_id}/records, annex-dry-runs
And, for the Teams relay only (relay certificate): POST /internal/v1/teams/intents.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import metrics
from . import acls, annex, annex_placements, asof, changes, data, feed, ops, recalls, stubs, teams
from .context import Services, edge_endpoint
from .problems import ApiError

E = "/v0/edges/{edge_id}"

# (method, path, handler, gated by min_edge_version)
ROUTES = [
    ("POST", f"{E}/certificate", ops.renew_certificate, False),
    ("POST", f"{E}/aws-certificate", ops.roles_anywhere_certificate, True),
    ("POST", f"{E}/heartbeat", ops.heartbeat, False),
    ("GET", f"{E}/feed", feed.get_feed, False),
    ("POST", f"{E}/feed/ack", feed.ack_feed, False),
    ("GET", f"{E}/snapshot", feed.get_snapshot, True),
    ("GET", f"{E}/digest", feed.get_digest, True),
    ("POST", f"{E}/snapshot-exports", feed.start_snapshot_export, True),
    ("GET", f"{E}/snapshot-exports/{{export_id}}", feed.get_snapshot_export, True),
    ("GET", f"{E}/asof", asof.get_asof, True),
    ("POST", f"{E}/changes", changes.send_changes, True),
    ("POST", f"{E}/uploads", data.start_upload, True),
    ("POST", f"{E}/uploads/{{upload_id}}/complete", data.complete_upload, True),
    ("POST", f"{E}/uploads/{{upload_id}}/abort", data.abort_upload, True),
    ("POST", f"{E}/credentials", data.mint_credential, True),
    ("POST", f"{E}/files/{{file_id}}/stub/prepare", stubs.prepare_stub, True),
    ("POST", f"{E}/files/{{file_id}}/stub/commit", stubs.commit_stub, True),
    ("POST", f"{E}/files/{{file_id}}/stub/skip", stubs.skip_stub, True),
    ("POST", f"{E}/files/{{file_id}}/filled", stubs.fill_done, True),
    ("POST", f"{E}/recalls", recalls.request_read, True),
    ("POST", f"{E}/recalls/{{recall_id}}/filled", recalls.recall_filled, True),
    ("POST", f"{E}/recalls/{{recall_id}}/failed", recalls.recall_failed, True),
    ("POST", f"{E}/reads", ops.report_reads, True),
    ("POST", f"{E}/audit", ops.send_audit, True),
    ("POST", f"{E}/acls", acls.put_acls, True),
    ("POST", f"{E}/acl-scans", acls.start_scan, True),
    ("POST", f"{E}/acl-scans/{{scan_id}}/complete", acls.complete_scan, True),
    ("GET", f"{E}/annexes", annex.list_annexes, True),
    ("POST", f"{E}/annex-events", annex.post_events, True),
    ("GET", f"{E}/annexes/{{annex_id}}/records", annex.get_records, True),
    ("GET", f"{E}/annex-dry-runs", annex.list_dry_runs, True),
    ("POST", f"{E}/annex-dry-runs/{{run_id}}", annex.post_dry_run, True),
    ("POST", f"{E}/annex-removal-tickets", annex.post_removal_tickets, True),
    ("POST", f"{E}/annex-removals/{{removal_id}}", annex.post_removal_step, True),
    ("POST", f"{E}/annex-placement-tickets", annex_placements.post_tickets, True),
    ("POST", f"{E}/annex-placements/{{placement_id}}", annex_placements.post_step, True),
]


async def healthz(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    if exc.status_code == 404:
        return ApiError("not_found", "no such endpoint").response()
    if exc.status_code == 405:
        return ApiError("invalid_request", "method not allowed").response()
    return ApiError("internal", exc.detail).response()


def create_app(services: Services, *, manage_lifecycle: bool = True) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        if manage_lifecycle:
            await services.catalog.open()
        try:
            yield
        finally:
            if manage_lifecycle:
                await services.planes.aclose()
                close = getattr(services.signer, "aclose", None)
                if close is not None:
                    await close()
                for closer in services.closers:
                    await closer()
                await services.catalog.close()

    routes = [Route("/healthz", healthz, methods=["GET"])]
    routes.append(Route(teams.PATH, teams.consume_intent, methods=["POST"]))
    routes += [
        Route(path, edge_endpoint(handler, gate_version=gated), methods=[method])
        for method, path, handler, gated in ROUTES
    ]
    app = Starlette(
        routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error}
    )
    app.add_middleware(metrics.RequestMetrics, name="edge_api")
    app.state.svc = services
    return app
