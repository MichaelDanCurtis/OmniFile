"""Locations (portal-api.md section 5.12; docs/design/migration-engine.md
section 9): ask for a prefix of a migrating dataset to move to its own
backend, and follow it through copy, verification and cutover.

`web` writes only the catalog (`migrate.request_location`); the worker's
`migrate` queue does the rest. Nothing is flipped before every file under
the prefix is verified: there is no "trust me".

Apps need `Archive.Adopt` to request and `Archive.Read` (or `Archive.Adopt`)
to read; signed-in users need ArchiveAdmin.
"""

from __future__ import annotations

from typing import Any

from ..api.validate import parse_id, rfc3339
from ..migrate import MigrationError, request_location
from .context import Ctx, WebResult, audit, page, tuple_rows
from .problems import WebError, invalid, not_found
from .routes_sources import _adopter, _reader

FIELDS = {"dataset_id", "prefix", "to_backend_id", "mode", "copy"}
STATES = ("pending", "copying", "verifying", "verified", "cut_over", "failed", "cancelled")


def location_json(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "migration_id": str(m["migration_id"]),
        "dataset_id": str(m["dataset_id"]),
        "source_id": str(m["source_id"]),
        "prefix": m["prefix"],
        "to_backend_id": str(m["to_backend_id"]),
        "to_container": m["to_container"],
        "target_root": m["target_root"],
        "mode": m["mode"],
        "copy": m["copy_by"],
        "state": m["state"],
        "run": None
        if m["last_run_id"] is None
        else {
            "job": m["storage_mover_job"],
            "run_id": m["last_run_id"],
            "state": m["run_state"],
            "counters": m["run_counters"] or {},
            "started_at": rfc3339(m["last_copy_started_at"]),
        },  # fmt: skip
        "files": {
            "total": m["files_total"],
            "verified": m["files_verified"],
            "failed": m["files_failed"],
        },  # fmt: skip
        "journal_checked_through": rfc3339(m["journal_checked_through"]),
        "verified_at": rfc3339(m["verified_at"]),
        "cut_over_at": rfc3339(m["cut_over_at"]),
        "last_error": m["last_error"],
        "requested_by": m["requested_by"],
        "created_at": rfc3339(m["created_at"]),
        "updated_at": rfc3339(m["updated_at"]),
    }


async def _load(db, mid: int) -> dict[str, Any]:
    m = await (
        await db.execute("SELECT * FROM catalog.migration_prefix WHERE migration_id = %s", (mid,))
    ).fetchone()
    if m is None:
        raise not_found("location")
    return m


async def request(ctx: Ctx) -> WebResult:
    who = ctx.who
    _adopter(who)
    b = ctx.body
    extra = set(b) - FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a location field")
    for field in ("dataset_id", "prefix", "to_backend_id"):
        if field not in b:
            raise invalid(field, "is required")
    dataset_id = parse_id(b["dataset_id"], "dataset_id")
    to_backend_id = parse_id(b["to_backend_id"], "to_backend_id")
    prefix = b["prefix"]
    if not isinstance(prefix, str) or len(prefix) > 1024:
        raise invalid("prefix", "must be a string of at most 1024 characters")
    mode = b.get("mode", "serve")
    copy = b.get("copy", "omnifile")
    if mode not in ("serve", "hand_off"):
        raise invalid("mode", "must be serve (hand_off is reserved)")
    if copy not in ("omnifile", "external"):
        raise invalid("copy", "must be omnifile or external")
    async with ctx.svc.catalog.tx(who.actor) as db:
        try:
            with tuple_rows(db):
                mid = await request_location(
                    db, dataset_id=dataset_id, prefix=prefix, to_backend_id=to_backend_id,
                    copy_by=copy, mode=mode, requested_by=who.actor,
                )  # fmt: skip
        except MigrationError as e:
            if e.code == "invalid_request":
                raise WebError("invalid_request", str(e),
                               errors=[{"field": "body", "message": str(e)}]) from None  # fmt: skip
            raise WebError(e.code, str(e)) from None
        await audit(db, who, "location.request", dataset_id=dataset_id,
                    details={"migration_id": str(mid), "prefix": prefix, "copy": copy,
                             "to_backend_id": str(to_backend_id)})  # fmt: skip
        out = location_json(await _load(db, mid))
    return WebResult(202, out)


async def list_locations(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    state = ctx.query("state")
    if state is not None and state not in STATES:
        raise invalid("state", "is not a location state")
    limit = ctx.limit()
    after = ctx.cursor("locations")
    async with ctx.svc.catalog.read() as db:
        found = await (
            await db.execute(
                "SELECT * FROM catalog.migration_prefix"
                " WHERE (%(ds)s::bigint IS NULL OR dataset_id = %(ds)s::bigint)"
                "   AND (%(state)s::text IS NULL OR state = %(state)s::text)"
                "   AND (%(after)s::bigint IS NULL OR migration_id < %(after)s::bigint)"
                " ORDER BY migration_id DESC LIMIT %(limit)s",
                {
                    "ds": ds,
                    "state": state,
                    "after": int(after[0]) if after else None,
                    "limit": limit + 1,
                },  # fmt: skip
            )
        ).fetchall()
    more = len(found) > limit
    found = list(found[:limit])
    nxt = ctx.next_cursor("locations", [found[-1]["migration_id"]]) if more else None
    return WebResult(200, page([location_json(m) for m in found], nxt))


async def get_location(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        return WebResult(200, location_json(await _load(db, ctx.path_id("migration_id"))))
