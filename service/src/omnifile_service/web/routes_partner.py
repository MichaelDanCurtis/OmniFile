"""Partner reads (portal-api.md section 5.11; ARCHITECTURE.md section 6):
catalog rows by path, and the restores users triggered.

Apps need `Archive.Read`. An app acts at dataset level, never ACL-filtered
(section 2.2), so the reads that name files (files, restores, events) are for
apps only: a role never grants read (decision 28, section 3.2), and a signed-in
user sees names through the ACL-filtered browse routes. Usage (totals, no
names) is also open to a signed-in ArchiveAdmin.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..api.validate import rfc3339, timestamp
from ..usage import report as usage_report
from .context import Ctx, Principal, WebResult, page
from .problems import WebError, forbidden, invalid, not_found, role_required

#: Up to this many duplicate file ids are listed per file.
DUPLICATES_SHOWN = 10
RESTORE_STATES = ("requested", "in_progress", "completed", "expired", "failed", "cancelled")
TRIGGERS = ("user_recall", "migration_wave", "api")


def _reader(who: Principal) -> None:
    """Totals without names: an app with `Archive.Read`, or a signed-in ArchiveAdmin."""
    if who.is_app:
        if not who.has("Archive.Read"):
            raise role_required("Archive.Read")
    elif not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def _names_reader(who: Principal) -> None:
    """Rows that name files, unfiltered by ACL: apps with `Archive.Read` only."""
    if not who.is_app:
        raise forbidden(
            "these partner reads name every file and are for apps; "
            "browse the share to see what your groups can read"
        )
    if not who.has("Archive.Read"):
        raise role_required("Archive.Read")


def _hex(b: bytes | None) -> str | None:
    return None if b is None else bytes(b).hex()


def _names(path: str) -> list[str]:
    if not path.startswith("/") or "//" in path or len(path) > 4096:
        raise invalid("path", "must start with '/', the dataset root")
    names = [n for n in path.strip("/").split("/") if n]
    if any(n in (".", "..") for n in names):
        raise invalid("path", "must not contain '.' or '..'")
    return names


_FILE = """
SELECT f.*, l.backend_id, l.container, l.object_key, l.backend_version_id, l.storage_class,
       l.md5,
       (SELECT coalesce('/' || string_agg(a.name, '/' ORDER BY a.depth DESC), '/')
          FROM catalog.file_ancestors(f.file_id) a WHERE a.parent_id IS NOT NULL) AS path,
       (SELECT count(*) FROM catalog.file d
         WHERE f.blake3 IS NOT NULL AND d.blake3 = f.blake3 AND d.file_id <> f.file_id
           AND d.deleted_at IS NULL) AS duplicate_count,
       (SELECT coalesce(array_agg(x.file_id ORDER BY x.file_id), '{}') FROM (
            SELECT d.file_id FROM catalog.file d
             WHERE f.blake3 IS NOT NULL AND d.blake3 = f.blake3 AND d.file_id <> f.file_id
               AND d.deleted_at IS NULL
             ORDER BY d.file_id LIMIT %(dups)s) x) AS duplicate_ids,
       (SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'hold_id', h.hold_id::text, 'kind', h.kind, 'on_file_id', h.file_id::text,
                   'until', h.until, 'set_at', h.set_at) ORDER BY h.hold_id), '[]')
          FROM catalog.hold h
         WHERE h.file_id IN (SELECT a.file_id FROM catalog.file_ancestors(f.file_id) a)
           AND h.released_at IS NULL AND (h.until IS NULL OR h.until > %(now)s)) AS holds
  FROM catalog.file f
  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
"""


def file_json(f: dict[str, Any]) -> dict[str, Any]:
    stored = None
    if f["backend_id"] is not None:
        stored = {
            "backend_id": str(f["backend_id"]),
            "container": f["container"],
            "object_key": f["object_key"],
            "backend_version_id": f["backend_version_id"],
            "storage_class": f["storage_class"],
        }
    return {
        "file_id": str(f["file_id"]),
        "dataset_id": str(f["dataset_id"]),
        "path": f["path"],
        "name": f["name"],
        "kind": f["kind"],
        "size": f["size"],
        "mtime": None if f["mtime_ns"] is None else str(f["mtime_ns"]),
        "state": f["state"],
        "tier": f["remote_tier"],
        "stored": stored,
        "restore": {
            "state": f["restore_state"],
            "expires_at": rfc3339(f["restore_expires_at"]),
        },  # fmt: skip
        "archive_at": None if f["archive_at"] is None else f["archive_at"].isoformat(),
        "next_action": f["next_action"],
        "next_action_at": rfc3339(f["next_action_at"]),
        "hashes": {
            "blake3": _hex(f["blake3"]),
            "crc64nvme": _hex(f["crc64nvme"]),
            "md5": _hex(f["md5"]),
        },  # fmt: skip
        "duplicates": {
            "count": f["duplicate_count"],
            "file_ids": [str(i) for i in f["duplicate_ids"]],
        },  # fmt: skip
        "holds": [
            {**h, "until": rfc3339_str(h["until"]), "set_at": rfc3339_str(h["set_at"])}
            for h in f["holds"]
        ],
        "origin": f["origin"],
        "entity_version": str(f["entity_version"]),
        "deleted_at": rfc3339(f["deleted_at"]),
    }


def rfc3339_str(v: str | None) -> str | None:
    """A timestamp from jsonb (Postgres's ISO form) in the API's RFC 3339 form."""
    return None if v is None else rfc3339(datetime.fromisoformat(v))


async def get_files(ctx: Ctx) -> WebResult:
    """`GET /v1/files?dataset_id&path`: the entry at the path and, for a
    folder, a page of its live children by name."""
    _names_reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    fid = ctx.query_id("file_id")
    raw = ctx.query("path")
    if (raw is None) == (fid is None):
        raise invalid("path", "give exactly one of path (with dataset_id) or file_id")
    limit = ctx.limit()
    after = ctx.cursor("files")
    p = {"now": ctx.now, "dups": DUPLICATES_SHOWN}
    async with ctx.svc.catalog.read() as db:
        if fid is not None:
            # By id, as an event names it: deleted entries too.
            row = await (
                await db.execute(
                    "SELECT file_id FROM catalog.file WHERE file_id = %s"
                    "   AND (%s::bigint IS NULL OR dataset_id = %s::bigint)",
                    (fid, ds, ds),
                )
            ).fetchone()
        else:
            if ds is None:
                raise invalid("dataset_id", "is required with path")
            names = _names(raw or "")
            row = await (
                await db.execute(
                    "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
                    (ds,),
                )
            ).fetchone()
            for name in names:
                if row is None:
                    break
                row = await (
                    await db.execute(
                        "SELECT file_id FROM catalog.file WHERE parent_id = %s AND name = %s"
                        "   AND deleted_at IS NULL",
                        (row["file_id"], name),
                    )
                ).fetchone()
        if row is None:
            raise not_found("file")
        entry = await (
            await db.execute(_FILE + " WHERE f.file_id = %(id)s", {**p, "id": row["file_id"]})
        ).fetchone()
        children: list[dict[str, Any]] = []
        more = False
        if entry["kind"] == "dir" and entry["deleted_at"] is None:
            found = await (
                await db.execute(
                    _FILE + " WHERE f.parent_id = %(id)s AND f.deleted_at IS NULL"
                    '   AND (%(after)s::text IS NULL OR f.name > %(after)s::text COLLATE "C")'
                    ' ORDER BY f.name COLLATE "C" LIMIT %(limit)s',
                    {
                        **p,
                        "id": entry["file_id"],
                        "after": after[0] if after else None,
                        "limit": limit + 1,
                    },  # fmt: skip
                )
            ).fetchall()
            more = len(found) > limit
            children = list(found[:limit])
    nxt = ctx.next_cursor("files", [children[-1]["name"]]) if more else None
    return WebResult(200, {"entry": file_json(entry),
                           **page([file_json(c) for c in children], nxt)})  # fmt: skip


def restore_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "restore_id": str(r["restore_id"]),
        "dataset_id": str(r["dataset_id"]),
        "file_id": str(r["file_id"]),
        "path": r["path"],
        "backend_id": str(r["backend_id"]),
        "container": r["container"],
        "object_key": r["object_key"],
        "backend_version_id": r["backend_version_id"],
        "mechanism": r["mechanism"],
        "speed": r["speed"],
        "state": r["state"],
        "triggered_by": r["triggered_by"],
        "migrate_on_complete": r["migrate_on_complete"],
        "bytes": r["bytes"],
        "requested_at": rfc3339(r["requested_at"]),
        "eta": rfc3339(r["eta"]),
        "completed_at": rfc3339(r["completed_at"]),
        "expires_at": rfc3339(r["expires_at"]),
        "failure": r["failure"],
    }


async def list_restores(ctx: Ctx) -> WebResult:
    """`GET /v1/restores?dataset_id&state&triggered_by&since`: provider
    restores, newest first, so a migration plan can fold them in."""
    _names_reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    state = ctx.query("state")
    if state is not None and state not in RESTORE_STATES:
        raise invalid("state", "is not a restore state")
    trigger = ctx.query("triggered_by")
    if trigger is not None and trigger not in TRIGGERS:
        raise invalid("triggered_by", "is not a restore trigger")
    since = timestamp(ctx.query("since"), "since") if ctx.query("since") else None
    limit = ctx.limit()
    after = ctx.cursor("restores")
    async with ctx.svc.catalog.read() as db:
        found = await (
            await db.execute(
                """
                SELECT r.*, l.file_id, l.container, l.object_key, l.backend_version_id,
                       f.dataset_id,
                       (SELECT coalesce('/' || string_agg(a.name, '/' ORDER BY a.depth DESC), '/')
                          FROM catalog.file_ancestors(l.file_id) a
                         WHERE a.parent_id IS NOT NULL) AS path
                  FROM catalog.restore r
                  JOIN catalog.location l ON l.location_id = r.location_id
                  JOIN catalog.file f ON f.file_id = l.file_id
                 WHERE (%(ds)s::bigint IS NULL OR f.dataset_id = %(ds)s::bigint)
                   AND (%(state)s::text IS NULL OR r.state = %(state)s::text)
                   AND (%(trig)s::text IS NULL OR r.triggered_by = %(trig)s::text)
                   AND (%(since)s::timestamptz IS NULL OR r.requested_at >= %(since)s)
                   AND (%(after)s::bigint IS NULL OR r.restore_id < %(after)s::bigint)
                 ORDER BY r.restore_id DESC LIMIT %(limit)s
                """,
                {
                    "ds": ds,
                    "state": state,
                    "trig": trigger,
                    "since": since,
                    "after": int(after[0]) if after else None,
                    "limit": limit + 1,
                },  # fmt: skip
            )
        ).fetchall()
    more = len(found) > limit
    found = list(found[:limit])
    nxt = ctx.next_cursor("restores", [found[-1]["restore_id"]]) if more else None
    return WebResult(200, page([restore_json(r) for r in found], nxt))


# --- the pull feed -----------------------------------------------------------------

#: Entities a partner sees in the feed; `acl` and `edge` are the edge's own.
EVENT_ENTITIES = ["file", "dataset", "share", "recall"]
EVENTS_MAX = 1000

#: The catalog feed's rows and the typed lifecycle events (migration 0029),
#: merged by (txid, event_id): both take their ids from one sequence, so one
#: cursor orders them, and both are read below the same horizon.
_EVENTS = """
SELECT * FROM (
    SELECT txid, event_id, created_at, dataset_id, entity, entity_id, entity_version,
           entity || '.' || op AS kind, NULL::jsonb AS data
      FROM catalog.feed_event
     WHERE (txid, event_id) > (%(t)s::xid8, %(e)s::bigint) AND txid < %(h)s::xid8
       AND dataset_id IS NOT NULL AND entity = ANY(%(entities)s::text[])
       AND op <> 'conflict'
       AND (%(ds)s::bigint IS NULL OR dataset_id = %(ds)s::bigint)
    UNION ALL
    SELECT txid, event_id, created_at, dataset_id, entity, entity_id, NULL::bigint,
           type, data
      FROM catalog.partner_event
     WHERE (txid, event_id) > (%(t)s::xid8, %(e)s::bigint) AND txid < %(h)s::xid8
       AND (%(ds)s::bigint IS NULL OR dataset_id = %(ds)s::bigint)
) x
 WHERE (%(types)s::text[] IS NULL OR x.kind = ANY(%(types)s::text[]))
 ORDER BY x.txid, x.event_id LIMIT %(limit)s
"""


def _feed_cursor(raw: str) -> tuple[int, int, int]:
    """(txid, event, incarnation): a third part after a catalog restore."""
    parts = raw.split(".")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts) or len(raw) > 64:
        raise invalid("after", "must be a cursor from a previous page, 0.0 or now")
    return int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) == 3 else 0


def _fmt(t: int, e: int, inc: int) -> str:
    return f"{t}.{e}" if inc == 0 else f"{t}.{e}.{inc}"


async def get_events(ctx: Ctx) -> WebResult:
    """`GET /v1/events?after&dataset_id&limit`: the catalog's ordered feed
    (the edge's, without its own entities), read below the horizon so a
    late-committing transaction is never skipped. Thin events: read the
    entity for its state. The typed lifecycle events (`migration.*`,
    `recall.state`, `restore.state`, `proposal.decided`; migration 0029) are
    merged in by cursor and carry `data`; `types` keeps only the kinds named.
    `after=now` returns no events and the cursor of now; a cursor older than
    the feed's retention is `410 cursor_expired`."""
    _names_reader(ctx.who)
    raw = ctx.query("after") or "0.0"
    ds = ctx.query_id("dataset_id")
    limit = ctx.limit(default=500, maximum=EVENTS_MAX)
    raw_types = ctx.query("types")
    if raw_types is not None and len(raw_types) > 1024:
        raise invalid("types", "is too long")
    # Unknown kinds are ignored (the set is open): they match nothing.
    types = [t for t in (raw_types or "").split(",") if t] or None
    async with ctx.svc.catalog.read() as db:
        h = int((await (await db.execute(
            "SELECT catalog.feed_horizon()::text AS h")).fetchone())["h"])  # fmt: skip
        floor = await (
            await db.execute(
                "SELECT dropped_through_txid::text AS t, dropped_through_event AS e, incarnation"
                "  FROM catalog.feed_retention"
            )
        ).fetchone()
        inc = int(floor["incarnation"]) if floor else 0
        if raw == "now":
            return WebResult(200, {"items": [], "next": _fmt(h, 0, inc)})
        t, e, raw_inc = _feed_cursor(raw)
        after = (t, e)
        if raw != "0.0" and raw_inc != inc:
            # Issued before the catalog was restored (migration 0019).
            raise WebError("cursor_expired", "the catalog was restored; start again from now")
        dropped = (int(floor["t"]), floor["e"]) if floor else (0, 0)
        if dropped > (0, 0) and after <= dropped:
            raise WebError("cursor_expired", "events after this cursor are gone; start again")
        rows = await (
            await db.execute(
                "SELECT txid::text AS txid, event_id, created_at, dataset_id, entity, entity_id,"
                "       entity_version, kind, data FROM (" + _EVENTS + ") y",
                {
                    "t": str(after[0]),
                    "e": after[1],
                    "h": str(h),
                    "entities": EVENT_ENTITIES,
                    "ds": ds,
                    "types": types,
                    "limit": limit,
                },  # fmt: skip
            )
        ).fetchall()
        files: dict[int, dict[str, Any]] = {}
        ids = sorted({r["entity_id"] for r in rows if r["entity"] == "file"})
        if ids:
            found = await (
                await db.execute(
                    "SELECT f.file_id, f.kind, f.deleted_at,"
                    "       (SELECT coalesce('/' || string_agg(a.name, '/' ORDER BY a.depth DESC),"
                    "                        '/')"
                    "          FROM catalog.file_ancestors(f.file_id) a"
                    "         WHERE a.parent_id IS NOT NULL) AS path"
                    "  FROM catalog.file f WHERE f.file_id = ANY(%s)",
                    (ids,),
                )
            ).fetchall()
            files = {r["file_id"]: r for r in found}
    if len(rows) >= limit:
        nxt = (int(rows[-1]["txid"]), rows[-1]["event_id"])
    else:
        nxt = max(after, (h, 0))  # everything below the horizon has been seen

    def locator(r: dict[str, Any]) -> dict[str, Any] | None:
        f = files.get(r["entity_id"]) if r["entity"] == "file" else None
        if f is None:
            return None
        return {"path": f["path"], "kind": f["kind"], "deleted": f["deleted_at"] is not None}

    items = [
        {
            "cursor": _fmt(int(r["txid"]), r["event_id"], inc),
            "kind": r["kind"],
            "dataset_id": str(r["dataset_id"]),
            "entity": r["entity"],
            "entity_id": str(r["entity_id"]),
            "entity_version": None if r["entity_version"] is None else str(r["entity_version"]),
            "at": rfc3339(r["created_at"]),
            "file": locator(r) if r["data"] is None else None,
            # Lifecycle events (migration.*, recall.state, restore.state,
            # proposal.decided) carry their facts; catalog events are thin.
            "data": r["data"],
        }
        for r in rows
    ]
    return WebResult(200, {"items": items, "next": _fmt(nxt[0], nxt[1], inc)})


# --- usage ------------------------------------------------------------------------


async def get_usage(ctx: Ctx) -> WebResult:
    """`GET /v1/usage?dataset_id`: the latest daily snapshot, priced
    (usage/__init__.py)."""
    _reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    async with ctx.svc.catalog.read() as db:
        out = await usage_report(db, ds)
    out["computed_at"] = rfc3339(out["computed_at"])
    return WebResult(200, out)
