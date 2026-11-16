"""The migrations operations console (portal-api.md section 5.22).

ArchiveAdmin sessions only. Everything here is read from the catalog
(`web` never calls a provider): the engine's rows, the edges' heartbeats,
restore waves and checksum jobs, and the Storage Mover agents as the
worker last listed them (`catalog.mover_agent`).

- `GET /v1/migration-console`: one row per migration with its phase, files
  and bytes verified of the total, failures and exclusions, the last hour's
  throughput, an ETA with its basis, a health badge and the next planned
  wave; plus the list of what needs attention, each with its fix.
- `GET /v1/migrations/{id}/agents`: who works on one migration.
- `GET /v1/migration-agents`: the same across all migrations.
- `POST /v1/migrations/{id}/rerun` (Copy again) and `/restore-again`
  (Restore again): ask the engine, at its next step, for a new Storage
  Mover run, or to restore again the restored copies about to expire.

**ETA.** Only from what the data shows: the bytes verified in the last hour,
over the bytes left. None when nothing was verified in that hour, and none
while objects still wait in an S3 archive class without a finished
restore (S3 gives no time a portal can promise for a Bulk restore).
No file names appear here: counts, prefixes and object keys of the
migration's own source only where the routes already give them.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from ..api.validate import rfc3339
from ..migrate.progress import KEEP_KEYS
from ..migrate.staging import LEFT_BEHIND
from .context import Ctx, Principal, WebResult, audit, page, tuple_rows
from .problems import WebError, forbidden, invalid, not_found, role_required
from .routes_edges import status_of
from .routes_migrations import _actor, _owned, migration_json

WINDOW = timedelta(hours=1)
AGENT_STALE = timedelta(minutes=15)
#: A last_error that points at access or the network: a Settings check fixes it.
ACCESS = re.compile(
    r"AccessDenied|Forbidden|\b403\b|not authori[sz]ed|AuthorizationFailure|private endpoint"
    r"|PrivateLink|ConnectTimeout|ConnectError|SignatureDoesNotMatch|InvalidAccessKeyId",
    re.IGNORECASE,
)


def _session_admin(who: Principal) -> None:
    if who.is_app:
        raise forbidden("the migrations console is for a signed-in ArchiveAdmin")
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


_ROWS = """
SELECT m.*, c.bytes_total, c.bytes_verified, c.failing_now, c.archived_waiting,
       c.restores_expiring, c.counted_at AS progress_at,
       c.copies_given_up, c.copies_stranded, c.copies_no_checksum,
       s.backend_id AS from_backend_id, s.container AS source_container,
       d.name AS dataset_name, d.edge_id,
       (SELECT count(*) FROM catalog.migration_exclusion x
         WHERE x.migration_id = m.migration_id) AS excluded,
       (SELECT count(*) FROM catalog.migration_run r
         WHERE r.migration_id = m.migration_id) AS runs_reported,
       (SELECT count(*) FROM catalog.migration_source_deletes(m.migration_id))
           AS source_deletes_pending,
       (SELECT jsonb_build_object('plan_id', w.plan_id::text, 'seq', w.seq)
          FROM catalog.migration_wave_prefix x JOIN catalog.migration_wave w USING (wave_id)
         WHERE x.migration_id = m.migration_id) AS plan_wave
  FROM catalog.migration_prefix m JOIN catalog.source s USING (source_id)
  JOIN catalog.dataset d ON d.dataset_id = m.dataset_id
  LEFT JOIN catalog.migration_count c ON c.migration_id = m.migration_id
"""

#: The last hour's samples of verified bytes (migrate/progress.py).
_SAMPLES = """
SELECT min(at) AS t0, max(at) AS t1,
       (array_agg(bytes_verified ORDER BY at))[1] AS b0,
       (array_agg(bytes_verified ORDER BY at DESC))[1] AS b1,
       (array_agg(files_verified ORDER BY at))[1] AS f0,
       (array_agg(files_verified ORDER BY at DESC))[1] AS f1
  FROM catalog.migration_progress WHERE migration_id = %s AND at >= %s
"""
#: Two samples at least this far apart give a rate.
MIN_SPAN = timedelta(minutes=10)


def _human(n: int) -> str:
    """Decimal units, as the portal shows sizes."""
    v, units = float(n), ["B", "kB", "MB", "GB", "TB", "PB"]
    i = 0
    while v >= 1000 and i < len(units) - 1:
        v, i = v / 1000, i + 1
    return f"{v:.0f} {units[i]}" if v >= 100 or i == 0 else f"{v:.1f} {units[i]}"


def _eta(m: dict[str, Any], rate: float | None, now) -> dict[str, Any]:
    if m["progress_at"] is None:
        return {"at": None, "basis": "not counted yet: the worker counts every 5 minutes"}
    left = int(m["bytes_total"] or 0) - int(m["bytes_verified"] or 0)
    if left <= 0:
        return {"at": None, "basis": "nothing left to verify"}
    if int(m["archived_waiting"] or 0):
        return {"at": None, "basis": f"{int(m['archived_waiting']):,} objects are still in an S3"
                " archive class and must be restored first: no time is promised"}  # fmt: skip
    if not rate:
        return {"at": None, "basis": "no copy was verified in the last hour"}
    basis = f"at the last hour's rate ({_human(int(rate * 3600))} an hour), {_human(left)} left"
    return {"at": rfc3339(now + timedelta(seconds=left / rate)), "basis": basis}


def _health(m: dict[str, Any]) -> str:
    if m["state"] == "cut_over":
        # Its files have moved. All that can still need someone is what it left behind.
        return "attention" if LEFT_BEHIND in (m["last_error"] or "") else "done"
    if m["state"] == "cancelled":
        return "cancelled"
    if m["state"] == "failed" or m["run_state"] == "failed":
        return "failing"
    if m["failing_now"] or m["restores_expiring"] or m["last_error"]:
        return "attention"
    if m.get("copies_given_up") or m.get("copies_stranded") or m.get("copies_no_checksum"):
        return "attention"
    return "ok"


async def _row(db, m: dict[str, Any], now) -> dict[str, Any]:
    """One console row, from the migration's stored counters and its last
    hour's samples (migrate/progress.py): no scan of its objects."""
    smp = await (await db.execute(_SAMPLES, (m["migration_id"], now - WINDOW))).fetchone()
    rate = files_h = None
    if smp and smp["t0"] is not None and smp["t1"] - smp["t0"] >= MIN_SPAN:
        span = (smp["t1"] - smp["t0"]).total_seconds()
        if int(smp["b1"]) > int(smp["b0"]):
            rate = (int(smp["b1"]) - int(smp["b0"])) / span
            files_h = round((int(smp["f1"]) - int(smp["f0"])) * 3600 / span)
    return {
        **migration_json(m),
        "dataset_name": m["dataset_name"],
        "bytes": {"total": int(m["bytes_total"] or 0), "verified": int(m["bytes_verified"] or 0)},
        "counted_at": rfc3339(m["progress_at"]),
        "failing_now": int(m["failing_now"] or 0),
        "throughput": None if rate is None else {
            "bytes_per_s": round(rate, 1), "files_per_h": files_h,
            "basis": f"copies verified between {rfc3339(smp['t0'])} and {rfc3339(smp['t1'])}"},
        "eta": _eta(m, rate, now),
        "health": _health(m),
        "restores_expiring": int(m["restores_expiring"] or 0),
        "archived_waiting": int(m["archived_waiting"] or 0),
    }  # fmt: skip


def _copies(n: int) -> str:
    return "1 copy" if n == 1 else f"{n:,} copies"


def _copy_items(row: dict[str, Any], m: dict[str, Any]) -> list[dict[str, Any]]:
    """Copies whose check won't end by itself, as the progress sweep last counted them. A new
    run revives none of them, since `_record` skips a pending copy."""
    mid, items = row["migration_id"], []
    if n := int(m.get("copies_given_up") or 0):
        items.append({"kind": "check_given_up", "migration_id": mid,
                      "message": f"The check of {_copies(n)} kept failing, and went to the"
                                 " migrate_verify dead letters. They hold up the cutover until an"
                                 " administrator sends them again",
                      "actions": []})  # fmt: skip
    if n := int(m.get("copies_stranded") or 0):
        items.append({"kind": "copy_stranded", "migration_id": mid,
                      "message": f"Copies waiting to be checked with no check queued and no dead"
                                 f" letter: {n:,}. They hold up the cutover",
                      "actions": []})  # fmt: skip
    if n := int(m.get("copies_no_checksum") or 0):
        items.append({"kind": "no_checksum", "migration_id": mid,
                      "message": f"Copies with nothing to be checked against: {n:,}. Their S3"
                                 " objects have no checksum on record and nothing is computing"
                                 " one (SSE-C objects can't have one). Exclude them, and they stay"
                                 " on S3, or they hold up the cutover",
                      "actions": ["exclude"]})  # fmt: skip
    return items


def _attention(row: dict[str, Any], m: dict[str, Any]) -> list[dict[str, Any]]:
    mid, items = row["migration_id"], []
    if m["state"] in ("cut_over", "cancelled"):
        return items
    err = m["last_error"] or ""
    items += _copy_items(row, m)
    if ACCESS.search(err):
        items.append({"kind": "access", "migration_id": mid, "message": err,
                      "actions": ["settings"]})  # fmt: skip
    if row["failing_now"]:
        items.append({"kind": "verify_failed", "migration_id": mid,
                      "message": f"{row['failing_now']:,} copies failed their check and have no"
                                 " verified copy yet",
                      "actions": (["rerun", "exclude"] if m["copy_by"] != "external"
                                  else ["exclude"])})  # fmt: skip
    if m["run_state"] == "failed" and m["copy_by"] != "external":
        items.append({"kind": "run_failed", "migration_id": mid,
                      "message": err or "the last Storage Mover run failed",
                      "actions": ["rerun"]})  # fmt: skip
    if row["restores_expiring"] and m["copy_by"] != "external":
        items.append({"kind": "restore_expiring", "migration_id": mid,
                      "message": f"{row['restores_expiring']:,} restored S3 copies expire within"
                                 " 24 hours with no verified copy",
                      "actions": ["restore_again"]})  # fmt: skip
    if m["state"] == "failed":
        items.append({"kind": "failed", "migration_id": mid, "message": err or "it stopped",
                      "actions": ["retry"]})  # fmt: skip
    return items


async def console(ctx: Ctx) -> WebResult:
    """`GET /v1/migration-console`."""
    _session_admin(ctx.who)
    now = ctx.now
    async with ctx.svc.catalog.read() as db:
        ms = await (await db.execute(_ROWS + " ORDER BY m.migration_id DESC LIMIT 500")).fetchall()
        rows, attention = [], []
        waves = await (
            await db.execute(
                "SELECT w.*, p.dataset_id, p.name AS plan_name FROM catalog.migration_wave w"
                " JOIN catalog.migration_plan p USING (plan_id)"
                " WHERE p.state = 'approved' AND w.state IN ('planned', 'waiting', 'blocked')"
                " ORDER BY w.start_at"
            )
        ).fetchall()
        for m in ms:
            row = await _row(db, m, now)
            nxt = next((w for w in waves if w["dataset_id"] == m["dataset_id"]), None)
            row["next_wave"] = None if nxt is None else {
                "plan_id": str(nxt["plan_id"]), "plan_name": nxt["plan_name"], "seq": nxt["seq"],
                "name": nxt["name"], "start_at": rfc3339(nxt["start_at"]),
                "waiting_reason": nxt["waiting_reason"]}  # fmt: skip
            rows.append(row)
            attention += _attention(row, m)
        agents = await (
            await db.execute("SELECT * FROM catalog.mover_agent ORDER BY name")
        ).fetchall()
        for a in agents:
            stale = now - a["polled_at"] > AGENT_STALE
            if a["status"] != "Online" or stale:
                attention.append({
                    "kind": "agent_offline", "migration_id": None, "agent": a["name"],
                    "message": (f"Storage Mover agent {a['name']} is {a['status'] or 'unknown'}"
                                if not stale else
                                f"Storage Mover agent {a['name']} wasn't read for"
                                f" {int((now - a['polled_at']).total_seconds() // 60)} minutes"),
                    "actions": ["agents"]})  # fmt: skip
        seeding = await (
            await db.execute(
                "SELECT s.source_id, s.container, s.prefix, s.state, s.objects_seen,"
                " (SELECT count(*) FROM catalog.migration_wave w"
                "   JOIN catalog.migration_plan p USING (plan_id)"
                "   WHERE p.dataset_id = s.dataset_id AND p.state = 'approved'"
                "     AND w.state IN ('planned', 'waiting')) AS waves_waiting"
                "  FROM catalog.source s WHERE s.kind = 'source'"
                "   AND s.state IN ('registered', 'seeding')"
            )
        ).fetchall()
        for s in seeding:
            attention.append({
                "kind": "seeding", "migration_id": None, "source_id": str(s["source_id"]),
                "message": f"{s['container']}/{s['prefix']} is being catalogued"
                           f" ({int(s['objects_seen']):,} objects so far)"
                           + ("; its planned waves start by themselves once it is done"
                              if s["waves_waiting"] else ""),
                "actions": ["wait"]})  # fmt: skip
    return WebResult(200, {"computed_at": rfc3339(now), "migrations": rows,
                           "attention": attention})  # fmt: skip


def _agent_json(a: dict[str, Any], now) -> dict[str, Any]:
    return {"name": a["name"], "status": a["status"], "version": a["version"], "host": a["host"],
            "arc_resource_id": a["arc_resource_id"],
            "last_status_at": rfc3339(a["last_status_at"]), "error": a["error"],
            "polled_at": rfc3339(a["polled_at"]),
            "stale": now - a["polled_at"] > AGENT_STALE}  # fmt: skip


def _edge_json(e: dict[str, Any] | None, now) -> dict[str, Any] | None:
    if e is None:
        return None
    return {"edge_id": str(e["edge_id"]), "name": e["name"], "status": status_of(e, now),
            "software_version": e["software_version"],
            "last_heartbeat_at": rfc3339(e["last_heartbeat_at"]),
            "guard_state": e["guard_state"]}  # fmt: skip


async def _work(db, m: dict[str, Any], now) -> dict[str, Any]:
    mid = m["migration_id"]
    waves = await (
        await db.execute(
            "SELECT w.*, (SELECT count(*) FROM catalog.restore r WHERE r.wave_id = w.wave_id"
            "   AND r.state = 'completed') AS done,"
            " (SELECT count(*) FROM catalog.restore r WHERE r.wave_id = w.wave_id"
            "   AND r.state IN ('requested', 'in_progress')) AS open,"
            " (SELECT min(r.expires_at) FROM catalog.restore r WHERE r.wave_id = w.wave_id"
            "   AND r.state = 'completed' AND r.expires_at > %s) AS first_expiry"
            "  FROM catalog.restore_wave w WHERE w.migration_id = %s ORDER BY w.wave_id DESC"
            " LIMIT 20",
            (now, mid),
        )
    ).fetchall()
    checks = await (
        await db.execute(
            "SELECT * FROM catalog.checksum_job WHERE migration_id = %s"
            " ORDER BY checksum_job_id DESC LIMIT 20",
            (mid,),
        )
    ).fetchall()
    edge = await (
        await db.execute(
            "SELECT e.* FROM catalog.edge e JOIN catalog.dataset d ON d.edge_id = e.edge_id"
            " WHERE d.dataset_id = %s",
            (m["dataset_id"],),
        )
    ).fetchone()
    return {
        "edge": _edge_json(edge, now),
        "restore_waves": [{
            "wave_id": str(w["wave_id"]), "job_id": w["job_id"], "state": w["state"],
            "objects": w["objects"], "restored": int(w["done"]), "open": int(w["open"]),
            "speed": w["speed"], "days": w["days"], "requested_at": rfc3339(w["requested_at"]),
            "finished_at": rfc3339(w["finished_at"]), "first_expiry": rfc3339(w["first_expiry"]),
            "failure": w["failure"]} for w in waves],
        "checksum_jobs": [{
            "checksum_job_id": str(c["checksum_job_id"]), "job_id": c["job_id"],
            "state": c["state"], "objects": c["objects"], "found": c["found"],
            "requested_at": rfc3339(c["requested_at"]), "finished_at": rfc3339(c["finished_at"]),
            "failure": c["failure"]} for c in checks],
    }  # fmt: skip


#: `migrate/progress.py`'s kinds, and the console's numbers for them.
_STUCK_KINDS = {"stranded": "copies_stranded", "no_checksum": "copies_no_checksum"}

#: A page of one kind's objects, by key. Never counted: the number is the worker's. An object
#: excluded since the worker's last look is left out at once.
_STUCK_PAGE = """
SELECT k.object_key FROM catalog.migration_stuck_copy k
 WHERE k.migration_id = %(mid)s AND k.kind = %(kind)s AND k.object_key > %(after)s
   AND NOT EXISTS (SELECT 1 FROM catalog.migration_exclusion x
                    WHERE x.migration_id = k.migration_id AND x.object_key = k.object_key)
 ORDER BY k.object_key LIMIT %(limit)s
"""


async def stuck_copies(ctx: Ctx) -> WebResult:
    """`GET /v1/migrations/{id}/stuck-copies?kind=`: which objects the worker last counted as
    stuck, so the Exclude dialog can take them. An ArchiveAdmin's, like the console, even for
    files they can't read, so it is audited, like the cutover report, the other read that names
    objects to them; with no AD identity it names none (section 3.3)."""
    who = ctx.who
    _session_admin(who)
    kind = ctx.request.query_params.get("kind")
    if kind not in _STUCK_KINDS:
        raise invalid("kind", f"must be one of {', '.join(sorted(_STUCK_KINDS))}")
    mid = ctx.path_id("migration_id")
    cursor_kind = f"stuck_copies:{mid}:{kind}"
    after = ctx.cursor(cursor_kind)
    limit = ctx.limit()
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await (
            await db.execute(
                "SELECT m.dataset_id, c.copies_stranded, c.copies_no_checksum, c.stuck_counted_at"
                "  FROM catalog.migration_prefix m"
                "  LEFT JOIN catalog.migration_count c USING (migration_id)"
                " WHERE m.migration_id = %s",
                (mid,),
            )
        ).fetchone()
        if m is None:
            raise not_found("migration")
        found: list[dict[str, Any]] = []
        if who.sid is not None:
            found = list(
                await (
                    await db.execute(
                        _STUCK_PAGE,
                        {
                            "mid": mid,
                            "kind": kind,
                            "after": after[0] if after else "",
                            "limit": limit + 1,
                        },
                    )  # fmt: skip
                ).fetchall()
            )
        more = len(found) > limit
        found = found[:limit]
        await audit(db, who, "migration.stuck_copies.read", dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "kind": kind, "rows": len(found),
                             "first_page": after is None})  # fmt: skip
    nxt = ctx.next_cursor(cursor_kind, [found[-1]["object_key"]]) if more else None
    return WebResult(200, {
        "migration_id": str(mid),
        "kind": kind,
        "count": int(m[_STUCK_KINDS[kind]] or 0),
        "counted_at": rfc3339(m["stuck_counted_at"]),
        "list_limit": KEEP_KEYS,
        **page([{"object_key": r["object_key"]} for r in found], nxt),
    })  # fmt: skip


async def migration_agents(ctx: Ctx) -> WebResult:
    """`GET /v1/migrations/{id}/agents`: the agents and jobs doing its work."""
    _session_admin(ctx.who)
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.read() as db:
        m = await (await db.execute(_ROWS + " WHERE m.migration_id = %s", (mid,))).fetchone()
        if m is None:
            raise not_found("migration")
        row = await _row(db, m, ctx.now)
        agents = await (
            await db.execute("SELECT * FROM catalog.mover_agent ORDER BY name")
        ).fetchall()
        work = await _work(db, m, ctx.now)
    return WebResult(200, {
        "migration_id": str(mid),
        "copier_kind": row["copier_kind"],
        "storage_mover": None if m["copy_by"] == "external" else {
            "job_ref": m["storage_mover_job"],
            "last_run": row["copier"]["last_run"],
            "agents": [_agent_json(a, ctx.now) for a in agents],
            # A cloud-to-cloud job (S3 through the Arc multicloud connector)
            # runs without an agent.
            "agentless": True},
        **work,
        "verification": {**row["files"], "bytes": row["bytes"],
                         "journal_checked_through": row["journal_checked_through"],
                         "archived_waiting": row["archived_waiting"]},
    })  # fmt: skip


async def all_agents(ctx: Ctx) -> WebResult:
    """`GET /v1/migration-agents`: which agent and job runs what, where."""
    _session_admin(ctx.who)
    now = ctx.now
    async with ctx.svc.catalog.read() as db:
        agents = await (
            await db.execute("SELECT * FROM catalog.mover_agent ORDER BY name")
        ).fetchall()
        ms = await (
            await db.execute(
                _ROWS + " WHERE m.state NOT IN ('cut_over', 'cancelled') ORDER BY m.migration_id"
            )
        ).fetchall()
        edges = {e["edge_id"]: e for e in await (await db.execute(
            "SELECT * FROM catalog.edge WHERE state <> 'revoked'")).fetchall()}  # fmt: skip
        jobs = []
        for m in ms:
            w = await _work(db, m, now)
            jobs.append({
                "migration_id": str(m["migration_id"]), "dataset_name": m["dataset_name"],
                "prefix": m["prefix"], "state": m["state"],
                "copier_kind": migration_json(m)["copier_kind"],
                "job_ref": m["storage_mover_job"], "run_state": m["run_state"],
                "run_counters": m["run_counters"] or {},
                "edge": _edge_json(edges.get(m["edge_id"]), now),
                "restore_waves_running": sum(1 for x in w["restore_waves"]
                                             if x["state"] == "running"),
                "checksum_jobs_running": sum(1 for x in w["checksum_jobs"]
                                             if x["state"] == "running")})  # fmt: skip
    return WebResult(200, {"agents": [_agent_json(a, now) for a in agents], "jobs": jobs,
                           "polled": bool(agents)})  # fmt: skip


async def _ask(ctx: Ctx, column: str, action: str) -> WebResult:
    who = ctx.who
    _actor(who)
    if who.is_app:
        raise forbidden("the console's fixes are an ArchiveAdmin's")
    if ctx.body:
        raise WebError("invalid_request", "this action takes an empty body")
    mid = ctx.path_id("migration_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        m = await _owned(db, who, mid)
        if m["copy_by"] == "external":
            raise WebError("migration_conflict", "an external copier copies and restores itself")
        if m["state"] in ("cut_over", "cancelled", "failed"):
            raise WebError("migration_conflict", f"the migration is {m['state']}")
        if column == "rerun_requested_at" and m["state"] == "verified":
            raise WebError("migration_conflict", "every file is verified: there is nothing to"
                           " copy again")  # fmt: skip
        await db.execute(
            f"UPDATE catalog.migration_prefix SET {column} = %s, updated_at = %s"
            " WHERE migration_id = %s",
            (ctx.now, ctx.now, mid),
        )
        from ..worker.queue import send

        with tuple_rows(db):
            await send(db, "migrate", {"migration_id": mid})
        await audit(db, who, action, dataset_id=m["dataset_id"],
                    details={"migration_id": str(mid), "state": m["state"]})  # fmt: skip
        from .routes_migrations import _load

        out = migration_json(await _load(db, mid))
    return WebResult(200, out)


async def rerun(ctx: Ctx) -> WebResult:
    """Copy again: a new Additive run at the engine's next step (it also
    copies again what failed its check)."""
    return await _ask(ctx, "rerun_requested_at", "migration.rerun")


async def restore_again(ctx: Ctx) -> WebResult:
    """Restore again the restored S3 copies about to expire unverified."""
    return await _ask(ctx, "restore_requested_at", "migration.restore_again")
