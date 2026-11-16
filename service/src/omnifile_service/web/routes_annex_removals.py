"""Removals from a NAS in the portal (portal-api.md section 5.23; phase A2.2;
decision 59).

- `POST /v1/annexes/{annex_id}/removals/confirm`: the end of the dry run.
  Removals never start by themselves. An ArchiveAdmin who has reviewed what
  the dry run would have removed confirms it, signed in within the last few
  minutes, for the running rule set version. That can't be the person who
  proposed the rule set, nor anyone answerable for the app it came from.
  A new version needs its own confirmation.
- `GET /v1/annexes/{annex_id}/removals?state=pending|done|cancelled`: the
  removals and their steps.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from ..annex import removals
from ..api.validate import rfc3339
from ..apiclients import registry as api_clients
from ..apiclients.access import require_owner_access
from ..catalog.read_set import readable_page_sql
from .access import acl_params
from .context import Ctx, WebResult, audit, etag, if_match, page
from .problems import WebError, forbidden, invalid, role_required

STATES = {
    "pending": ("ticketed", "hidden", "deleting"),
    "done": ("deleted", "done"),
    "cancelled": ("cancelled",),
    # Hidden files the edge couldn't put back: the Owners' to sort out.
    "stuck": ("stuck",),
}
#: Sample paths of the dry run's would-remove list shown before a confirmation.
SAMPLES = 20


async def would_remove(db, a: dict[str, Any], since) -> dict[str, Any]:
    """What the dry run says the running rules would remove (decision 59): the
    distinct files of its `would_remove` events since the version was
    approved, their bytes, and a sample."""
    rows = await (
        await db.execute(
            "SELECT DISTINCT ON (e.file_id) e.path, coalesce(e.size, af.size) AS size"
            "  FROM catalog.annex_event e"
            "  LEFT JOIN catalog.annex_file af ON af.annex_id = e.annex_id"
            "       AND af.file_id = e.file_id"
            " WHERE e.annex_id = %s AND e.kind = 'would_remove' AND e.at > %s"
            " ORDER BY e.file_id, e.event_id DESC",
            (a["annex_id"], since),
        )
    ).fetchall()
    return {
        "files": len(rows),
        "bytes": sum(int(r["size"] or 0) for r in rows),
        "samples": sorted(r["path"] for r in rows if r["path"])[:SAMPLES],
    }


def _routes():
    from . import routes_annex

    return routes_annex


async def confirm_removals(ctx: Ctx) -> WebResult:
    who = ctx.who
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    if who.identity_id is None:
        raise forbidden("your AD identity is not known yet; sign in again")
    ctx.svc.auth.require_recent_auth(who)
    r = _routes()
    async with ctx.svc.catalog.tx(who.actor) as db:
        a = await r._annex(db, ctx.path_id("annex_id"), who, lock=True)
        if_match(ctx.request, a["entity_version"])
        rs = await (
            await db.execute(
                "SELECT * FROM catalog.annex_ruleset WHERE annex_id = %s AND state = 'active'",
                (a["annex_id"],),
            )
        ).fetchone()
        if rs is None or not rs["removes"]:
            raise WebError("version_conflict", "the running rule set removes nothing")
        if a["dry_run_until"] is None or a["dry_run_until"] > ctx.now:
            raise WebError("version_conflict", "the dry run hasn't ended: it runs until "
                           f"{rfc3339(a['dry_run_until'])}")  # fmt: skip
        if a["removals_confirmed_version"] != rs["version"]:
            # Decision 59: not the proposal's own person, nor anyone answerable
            # for the app it came from.
            if rs["owner_identity_id"] == who.identity_id:
                raise WebError("self_approval", "you proposed these rules, so another"
                               " ArchiveAdmin confirms their dry run")  # fmt: skip
            if rs["proposed_via"] == "api" and rs["api_client_id"] is not None:
                client = await api_clients.load(db, int(rs["api_client_id"]))
                if who.identity_id in await api_clients.answerable(
                    db,
                    {
                        "api_client_id": rs["api_client_id"],
                        "key_id": rs["key_id"],
                        "owner_identity_id": client["owner_identity_id"] if client else 0,
                    },
                ):
                    raise WebError("self_approval", "you answer for the app these rules came"
                                   " from, so another ArchiveAdmin confirms")  # fmt: skip
            would = await would_remove(db, a, rs["decided_at"])
            would["n"] = would["files"]
            await db.execute(
                "UPDATE catalog.annex SET removals_confirmed_version = %s,"
                " removals_confirmed_by = %s, removals_confirmed_at = now() WHERE annex_id = %s",
                (rs["version"], who.identity_id, a["annex_id"]),
            )
            await db.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, details)"
                " VALUES (%s, 'settings', %s)",
                (a["annex_id"], Jsonb({"by": who.actor, "removals_confirmed": rs["version"],
                                       "would_remove": would["n"]})),
            )  # fmt: skip
            await audit(db, who, "annex.removals.confirm", dataset_id=a["dataset_id"],
                        details={"annex_id": str(a["annex_id"]), "version": rs["version"],
                                 "would_remove": would["n"]})  # fmt: skip
        a = await r._annex(db, a["annex_id"], r._AllSeeing)
    return WebResult(200, r.annex_json(a), headers={"ETag": etag(a["entity_version"])})


def _removal_json(x: dict[str, Any]) -> dict[str, Any]:
    return {
        "removal_id": str(x["removal_id"]),
        "file_id": str(x["file_id"]),
        "version_id": str(x["version_id"]),
        "path": x["path"],
        "size": x["size"],
        "rule": x["rule"],
        "ruleset_version": x["ruleset_version"],
        "state": x["state"],
        "issued_at": rfc3339(x["issued_at"]),
        "hidden_at": rfc3339(x["hidden_at"]),
        "deleted_at": rfc3339(x["deleted_at"]),
        "done_at": rfc3339(x["done_at"]),
        "pointer_written": x["pointer_written"],
        "cancel_reason": x["cancel_reason"],
    }


async def list_removals(ctx: Ctx) -> WebResult:
    state = ctx.query("state")
    if state is not None and state not in STATES:
        raise invalid("state", "pending, done or cancelled")
    r = _routes()
    limit = ctx.limit()
    after = ctx.cursor("annex-removals")
    before_id = int(after[0]) if after else None
    async with ctx.svc.catalog.read() as db:
        a = await r._annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        if ctx.who.is_app:
            vis, params = "true", {}
        else:
            params = acl_params(ctx.who)
            vis = "(acl.acl_id IS NOT NULL AND " + readable_page_sql("f.file_id") + ")"
        rows = await (
            await db.execute(
                "SELECT x.* FROM catalog.annex_removal x"
                "  JOIN catalog.file f ON f.file_id = x.file_id"
                "  LEFT JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
                " WHERE x.annex_id = %(a)s"
                "   AND (%(states)s::text[] IS NULL OR x.state = ANY(%(states)s::text[]))"
                "   AND (%(before)s::bigint IS NULL OR x.removal_id < %(before)s::bigint)"
                f"   AND {vis} ORDER BY x.removal_id DESC LIMIT %(limit)s",
                {
                    "a": a["annex_id"],
                    "states": list(STATES[state]) if state else None,
                    "before": before_id,
                    "limit": limit + 1,
                    **params,
                },
            )  # fmt: skip
        ).fetchall()
        lv = await removals.live(db, a["annex_id"], ctx.now)
        rs = await (
            await db.execute(
                "SELECT decided_at FROM catalog.annex_ruleset WHERE annex_id = %s"
                " AND state = 'active'",
                (a["annex_id"],),
            )
        ).fetchone()
        # Counts and names of an unfiltered listing (as #57's dry runs):
        # ArchiveAdmins and apps only.
        sees = ctx.who.is_app or ctx.who.has("ArchiveAdmin")
        would = (
            await would_remove(db, a, rs["decided_at"])
            if sees and rs and rs["decided_at"]
            else None
        )
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = ctx.next_cursor("annex-removals", [str(rows[-1]["removal_id"])]) if more else None
    return WebResult(200, {**page([_removal_json(x) for x in rows], nxt),
                           "live": bool(lv["live"]),
                           "why_not": removals.why_not_live(lv, ctx.now),
                           "would_remove": would})  # fmt: skip
