"""The `web` app: every endpoint of docs/contracts/portal-api.md section 10.

`ROUTES` is the route table; tests/test_portal_api_contract.py checks it
against service/openapi/portal-api.yaml. Every M5 partner route of section 11
is routed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .. import metrics
from ..api.problems import ApiError
from . import (
    live,
    routes_activity,
    routes_admin,
    routes_annex,
    routes_annex_removals,
    routes_annex_returns,
    routes_annex_rules,
    routes_browse,
    routes_cloud,
    routes_console,
    routes_edges,
    routes_locations,
    routes_me,
    routes_migrations,
    routes_partner,
    routes_plans,
    routes_policies,
    routes_provision,
    routes_recall_batches,
    routes_recalls,
    routes_rewind,
    routes_settings,
    routes_settings_clients,
    routes_shares,
    routes_sources,
    routes_tiers,
    routes_visual,
)
from .auth import auth_route
from .context import WebServices, client_addr, endpoint
from .static import SecurityHeaders, Spa


@dataclass(frozen=True)
class R:
    method: str
    path: str
    handler: Any
    opts: dict[str, Any] = field(default_factory=dict)


S = {"bearer": False}
SB = {"bearer": True}
PREVIEW = {"preview": True}

ROUTES: list[R] = [
    R("GET", "/auth/session", routes_me.get_session, {"needs_role": False}),
    R("POST", "/auth/logout", routes_me.logout, {"needs_role": False, "idempotent": False}),
    R("GET", "/v1/me", routes_me.get_me, {"needs_role": False}),
    R("GET", "/v1/directory/groups", routes_me.search_groups),
    R("GET", "/v1/shares", routes_shares.list_shares),
    R("GET", "/v1/shares/{share_id}", routes_shares.get_share),
    R("PATCH", "/v1/shares/{share_id}/settings", routes_shares.patch_settings),
    R("POST", "/v1/shares/{share_id}/settings/preview", routes_shares.preview_settings, PREVIEW),
    R("PUT", "/v1/shares/{share_id}/access", routes_shares.put_access),
    R("POST", "/v1/shares/{share_id}/worm/enable", routes_shares.worm_enable),
    R("POST", "/v1/shares/{share_id}/worm/cancel", routes_shares.worm_cancel),
    R("POST", "/v1/shares/{share_id}/worm/extend", routes_shares.worm_extend),
    R("GET", "/v1/dirs/{dir_id}/children", routes_browse.list_children),
    R("GET", "/v1/files/{file_id}", routes_browse.get_file),
    R("GET", "/v1/files/{file_id}/versions", routes_browse.list_versions),
    R("GET", "/v1/files/{file_id}/recall-options", routes_browse.get_recall_options),
    R("GET", "/v1/files/{file_id}/journey", routes_visual.get_journey),
    R("POST", "/v1/files/{file_id}/versions/{version_id}/restore", routes_rewind.restore_version),
    R("POST", "/v1/files/{file_id}/versions/{version_id}/downloads",
      routes_rewind.request_download),
    R("GET", "/v1/version-restores/{restore_id}", routes_rewind.get_version_restore),
    R("GET", "/v1/downloads/{download_id}", routes_rewind.get_download),
    R("POST", "/v1/recalls", routes_recalls.request_recall, SB),
    R("GET", "/v1/recalls", routes_recalls.list_recalls),
    R("GET", "/v1/recalls/{recall_id}", routes_recalls.get_recall),
    R("POST", "/v1/pins/preview", routes_tiers.preview_pins, PREVIEW),
    R("POST", "/v1/pins", routes_tiers.create_pins),
    R("GET", "/v1/pins", routes_tiers.list_pins),
    R("POST", "/v1/pins/{pin_id}/release", routes_tiers.release_pin),
    R("POST", "/v1/holds", routes_tiers.create_holds),
    R("GET", "/v1/holds", routes_tiers.list_holds),
    R("POST", "/v1/holds/{hold_id}/release", routes_tiers.release_hold),
    R("GET", "/v1/datasets", routes_policies.list_datasets),
    R("GET", "/v1/datasets/{dataset_id}/policy", routes_policies.get_active_policy, SB),
    R("GET", "/v1/datasets/{dataset_id}/policies", routes_policies.list_policies, SB),
    R("POST", "/v1/datasets/{dataset_id}/policies", routes_policies.create_policy),
    R("POST", "/v1/policies/preview", routes_policies.preview_policy, {**SB, **PREVIEW}),
    R("POST", "/v1/policies/proposals", routes_policies.submit_proposal, SB),
    R("GET", "/v1/policies/proposals", routes_policies.list_proposals, SB),
    R("GET", "/v1/policies/proposals/{proposal_id}", routes_policies.get_proposal, SB),
    R("POST", "/v1/policies/proposals/{proposal_id}/approve", routes_policies.approve_proposal),
    R("POST", "/v1/policies/proposals/{proposal_id}/reject", routes_policies.reject_proposal),
    R("POST", "/v1/policies/proposals/{proposal_id}/withdraw",
      routes_policies.withdraw_proposal, SB),
    R("GET", "/v1/price-tables/{backend_kind}", routes_admin.get_price_table),
    R("PUT", "/v1/price-tables/{backend_kind}", routes_admin.put_price_table),
    R("DELETE", "/v1/price-tables/{backend_kind}", routes_admin.delete_price_table),
    R("GET", "/v1/audit", routes_admin.list_audit),
    R("POST", "/v1/sources", routes_sources.register, SB),
    R("POST", "/v1/adopt", routes_sources.adopt, SB),
    R("GET", "/v1/sources", routes_sources.list_sources, SB),
    R("GET", "/v1/sources/{source_id}", routes_sources.get_source, SB),
    R("POST", "/v1/sources/{source_id}/retry", routes_sources.retry, SB),
    R("GET", "/v1/source-backends", routes_sources.list_source_backends),
    R("GET", "/v1/files", routes_partner.get_files, SB),
    R("GET", "/v1/restores", routes_partner.list_restores, SB),
    R("GET", "/v1/events", routes_partner.get_events, SB),
    R("GET", "/v1/usage", routes_partner.get_usage, SB),
    R("POST", "/v1/locations", routes_locations.request, SB),
    R("GET", "/v1/locations", routes_locations.list_locations, SB),
    R("GET", "/v1/locations/{migration_id}", routes_locations.get_location, SB),
    R("POST", "/v1/migrations", routes_migrations.start, SB),
    R("GET", "/v1/migrations", routes_migrations.list_migrations, SB),
    R("GET", "/v1/migrations/{migration_id}", routes_migrations.get_migration, SB),
    R("POST", "/v1/migrations/{migration_id}/cutover", routes_migrations.cutover, SB),
    R("POST", "/v1/migrations/{migration_id}/cancel", routes_migrations.cancel, SB),
    R("POST", "/v1/migrations/{migration_id}/retry", routes_migrations.retry, SB),
    R("POST", "/v1/migrations/{migration_id}/runs", routes_migrations.report_run, SB),
    R("POST", "/v1/migrations/{migration_id}/exclude", routes_migrations.exclude, S),
    R("GET", "/v1/migrations/{migration_id}/tombstones", routes_migrations.tombstones, SB),
    # The migrations console and staged plans (portal-api.md 5.20).
    R("GET", "/v1/migration-console", routes_console.console),
    R("GET", "/v1/migration-agents", routes_console.all_agents),
    R("GET", "/v1/migrations/{migration_id}/agents", routes_console.migration_agents),
    R("GET", "/v1/migrations/{migration_id}/stuck-copies", routes_console.stuck_copies),
    R("POST", "/v1/migrations/{migration_id}/rerun", routes_console.rerun),
    R("POST", "/v1/migrations/{migration_id}/restore-again", routes_console.restore_again),
    R("POST", "/v1/migration-plans", routes_plans.create, SB),
    R("GET", "/v1/migration-plans", routes_plans.list_plans, SB),
    R("GET", "/v1/migration-plans/split", routes_plans.split, SB),
    R("GET", "/v1/migration-plans/{plan_id}", routes_plans.get_plan, SB),
    R("POST", "/v1/migration-plans/{plan_id}/approve", routes_plans.approve),
    R("POST", "/v1/migration-plans/{plan_id}/reject", routes_plans.reject),
    R("POST", "/v1/migration-plans/{plan_id}/cancel", routes_plans.cancel),
    R("PATCH", "/v1/migration-plans/{plan_id}/waves/{seq}", routes_plans.reschedule),
    R("POST", "/v1/migration-plans/{plan_id}/waves/{seq}/cutover", routes_plans.wave_cutover),
    R("POST", "/v1/migration-plans/{plan_id}/waves/{seq}/retry", routes_plans.wave_retry),
    R("POST", "/v1/migration-plans/{plan_id}/waves/{seq}/cancel", routes_plans.wave_cancel),
    R("POST", "/v1/recall-batches", routes_recall_batches.create, SB),
    R("GET", "/v1/recall-batches/{batch_id}", routes_recall_batches.get_batch, SB),
    R("GET", "/v1/activity", routes_activity.get_activity, SB),
    R("GET", "/v1/activity/live", live.get_live, SB),
    R("GET", "/v1/edges", routes_edges.list_edges, SB),
    R("GET", "/v1/edges/appliance", routes_edges.get_appliance),
    # Never replayed: the idempotency store would keep the bundle's token in
    # the clear (the catalog keeps only its hash). A retried add says
    # name_taken, and the admin asks for a new bundle.
    R("POST", "/v1/edges", routes_edges.add_edge, {"idempotent": False}),
    R("POST", "/v1/edges/{edge_id}/bundle", routes_edges.new_bundle, {"idempotent": False}),
    R("POST", "/v1/edges/{edge_id}/revoke", routes_edges.revoke_edge),
    R("GET", "/v1/backends", routes_provision.list_backends),
    R("POST", "/v1/datasets", routes_provision.add_dataset),
    R("POST", "/v1/shares", routes_provision.add_share),
    R("DELETE", "/v1/shares/{share_id}", routes_provision.remove_share),
    # A live count for non-admins: on the preview limiter (portal-api.md 5.22).
    R("GET", "/v1/shares/{share_id}/map", routes_visual.get_map, PREVIEW),
    R("GET", "/v1/shares/{share_id}/map/files", routes_visual.list_map_files, PREVIEW),
    # Settings (portal-api.md 5.20) and API clients (5.21): ArchiveAdmin sessions only.
    R("GET", "/v1/settings/storage", routes_settings.get_storage),
    R("POST", "/v1/settings/storage", routes_settings.add_backend),
    R("PUT", "/v1/settings/storage/{backend_id}", routes_settings.edit_backend),
    R("GET", "/v1/settings/storage/{backend_id}/checks", routes_settings.check_backend),
    R("GET", "/v1/settings/aws", routes_settings.get_aws),
    R("GET", "/v1/settings/aws/trust-policy", routes_settings.get_trust_policy),
    R("POST", "/v1/settings/aws/test", routes_settings.test_aws),
    R("GET", "/v1/settings/identity", routes_settings.get_identity),
    R("GET", "/v1/settings/identity/whoami", routes_settings.whoami),
    R("GET", "/v1/settings/teams", routes_settings.get_teams),
    R("GET", "/v1/settings/teams/package", routes_settings.get_teams_package),
    R("POST", "/v1/settings/teams/test-card", routes_settings.test_card),
    R("GET", "/v1/settings/email", routes_settings.get_email),
    R("POST", "/v1/settings/email/test", routes_settings.test_email),
    R("GET", "/v1/settings/tests/{test_id}", routes_settings.get_test),
    R("GET", "/v1/settings/storage-mover", routes_settings.get_mover),
    # Connect Azure and AWS, Storage Mover deployment (docs/design/cloud-connect.md).
    R("GET", "/v1/settings/azure", routes_cloud.get_azure),
    R("POST", "/v1/settings/azure/sign-in", routes_cloud.azure_sign_in, {"idempotent": False}),
    R("PUT", "/v1/settings/azure/choice", routes_cloud.put_azure_choice),
    R("POST", "/v1/settings/azure/discover", routes_cloud.discover_azure),
    R("POST", "/v1/settings/azure/confirm", routes_cloud.confirm_azure),
    R("GET", "/v1/settings/aws/connect", routes_cloud.get_aws_connect),
    R("GET", "/v1/settings/aws/connect/template", routes_cloud.get_aws_template),
    R("POST", "/v1/settings/aws/connect/fetch", routes_cloud.fetch_aws),
    R("POST", "/v1/settings/aws/connect/outputs", routes_cloud.parse_aws_outputs),
    R("POST", "/v1/settings/aws/connect/confirm", routes_cloud.confirm_aws),
    R("POST", "/v1/settings/storage-mover/deploy", routes_cloud.deploy_mover),
    R("GET", "/v1/settings/tasks/{task_id}", routes_cloud.get_task),
    R("POST", "/v1/source-scans", routes_cloud.start_scan),
    R("GET", "/v1/source-scans/{scan_id}", routes_cloud.get_scan),
    R("GET", "/v1/settings/alerts", routes_settings.get_alerts),
    R("GET", "/v1/settings/prices", routes_admin.get_price_display),
    R("PUT", "/v1/settings/prices", routes_admin.put_price_display),
    R("GET", "/v1/settings/people", routes_settings.search_people),
    R("GET", "/v1/settings/api-clients", routes_settings_clients.list_clients),
    R("POST", "/v1/settings/api-clients", routes_settings_clients.register_client),
    R("GET", "/v1/settings/api-clients/{api_client_id}", routes_settings_clients.get_client),
    R("PATCH", "/v1/settings/api-clients/{api_client_id}", routes_settings_clients.edit_client),
    R("POST", "/v1/settings/api-clients/{api_client_id}/revoke",
      routes_settings_clients.revoke_client),
    # Never replayed: the idempotency store would keep the key in the clear.
    R("POST", "/v1/settings/api-clients/{api_client_id}/keys",
      routes_settings_clients.create_key, {"idempotent": False}),
    R("POST", "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/rotate",
      routes_settings_clients.rotate_key, {"idempotent": False}),
    R("POST", "/v1/settings/api-clients/{api_client_id}/keys/{key_id}/revoke",
      routes_settings_clients.revoke_key),
    # Annexes, phase A1 (portal-api.md section 5.23).
    R("GET", "/v1/filers", routes_annex.list_filers, SB),
    R("POST", "/v1/filers", routes_annex.add_filer),
    R("GET", "/v1/filers/{filer_id}", routes_annex.get_filer, SB),
    R("GET", "/v1/filers/{filer_id}/health", routes_annex.get_filer_health, SB),
    R("GET", "/v1/annexes", routes_annex.list_annexes, SB),
    R("POST", "/v1/annexes", routes_annex.add_annex, SB),
    R("GET", "/v1/annexes/{annex_id}", routes_annex.get_annex, SB),
    R("PATCH", "/v1/annexes/{annex_id}", routes_annex.patch_annex, SB),
    R("GET", "/v1/annexes/{annex_id}/rulesets", routes_annex_rules.list_rulesets, SB),
    R("POST", "/v1/annexes/{annex_id}/rulesets", routes_annex_rules.propose_ruleset, SB),
    R("GET", "/v1/annexes/{annex_id}/rulesets/{version}", routes_annex_rules.get_ruleset, SB),
    R("POST", "/v1/annexes/{annex_id}/rulesets/{version}/approve",
      routes_annex_rules.approve_version),
    R("POST", "/v1/annexes/{annex_id}/rulesets/{version}/reject",
      routes_annex_rules.reject_version),
    R("POST", "/v1/annexes/{annex_id}/dry-runs", routes_annex_rules.create_dry_run, SB),
    R("GET", "/v1/annexes/{annex_id}/dry-runs/{run_id}", routes_annex_rules.get_dry_run, SB),
    # The return path (phase A3.1; portal-api.md 5.25).
    R("POST", "/v1/annexes/{annex_id}/return-preview", routes_annex_returns.return_preview, SB),
    R("GET", "/v1/files/{file_id}/return-targets", routes_annex_returns.get_return_targets, SB),
    R("POST", "/v1/placements", routes_annex_returns.request_placement, SB),
    R("GET", "/v1/placements/{placement_id}", routes_annex_returns.get_placement, SB),
    R("POST", "/v1/placements/{placement_id}/cancel", routes_annex_returns.cancel_placement),
    R("POST", "/v1/placements/{placement_id}/abandon", routes_annex_returns.abandon_placement),
    R("PATCH", "/v1/filers/{filer_id}", routes_annex.patch_filer),
    R("POST", "/v1/annexes/{annex_id}/files/{file_id}/clear-breaker", routes_annex.clear_breaker),
    R("POST", "/v1/annexes/{annex_id}/removals/confirm", routes_annex_removals.confirm_removals),
    R("GET", "/v1/annexes/{annex_id}/removals", routes_annex_removals.list_removals, SB),
    R("GET", "/v1/annexes/{annex_id}/events", routes_annex.list_events, SB),
    R("GET", "/v1/annexes/{annex_id}/files", routes_annex.list_files, SB),
    R("GET", "/v1/annexes/{annex_id}/timeline", routes_annex.get_timeline, SB),
    R("GET", "/v1/annexes/{annex_id}/problems", routes_annex.get_problems, SB),
    R("POST", "/v1/annexes/{annex_id}/files/{file_id}/purge", routes_annex.purge_file),
    # A copy Azure keeps refusing (#81's follow-up, migration 0058).
    R("POST", "/v1/annexes/{annex_id}/files/{file_id}/copy-stop", routes_annex.stop_copy),
    R("POST", "/v1/annexes/{annex_id}/files/{file_id}/copy-resume", routes_annex.resume_copy),
    R("POST", "/v1/annexes/{annex_id}/files/{file_id}/copy-refused-ack",
      routes_annex.ack_copy_refused),
    R("GET", "/v1/settings/annex-defaults", routes_annex.get_defaults),
    R("PUT", "/v1/settings/annex-defaults", routes_annex.put_defaults),
]  # fmt: skip

#: Routes outside the endpoint wrapper (no session): sign-in and liveness.
UNWRAPPED = [
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("GET", "/auth/azure/callback"),
    ("GET", "/healthz"),
]


async def healthz(_: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    if exc.status_code == 404:
        return ApiError("not_found", "no such endpoint").response()
    if exc.status_code == 405:
        return ApiError("invalid_request", "method not allowed").response()
    return ApiError("internal", str(exc.detail)).response()


async def api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return exc.response()


def create_app(
    services: WebServices, *, dev_idp: Starlette | None = None, manage_lifecycle: bool = True
) -> Starlette:
    """`dev_idp`: the dev issuer's app, mounted at /dev-idp (dev mode only;
    `__main__` decides)."""

    @asynccontextmanager
    async def lifespan(app: Starlette):
        if manage_lifecycle:
            await services.catalog.open()
        try:
            yield
        finally:
            if manage_lifecycle:
                for closer in services.closers:
                    await closer()
                await services.catalog.close()

    auth = services.auth

    async def login(request: Request) -> Response:
        assert services.login_limiter is not None
        addr = client_addr(request, services.settings.trusted_ingress) or "-"
        wait = services.login_limiter.take(addr)
        if wait is not None:
            return ApiError(
                "throttled", "too many sign-ins", retry_after_s=max(1, int(wait + 0.999))
            ).response()
        return await auth_route(request, auth.login)

    async def callback(request: Request) -> Response:
        return await auth_route(request, auth.callback)

    async def azure_callback(request: Request) -> Response:
        from . import azure_connect

        return await auth_route(request, azure_connect.callback)

    routes: list[Any] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/auth/login", login, methods=["GET"]),
        Route("/auth/callback", callback, methods=["GET"]),
        Route("/auth/azure/callback", azure_callback, methods=["GET"]),
    ]
    routes += [Route(r.path, endpoint(r.handler, **r.opts), methods=[r.method]) for r in ROUTES]
    if dev_idp is not None:
        routes.append(Mount("/dev-idp", app=dev_idp))
    spa = Spa(services.settings.static_dir)

    async def static(request: Request) -> Response:
        try:
            return await spa(request)
        except ApiError as exc:
            return exc.response()

    routes.append(Route("/{path:path}", static, methods=["GET", "HEAD"]))
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={HTTPException: http_error, ApiError: api_error},
    )
    app.add_middleware(SecurityHeaders)
    app.add_middleware(metrics.RequestMetrics, name="portal_api")
    app.state.svc = services
    return app
