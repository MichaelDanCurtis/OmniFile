"""Connect Azure's delegated sign-in (docs/design/cloud-connect.md 1.2).

`start` records an operation (`catalog.azure_connect_op`: its purpose, the
plan it carries out, a PKCE verifier and a nonce, the admin who asked) and
returns Entra's authorize URL for ``openid`` and ARM's ``user_impersonation``.
The state also goes in a ``__Host-`` cookie (SameSite=Lax: Entra's redirect
back is a cross-site navigation, which the Strict session cookie doesn't
survive, so the operation row says who started it).

`callback` checks the state against the cookie and the row, redeems the code
(the portal app's own client credential, as at sign-in), and checks the ID
token: signature, issuer, tenant, nonce, and that its ``oid`` is the admin's.
Grants and removals also need it to be a fresh sign-in (``max_age``). Then it
does the operation with the access token, through `settings.azure_connect`,
and drops the token: nothing is cached, no refresh token is asked for, and the
verifier was deleted when the code was taken.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..settings import azure_connect as az
from ..settings.arm import HttpArm, StaticToken
from .auth import ACTOR, _cookie, clear_cookie, pkce_challenge
from .context import Principal
from .problems import WebError

log = logging.getLogger("omnifile_service.web.azure_connect")

AZURE_COOKIE = "__Host-omnifile_azure"
OP_TTL = timedelta(minutes=10)
#: Grants and removals need a sign-in this fresh.
FRESH = timedelta(minutes=10)
ARM_SCOPE = "https://management.azure.com/user_impersonation"
DONE_PATH = "/settings/azure"


async def start(svc, who: Principal, purpose: str, items: list[dict[str, Any]], now: datetime):
    """The operation and its authorize URL. Returns (row, url, Set-Cookie)."""
    s = svc.settings
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    async with svc.catalog.tx(who.actor) as db:
        op = await (
            await db.execute(
                "INSERT INTO catalog.azure_connect_op (purpose, oauth_state, nonce, code_verifier,"
                " identity_id, entra_object_id, plan, created_at, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING op_id, expires_at",
                (purpose, state, nonce, verifier, who.identity_id, who.object_id, Jsonb(items),
                 now, now + OP_TTL),
            )
        ).fetchone()  # fmt: skip
        await _audit(
            db, who.identity_id, who.sid, f"settings.azure_{purpose}_start", "allow",
            {"op_id": str(op["op_id"]), "items": [i.get("key") or i.get("grant_id")
                                                   for i in items]},
        )  # fmt: skip
    params = {
        "client_id": s.client_id,
        "response_type": "code",
        "redirect_uri": s.azure_redirect_uri,
        "response_mode": "query",
        "scope": f"openid {ARM_SCOPE}",
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
        "claims": '{"id_token":{"auth_time":{"essential":true}}}',
    }
    if who.upn:
        params["login_hint"] = who.upn
    if purpose in ("grant", "remove"):
        params["max_age"] = str(int(FRESH.total_seconds()))
    cookie = _cookie(AZURE_COOKIE, state, same_site="Lax", max_age=int(OP_TTL.total_seconds()))
    return op, f"{s.oidc.authorize}?{urlencode(params)}", cookie


async def _audit(db, identity_id, sid, action: str, decision: str, details: dict) -> None:
    await db.execute(
        "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision, details)"
        " VALUES (%s, %s, 'proven', %s, %s, %s::jsonb)",
        (f"user:{identity_id}", sid, action, decision, json.dumps(details)),
    )


async def _redeem(svc, code: str, verifier: str) -> dict[str, Any]:
    s, auth = svc.settings, svc.auth
    form = {
        "grant_type": "authorization_code",
        "client_id": s.client_id,
        "code": code,
        "redirect_uri": s.azure_redirect_uri,
        "code_verifier": verifier,
        "scope": f"openid {ARM_SCOPE}",
        **(await auth.credential.fields()),
    }
    try:
        resp = await auth.http.post(s.oidc.token, data=form, timeout=10)
    except httpx.HTTPError:
        raise az.ConnectError("the sign-in service was unreachable") from None
    if resp.status_code != 200:
        raise az.ConnectError("the sign-in code was refused")
    try:
        data = resp.json()
    except ValueError:
        raise az.ConnectError("the sign-in service answered badly") from None
    if not isinstance(data.get("id_token"), str) or not isinstance(data.get("access_token"), str):
        raise az.ConnectError("no ID token or Azure token came back")
    return data


async def callback(request: Request) -> Response:
    svc = request.app.state.svc
    s = svc.settings
    q = request.query_params
    state = q.get("state")
    if not state:
        raise WebError("invalid_request", "state is required")
    cookie = request.cookies.get(AZURE_COOKIE)
    if not cookie or not hmac.compare_digest(cookie.encode(), state.encode()):
        raise WebError("unauthenticated", "this sign-in was not started in this browser")
    now = s.clock()
    async with svc.catalog.tx(ACTOR) as db:
        # Take the verifier: a second use of this state finds none.
        op = await (
            await db.execute(
                "UPDATE catalog.azure_connect_op o SET code_verifier = NULL"
                "  FROM catalog.azure_connect_op old"
                " WHERE o.op_id = old.op_id AND o.oauth_state = %s AND o.state = 'started'"
                "   AND o.code_verifier IS NOT NULL"
                " RETURNING o.*, old.code_verifier AS verifier",
                (state,),
            )
        ).fetchone()
    if op is None:
        raise WebError("unauthenticated", "this sign-in was already used or never started")
    try:
        if op["expires_at"] <= now:
            raise az.ConnectError("the sign-in took too long; start again")
        if q.get("error") or not q.get("code"):
            raise az.ConnectError("the sign-in was cancelled or refused")
        tokens = await _redeem(svc, q["code"], op["verifier"])
        claims = await _id_token(svc, tokens["id_token"], op, now)
        arm = (svc.arm_for or (lambda t: HttpArm(svc.auth.http, StaticToken(t))))(
            tokens["access_token"]
        )
        detail = await _run(svc, op, arm, claims, now)
        state_out = "done"
    except az.ConnectError as e:
        detail, state_out = str(e), "failed"
    except WebError as e:
        detail, state_out = e.detail or "the sign-in was refused", "failed"
    except Exception as e:  # noqa: BLE001 - the page says what went wrong
        log.exception("connect azure failed")
        detail, state_out = f"unexpected error: {type(e).__name__}", "failed"
    async with svc.catalog.tx(f"user:{op['identity_id']}") as db:
        await db.execute(
            "UPDATE catalog.azure_connect_op SET state = %s, detail = %s, finished_at = %s"
            " WHERE op_id = %s",
            (state_out, detail, now, op["op_id"]),
        )
        if state_out == "failed":
            await _audit(
                db, op["identity_id"], await _sid(db, op["identity_id"]),
                f"settings.azure_{op['purpose']}", "deny",
                {"op_id": str(op["op_id"]), "detail": detail},
            )  # fmt: skip
    resp = RedirectResponse(f"{DONE_PATH}?{urlencode({'op': op['op_id']})}", status_code=302)
    resp.headers.append("set-cookie", clear_cookie(AZURE_COOKIE))
    return resp


async def _id_token(svc, token: str, op, now: datetime) -> dict[str, Any]:
    claims = await svc.auth._decode(token, audience=svc.settings.client_id)
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode(), op["nonce"].encode()):
        raise az.ConnectError("the ID token's nonce doesn't match this sign-in")
    if str(claims.get("oid") or "").lower() != str(op["entra_object_id"]).lower():
        raise az.ConnectError(
            "you signed in to Azure as someone else: use the same account as the portal"
        )
    if op["purpose"] in ("grant", "remove"):
        at = claims.get("auth_time")
        if isinstance(at, bool) or not isinstance(at, int | float):
            raise az.ConnectError("the sign-in service didn't confirm a fresh sign-in")
        if now.timestamp() - at > FRESH.total_seconds() + 60:
            raise az.ConnectError("grants need a fresh sign-in; start again")
    return claims


async def _sid(db, identity_id: int) -> str | None:
    row = await (
        await db.execute(
            "SELECT sid::text AS sid FROM catalog.identity WHERE identity_id = %s", (identity_id,)
        )
    ).fetchone()
    return row["sid"] if row else None


async def _run(svc, op, arm, claims, now: datetime) -> str:
    s = svc.settings
    me, purpose = op["identity_id"], op["purpose"]
    if purpose == "list":
        listing = await az.list_with(arm, s.tenant_id)
        async with svc.catalog.tx(f"user:{me}") as db:
            await az.upsert(db, s.tenant_id, listing=listing, listed_at=now, listed_by=me)
            await _audit(
                db, me, await _sid(db, me), "settings.azure_list", "allow",
                {"op_id": str(op["op_id"]), "subscriptions": len(listing["subscriptions"]),
                 "resource_groups": len(listing["resource_groups"])},
            )  # fmt: skip
        return (
            f"Listed {len(listing['subscriptions'])} subscriptions and"
            f" {len(listing['resource_groups'])} resource groups."
        )
    if purpose == "grant":
        items = list(op["plan"] or [])
        made, error = await az.grant_with(arm, items)
        async with svc.catalog.tx(f"user:{me}") as db:
            sid = await _sid(db, me)
            for m in made:
                await db.execute(
                    "INSERT INTO catalog.azure_grant (purpose, scope, principal, principal_id,"
                    " role_name, role_definition_id, assignment_id, made_by, made_at, op_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (assignment_id) WHERE state = 'active' DO NOTHING",
                    (m["purpose"], m["scope"], m["principal"], m["principal_id"], m["role_name"],
                     m["role_definition_id"], m["assignment_id"], me, now, op["op_id"]),
                )  # fmt: skip
                await _audit(
                    db, me, sid, "settings.azure_grant", "allow",
                    {"op_id": str(op["op_id"]), "scope": m["scope"], "role": m["role_name"],
                     "role_definition_id": m["role_definition_id"], "principal": m["principal"],
                     "principal_id": m["principal_id"], "actions": m["actions"],
                     "assignment_id": m["assignment_id"],
                     "uncertain": bool(m.get("uncertain"))},
                )  # fmt: skip
        if error:
            raise az.ConnectError(f"{error} ({len(made)} of {len(items)} grants made)")
        return f"Made {len(made)} grants."
    if purpose == "remove":
        ids = [int(g["grant_id"]) if isinstance(g, dict) else int(g) for g in op["plan"] or []]
        async with svc.catalog.read() as db:
            active = await az.active_grants(db)
        grants = [g for g in active if g["grant_id"] in ids]
        if not grants:
            return "Nothing to remove: those grants are already gone."
        still = {g["role_definition_id"] for g in active if g["grant_id"] not in ids}
        done, error = await az.remove_with(arm, grants, still)
        async with svc.catalog.tx(f"user:{me}") as db:
            sid = await _sid(db, me)
            for g in grants:
                if g["grant_id"] not in done:
                    continue
                await db.execute(
                    "UPDATE catalog.azure_grant SET state = 'removed', removed_at = %s,"
                    " removed_by = %s WHERE grant_id = %s AND state = 'active'",
                    (now, me, g["grant_id"]),
                )
                await _audit(
                    db, me, sid, "settings.azure_grant_remove", "allow",
                    {"op_id": str(op["op_id"]), "grant_id": str(g["grant_id"]),
                     "scope": g["scope"], "role": g["role_name"],
                     "assignment_id": g["assignment_id"]},
                )  # fmt: skip
        if error:
            raise az.ConnectError(f"{error} ({len(done)} of {len(grants)} removed)")
        return f"Removed {len(done)} grants."
    raise az.ConnectError(f"unknown purpose {purpose}")
