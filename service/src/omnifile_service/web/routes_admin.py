"""Price tables, who sees prices, and the audit log (portal-api.md 5.8, 5.9, 5.24)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from ..api.validate import rfc3339, timestamp
from ..catalog.read_set import readable_page_sql
from .access import acl_params, share_roles
from .context import Ctx, Principal, WebResult, audit, etag, if_match, page
from .prices import ROLES
from .problems import forbidden, invalid, role_required

KINDS = ("azure_blob", "s3", "sim")


def _kind(ctx: Ctx) -> str:
    kind = ctx.request.path_params["backend_kind"]
    if kind not in KINDS:
        raise invalid("backend_kind", "must be azure_blob, s3 or sim")
    return kind


async def _override(db, kind: str, *, lock: bool = False) -> dict[str, Any] | None:
    return await (
        await db.execute(
            "SELECT * FROM catalog.price_table WHERE backend_kind = %s AND region = ''"
            + (" FOR UPDATE" if lock else ""),
            (kind,),
        )
    ).fetchone()


def _table(ctx: Ctx, kind: str, row: dict[str, Any] | None) -> WebResult:
    out = ctx.svc.preview.price_table(kind, row["body"] if row else None)
    out["etag"] = etag(row["entity_version"] if row else 0)
    # The list prices (the built-in table), for the editor's "reset to list price".
    builtin = ctx.svc.preview.price_table(kind, None)
    out["list"] = {k: builtin[k] for k in ("id", "region", "currency", "as_of", "classes")}
    return WebResult(200, out, headers={"ETag": out["etag"]})


async def get_price_table(ctx: Ctx) -> WebResult:
    if not ctx.who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")
    if not ctx.who.has("ArchiveAdmin") and not await ctx.svc.prices.visible(
        ctx.svc.catalog, ctx.who
    ):
        raise forbidden("an ArchiveAdmin has hidden prices from your role")
    kind = _kind(ctx)
    async with ctx.svc.catalog.read() as db:
        row = await _override(db, kind)
    return _table(ctx, kind, row)


async def put_price_table(ctx: Ctx) -> WebResult:
    who = ctx.who
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    if who.identity_id is None:
        raise invalid("identity", "setting prices needs an AD identity")
    kind = _kind(ctx)
    body = {k: v for k, v in ctx.body.items() if k not in ("overridden", "etag", "list")}
    if body.get("backend_kind", kind) != kind:
        raise invalid("backend_kind", "must match the path")
    body["backend_kind"] = kind
    # Validates the shape (400 invalid_request naming the problem).
    ctx.svc.preview.price_table(kind, body)
    try:
        as_of = date.fromisoformat(str(body.get("as_of")))
    except ValueError:
        raise invalid("as_of", "must be a date") from None
    async with ctx.svc.catalog.tx(who.actor) as db:
        row = await _override(db, kind, lock=True)
        if_match(ctx.request, row["entity_version"] if row else 0)
        row = await (
            await db.execute(
                "INSERT INTO catalog.price_table (backend_kind, region, body, as_of,"
                " set_by_identity_id, set_at) VALUES (%s, '', %s::jsonb, %s, %s, %s)"
                " ON CONFLICT (backend_kind, region) DO UPDATE SET body = EXCLUDED.body,"
                " as_of = EXCLUDED.as_of, set_by_identity_id = EXCLUDED.set_by_identity_id,"
                " set_at = EXCLUDED.set_at,"
                " entity_version = catalog.price_table.entity_version + 1 RETURNING *",
                (kind, json.dumps(body), as_of, who.identity_id, ctx.now),
            )
        ).fetchone()
        await audit(db, who, "price_table.set",
                    details={"backend_kind": kind, "as_of": as_of.isoformat()})  # fmt: skip
    return _table(ctx, kind, row)


async def delete_price_table(ctx: Ctx) -> WebResult:
    who = ctx.who
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    kind = _kind(ctx)
    async with ctx.svc.catalog.tx(who.actor) as db:
        row = await _override(db, kind, lock=True)
        if_match(ctx.request, row["entity_version"] if row else 0)
        if row is not None:
            await db.execute(
                "DELETE FROM catalog.price_table WHERE backend_kind = %s AND region = ''", (kind,)
            )
            await audit(db, who, "price_table.set",
                        details={"backend_kind": kind, "override": "removed"})  # fmt: skip
    return _table(ctx, kind, None)


async def list_audit(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin = who.has("ArchiveAdmin")
    share_id = ctx.query_id("share_id")
    limit = ctx.limit()
    after = ctx.cursor("audit")
    async with ctx.svc.catalog.read() as db:
        if not admin:
            if share_id is None:
                raise role_required("ArchiveAdmin")
            s = await (
                await db.execute("SELECT * FROM catalog.share WHERE share_id = %s", (share_id,))
            ).fetchone()
            if s is None or not share_roles(who, s).owner:
                raise role_required("ArchiveAdmin", "the share's Owners")
        params: dict[str, Any] = {
            "share": share_id,
            "file": ctx.query_id("file_id"),
            "actor": ctx.query("actor"),
            "action": ctx.query("action"),
            "from": timestamp(ctx.query("from"), "from") if ctx.query("from") else None,
            "to": timestamp(ctx.query("to"), "to") if ctx.query("to") else None,
            "a_at": None,
            # Owners see rows about files only if they can read them (decision
            # 28: roles never grant read).
            "admin": admin,
            **(acl_params(who) if not admin else {"sid": "S-1-0-0", "groups": []}),
            "a_id": None,
            "limit": limit + 1,
        }
        for f in ("actor", "action"):
            if params[f] is not None and len(params[f]) > 128:
                raise invalid(f, "at most 128 characters")
        if after is not None:
            params["a_at"] = timestamp(after[0], "cursor")
            params["a_id"] = int(after[1])
        found = await (
            await db.execute(
                f"""
                SELECT a.*, i.display_name AS actor_display_name
                  FROM catalog.audit_log a
                  LEFT JOIN catalog.identity i
                         ON i.identity_id = CASE WHEN a.actor ~ '^user:[0-9]{{1,18}}$'
                                                 THEN substr(a.actor, 6)::bigint END
                 WHERE (%(share)s::bigint IS NULL OR a.share_id = %(share)s::bigint)
                   AND (%(file)s::bigint IS NULL OR a.file_id = %(file)s::bigint)
                   AND (%(actor)s::text IS NULL OR a.actor = %(actor)s::text)
                   AND (%(action)s::text IS NULL OR a.action = %(action)s::text)
                   AND (%(from)s::timestamptz IS NULL OR a.at >= %(from)s::timestamptz)
                   AND (%(to)s::timestamptz IS NULL OR a.at < %(to)s::timestamptz)
                   AND (%(admin)s OR a.file_id IS NULL OR EXISTS (
                        SELECT 1 FROM catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id
                         WHERE f.file_id = a.file_id AND {readable_page_sql("f.file_id")}))
                   AND (%(a_at)s::timestamptz IS NULL
                        OR (a.at, a.audit_id) < (%(a_at)s::timestamptz, %(a_id)s::bigint))
                 ORDER BY a.at DESC, a.audit_id DESC LIMIT %(limit)s
                """,
                params,
            )
        ).fetchall()
    more = len(found) > limit
    found = list(found[:limit])
    items = [
        {
            "audit_id": str(a["audit_id"]),
            "at": rfc3339(a["at"]),
            "actor": a["actor"],
            "actor_display_name": a["actor_display_name"],
            "actor_sid": a["actor_sid"],
            "proof": a["proof"],
            "trust_level": a["trust_level"],
            "action": a["action"],
            "decision": a["decision"],
            "dataset_id": None if a["dataset_id"] is None else str(a["dataset_id"]),
            "share_id": None if a["share_id"] is None else str(a["share_id"]),
            "file_id": None if a["file_id"] is None else str(a["file_id"]),
            "version_id": None if a["version_id"] is None else str(a["version_id"]),
            "client_addr": str(a["client_addr"]) if admin and a["client_addr"] else None,
            "details": a["details"] or {},
        }
        for a in found
    ]
    nxt = None
    if more:
        last = found[-1]
        nxt = ctx.next_cursor("audit", [last["at"].isoformat(), last["audit_id"]])
    return WebResult(200, page(items, nxt))


# --- who sees prices (section 5.24; prices.py) ----------------------------------

_DISPLAY = """
SELECT p.*, i.display_name AS set_by FROM catalog.price_display p
  LEFT JOIN catalog.identity i ON i.identity_id = p.set_by_identity_id
"""


def _display_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "show_to": [k for k in ROLES if k in r["show_to"]],
        "set_by": r["set_by"],
        "set_at": rfc3339(r["set_at"]),
    }


def _settings_admin(who: Principal) -> None:
    """Settings are for signed-in ArchiveAdmins (section 5.20), never an app."""
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


async def get_price_display(ctx: Ctx) -> WebResult:
    _settings_admin(ctx.who)
    async with ctx.svc.catalog.read() as db:
        r = await (await db.execute(_DISPLAY)).fetchone()
    return WebResult(200, _display_json(r), headers={"ETag": etag(r["entity_version"])})


async def put_price_display(ctx: Ctx) -> WebResult:
    """`PUT /v1/settings/prices {show_to}`: which roles see prices. Audited."""
    who = ctx.who
    _settings_admin(who)
    if who.identity_id is None:
        raise invalid("identity", "changing who sees prices needs an AD identity")
    extra = set(ctx.body) - {"show_to"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a price display field")
    show = ctx.body.get("show_to")
    if (
        not isinstance(show, list)
        or not all(isinstance(k, str) and k in ROLES for k in show)
        or len(set(show)) != len(show)
    ):
        raise invalid("show_to", "a list of distinct roles from " + ", ".join(ROLES))
    show = [k for k in ROLES if k in show]
    async with ctx.svc.catalog.tx(who.actor) as db:
        r = await (await db.execute(_DISPLAY + " FOR UPDATE OF p")).fetchone()
        if_match(ctx.request, r["entity_version"])
        before = [k for k in ROLES if k in r["show_to"]]
        await db.execute(
            "UPDATE catalog.price_display SET show_to = %s, set_by_identity_id = %s, set_at = %s",
            (show, who.identity_id, ctx.now),
        )
        await audit(db, who, "settings.prices.visibility", details={"from": before, "to": show})
        r = await (await db.execute(_DISPLAY)).fetchone()
    ctx.svc.prices.forget()
    return WebResult(200, _display_json(r), headers={"ETag": etag(r["entity_version"])})
