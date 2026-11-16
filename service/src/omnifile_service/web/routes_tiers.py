"""Tier pins and holds (portal-api.md 5.6, decision 29).

Pins are for the share's Owners and DataStewards, with read on every item.
`keep_online` holds likewise; Teams' "Keep online" button sets the same kind
of hold for any reader of one file (decision 33, `api/teams.py`). Legal holds
are ArchiveAdmin's, after re-authentication, through `retention.worm`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..api.validate import parse_id, timestamp
from ..catalog.read_set import readable_page_sql
from ..retention import worm
from ..retention.common import RetentionError
from . import rows
from .access import FileScope, acl_params, load_scope
from .context import Ctx, WebResult, page, tuple_rows
from .facts import dataset_facts, pin_facts
from .problems import WebError, forbidden, invalid, not_found, role_required

PIN_TIERS = ("hot", "warm", "cold", "archive")
MAX_ITEMS = 1000
EXPAND_CAP = 1000

DESCENDANT_FILES = """
WITH RECURSIVE t(file_id) AS (
    SELECT %(root)s::bigint
    UNION ALL
    SELECT f.file_id FROM catalog.file f JOIN t ON f.parent_id = t.file_id
     WHERE f.kind = 'dir' AND f.deleted_at IS NULL
)
SELECT f.* FROM catalog.file f
 WHERE f.parent_id IN (SELECT file_id FROM t) AND f.kind = 'file' AND f.deleted_at IS NULL
 ORDER BY f.file_id LIMIT %(cap)s
"""

#: The files below a folder the caller can read: what a preview may count and
#: price (decision 28; M7 review). Parameters as `readable_sql`.
DESCENDANT_READABLE = f"""
WITH RECURSIVE t(file_id) AS (
    SELECT %(root)s::bigint
    UNION ALL
    SELECT f.file_id FROM catalog.file f JOIN t ON f.parent_id = t.file_id
     WHERE f.kind = 'dir' AND f.deleted_at IS NULL
)
SELECT f.* FROM catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id
 WHERE f.parent_id IN (SELECT file_id FROM t) AND f.kind = 'file' AND f.deleted_at IS NULL
   AND {readable_page_sql("f.file_id")}
 ORDER BY f.file_id LIMIT %(cap)s
"""


def _items(b: dict[str, Any]) -> list[int]:
    items = b.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise invalid("items", f"must be 1 to {MAX_ITEMS} ids")
    out: list[int] = []
    for i, v in enumerate(items):
        fid = parse_id(v, f"items[{i}]")
        if fid not in out:
            out.append(fid)
    return out


def _until(b: dict[str, Any], now: datetime) -> datetime | None:
    if b.get("until") is None:
        return None
    t = timestamp(b["until"], "until")
    if t <= now:
        raise invalid("until", "must be in the future")
    return t


def _pin_request(b: dict[str, Any], *, create: bool, now: datetime) -> dict[str, Any]:
    allowed = {"items", "tier", "until", "override_archive"} | ({"reason"} if create else set())
    extra = set(b) - allowed
    if extra:
        raise invalid(sorted(extra)[0], "is not a pin field")
    if b.get("tier") not in PIN_TIERS:
        raise invalid("tier", "must be hot, warm, cold or archive")
    override = b.get("override_archive", False)
    if not isinstance(override, bool):
        raise invalid("override_archive", "must be a boolean")
    out = {"items": _items(b), "tier": b["tier"], "until": _until(b, now),
           "override_archive": override}  # fmt: skip
    if create:
        reason = b.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            raise invalid("reason", "is required (1 to 500 characters)")
        out["reason"] = reason
    return out


def _sum_money(values: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    vals = [v for v in values if v]
    if not vals:
        return None
    low = sum((Decimal(v["amount"]) for v in vals), Decimal(0))
    high = sum((Decimal(v.get("high") or v["amount"]) for v in vals), Decimal(0))
    out = {"amount": format(low, "f"), "currency": vals[0]["currency"]}
    if high != low:
        out["high"] = format(high, "f")
    return out


async def _locations(db, files: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    ids = [f["primary_location_id"] for f in files if f.get("primary_location_id")]
    found = await (
        await db.execute(
            "SELECT l.location_id, l.logical_tier, l.storage_class, l.explicit_tier_set,"
            "       l.tier_changed_at,"
            "       EXISTS (SELECT 1 FROM catalog.location o"
            "                WHERE o.backend_id = l.backend_id AND o.container = l.container"
            "                  AND o.object_key = l.object_key"
            "                  AND o.backend_version_id IS NOT DISTINCT FROM l.backend_version_id"
            "                  AND o.state = 'committed' AND o.role = 'primary'"
            "                  AND o.location_id <> l.location_id) AS shares_blob"
            " FROM catalog.location l WHERE l.location_id = ANY(%s)",
            (ids,),
        )
    ).fetchall()
    return {r["location_id"]: r for r in found}


async def preview_pins(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    req = _pin_request(ctx.body, create=False, now=now)
    items: list[dict[str, Any]] = []
    total_files = 0
    async with ctx.svc.catalog.read() as db:
        facts_by_ds: dict[int, Any] = {}
        for fid in req["items"]:
            try:
                scope = (await load_scope(db, who, fid)).require_read()
            except WebError as e:
                if e.code == "not_found":
                    continue
                raise
            # Who may pin it may preview it, and only what they can read is
            # counted: a preview names no file but its totals and fees
            # would reveal what an unreadable folder below holds.
            if not (scope.roles.owner or who.has("DataSteward")):
                continue
            if scope.dataset_id not in facts_by_ds:
                facts_by_ds[scope.dataset_id] = (await dataset_facts(db, scope.dataset_id))[0]
            facts = facts_by_ds[scope.dataset_id]
            below = {**acl_params(who), "root": fid, "cap": EXPAND_CAP}
            files = [scope.file] if scope.file["kind"] == "file" else list(
                await (await db.execute(DESCENDANT_READABLE, below)).fetchall()
            )  # fmt: skip
            total_files += len(files)
            locs = await _locations(db, files)
            checks: dict[str, dict[str, Any]] = {}
            fees, changes = [], []
            for f in files:
                found, change = ctx.svc.preview.pin_checks(
                    req["tier"], pin_facts(f, locs.get(f["primary_location_id"]), facts.layout,
                                           now),
                    facts, now, until=req["until"], override_archive=req["override_archive"],
                )  # fmt: skip
                changes.append(change)
                for c in found:
                    fees.append(c.get("fee"))
                    checks.setdefault(c["code"], c)
            items.append({
                "file_id": str(fid), "name": scope.file["name"], "checks": list(checks.values()),
                "fee": _sum_money(fees), "monthly_cost_change": _sum_money(changes),
            })  # fmt: skip
    totals = {
        "fee": _sum_money([i["fee"] for i in items]),
        "monthly_cost_change": _sum_money([i["monthly_cost_change"] for i in items]),
        "files": total_files,
    }
    return WebResult(200, {"items": items, "totals": totals})


async def _pin_rows(db, where: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return list(
        await (
            await db.execute(
                "SELECT p.*, f.name, f.kind, i.display_name AS setter_name"
                "  FROM catalog.tier_pin p JOIN catalog.file f ON f.file_id = p.file_id"
                "  LEFT JOIN catalog.identity i ON i.identity_id = p.set_by_identity_id"
                " WHERE " + where,
                params,
            )
        ).fetchall()
    )


def _skip(fid: int, code: str, message: str) -> dict[str, Any]:
    return {"file_id": str(fid), "code": code, "message": message}


async def _scope_or_skip(db, ctx: Ctx, fid: int, skipped: list) -> FileScope | None:
    try:
        return (await load_scope(db, ctx.who, fid)).require_read()
    except WebError as e:
        if e.code != "not_found":
            raise
        skipped.append(_skip(fid, "not_found", "no such file or folder"))
        return None


async def create_pins(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    req = _pin_request(ctx.body, create=True, now=now)
    if who.identity_id is None:
        raise forbidden("pinning needs an AD identity")
    pins, skipped = [], []
    async with ctx.svc.catalog.tx(who.actor) as db:
        for fid in req["items"]:
            scope = await _scope_or_skip(db, ctx, fid, skipped)
            if scope is None:
                continue
            roles = scope.roles
            if not (roles.owner or who.has("DataSteward")):
                skipped.append(_skip(fid, "forbidden", "needs the share's Owners or DataSteward"))
                continue
            if req["override_archive"] and not roles.owner:
                skipped.append(_skip(fid, "override_archive_owner_only",
                                     "only the share's Owners may keep a file past its archive"
                                     " date"))  # fmt: skip
                continue
            await db.execute(
                "UPDATE catalog.tier_pin SET released_at = %s, released_by_identity_id = %s"
                " WHERE file_id = %s AND released_at IS NULL",
                (now, who.identity_id, fid),
            )
            row = await (
                await db.execute(
                    "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, until,"
                    " reason, set_by_identity_id, set_at, override_archive)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING pin_id",
                    (
                        scope.dataset_id,
                        fid,
                        req["tier"],
                        req["until"],
                        req["reason"],
                        who.identity_id,
                        now,
                        req["override_archive"],
                    ),
                )  # fmt: skip
            ).fetchone()
            [pin] = await _pin_rows(db, "p.pin_id = %(p)s", {"p": row["pin_id"]})
            pins.append(rows.pin_json(pin))
    return WebResult(201, {"pins": pins, "skipped": skipped})


async def _visible_listing(ctx: Ctx, db, found: list[dict[str, Any]], share_id: int | None):
    """Keep rows on items the caller can read, in shares where the caller is
    Owner (or any share, for DataStewards and ArchiveAdmins)."""
    out = []
    for r in found:
        try:
            scope = await load_scope(db, ctx.who, r["file_id"], include_deleted=True)
        except WebError as e:
            if e.code == "not_found":
                continue
            raise
        if not scope.readable:
            continue
        if share_id is not None and not any(
            s["share_id"] == share_id and (s["root_dir_id"] is None
                                           or s["root_dir_id"] in scope.ancestor_ids)
            for s in scope.shares
        ):  # fmt: skip
            continue
        if not (ctx.who.has("DataSteward", "ArchiveAdmin") or scope.roles.owner):
            continue
        out.append(r)
    return out


async def list_pins(ctx: Ctx) -> WebResult:
    share_id = ctx.query_id("share_id")
    raw_active = ctx.query("active")
    if raw_active not in (None, "true", "false"):
        raise invalid("active", "must be true or false")
    active = raw_active != "false"
    limit = ctx.limit()
    after = ctx.cursor("pins")
    async with ctx.svc.catalog.read() as db:
        params = {"after": int(after[0]) if after else None, "batch": limit * 4 + 1}
        where = ("(%(after)s::bigint IS NULL OR p.pin_id < %(after)s::bigint)"
                 + (" AND p.released_at IS NULL" if active else "")
                 + " ORDER BY p.pin_id DESC LIMIT %(batch)s")  # fmt: skip
        found = await _pin_rows(db, where, params)
        kept = await _visible_listing(ctx, db, found, share_id)
    more = len(kept) > limit or len(found) == params["batch"]
    kept = kept[:limit]
    last = kept[-1]["pin_id"] if kept else (found[-1]["pin_id"] if found else None)
    nxt = ctx.next_cursor("pins", [last]) if more and last is not None else None
    return WebResult(200, page([rows.pin_json(p) for p in kept], nxt))


async def release_pin(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    pid = ctx.path_id("pin_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        found = await _pin_rows(db, "p.pin_id = %(p)s", {"p": pid})
        if not found:
            raise not_found("pin")
        p = found[0]
        scope = (await load_scope(db, who, p["file_id"], include_deleted=True)).require_read()
        if not (scope.roles.owner or who.has("DataSteward")):
            raise forbidden("releasing a pin needs the share's Owners or DataSteward")
        if who.identity_id is None:
            raise forbidden("releasing a pin needs an AD identity")
        if p["released_at"] is None:
            await db.execute(
                "UPDATE catalog.tier_pin SET released_at = %s, released_by_identity_id = %s"
                " WHERE pin_id = %s AND released_at IS NULL",
                (now, who.identity_id, pid),
            )
        [p] = await _pin_rows(db, "p.pin_id = %(p)s", {"p": pid})
    return WebResult(200, rows.pin_json(p))


# --- holds -------------------------------------------------------------------------


async def _hold_rows(db, where: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return list(
        await (
            await db.execute(
                "SELECT h.*, f.name, i.display_name AS setter_name"
                "  FROM catalog.hold h JOIN catalog.file f ON f.file_id = h.file_id"
                "  LEFT JOIN catalog.identity i ON i.identity_id = h.set_by_identity_id"
                " WHERE " + where,
                params,
            )
        ).fetchall()
    )


async def create_holds(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    b = ctx.body
    extra = set(b) - {"items", "kind", "until", "reason"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a hold field")
    items = _items(b)
    kind = b.get("kind")
    if kind not in ("keep_online", "legal"):
        raise invalid("kind", "must be keep_online or legal")
    reason = b.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
        raise invalid("reason", "is required (1 to 500 characters)")
    until = _until(b, now)
    if kind == "legal":
        if until is not None:
            raise invalid("until", "a legal hold lasts until it is released")
        if not who.has("ArchiveAdmin"):
            raise role_required("ArchiveAdmin")
        ctx.svc.auth.require_recent_auth(who)
    if who.identity_id is None:
        raise forbidden("holds need an AD identity")
    holds, skipped = [], []
    async with ctx.svc.catalog.tx(who.actor) as db:
        for fid in items:
            scope = await _scope_or_skip(db, ctx, fid, skipped)
            if scope is None:
                continue
            if kind == "keep_online":
                if not (scope.roles.owner or who.has("DataSteward")):
                    skipped.append(_skip(fid, "forbidden",
                                         "needs the share's Owners or DataSteward"))  # fmt: skip
                    continue
                row = await (
                    await db.execute(
                        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason,"
                        " set_by_identity_id, set_at, until)"
                        " VALUES (%s, %s, 'keep_online', %s, %s, %s, %s) RETURNING hold_id",
                        (scope.dataset_id, fid, reason, who.identity_id, now, until),
                    )
                ).fetchone()
                hold_id = row["hold_id"]
            else:
                with tuple_rows(db):
                    hold_id = await worm.set_legal_hold(
                        db, fid, reason=reason, actor=who.actor, identity_id=who.identity_id
                    )
            [h] = await _hold_rows(db, "h.hold_id = %(h)s", {"h": hold_id})
            holds.append(rows.hold_json(h))
    return WebResult(201, {"holds": holds, "skipped": skipped})


async def list_holds(ctx: Ctx) -> WebResult:
    share_id = ctx.query_id("share_id")
    file_id = ctx.query_id("file_id")
    kind = ctx.query("kind")
    if kind not in (None, "keep_online", "legal"):
        raise invalid("kind", "must be keep_online or legal")
    limit = ctx.limit()
    after = ctx.cursor("holds")
    async with ctx.svc.catalog.read() as db:
        params = {"after": int(after[0]) if after else None, "batch": limit * 4 + 1,
                  "kind": kind, "file": file_id}  # fmt: skip
        found = await _hold_rows(
            db,
            "h.released_at IS NULL AND (%(kind)s::text IS NULL OR h.kind = %(kind)s::text)"
            # file_id: the holds that apply to it, on it or on a folder above it.
            " AND (%(file)s::bigint IS NULL OR h.file_id IN"
            "      (SELECT a.file_id FROM catalog.file_ancestors(%(file)s::bigint) a))"
            " AND (%(after)s::bigint IS NULL OR h.hold_id < %(after)s::bigint)"
            " ORDER BY h.hold_id DESC LIMIT %(batch)s",
            params,
        )
        kept = await _visible_listing(ctx, db, found, share_id)
    more = len(kept) > limit or len(found) == params["batch"]
    kept = kept[:limit]
    last = kept[-1]["hold_id"] if kept else (found[-1]["hold_id"] if found else None)
    nxt = ctx.next_cursor("holds", [last]) if more and last is not None else None
    return WebResult(200, page([rows.hold_json(h, for_file=file_id) for h in kept], nxt))


async def release_hold(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    hid = ctx.path_id("hold_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        found = await _hold_rows(db, "h.hold_id = %(h)s", {"h": hid})
        if not found:
            raise not_found("hold")
        h = found[0]
        scope = (await load_scope(db, who, h["file_id"], include_deleted=True)).require_read()
        if who.identity_id is None:
            raise forbidden("releasing a hold needs an AD identity")
        if h["released_at"] is None:
            if h["kind"] == "legal":
                if not who.has("ArchiveAdmin"):
                    raise role_required("ArchiveAdmin")
                ctx.svc.auth.require_recent_auth(who)
                try:
                    with tuple_rows(db):
                        await worm.clear_legal_hold(
                            db, hid, actor=who.actor, identity_id=who.identity_id, now=now
                        )
                except RetentionError as e:
                    raise WebError("version_conflict", str(e)) from None
            else:
                setter = h["set_by_identity_id"] == who.identity_id
                if not (setter or scope.roles.owner or who.has("DataSteward")):
                    raise forbidden("needs the share's Owners, DataSteward or the hold's setter")
                await db.execute(
                    "UPDATE catalog.hold SET released_at = %s, released_by_identity_id = %s"
                    " WHERE hold_id = %s AND released_at IS NULL",
                    (now, who.identity_id, hid),
                )
        [h] = await _hold_rows(db, "h.hold_id = %(h)s", {"h": hid})
    return WebResult(200, rows.hold_json(h))
