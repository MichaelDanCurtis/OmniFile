"""Recalls ahead of time and the four-step view (portal-api.md 5.5)."""

from __future__ import annotations

from typing import Any

from ..api.validate import parse_id, timestamp
from .access import FileScope, app_dataset_scope, load_scope
from .context import Ctx, WebResult, audit, client_addr, page
from .problems import WebError, invalid, not_found, role_required
from .recalls import archived, recall_json, recall_options, start_or_join, version_location
from .routes_browse import readable_version


async def request_recall(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"file_id", "version_id", "priority", "due_by"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a recall field")
    if "file_id" not in b:
        raise invalid("file_id", "is required")
    file_id = parse_id(b["file_id"], "file_id")
    version_id = parse_id(b["version_id"], "version_id") if "version_id" in b else None
    priority = b.get("priority")
    # Any string: checked below against the options this file offers (an open set,
    # RecallOption.priority), so a new priority needs no contract change.
    if not isinstance(priority, str) or not 0 < len(priority) <= 32:
        raise invalid("priority", "must be one of the file's offered recall options")
    due_by = timestamp(b["due_by"], "due_by") if "due_by" in b else None
    now = ctx.now
    async with ctx.svc.catalog.tx(who.actor) as db:
        scope: FileScope | None = None
        if who.is_app:
            if not who.has("Recall.Request"):
                raise role_required("Recall.Request")
            f = await app_dataset_scope(db, file_id)
        else:
            if who.identity_id is None:
                raise not_found()
            scope = (await load_scope(db, who, file_id)).require_read()
            f = scope.file
        vid = version_id or f["content_version_id"]
        if vid is None:
            raise WebError("recall_not_needed", "the file has no stored bytes yet")
        if scope is not None:
            await readable_version(db, ctx, scope, vid)
        loc = await version_location(db, file_id, vid)
        if loc["version_state"] != "committed" or loc["location_id"] is None:
            raise not_found("version")
        if not archived(loc, now):
            raise WebError("recall_not_needed", f"the file is online ({f['state']})")
        options = (await recall_options(db, loc, now, None))["options"]
        offered = [o["priority"] for o in options if o["offered"]] or ["standard"]
        if priority not in offered:
            raise invalid(
                "priority", f"is not offered for this file (offered: {', '.join(offered)})"
            )
        recall, joined = await start_or_join(
            db, file=f, loc=loc, priority=priority, due_by=due_by,
            identity_id=who.identity_id, app_id=who.app_id, now=now,
        )  # fmt: skip
        await audit(
            db, who, "recall.request", dataset_id=f["dataset_id"],
            share_id=scope.share["share_id"] if scope and scope.share else None,
            file_id=file_id, version_id=loc["content_id"], client_addr=client_addr(ctx.request),
            details={"recall_id": str(recall["recall_id"]), "joined": joined,
                     "priority": priority, "via": "api" if who.is_app else "portal"},
        )  # fmt: skip
        body = await recall_json(
            db, recall,
            name=f["name"] if scope is not None else None,
            share_id=scope.share["share_id"] if scope and scope.share else None,
            parent_id=f["parent_id"] if scope is not None else None,
            me=True,
        )  # fmt: skip
    body["joined"] = joined
    return WebResult(200 if joined else 201, body)


async def _view(ctx: Ctx, db, r: dict[str, Any], *, me: bool) -> dict[str, Any] | None:
    """The recall as this caller may see it: None if neither a requester nor
    a reader of the file. Without read, the name and share are hidden."""
    try:
        scope = await load_scope(db, ctx.who, r["file_id"], include_deleted=True)
        can = scope.readable
    except WebError as e:
        if e.code != "not_found":
            raise
        scope, can = None, False
    if not (me or can):
        return None
    return await recall_json(
        db, r,
        name=scope.file["name"] if can and scope else None,
        share_id=scope.share["share_id"] if can and scope and scope.share else None,
        parent_id=scope.file["parent_id"] if can and scope else None,
        me=me,
    )  # fmt: skip


async def _is_requester(db, ctx: Ctx, recall_id: int) -> bool:
    if ctx.who.identity_id is None:
        return False
    row = await (
        await db.execute(
            "SELECT 1 FROM catalog.recall_requester WHERE recall_id = %s AND identity_id = %s",
            (recall_id, ctx.who.identity_id),
        )
    ).fetchone()
    return row is not None


async def get_recall(ctx: Ctx) -> WebResult:
    rid = ctx.path_id("recall_id")
    async with ctx.svc.catalog.read() as db:
        r = await (
            await db.execute("SELECT * FROM catalog.recall WHERE recall_id = %s", (rid,))
        ).fetchone()
        if r is None:
            raise not_found("recall")
        body = await _view(ctx, db, r, me=await _is_requester(db, ctx, rid))
    if body is None:
        raise not_found("recall")
    return WebResult(200, body)


STATES = ("requested", "rehydrating", "copying", "downloading", "available", "failed", "cancelled",
          "superseded")  # fmt: skip


async def list_recalls(ctx: Ctx) -> WebResult:
    who = ctx.who
    scope_name = ctx.query("scope") or "mine"
    if scope_name not in ("mine", "share", "all"):
        raise invalid("scope", "must be mine, share or all")
    state = ctx.query("state")
    if state is not None and state not in STATES:
        raise invalid("state", "is not a recall state")
    limit = ctx.limit()
    after = ctx.cursor("recalls")
    where = ["(%(state)s::text IS NULL OR r.state = %(state)s::text)",
             "(%(after)s::bigint IS NULL OR r.recall_id < %(after)s::bigint)"]  # fmt: skip
    params: dict[str, Any] = {"state": state, "after": int(after[0]) if after else None,
                              "limit": limit + 1, "me": who.identity_id}  # fmt: skip
    async with ctx.svc.catalog.read() as db:
        if scope_name == "mine":
            if who.identity_id is None:
                return WebResult(200, page([], None))
            where.append(
                "EXISTS (SELECT 1 FROM catalog.recall_requester q"
                " WHERE q.recall_id = r.recall_id AND q.identity_id = %(me)s)"
            )
        elif scope_name == "share":
            share_id = ctx.query_id("share_id")
            if share_id is None:
                raise invalid("share_id", "is required for scope=share")
            s = await (
                await db.execute("SELECT * FROM catalog.share WHERE share_id = %s", (share_id,))
            ).fetchone()
            from .access import share_roles

            if s is None or not (who.has("ArchiveAdmin") or share_roles(who, s).owner):
                raise WebError("forbidden", "scope=share needs the share's Owners or ArchiveAdmin")
            where.append(
                "f.dataset_id = %(ds)s AND (%(root)s::bigint IS NULL OR EXISTS ("
                " SELECT 1 FROM catalog.file_ancestors(r.file_id) a WHERE a.file_id = %(root)s))"
            )
            params.update({"ds": s["dataset_id"], "root": s["root_dir_id"]})
        elif not who.has("ArchiveAdmin"):
            raise role_required("ArchiveAdmin")
        found = await (
            await db.execute(
                "SELECT r.* FROM catalog.recall r JOIN catalog.file f ON f.file_id = r.file_id"
                " WHERE " + " AND ".join(where) + " ORDER BY r.recall_id DESC LIMIT %(limit)s",
                params,
            )
        ).fetchall()
        more = len(found) > limit
        found = list(found[:limit])
        items = []
        for r in found:
            me = await _is_requester(db, ctx, r["recall_id"])
            view = await _view(ctx, db, r, me=True if scope_name != "mine" else me)
            if view is not None:
                if scope_name != "mine":
                    view["requesters"]["me"] = me
                items.append(view)
    nxt = ctx.next_cursor("recalls", [found[-1]["recall_id"]]) if more else None
    return WebResult(200, page(items, nxt))
