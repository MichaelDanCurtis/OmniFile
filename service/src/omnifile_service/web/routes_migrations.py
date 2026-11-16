"""Migrations (portal-api.md section 5.17; docs/design/migration-engine.md):
the partner lifecycle of a migration prefix, over the engine's
`catalog.migration_prefix` rows (the same rows `/v1/locations` shows).

- `POST /v1/migrations` starts one (`migrate.request_location`), with the
  lifecycle's extra choices: `cutover: auto | manual` and `copier: auto |
  external`.
- `cutover` (manual migrations), `cancel` and `retry` (a failed one) are for
  the app that started the migration (`Archive.Adopt`) or an ArchiveAdmin
  session. Another app gets `404`, as if the migration didn't exist.
- `runs`: an external copier (Prizmanyx) reports its runs. Display only:
  OmniFile verifies every file itself (decision 24), and a run's start moves
  the journal gate.
- `exclude`: an ArchiveAdmin, re-authenticated, takes objects that can never
  be verified out of the bar. Never a partner: lowering the bar is a
  person's decision (anti-pattern 7). Excluded objects stay on the source.
- `tombstones`: the cutover report. Deletes made through OmniFile while the
  bytes still live in the source, which OmniFile never applies there
  (decision 40). The full list (owner, 2026-11-04) is for the app that
  started the migration, a registered API client with `Archive.Adopt` for
  one started in the portal, and a signed-in ArchiveAdmin with an AD
  identity. Every read is audited, one returning no rows too.

`web` writes only the catalog. The worker's `migrate` queue does the rest,
and the actions here wake it.
"""

from __future__ import annotations

import json
from typing import Any

from ..api.validate import parse_id, rfc3339, timestamp
from ..migrate import MigrationError, request_location
from ..worker.queue import send
from .context import Ctx, Principal, WebResult, audit, page, tuple_rows
from .problems import WebError, forbidden, invalid, not_found, role_required
from .routes_sources import _adopter, _reader

FIELDS = {"dataset_id", "prefix", "to_backend_id", "mode", "cutover", "copier"}
STATES = ("pending", "copying", "verifying", "verified", "cut_over", "failed", "cancelled")
#: `copier` in a request -> the engine's `copy_by`.
COPIERS = {"auto": "omnifile", "external": "external"}
#: The engine's `copy_by` -> `copier_kind` on the wire (open: other copiers
#: are reserved, decision 35).
COPIER_KIND = {"omnifile": "storage_mover", "external": "external"}
MAX_EXCLUDE = 1000
MIGRATE_QUEUE = "migrate"

_LOAD = """
SELECT m.*, s.backend_id AS from_backend_id, s.container AS source_container,
       (SELECT count(*) FROM catalog.migration_exclusion x
         WHERE x.migration_id = m.migration_id) AS excluded,
       (SELECT count(*) FROM catalog.migration_run r
         WHERE r.migration_id = m.migration_id) AS runs_reported,
       (SELECT count(*) FROM catalog.migration_source_deletes(m.migration_id))
           AS source_deletes_pending,
       (SELECT jsonb_build_object('plan_id', w.plan_id::text, 'seq', w.seq)
          FROM catalog.migration_wave_prefix x JOIN catalog.migration_wave w USING (wave_id)
         WHERE x.migration_id = m.migration_id) AS plan_wave,
       c.copies_given_up, c.copies_stranded, c.copies_no_checksum
  FROM catalog.migration_prefix m JOIN catalog.source s USING (source_id)
  LEFT JOIN catalog.migration_count c ON c.migration_id = m.migration_id
"""


def migration_json(m: dict[str, Any]) -> dict[str, Any]:
    last_run = None
    if m["last_run_id"] is not None:
        last_run = {
            "run_ref": m["last_run_id"],
            "state": m["run_state"],
            "counters": m["run_counters"] or {},
            "started_at": rfc3339(m["last_copy_started_at"]),
        }
    return {
        "migration_id": str(m["migration_id"]),
        "dataset_id": str(m["dataset_id"]),
        "source_id": str(m["source_id"]),
        "prefix": m["prefix"],
        "from_backend_id": str(m["from_backend_id"]),
        "to_backend_id": str(m["to_backend_id"]),
        "to_container": m["to_container"],
        "target_root": m["target_root"],
        "mode": m["mode"],
        "cutover": m["cutover"],
        "copier_kind": COPIER_KIND.get(m["copy_by"], m["copy_by"]),
        "state": m["state"],
        "files": {
            "total": m["files_total"],
            "verified": m["files_verified"],
            "failed": m["files_failed"],
            "excluded": int(m["excluded"]),
        },  # fmt: skip
        "copier": {
            "job_ref": m["storage_mover_job"],
            "last_run": last_run,
            "runs_reported": int(m["runs_reported"]),
        },  # fmt: skip
        "journal_checked_through": rfc3339(m["journal_checked_through"]),
        "verified_at": rfc3339(m["verified_at"]),
        "cutover_requested_at": rfc3339(m["cutover_requested_at"]),
        "cut_over_at": rfc3339(m["cut_over_at"]),
        "cancelled_at": rfc3339(m["cancelled_at"]),
        "source_deletes_pending": int(m["source_deletes_pending"]),
        "last_error": m["last_error"],
        # Numbers only, from the worker's count: why a migration may be stuck with no error.
        "stuck_copies": {
            "given_up": int(m.get("copies_given_up") or 0),
            "stranded": int(m.get("copies_stranded") or 0),
            "no_checksum": int(m.get("copies_no_checksum") or 0),
        },
        # A staged plan's wave owns it: its cutover, cancel and retry go
        # through the plan (portal-api.md 5.22).
        "plan": m.get("plan_wave"),
        "created_by": m["requested_by"],
        "created_at": rfc3339(m["created_at"]),
        "updated_at": rfc3339(m["updated_at"]),
    }


async def _load(db, mid: int) -> dict[str, Any]:
    m = await (await db.execute(_LOAD + " WHERE m.migration_id = %s", (mid,))).fetchone()
    if m is None:
        raise not_found("migration")
    return m


async def _owned(db, who: Principal, mid: int) -> dict[str, Any]:
    """Lock the migration if the caller may act on it or read its report:
    the app that started it, or an ArchiveAdmin session. Any other app gets
    `404`, as if it didn't exist (no IDOR across partners)."""
    row = await (
        await db.execute(
            "SELECT requested_by FROM catalog.migration_prefix WHERE migration_id = %s FOR UPDATE",
            (mid,),
        )
    ).fetchone()
    if row is None or (who.is_app and row["requested_by"] != who.actor):
        raise not_found("migration")
    return await _load(db, mid)


def _actor(who: Principal) -> None:
    """Cutover, cancel, retry: an app with `Archive.Adopt`, or an
    ArchiveAdmin session with an AD identity (the audit names a person)."""
    if who.is_app:
        if not who.has("Archive.Adopt"):
            raise role_required("Archive.Adopt")
        return
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    if who.identity_id is None:
        raise forbidden("this needs an AD identity (an on-premises SID)")


def _not_a_wave(m: dict[str, Any]) -> None:
    """A plan wave's migration is the plan's: its cutover, cancel and retry
    go through the plan's routes, so the wave's window, blackouts and
    approval hold, and an app that proposed the plan can't act on it."""
    pw = m.get("plan_wave")
    if pw:
        raise _conflict(f"this migration belongs to wave {pw['seq']} of plan {pw['plan_id']}:"
                        " cut it over, cancel or retry it on the plan")  # fmt: skip


def _empty(ctx: Ctx) -> None:
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "this action takes an empty body")


def _conflict(message: str) -> WebError:
    return WebError("migration_conflict", message)


async def _wake(db, mid: int) -> None:
    with tuple_rows(db):
        await send(db, MIGRATE_QUEUE, {"migration_id": mid})


# --- start, read ---------------------------------------------------------------------


async def start(ctx: Ctx) -> WebResult:
    """`POST /v1/migrations`."""
    who = ctx.who
    _adopter(who)
    b = ctx.body
    extra = set(b) - FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a migration field")
    for field in ("dataset_id", "prefix"):
        if field not in b:
            raise invalid(field, "is required")
    dataset_id = parse_id(b["dataset_id"], "dataset_id")
    prefix = b["prefix"]
    if not isinstance(prefix, str) or len(prefix) > 1024:
        raise invalid("prefix", "must be a string of at most 1024 characters")
    mode = b.get("mode", "serve")
    if mode not in ("serve", "hand_off"):
        raise invalid("mode", "must be serve (hand_off is reserved)")
    cutover = b.get("cutover", "auto")
    if cutover not in ("auto", "manual"):
        raise invalid("cutover", "must be auto or manual")
    copier = b.get("copier", "auto")
    if copier not in COPIERS:
        raise invalid("copier", "must be auto or external")
    async with ctx.svc.catalog.tx(who.actor) as db:
        to_backend_id = None
        if "to_backend_id" in b:
            to_backend_id = parse_id(b["to_backend_id"], "to_backend_id")
        else:
            ds = await (
                await db.execute(
                    "SELECT backend_id FROM catalog.dataset WHERE dataset_id = %s", (dataset_id,)
                )
            ).fetchone()
            if ds is None:
                raise invalid("dataset_id", "is not a dataset")
            to_backend_id = int(ds["backend_id"])
        try:
            with tuple_rows(db):
                mid = await request_location(
                    db, dataset_id=dataset_id, prefix=prefix, to_backend_id=to_backend_id,
                    copy_by=COPIERS[copier], mode=mode, requested_by=who.actor,
                    cutover=cutover,
                )  # fmt: skip
        except MigrationError as e:
            if e.code == "invalid_request":
                raise WebError("invalid_request", str(e),
                               errors=[{"field": "body", "message": str(e)}]) from None  # fmt: skip
            raise WebError(e.code, str(e)) from None
        await audit(db, who, "migration.start", dataset_id=dataset_id,
                    details={"migration_id": str(mid), "prefix": prefix, "cutover": cutover,
                             "copier": COPIER_KIND[COPIERS[copier]],
                             "to_backend_id": str(to_backend_id)})  # fmt: skip
        out = migration_json(await _load(db, mid))
    return WebResult(202, out)


async def list_migrations(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    state = ctx.query("state")
    if state is not None and state not in STATES:
        raise invalid("state", "is not a migration state")
    limit = ctx.limit()
    after = ctx.cursor("migrations")
    async with ctx.svc.catalog.read() as db:
        found = await (
            await db.execute(
                _LOAD + " WHERE (%(ds)s::bigint IS NULL OR m.dataset_id = %(ds)s::bigint)"
                "   AND (%(state)s::text IS NULL OR m.state = %(state)s::text)"
                "   AND (%(after)s::bigint IS NULL OR m.migration_id < %(after)s::bigint)"
                " ORDER BY m.migration_id DESC LIMIT %(limit)s",
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
    nxt = ctx.next_cursor("migrations", [found[-1]["migration_id"]]) if more else None
    return WebResult(200, page([migration_json(m) for m in found], nxt))


async def get_migration(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        return WebResult(200, migration_json(await _load(db, ctx.path_id("migration_id"))))


# --- actions -------------------------------------------------------------------------


async def cutover(ctx: Ctx) -> WebResult:
    """Flip a `manual` migration once it is verified. Asked earlier, the
    flip waits for verification; nothing is flipped before every file is
    verified (decision 24)."""
    who = ctx.who
    _actor(who)
    _empty(ctx)
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _owned(db, who, mid)
        _not_a_wave(m)
        if m["cutover"] != "manual":
            raise _conflict("this migration cuts over by itself (cutover = auto)")
        if m["state"] in ("cut_over", "cancelled", "failed"):
            raise _conflict(f"the migration is {m['state']}")
        if m["cutover_requested_at"] is None:
            await db.execute(
                "UPDATE catalog.migration_prefix SET cutover_requested_at = %s,"
                " cutover_requested_by = %s, updated_at = %s WHERE migration_id = %s",
                (ctx.now, who.actor, ctx.now, mid),
            )
            await audit(db, who, "migration.cutover", dataset_id=m["dataset_id"],
                        details={"migration_id": str(mid), "state": m["state"]})  # fmt: skip
            if m["state"] == "verified":
                await _wake(db, mid)
        out = migration_json(await _load(db, mid))
    return WebResult(200, out)


async def cancel(ctx: Ctx) -> WebResult:
    """Stop a migration. Nothing is deleted anywhere: files already flipped
    keep their verified copy, the rest stay served from the source, and a
    Storage Mover run already under way finishes on its own (a migration of
    the prefix started again finds its copies)."""
    who = ctx.who
    _actor(who)
    _empty(ctx)
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _owned(db, who, mid)
        _not_a_wave(m)
        if m["state"] in ("cut_over", "cancelled"):
            raise _conflict(f"the migration is {m['state']}")
        await db.execute(
            "UPDATE catalog.migration_prefix SET state = 'cancelled', cancelled_at = %s,"
            " cancelled_by = %s, updated_at = %s WHERE migration_id = %s",
            (ctx.now, who.actor, ctx.now, mid),
        )
        await audit(db, who, "migration.cancel", dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "state": m["state"]})  # fmt: skip
        out = migration_json(await _load(db, mid))
    return WebResult(200, out)


async def retry(ctx: Ctx) -> WebResult:
    """Start a `failed` migration again from `pending`: a new Additive run
    copies only what is missing, and verified copies stay verified."""
    who = ctx.who
    _actor(who)
    _empty(ctx)
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _owned(db, who, mid)
        _not_a_wave(m)
        if m["state"] != "failed":
            raise _conflict(f"only a failed migration can be retried; this one is {m['state']}")
        await db.execute(
            "UPDATE catalog.migration_prefix SET state = 'pending', last_error = NULL,"
            " updated_at = %s WHERE migration_id = %s",
            (ctx.now, mid),
        )
        await _wake(db, mid)
        await audit(db, who, "migration.retry", dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "last_error": m["last_error"]})  # fmt: skip
        out = migration_json(await _load(db, mid))
    return WebResult(200, out)


async def report_run(ctx: Ctx) -> WebResult:
    """`POST /v1/migrations/{id}/runs`: an external copier's run. A run that
    is copying again sends a verified prefix back to verifying, so nothing
    flips under it."""
    who = ctx.who
    if not who.is_app:
        raise forbidden("only the external copier (an app) reports runs")
    if not who.has("Archive.Adopt"):
        raise role_required("Archive.Adopt")
    b = ctx.body
    extra = set(b) - {"run_ref", "state", "started_at", "finished_at", "counters"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a field of a run")
    run_ref = b.get("run_ref")
    if not isinstance(run_ref, str) or not 0 < len(run_ref) <= 256:
        raise invalid("run_ref", "must be 1 to 256 characters")
    state = b.get("state")
    if state not in ("running", "succeeded", "failed"):
        raise invalid("state", "must be running, succeeded or failed")
    started = timestamp(b.get("started_at"), "started_at")
    finished = timestamp(b["finished_at"], "finished_at") if b.get("finished_at") else None
    counters = b.get("counters") or {}
    if not isinstance(counters, dict) or len(counters) > 32 or any(
        not isinstance(k, str) or len(k) > 64 or isinstance(v, bool) or not isinstance(v, int)
        or v < 0 for k, v in counters.items()
    ):  # fmt: skip
        raise invalid("counters", "must map up to 32 names to non-negative integers")
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _owned(db, who, mid)
        if m["copy_by"] != "external":
            raise _conflict("only a migration with an external copier takes run reports")
        if m["state"] in ("cut_over", "cancelled"):
            raise _conflict(f"the migration is {m['state']}")
        await db.execute(
            "INSERT INTO catalog.migration_run (migration_id, run_ref, state, started_at,"
            " finished_at, counters, reported_by) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)"
            " ON CONFLICT (migration_id, run_ref) DO UPDATE SET state = EXCLUDED.state,"
            " started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,"
            " counters = EXCLUDED.counters, reported_by = EXCLUDED.reported_by,"
            " reported_at = now()",
            (mid, run_ref, state, started, finished, _json(counters), who.actor),
        )
        back = m["state"] == "verified" and state == "running"
        await db.execute(
            "UPDATE catalog.migration_prefix SET last_run_id = %(ref)s, run_state = %(st)s,"
            " run_counters = %(c)s::jsonb,"
            " last_copy_started_at = greatest(last_copy_started_at, %(start)s),"
            " state = CASE WHEN %(back)s THEN 'verifying' ELSE state END,"
            " verified_at = CASE WHEN %(back)s THEN NULL ELSE verified_at END,"
            " updated_at = %(now)s WHERE migration_id = %(mid)s",
            {
                "ref": run_ref,
                "st": state,
                "c": _json(counters),
                "start": started,
                "back": back,
                "now": ctx.now,
                "mid": mid,
            },  # fmt: skip
        )
        await _wake(db, mid)
        await audit(db, who, "migration.run_report", dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "run_ref": run_ref,
                             "state": state})  # fmt: skip
        out = migration_json(await _load(db, mid))
    return WebResult(202, out)


def _json(v: Any) -> str:
    return json.dumps(v, sort_keys=True)


_EXCLUDABLE = """
SELECT k.key
  FROM unnest(%(keys)s::text[]) AS k(key)
 WHERE starts_with(k.key, %(prefix)s)
   AND EXISTS (
       SELECT 1 FROM catalog.location l
         JOIN catalog.file f ON f.content_version_id = l.version_id AND f.deleted_at IS NULL
        WHERE l.backend_id = %(sb)s AND l.container = %(sc)s AND l.object_key = k.key
          AND l.state = 'committed'
          AND NOT EXISTS (SELECT 1 FROM catalog.location t
                           WHERE t.version_id = l.version_id AND t.backend_id = %(tb)s
                             AND t.state = 'committed' AND t.verified))
"""


async def exclude(ctx: Ctx) -> WebResult:
    """`POST /v1/migrations/{id}/exclude`: ArchiveAdmin with
    re-authentication. Only live files' objects under the prefix without a
    verified copy can be excluded; other keys are ignored (the audit counts
    them)."""
    who = ctx.who
    if who.is_app:
        raise forbidden("excluding objects from verification is an ArchiveAdmin's decision")
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    if who.identity_id is None:
        raise forbidden("this needs an AD identity (an on-premises SID)")
    ctx.svc.auth.require_recent_auth(who)
    b = ctx.body
    extra = set(b) - {"object_keys", "reason"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a field of an exclusion")
    keys = b.get("object_keys")
    if (not isinstance(keys, list) or not 1 <= len(keys) <= MAX_EXCLUDE
            or not all(isinstance(k, str) and 0 < len(k) <= 1024 for k in keys)):  # fmt: skip
        raise invalid("object_keys", f"must be 1 to {MAX_EXCLUDE:,} object keys")
    reason = b.get("reason")
    if not isinstance(reason, str) or not 0 < len(reason) <= 500:
        raise invalid("reason", "must be 1 to 500 characters")
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _owned(db, who, mid)
        if m["state"] in ("cut_over", "cancelled"):
            raise _conflict(f"the migration is {m['state']}")
        ok = [
            r["key"]
            for r in await (
                await db.execute(
                    _EXCLUDABLE,
                    {
                        "keys": list(dict.fromkeys(keys)),
                        "prefix": m["prefix"],
                        "sb": m["from_backend_id"],
                        "sc": m["source_container"],
                        "tb": m["to_backend_id"],
                    },  # fmt: skip
                )
            ).fetchall()
        ]
        n = 0
        for key in ok:
            cur = await db.execute(
                "INSERT INTO catalog.migration_exclusion (migration_id, object_key, reason,"
                " excluded_by) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (mid, key, reason, who.actor),
            )
            n += cur.rowcount
        await _wake(db, mid)
        await audit(db, who, "migration.exclude", dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "requested": len(keys),
                             "excluded": n, "reason": reason})  # fmt: skip
        out = migration_json(await _load(db, mid))
    return WebResult(200, out)


# --- the cutover report ---------------------------------------------------------------

_TOMBSTONES = """
SELECT d.*, f.dataset_id,
       (SELECT coalesce('/' || string_agg(a.name, '/' ORDER BY a.depth DESC), '/')
          FROM catalog.file_ancestors(d.file_id) a WHERE a.parent_id IS NOT NULL) AS path,
       t.actor_proof, t.actor_identity_id
  FROM catalog.migration_source_deletes(%(mid)s) d
  JOIN catalog.file f ON f.file_id = d.file_id
  -- Who: the actor the edge reported with the delete (migration 0030).
  LEFT JOIN LATERAL (SELECT v.actor_proof, v.actor_identity_id FROM catalog.file_version v
                      WHERE v.file_id = d.file_id AND v.kind = 'tombstone'
                      ORDER BY v.seq DESC LIMIT 1) t ON true
 WHERE d.location_id > %(after)s
 ORDER BY d.location_id LIMIT %(limit)s
"""


async def _registered_client(db, who: Principal) -> bool:
    """May this app read the full cutover report of a migration started in
    the portal? The owner (2026-11-04): a registered API client with
    `Archive.Adopt`. The auth hook (apiclients/registry.py) refuses an
    unregistered or disabled app before any handler, sets `api_client`, and
    cuts the token's roles to the client's scopes, so a role the client
    isn't registered for doesn't count here."""
    del db
    return who.is_app and who.api_client is not None and who.has("Archive.Adopt")


def tombstone_json(d: dict[str, Any], from_backend_id: int) -> dict[str, Any]:
    return {
        "file_id": str(d["file_id"]),
        "path": d["path"],
        "source": {
            "backend_id": str(from_backend_id),
            "container": d["container"],
            "key": d["object_key"],
            "backend_version_id": d["backend_version_id"],
            "etag": d["etag"],
            # Unversioned (no version id): a delete by key could remove newer
            # bytes, so check the ETag first (DeleteObject with If-Match).
            "verify_etag_before_delete": d["backend_version_id"] is None,
        },
        "deleted_at": rfc3339(d["deleted_at"]),
        # The edge the delete came through and, when the edge reported who
        # made it (control-api.md section 4.1 `actor`), that user's catalog
        # identity with the service's proof. `unknown` when the edge couldn't
        # tell (its reconcile pass found the delete) or sent no actor.
        "deleted_by": {
            "kind": "edge",
            "edge_id": None if d["edge_id"] is None else str(d["edge_id"]),
            "identity_id": (
                None if d["actor_identity_id"] is None else str(d["actor_identity_id"])
            ),
            "proof": d["actor_proof"] or "unknown",
        },
    }


async def tombstones(ctx: Ctx) -> WebResult:
    """`GET /v1/migrations/{id}/tombstones`: the full list (owner, 2026-11-04).
    It names S3 keys and paths, never contents, so it is for whoever cleans
    up the source: the app that started the migration, a registered API
    client with `Archive.Adopt` for one started in the portal, and a signed-in
    ArchiveAdmin, even for files they can't read. An ArchiveAdmin with no AD
    identity reads nothing (as everywhere, section 3.3). An API client never
    reads another client's migration (404). Every read is audited, one
    returning no rows too (`migration.tombstones.read`)."""
    who = ctx.who
    if who.is_app:
        if not who.has("Archive.Read", "Archive.Adopt"):
            raise role_required("Archive.Read", "Archive.Adopt")
    elif not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    mid = ctx.path_id("migration_id")
    kind = f"tombstones:{mid}"
    after = ctx.cursor(kind)
    limit = ctx.limit()
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _load(db, mid)
        if who.is_app and m["requested_by"] != who.actor:
            # A migration started in the portal has no app of its own: a
            # registered client reads its report. Another app's: 404.
            portal = str(m["requested_by"]).startswith("user:")
            if not (portal and await _registered_client(db, who)):
                raise not_found("migration")
        found: list[dict[str, Any]] = []
        if who.is_app or who.sid is not None:
            found = list(
                await (
                    await db.execute(
                        _TOMBSTONES,
                        {"mid": mid, "after": int(after[0]) if after else 0, "limit": limit + 1},
                    )
                ).fetchall()
            )
        more = len(found) > limit
        found = found[:limit]
        # Every read of the list is audited, one that returns no rows too
        # (an admin with no AD identity, an empty report).
        await audit(db, who, "migration.tombstones.read", dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "rows": len(found),
                             "first_page": after is None})  # fmt: skip
    nxt = ctx.next_cursor(kind, [found[-1]["location_id"]]) if more else None
    return WebResult(200, {
        "migration_id": str(mid),
        "state": m["state"],
        "cut_over_at": rfc3339(m["cut_over_at"]),
        "source_deletes_pending": int(m["source_deletes_pending"]),
        "applied_by_omnifile": False,
        **page([tombstone_json(d, m["from_backend_id"]) for d in found], nxt),
    })  # fmt: skip
