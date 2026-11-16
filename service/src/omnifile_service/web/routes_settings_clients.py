"""Settings, API clients (portal-api.md 5.21): register applications, their
scopes and owners; disable, revoke; and OmniFile API keys.

ArchiveAdmin, signed in. Registering, changing scopes or owner, enabling, and
creating or rotating a key need a sign-in within 10 minutes. Disabling and
revoking (a client or a key) need none: they only take access away, and must
work at once. Every change is audited; a key is audited by its public id only.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from ..apiclients import keys as api_keys
from ..apiclients import registry
from .context import Ctx, WebResult, audit, if_match
from .problems import WebError, invalid, not_found
from .routes_settings import admin_only, identity_of

GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Dev issuer apps are named, not GUIDs (dev mode only).
DEV_APP = re.compile(r"^[a-z][a-z0-9-]{2,62}$")


def setup(ctx: Ctx) -> dict[str, Any]:
    s = ctx.svc.settings
    aud = list(s.api_audiences)
    uri = next((a for a in aud if a.startswith("api://")), aud[0] if aud else None)
    token = f"https://login.microsoftonline.com/{s.tenant_id}/oauth2/v2.0/token"
    return {
        "tenant_id": s.tenant_id,
        "token_endpoint": s.oidc.token if s.dev_mode else token,
        "scope": f"{uri}/.default" if uri else None,
        "audiences": aud,
        "app_roles": list(registry.SCOPES),
        "steps": [
            "Recommended: the client uses its own Entra app registration. Nothing is stored or"
            " shared on OmniFile's side.",
            "In the client's own app registration, add a certificate (Certificates & secrets,"
            " Certificates). Prefer it to a client secret; on Azure, a managed identity with a"
            " federated credential needs neither.",
            "In OmniFile's app registration, Enterprise applications, give the client the app"
            " roles it needs (Policy.Propose, Recall.Request, Archive.Read, Archive.Adopt), and"
            " grant admin consent.",
            "Register the client here with its application (client) id, the same scopes and"
            " its owner. A token from an application not registered here, or disabled, is"
            " refused; roles not in its scopes here are ignored.",
            f"The client asks {token} for client credentials with scope"
            f" {uri + '/.default' if uri else '<application ID URI>/.default'}, and sends"
            " Authorization: Bearer <token>.",
            "The alternative, for a client that can't hold an Entra credential: an OmniFile API"
            " key, made on the client's page. It is shown once and kept only as a hash, expires"
            " (90 days by default), and goes only in Authorization: Bearer <key>, never in a"
            " URL.",
        ],
    }


def _scopes(v: Any, field: str = "scopes") -> list[str]:
    if not isinstance(v, list) or any(s not in registry.SCOPES for s in v):
        raise invalid(field, f"a list of {', '.join(registry.SCOPES)}")
    if len(set(v)) != len(v):
        raise invalid(field, "has a scope twice")
    return [s for s in registry.SCOPES if s in v]


async def _owner(db, v: Any) -> int:
    if not isinstance(v, str) or not v.isdigit():
        raise invalid("owner_identity_id", "is required: a person in the tenant")
    o = await (
        await db.execute(
            "SELECT identity_id FROM catalog.identity WHERE identity_id = %s AND kind = 'user'",
            (int(v),),
        )
    ).fetchone()
    if o is None:
        raise invalid("owner_identity_id", "no such person: pick one from the people search")
    return int(o["identity_id"])


async def list_clients(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT c.* FROM catalog.api_client c ORDER BY c.display_name, c.api_client_id"
            )
        ).fetchall()
        items = [await registry.client_json(db, r, ctx.now) for r in rows]
    return WebResult(200, {"items": items, "setup": setup(ctx)})


async def register_client(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    ctx.svc.auth.require_recent_auth(who)
    b = ctx.body
    extra = set(b) - {"app_id", "display_name", "scopes", "owner_identity_id"}
    if extra:
        raise invalid(sorted(extra)[0], "is not an API client field")
    app_id = str(b.get("app_id") or "").strip().lower()
    if not GUID.match(app_id) and not (ctx.svc.settings.dev_mode and DEV_APP.match(app_id)):
        raise invalid("app_id", "the application (client) id, a GUID")
    name = b.get("display_name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise invalid("display_name", "1 to 80 characters")
    scopes = _scopes(b.get("scopes"))
    async with ctx.svc.catalog.tx(who.actor) as db:
        owner = await _owner(db, b.get("owner_identity_id"))
        c = await (
            await db.execute(
                "INSERT INTO catalog.api_client (app_id, display_name, scopes, owner_identity_id,"
                " created_by_identity_id, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (app_id) DO NOTHING RETURNING *",
                (app_id, name.strip(), scopes, owner, me, ctx.now, ctx.now),
            )
        ).fetchone()
        if c is None:
            raise WebError("name_taken", f"application {app_id} is registered already")
        await audit(
            db,
            who,
            "api_client.register",
            details={
                "api_client_id": str(c["api_client_id"]),
                "app_id": app_id,
                "scopes": scopes,
                "owner_identity_id": str(owner),
            },
        )
        out = await registry.client_json(db, c, ctx.now)
    return WebResult(201, out)


async def get_client(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    cid = ctx.path_id("api_client_id")
    async with ctx.svc.catalog.read() as db:
        c = await registry.load(db, cid)
        if c is None:
            raise not_found("API client")
        out = await registry.client_json(db, c, ctx.now)
    return WebResult(200, out)


async def edit_client(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    identity_of(who)
    cid = ctx.path_id("api_client_id")
    b = ctx.body
    extra = set(b) - {"display_name", "scopes", "owner_identity_id", "enabled"}
    if extra:
        raise invalid(sorted(extra)[0], "is not an API client field")
    if not b:
        raise invalid("display_name", "nothing to change")
    if "enabled" in b and not isinstance(b["enabled"], bool):
        raise invalid("enabled", "true or false")
    only_disables = set(b) == {"enabled"} and b["enabled"] is False
    if not only_disables:
        ctx.svc.auth.require_recent_auth(who)
    async with ctx.svc.catalog.tx(who.actor) as db:
        c = await registry.load(db, cid, lock=True)
        if c is None:
            raise not_found("API client")
        if_match(ctx.request, c["entity_version"])
        if c["state"] == "revoked":
            raise WebError("forbidden", "a revoked API client can't be changed")
        sets: dict[str, Any] = {}
        if "display_name" in b:
            n = b["display_name"]
            if not isinstance(n, str) or not 1 <= len(n.strip()) <= 80:
                raise invalid("display_name", "1 to 80 characters")
            sets["display_name"] = n.strip()
        if "scopes" in b:
            sets["scopes"] = _scopes(b["scopes"])
        if "owner_identity_id" in b:
            sets["owner_identity_id"] = await _owner(db, b["owner_identity_id"])
        if "enabled" in b:
            sets["state"] = "enabled" if b["enabled"] else "disabled"
            sets["disabled_at"] = None if b["enabled"] else ctx.now
        cols = ", ".join(f"{k} = %({k})s" for k in sets)
        c = await (
            await db.execute(
                f"UPDATE catalog.api_client SET {cols}, updated_at = %(now)s,"
                " entity_version = entity_version + 1 WHERE api_client_id = %(id)s RETURNING *",
                {**sets, "now": ctx.now, "id": cid},
            )
        ).fetchone()
        action = "api_client.disable" if only_disables else "api_client.update"
        await audit(
            db,
            who,
            action,
            details={
                "api_client_id": str(cid),
                **{
                    k: (str(v) if k == "owner_identity_id" else v)
                    for k, v in sets.items()
                    if k != "disabled_at"
                },
            },
        )
        out = await registry.client_json(db, c, ctx.now)
    return WebResult(200, out)


async def revoke_client(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    identity_of(who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "is not a field")
    cid = ctx.path_id("api_client_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        c = await registry.load(db, cid, lock=True)
        if c is None:
            raise not_found("API client")
        if c["state"] != "revoked":
            c = await (
                await db.execute(
                    "UPDATE catalog.api_client SET state = 'revoked', revoked_at = %(now)s,"
                    " updated_at = %(now)s, entity_version = entity_version + 1"
                    " WHERE api_client_id = %(id)s RETURNING *",
                    {"now": ctx.now, "id": cid},
                )
            ).fetchone()
            await db.execute(
                "UPDATE catalog.api_key SET revoked_at = %s"
                " WHERE api_client_id = %s AND revoked_at IS NULL",
                (ctx.now, cid),
            )
            # Its pending proposals can't be approved any more: shown as stale.
            await db.execute(
                "UPDATE catalog.policy_proposal SET stale_reason = 'client_revoked'"
                " WHERE api_client_id = %s AND state = 'pending'",
                (cid,),
            )
            await audit(db, who, "api_client.revoke", details={"api_client_id": str(cid)})
        out = await registry.client_json(db, c, ctx.now)
    return WebResult(200, out)


# --- API keys -------------------------------------------------------------------------


def _days(v: Any, field: str) -> timedelta:
    if v is None:
        return api_keys.DEFAULT_TTL
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= api_keys.MAX_TTL.days:
        raise invalid(field, f"an integer from 1 to {api_keys.MAX_TTL.days}")
    return timedelta(days=v)


async def _key(db, cid: int, key_id: str, *, lock: bool = False) -> dict[str, Any]:
    if not api_keys.KEY_ID.match(key_id):
        raise not_found("API key")
    k = await (
        await db.execute(
            "SELECT k.*, i.display_name AS created_by_name FROM catalog.api_key k"
            "  LEFT JOIN catalog.identity i ON i.identity_id = k.created_by_identity_id"
            " WHERE k.key_id = %s AND k.api_client_id = %s" + (" FOR UPDATE OF k" if lock else ""),
            (key_id, cid),
        )
    ).fetchone()
    if k is None:
        raise not_found("API key")
    return k


async def _insert_key(
    db, cid: int, scopes: list[str], me: int, ttl: timedelta, now, rotated_from: str | None = None
) -> tuple[str, dict[str, Any]]:
    new = api_keys.generate()
    k = await (
        await db.execute(
            "INSERT INTO catalog.api_key (key_id, api_client_id, secret_sha256, scopes,"
            " created_by_identity_id, created_at, expires_at, rotated_from)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (new.key_id, cid, new.secret_sha256, scopes, me, now, now + ttl, rotated_from),
        )
    ).fetchone()
    return new.key, k


async def create_key(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    ctx.svc.auth.require_recent_auth(who)
    b = ctx.body
    extra = set(b) - {"scopes", "expires_in_days"}
    if extra:
        raise invalid(sorted(extra)[0], "is not an API key field")
    cid = ctx.path_id("api_client_id")
    ttl = _days(b.get("expires_in_days"), "expires_in_days")
    async with ctx.svc.catalog.tx(who.actor) as db:
        c = await registry.load(db, cid, lock=True)
        if c is None:
            raise not_found("API client")
        if c["state"] != "enabled":
            raise WebError("client_disabled", f"API client {c['display_name']!r} is {c['state']}")
        scopes = _scopes(b["scopes"]) if "scopes" in b else list(c["scopes"])
        wider = sorted(set(scopes) - set(c["scopes"]))
        if wider:
            raise invalid("scopes", f"{', '.join(wider)} is not one of the client's scopes")
        key, k = await _insert_key(db, cid, scopes, me, ttl, ctx.now)
        k["created_by_name"] = who.display_name
        await audit(
            db,
            who,
            "api_key.create",
            details={
                "api_client_id": str(cid),
                "key_id": k["key_id"],
                "scopes": scopes,
                "expires_at": k["expires_at"].isoformat(),
            },
        )
    return WebResult(201, {"key": key, "api_key": registry.key_json(k, ctx.now)})


async def rotate_key(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    ctx.svc.auth.require_recent_auth(who)
    b = ctx.body
    extra = set(b) - {"expires_in_days", "overlap_hours"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a rotation field")
    ttl = _days(b.get("expires_in_days"), "expires_in_days")
    overlap = api_keys.DEFAULT_OVERLAP
    if "overlap_hours" in b:
        h = b["overlap_hours"]
        if isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 720:
            raise invalid("overlap_hours", "an integer from 0 to 720")
        overlap = timedelta(hours=h)
    cid = ctx.path_id("api_client_id")
    key_id = ctx.request.path_params["key_id"]
    async with ctx.svc.catalog.tx(who.actor) as db:
        c = await registry.load(db, cid, lock=True)
        if c is None:
            raise not_found("API client")
        if c["state"] != "enabled":
            raise WebError("client_disabled", f"API client {c['display_name']!r} is {c['state']}")
        old = await _key(db, cid, key_id, lock=True)
        if registry.key_state(old, ctx.now) != "active":
            raise WebError("forbidden", "only an active key is rotated; create a new one")
        scopes = [s for s in old["scopes"] if s in c["scopes"]]
        key, k = await _insert_key(db, cid, scopes, me, ttl, ctx.now, rotated_from=key_id)
        k["created_by_name"] = who.display_name
        until = min(old["expires_at"], ctx.now + overlap)
        await db.execute(
            "UPDATE catalog.api_key SET expires_at = %s WHERE key_id = %s",
            (max(until, old["created_at"] + timedelta(microseconds=1)), key_id),
        )
        prev = await _key(db, cid, key_id)
        await audit(
            db,
            who,
            "api_key.rotate",
            details={
                "api_client_id": str(cid),
                "key_id": k["key_id"],
                "rotated_from": key_id,
                "previous_expires_at": prev["expires_at"].isoformat(),
            },
        )
    return WebResult(
        201,
        {
            "key": key,
            "api_key": registry.key_json(k, ctx.now),
            "previous": registry.key_json(prev, ctx.now),
        },
    )


async def revoke_key(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    identity_of(who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "is not a field")
    cid = ctx.path_id("api_client_id")
    key_id = ctx.request.path_params["key_id"]
    async with ctx.svc.catalog.tx(who.actor) as db:
        k = await _key(db, cid, key_id, lock=True)
        if k["revoked_at"] is None:
            await db.execute(
                "UPDATE catalog.api_key SET revoked_at = %s WHERE key_id = %s", (ctx.now, key_id)
            )
            await audit(
                db, who, "api_key.revoke", details={"api_client_id": str(cid), "key_id": key_id}
            )
        k = await _key(db, cid, key_id)
    return WebResult(200, registry.key_json(k, ctx.now))
