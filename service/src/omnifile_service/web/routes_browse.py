"""Browse, file detail, versions and recall options (portal-api.md 5.3-5.5)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..api.validate import rfc3339, timestamp
from ..catalog.read_set import readable_in_folder_sql, readable_row_sql
from . import rows
from .access import acl_params, load_scope
from .context import Ctx, WebResult, page
from .entries import decorate
from .facts import price_override
from .problems import WebError, invalid, not_found
from .recalls import recall_options, version_location

VERSION_TIERS = """
SELECT DISTINCT ON (l.version_id) l.version_id, l.location_id, l.logical_tier, l.storage_class,
       l.explicit_tier_set, l.tier_changed_at, r.state AS restore_state,
       r.expires_at AS restore_expires_at
  FROM catalog.location l
  LEFT JOIN LATERAL (
        SELECT state, expires_at FROM catalog.restore
         WHERE location_id = l.location_id AND state <> 'cancelled'
         ORDER BY requested_at DESC, restore_id DESC LIMIT 1) r ON true
 WHERE l.version_id = ANY(%s) AND l.role = 'primary' AND l.state = 'committed'
 ORDER BY l.version_id, l.location_id
"""


async def version_tiers(db, content_ids: list[int]) -> dict[int, dict[str, Any]]:
    ids = [i for i in content_ids if i is not None]
    if not ids:
        return {}
    return {r["version_id"]: r for r in await (await db.execute(VERSION_TIERS, (ids,))).fetchall()}


async def depth_days(db, dataset_id: int, folder_id: int | None) -> int:
    row = await (
        await db.execute("SELECT catalog.rewind_depth_days(%s, %s) AS d", (dataset_id, folder_id))
    ).fetchone()
    return int(row["d"] or 0)


async def list_children(ctx: Ctx) -> WebResult:
    who = ctx.who
    dir_id = ctx.path_id("dir_id")
    limit = ctx.limit(100, 1000)
    raw_as_of = ctx.query("as_of")
    after = ctx.cursor("children")
    now = ctx.now
    async with ctx.svc.catalog.read() as db:
        scope = (await load_scope(db, who, dir_id)).require_read("folder")
        if scope.file["kind"] != "dir":
            raise not_found("folder")
        depth = await depth_days(db, scope.dataset_id, dir_id)
        header = {
            "file_id": str(dir_id),
            "name": scope.file["name"],
            "share_id": str(scope.share["share_id"]),  # type: ignore[index]
            "path": scope.path(),
            "rewind_depth_days": depth,
        }
        params = {**acl_params(who), "d": dir_id, "after": after[0] if after else None,
                  "limit": limit + 1}  # fmt: skip
        if raw_as_of is None:
            found = await (
                await db.execute(
                    f"""
                    SELECT f.* FROM catalog.file f
                      JOIN catalog.acl acl ON acl.acl_id = f.acl_id
                     WHERE f.parent_id = %(d)s AND f.deleted_at IS NULL
                       AND {readable_in_folder_sql("f.file_id", "%(d)s::bigint")}
                       AND (%(after)s::text IS NULL OR f.name > %(after)s::text)
                     ORDER BY f.name LIMIT %(limit)s
                    """,
                    params,
                )
            ).fetchall()
            more = len(found) > limit
            found = list(found[:limit])
            items = await decorate(db, found, scope.ancestor_ids, now)
            as_of = None
        else:
            at = timestamp(raw_as_of, "as_of")
            if at > now:
                raise invalid("as_of", "is in the future")
            if at < now - timedelta(days=depth):
                raise WebError("beyond_rewind_depth", f"this share keeps {depth} days of versions")
            params["at"] = at
            found = await (
                await db.execute(
                    f"""
                    SELECT v.*, f.kind AS file_kind, f.deleted_at AS file_deleted_at
                      FROM catalog.folder_as_of(%(d)s, %(at)s) v
                      JOIN catalog.file f ON f.file_id = v.file_id
                      JOIN catalog.acl acl ON acl.acl_id = v.acl_id
                     WHERE {readable_in_folder_sql("v.file_id", "%(d)s::bigint")}
                       AND (%(after)s::text IS NULL OR v.name > %(after)s::text)
                     ORDER BY v.name COLLATE "C" LIMIT %(limit)s
                    """,
                    params,
                )
            ).fetchall()
            more = len(found) > limit
            found = list(found[:limit])
            content = {
                v["version_id"]: (v["version_id"] if v["kind"] == "content"
                                  else v["content_version_id"])
                for v in found
            }  # fmt: skip
            tiers = await version_tiers(db, list(content.values()))
            items = []
            for v in found:
                cid = content[v["version_id"]]
                loc = tiers.get(cid)
                items.append({
                    "file_id": str(v["file_id"]),
                    "name": v["name"],
                    "kind": v["file_kind"],
                    "version_id": str(v["version_id"]),
                    "content_version_id": rows.sid_(cid),
                    "version_kind": v["kind"],
                    "committed_at": rfc3339(v["committed_at"]),
                    "size": v["size"],
                    "mtime_ns": rows.ns(v["mtime_ns"]),
                    "tier": rows.tier_state(loc, now) if loc else None,
                    "live": v["file_deleted_at"] is None,
                    "current": v["superseded_at"] is None,
                })  # fmt: skip
            as_of = rfc3339(at)
    nxt = ctx.next_cursor("children", [found[-1]["name"]]) if more else None
    return WebResult(200, {"dir": header, "as_of": as_of, **page(items, nxt)})


def _hex(b: bytes | None) -> str | None:
    return None if b is None else bytes(b).hex()


async def get_file(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    async with ctx.svc.catalog.read() as db:
        scope = (await load_scope(db, who, ctx.path_id("file_id"))).require_read()
        f = scope.file
        [entry] = await decorate(db, [f], scope.ancestor_ids[1:], now)
        cv = None
        if f["content_version_id"]:
            cv = await (
                await db.execute(
                    "SELECT retention_until, legal_hold FROM catalog.file_version"
                    " WHERE version_id = %s",
                    (f["content_version_id"],),
                )
            ).fetchone()
        count = await (
            await db.execute(
                "SELECT count(*) AS n FROM catalog.file_version WHERE file_id = %s"
                " AND state = 'committed'",
                (f["file_id"],),
            )
        ).fetchone()
        depth = await depth_days(db, scope.dataset_id, f["parent_id"] or f["file_id"])
    entry.update({
        "share_id": str(scope.share["share_id"]),  # type: ignore[index]
        "dataset_id": str(scope.dataset_id),
        "path": scope.path(),
        "blake3": _hex(f["blake3"]),
        "crc64nvme": _hex(f["crc64nvme"]),
        "owner_sid": f["owner_sid"],
        "uid": f["uid"],
        "gid": f["gid"],
        "mode": rows.mode_str(f["mode"]),
        "written_at": rfc3339(f["written_at"]),
        "last_read_at": rfc3339(f["last_read_at"]),
        "retention_until": rfc3339(cv["retention_until"]) if cv else None,
        "legal_hold": bool(cv["legal_hold"]) if cv else False,
        "versions_count": count["n"],
        "rewind_depth_days": depth,
    })  # fmt: skip
    return WebResult(200, entry)


def version_json(v: dict[str, Any], loc: dict[str, Any] | None, now) -> dict[str, Any]:
    cid = v["version_id"] if v["kind"] == "content" else v["content_version_id"]
    return {
        "version_id": str(v["version_id"]),
        "seq": v["seq"],
        "kind": v["kind"],
        "state": v["state"],
        "content_version_id": rows.sid_(cid),
        "name": v["name"],
        "parent_id": rows.sid_(v["parent_id"]),
        "committed_at": rfc3339(v["committed_at"]),
        "superseded_at": rfc3339(v["superseded_at"]),
        "size": v["size"],
        "mtime_ns": rows.ns(v["mtime_ns"]),
        "tier": rows.tier_state(loc, now) if loc else None,
        "retention_until": rfc3339(v["retention_until"]),
        "legal_hold": bool(v["legal_hold"]),
        "restorable": v["kind"] != "tombstone" and v["state"] == "committed" and loc is not None,
    }


async def list_versions(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    limit = ctx.limit()
    after = ctx.cursor("versions")
    async with ctx.svc.catalog.read() as db:
        scope = (
            await load_scope(db, who, ctx.path_id("file_id"), include_deleted=True)
        ).require_read()
        f = scope.file
        depth = await depth_days(db, scope.dataset_id, f["parent_id"] or f["file_id"])
        found = await (
            await db.execute(
                f"""
                SELECT v.* FROM catalog.file_version v
                  LEFT JOIN catalog.acl acl ON acl.acl_id = v.acl_id
                 WHERE v.file_id = %(f)s AND v.state = 'committed'
                   AND (v.superseded_at IS NULL OR v.superseded_at >= %(since)s
                        OR v.retention_until > %(now)s)
                   AND ((v.kind = 'tombstone' AND v.acl_id IS NULL)
                        OR (v.acl_id IS NOT NULL AND {readable_row_sql("v.file_id")}))
                   AND (%(after)s::int IS NULL OR v.seq < %(after)s::int)
                 ORDER BY v.seq DESC LIMIT %(limit)s
                """,
                {
                    **acl_params(who),
                    "f": f["file_id"],
                    "since": now - timedelta(days=depth),
                    "now": now,
                    "after": after[0] if after else None,
                    "limit": limit + 1,
                },
            )  # fmt: skip
        ).fetchall()
        more = len(found) > limit
        found = list(found[:limit])
        content = [v["version_id"] if v["kind"] == "content" else v["content_version_id"]
                   for v in found]  # fmt: skip
        tiers = await version_tiers(db, content)
    items = [version_json(v, tiers.get(c), now) for v, c in zip(found, content, strict=True)]
    nxt = ctx.next_cursor("versions", [found[-1]["seq"]]) if more else None
    return WebResult(200, page(items, nxt))


async def readable_version(db, ctx: Ctx, scope, version_id: int) -> dict[str, Any]:
    """A committed version of the scope's file the caller may read: its own
    ACL at that time, the file now (through `scope`), and inside the share's
    rewind depth or still retained by WORM, as `list_versions` shows them
    (decision 30). Anything else is 404, so guessed version ids reveal nothing."""
    f = scope.file
    depth = await depth_days(db, scope.dataset_id, f["parent_id"] or f["file_id"])
    now = ctx.now
    row = await (
        await db.execute(
            f"""
            SELECT v.*, (v.acl_id IS NOT NULL AND {readable_row_sql("v.file_id")}) AS readable
              FROM catalog.file_version v
              LEFT JOIN catalog.acl acl ON acl.acl_id = v.acl_id
             WHERE v.version_id = %(v)s AND v.file_id = %(f)s
               AND (v.superseded_at IS NULL OR v.superseded_at >= %(since)s
                    OR v.retention_until > %(now)s)
            """,
            {
                **acl_params(ctx.who),
                "v": version_id,
                "f": scope.file_id,
                "since": now - timedelta(days=depth),
                "now": now,
            },
        )  # fmt: skip
    ).fetchone()
    if row is None or not row["readable"]:
        raise not_found("version")
    return row


async def get_recall_options(ctx: Ctx) -> WebResult:
    who = ctx.who
    async with ctx.svc.catalog.read() as db:
        scope = (await load_scope(db, who, ctx.path_id("file_id"))).require_read()
        vid = ctx.query_id("version_id") or scope.file["content_version_id"]
        if vid is None:
            raise not_found("version")
        await readable_version(db, ctx, scope, vid)
        loc = await version_location(db, scope.file_id, vid)
        table = await price_table_or_none(ctx, db, loc)
        out = await recall_options(db, loc, ctx.now, table)
    return WebResult(200, out)


async def price_table_or_none(ctx: Ctx, db, loc: dict[str, Any]) -> dict[str, Any] | None:
    """The price table fee estimates use (the admin's override, if any)."""
    if loc.get("backend_kind") is None:
        return None
    try:
        override = await price_override(db, loc["backend_kind"])
        return ctx.svc.preview.price_table(loc["backend_kind"], override)
    except Exception:  # noqa: BLE001 - a fee estimate is optional
        return None
