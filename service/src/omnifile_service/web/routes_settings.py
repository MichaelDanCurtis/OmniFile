"""Settings (portal-api.md 5.20): storage, AWS, identity, Teams, email, Storage
Mover and alerts. ArchiveAdmin, signed in (never an app). Every change is
audited; adding or editing storage needs a sign-in within 10 minutes.

The work is in `omnifile_service.settings`; these handlers check who may call
and shape the answers.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.responses import Response

from ..admin import backends as admin
from ..api.problems import ApiError
from ..api.validate import parse_id, rfc3339
from ..settings import alerts, aws, identity, mover, notify, storage, tests
from .context import Ctx, Principal, WebResult, audit, client_addr
from .problems import WebError, invalid, not_found, role_required


def admin_only(who: Principal) -> None:
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def identity_of(who: Principal) -> int:
    if who.identity_id is None:
        raise WebError("forbidden", "Settings changes record an AD identity, and you have none")
    return who.identity_id


def _env(ctx: Ctx) -> dict[str, str]:
    return ctx.svc.settings_env


# --- storage ------------------------------------------------------------------------


async def get_storage(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    rows = await asyncio.to_thread(storage.list_sync, ctx.svc.catalog.db)
    env, dev = _env(ctx), ctx.svc.settings.dev_mode
    return WebResult(
        200,
        {
            "backends": [storage.backend_json(r) for r in rows],
            "private_network": storage.private_deployment(env, dev),
            "aws_configured": storage.aws_configured(env),
            "dev_mode": dev,
        },
    )


async def _save(ctx: Ctx, draft: dict[str, Any], name: str | None) -> tuple[admin.Result, dict]:
    who = ctx.who
    admin_only(who)
    identity_of(who)
    ctx.svc.auth.require_recent_auth(who)
    if name is not None:
        if draft.get("name") not in (None, name):
            raise invalid("name", "a backend keeps its name; register new storage instead")
        draft = {**draft, "name": name}
    try:
        row = storage.build_row(draft, _env(ctx), ctx.svc.settings.dev_mode)
    except admin.Refused as e:
        raise invalid(storage.field_of(str(e)), str(e)) from None
    actor = storage.Actor(
        actor=who.actor,
        sid=who.sid,
        client_addr=client_addr(ctx.request, ctx.svc.settings.trusted_ingress),
    )
    try:
        async with ctx.svc.catalog.tx(who.actor) as db:
            result = await storage.register(db, row, actor)
    except ApiError as e:
        if e.code != "invalid_request":
            raise
        raise WebError(
            "invalid_request",
            e.detail,
            errors=[{"field": storage.field_of(e.detail or ""), "message": e.detail or ""}],
        ) from None
    shown = await asyncio.to_thread(storage.show_sync, ctx.svc.catalog.db, result.backend_id)
    assert shown is not None
    return result, {
        "outcome": result.outcome,
        "backend": storage.backend_json(shown),
        "changes": list(result.changes),
    }


async def add_backend(ctx: Ctx) -> WebResult:
    result, body = await _save(ctx, dict(ctx.body), None)
    return WebResult(201 if result.outcome == "added" else 200, body)


async def edit_backend(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    bid = ctx.path_id("backend_id")
    b = await asyncio.to_thread(storage.show_sync, ctx.svc.catalog.db, bid)
    if b is None:
        raise not_found("backend")
    kind = ctx.body.get("kind")
    if storage_kind(kind) != b["kind"]:
        raise invalid("kind", f"backend {b['name']} is {b['kind']}; a backend never changes kind")
    _result, body = await _save(ctx, dict(ctx.body), b["name"])
    return WebResult(200, body)


def storage_kind(kind: Any) -> str | None:
    return admin.KINDS.get(kind) if isinstance(kind, str) else None


async def check_backend(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    bid = ctx.path_id("backend_id")
    b = await asyncio.to_thread(storage.show_sync, ctx.svc.catalog.db, bid)
    if b is None:
        raise not_found("backend")
    async with ctx.svc.catalog.read() as db:
        checks = await storage.check_backend(
            db, ctx.svc.probes, b, _env(ctx), ctx.svc.settings.dev_mode, ctx.now
        )
    return WebResult(
        200, {"backend_id": str(bid), "checked_at": rfc3339(ctx.now), "checks": checks}
    )


# --- AWS ----------------------------------------------------------------------------


async def get_aws(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    return WebResult(200, aws.settings_json(_env(ctx), ctx.svc.settings.tenant_id))


async def get_trust_policy(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    arn = ctx.query("role_arn")
    if not arn:
        raise invalid("role_arn", "is required")
    try:
        out = aws.trust_policy(arn, _env(ctx), ctx.svc.settings.tenant_id)
    except ValueError as e:
        raise invalid("role_arn", str(e)) from None
    return WebResult(200, out)


async def _queue(ctx: Ctx, kind: str, params: dict[str, Any], action: str) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    async with ctx.svc.catalog.tx(who.actor) as db:
        t = await tests.queue_test(db, kind, me, params, ctx.now)
        await audit(db, who, action, details={"test_id": str(t["test_id"]), **params})
    return WebResult(202, tests.test_json(t))


async def test_aws(ctx: Ctx) -> WebResult:
    b = ctx.body
    extra = set(b) - {"role_arn"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a field")
    arn = b.get("role_arn")
    if not isinstance(arn, str) or not aws.ROLE_ARN.match(arn):
        raise invalid("role_arn", "an IAM role ARN: arn:aws:iam::<12 digits>:role/<name>")
    return await _queue(ctx, "aws_assume_role", {"role_arn": arn}, "settings.aws_test")


async def get_test(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    tid = ctx.path_id("test_id")
    async with ctx.svc.catalog.read() as db:
        t = await tests.get_test(db, tid, identity_of(ctx.who))
    if t is None:
        raise not_found("test")
    return WebResult(200, tests.test_json(t))


# --- identity -------------------------------------------------------------------------


async def get_identity(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    s = ctx.svc.settings
    async with ctx.svc.catalog.read() as db:
        out = await identity.identity_settings(
            db,
            ctx.svc.probes,
            s.tenant_id,
            s.client_id,
            list(s.api_audiences),
            ctx.now,
            s.role_holders,
        )
    return WebResult(200, out)


async def whoami(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    async with ctx.svc.catalog.read() as db:
        out = await identity.whoami(db, ctx.who, ctx.svc.settings, ctx.now)
    return WebResult(200, out)


# --- Teams and email ----------------------------------------------------------------


async def get_teams(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    async with ctx.svc.catalog.read() as db:
        out = await notify.teams_settings(db, _env(ctx), ctx.now)
    return WebResult(200, out)


async def get_teams_package(ctx: Ctx) -> Response:
    admin_only(ctx.who)
    p = notify.package_path(_env(ctx))
    if p is None:
        raise not_found("Teams app package: build it with deploy/teams/build.sh")
    data = await asyncio.to_thread(p.read_bytes)
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


async def test_card(ctx: Ctx) -> WebResult:
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "is not a field")
    return await _queue(ctx, "teams_card", {}, "settings.teams_test")


async def get_email(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    async with ctx.svc.catalog.read() as db:
        out = await notify.email_settings(db, _env(ctx), ctx.who.identity_id, ctx.now)
    return WebResult(200, out)


async def test_email(ctx: Ctx) -> WebResult:
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "is not a field")
    return await _queue(ctx, "email", {}, "settings.email_test")


# --- Storage Mover and alerts ---------------------------------------------------------


async def get_mover(ctx: Ctx) -> WebResult:
    from .routes_cloud import mover_extras

    admin_only(ctx.who)
    extras = await mover_extras(ctx)
    d = extras["deployment"]
    deployed = d if d is not None and d["state"] == "done" else None
    out = await mover.mover_settings(ctx.svc.probes, _env(ctx), deployed)
    return WebResult(200, {**out, **extras})


async def get_alerts(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    async with ctx.svc.catalog.read() as db:
        out = await alerts.alert_settings(db, ctx.now)
    return WebResult(200, out)


# --- people (API client owners) -------------------------------------------------------


async def search_people(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    q = (ctx.query("q") or "").strip()
    if not 2 <= len(q) <= 128:
        raise invalid("q", "2 to 128 characters")
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT identity_id, display_name, upn, app_roles, app_roles_seen_at"
                "  FROM catalog.identity WHERE kind = 'user'"
                "   AND (display_name ILIKE %(q)s OR upn ILIKE %(q)s)"
                " ORDER BY display_name NULLS LAST, identity_id LIMIT 20",
                {"q": like},
            )
        ).fetchall()
    return WebResult(
        200,
        {
            "items": [
                {
                    "identity_id": str(r["identity_id"]),
                    "display_name": r["display_name"],
                    "upn": r["upn"],
                    "roles": list(r["app_roles"] or []),
                    "roles_seen_at": rfc3339(r["app_roles_seen_at"]),
                }
                for r in rows
            ]
        },
    )


__all__ = ["admin_only", "identity_of", "parse_id"]
