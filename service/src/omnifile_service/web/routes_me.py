"""Me, the session, sign-out and the group directory (portal-api.md 2.1, 5.1)."""

from __future__ import annotations

import re
from typing import Any

from ..api.validate import rfc3339
from .access import share_roles
from .context import Ctx, Principal, WebResult, page
from .prices import ROLES, visible_for
from .problems import invalid, role_required

_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")


async def me_json(db, who: Principal) -> dict[str, Any]:
    owner, writer, reader = [], [], []
    for s in await (await db.execute("SELECT * FROM catalog.share ORDER BY share_id")).fetchall():
        r = share_roles(who, s)
        if r.owner:
            owner.append(str(s["share_id"]))
        if r.writer:
            writer.append(str(s["share_id"]))
        if r.reader:
            reader.append(str(s["share_id"]))
    shown = await (await db.execute("SELECT show_to FROM catalog.price_display")).fetchone()
    return {
        "object_id": who.object_id,
        "identity_id": None if who.identity_id is None else str(who.identity_id),
        "display_name": who.display_name,
        "upn": who.upn,
        "sid": who.sid,
        "roles": sorted(who.roles),
        "groups_source": who.groups_source,
        "groups_resolved_at": rfc3339(who.groups_resolved_at),
        "owner_of": owner,
        "writer_of": writer,
        "reader_of": reader,
        # Settings, Prices and alerts (prices.py): without it, the portal shows no costs.
        "prices_visible": visible_for(who, list(shown["show_to"]) if shown else list(ROLES)),
    }


async def get_me(ctx: Ctx) -> WebResult:
    async with ctx.svc.catalog.read() as db:
        return WebResult(200, await me_json(db, ctx.who))


async def get_session(ctx: Ctx) -> WebResult:
    s = ctx.who.session or {}
    async with ctx.svc.catalog.read() as db:
        user = await me_json(db, ctx.who)
    return WebResult(
        200,
        {
            "authenticated": True,
            "user": user,
            "csrf_token": s["csrf_token"],
            "expires_at": rfc3339(s["expires_at"]),
            "idle_expires_at": rfc3339(max(s["idle_expires_at"], s["last_seen_at"])),
            "auth_time": rfc3339(s["auth_time"]),
        },
    )


async def logout(ctx: Ctx) -> WebResult:
    cookie, url = await ctx.svc.auth.logout(ctx.who)
    return WebResult(200, {"logout_url": url}, headers={"set-cookie": cookie})


async def search_groups(ctx: Ctx) -> WebResult:
    who = ctx.who
    limit = ctx.limit()
    q = ctx.query("q")
    if q is not None and not 1 <= len(q) <= 256:
        raise invalid("q", "must be 1 to 256 characters")
    after = ctx.cursor("groups")
    async with ctx.svc.catalog.read() as db:
        if not who.has("ArchiveAdmin"):
            owns = any(
                share_roles(who, s).owner
                for s in await (await db.execute("SELECT * FROM catalog.share")).fetchall()
            )
            if not owns:
                raise role_required("ArchiveAdmin", "share Owner")
        where, params = ["kind = 'group'"], {"limit": limit + 1}
        if q is not None and _SID.match(q):
            where.append("sid = %(q)s")
            params["q"] = q
        elif q is not None:
            like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(display_name ILIKE %(like)s OR sam_account_name ILIKE %(like)s)")
            params["like"] = like
        if after is not None:
            where.append("(coalesce(display_name, sam_account_name, sid) COLLATE \"C\","
                         " identity_id) > (%(an)s, %(ai)s)")  # fmt: skip
            params.update({"an": after[0], "ai": int(after[1])})
        found = await (
            await db.execute(
                "SELECT identity_id, sid, display_name, sam_account_name,"
                ' coalesce(display_name, sam_account_name, sid) COLLATE "C" AS sort'
                " FROM catalog.identity WHERE "
                + " AND ".join(where)
                + " ORDER BY sort, identity_id LIMIT %(limit)s",
                params,
            )
        ).fetchall()
    more = len(found) > limit
    found = found[:limit]
    items = [
        {"sid": g["sid"], "display_name": g["display_name"],
         "sam_account_name": g["sam_account_name"]}
        for g in found
    ]  # fmt: skip
    nxt = ctx.next_cursor("groups", [found[-1]["sort"], found[-1]["identity_id"]]) if more else None
    return WebResult(200, page(items, nxt))
