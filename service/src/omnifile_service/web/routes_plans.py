"""Staged migration plans (portal-api.md section 5.22; migration 0037).

A plan splits a dataset's migration into waves of S3 prefixes, each with a
start time, an optional daily window (UTC), blackouts, the wave it waits
for, a cutover mode and a copier. The worker's plan sweep
(migrate/plans.py) starts the waves and cuts them over; nothing here
touches a migration.

- **Who.** A signed-in ArchiveAdmin's plan is approved as it is made. An API
  client's (`Archive.Adopt`) is a proposal that an ArchiveAdmin approves or
  rejects (anti-pattern 7). Every plan names an owner: an AD identity in the
  Owners or Writers group of the sources its prefixes lie in (a signed-in
  ArchiveAdmin may name themself).
- **Checks.** Prefixes are `''` or end in `/`, lie inside a source of the
  dataset (one still being catalogued is fine: its wave waits), and don't
  overlap one another or a migration under way. A wave waits only for an
  earlier wave. Its window and blackouts must leave a minute to start within
  60 days of its start time.
- **Rescheduling** (`PATCH .../waves/{seq}`, the schedule view's drag): only a
  wave not yet started; it may not start before the wave it waits for, nor
  after a wave that waits for it. `base_version` guards concurrent edits.
- **Advisory caps.** `bandwidth_mbps` and `egress_gb_per_day` are recorded
  and shown, not applied.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..api.validate import parse_id, rfc3339, timestamp
from ..apiclients.access import owner_can_see_dataset, require_owner_access
from ..migrate.plans import next_open
from ..worker.queue import send
from .access import load_scope
from .context import Ctx, Principal, WebResult, audit, page, tuple_rows
from .problems import WebError, forbidden, invalid, not_found, role_required

PLAN_FIELDS = {"dataset_id", "name", "owner_identity_id", "waves"}
WAVE_FIELDS = {"name", "prefixes", "start_at", "window", "blackouts", "after", "cutover", "copier",
               "bandwidth_mbps", "egress_gb_per_day"}  # fmt: skip
EDITABLE = {"start_at", "window", "blackouts", "after", "cutover", "base_version"}
MAX_WAVES = 100
HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
HORIZON = timedelta(days=60)
QUEUE = "migrate"


def _admin_session(who: Principal) -> None:
    if who.is_app:
        raise forbidden("only a signed-in ArchiveAdmin does this")
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    if who.identity_id is None:
        raise forbidden("this needs an AD identity (an on-premises SID)")


def _maker(who: Principal) -> None:
    if who.is_app:
        if not who.has("Archive.Adopt"):
            raise role_required("Archive.Adopt")
        return
    _admin_session(who)


def _reader(who: Principal) -> None:
    if who.is_app:
        if not who.has("Archive.Read", "Archive.Adopt"):
            raise role_required("Archive.Read", "Archive.Adopt")
    elif not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def _minutes(v: str, field: str) -> int:
    m = HHMM.match(v) if isinstance(v, str) else None
    if m is None:
        raise invalid(field, "must be HH:MM (UTC)")
    return int(m.group(1)) * 60 + int(m.group(2))


def _window(v: Any, field: str) -> tuple[int | None, int | None]:
    if v is None:
        return None, None
    if not isinstance(v, dict) or set(v) != {"start", "end"}:
        raise invalid(field, "must be {start, end} in HH:MM (UTC), or null")
    a, b = _minutes(v["start"], f"{field}.start"), _minutes(v["end"], f"{field}.end")
    if a == b:
        raise invalid(field, "start and end are the same minute: leave the window out instead")
    return a, b


def _blackouts(v: Any, field: str) -> list[dict[str, str]]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 50:
        raise invalid(field, "must be up to 50 {from, to} ranges")
    out = []
    for i, b in enumerate(v):
        if not isinstance(b, dict) or set(b) != {"from", "to"}:
            raise invalid(f"{field}[{i}]", "must be {from, to}")
        f, t = timestamp(b["from"], f"{field}[{i}].from"), timestamp(b["to"], f"{field}[{i}].to")
        if t <= f:
            raise invalid(f"{field}[{i}]", "to must be after from")
        out.append({"from": rfc3339(f), "to": rfc3339(t)})
    return out


def _wave(v: Any, seq: int) -> dict[str, Any]:
    f = f"waves[{seq - 1}]"
    if not isinstance(v, dict):
        raise invalid(f, "must be a wave")
    extra = set(v) - WAVE_FIELDS
    if extra:
        raise invalid(f"{f}.{sorted(extra)[0]}", "is not a wave field")
    name = v.get("name") or f"Wave {seq}"
    if not isinstance(name, str) or not 0 < len(name) <= 120:
        raise invalid(f"{f}.name", "must be 1 to 120 characters")
    prefixes = v.get("prefixes")
    if not isinstance(prefixes, list) or not 1 <= len(prefixes) <= 100:
        raise invalid(f"{f}.prefixes", "must be 1 to 100 prefixes")
    for p in prefixes:
        if not isinstance(p, str) or len(p) > 1024 or (p and not p.endswith("/")):
            raise invalid(f"{f}.prefixes", "each is '' or ends in '/'")
    start = timestamp(v.get("start_at"), f"{f}.start_at")
    ws, we = _window(v.get("window"), f"{f}.window")
    blackouts = _blackouts(v.get("blackouts"), f"{f}.blackouts")
    after = v.get("after")
    if after is not None and (not isinstance(after, int) or isinstance(after, bool)
                              or not 1 <= after < seq):  # fmt: skip
        raise invalid(f"{f}.after", "must be the number of an earlier wave")
    cutover, copier = v.get("cutover", "auto"), v.get("copier", "auto")
    if cutover not in ("auto", "manual"):
        raise invalid(f"{f}.cutover", "must be auto or manual")
    if copier not in ("auto", "external"):
        raise invalid(f"{f}.copier", "must be auto or external")
    if copier == "external":
        # An external copier reports its runs as the app that started the
        # migration, and a plan's migrations are the plan's (system:plans).
        raise WebError(
            "action_not_allowed",
            f"{f}.copier: an external copier isn't offered in plans yet (its run reports"
            " can't reach a plan's migrations); start the migration with POST /v1/migrations"
            " and copier external instead",
        )
    caps = {}
    for k in ("bandwidth_mbps", "egress_gb_per_day"):
        c = v.get(k)
        if c is not None and (not isinstance(c, int) or isinstance(c, bool) or not 0 < c <= 10**6):
            raise invalid(f"{f}.{k}", "must be a positive whole number, or null")
        caps[k] = c
    if next_open(ws, we, blackouts, start, HORIZON) is None:
        raise invalid(f"{f}.window", "the window and blackouts leave no minute to start within"
                      " 60 days of start_at")  # fmt: skip
    return {"seq": seq, "name": name, "prefixes": prefixes, "start_at": start,
            "window_start_min": ws, "window_end_min": we, "blackouts": blackouts,
            "after_seq": after, "cutover": cutover, "copier": copier, **caps}  # fmt: skip


def _overlap(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


async def _check_prefixes(db, dataset_id: int, waves: list[dict[str, Any]]) -> list[dict]:
    """Every prefix inside a source of the dataset, none overlapping another
    or a migration under way. Returns the sources the waves touch."""
    seen: list[str] = []
    for w in waves:
        for p in w["prefixes"]:
            clash = next((q for q in seen if _overlap(p, q)), None)
            if clash is not None:
                raise invalid("waves", f"prefix {p!r} overlaps {clash!r} in this plan")
            seen.append(p)
    sources = await (
        await db.execute(
            "SELECT s.source_id, s.prefix, s.state, s.owners_sid, s.writers_sid"
            "  FROM catalog.source s JOIN catalog.dataset d USING (dataset_id)"
            " WHERE s.dataset_id = %s AND s.kind = 'source'"
            "   AND s.backend_id = d.source_backend_id",
            (dataset_id,),
        )
    ).fetchall()
    touched: dict[int, dict] = {}
    for p in seen:
        inside = [s for s in sources if p.startswith(s["prefix"])]
        if not inside:
            raise invalid("waves", f"prefix {p!r} is not inside a source of the dataset")
        s = max(inside, key=lambda s: len(s["prefix"]))
        touched[s["source_id"]] = s
    running = await (
        await db.execute(
            "SELECT migration_id, prefix FROM catalog.migration_prefix"
            " WHERE dataset_id = %s AND state <> 'cancelled'",
            (dataset_id,),
        )
    ).fetchall()
    for p in seen:
        for r in running:
            if _overlap(p, r["prefix"]):
                raise WebError("migration_conflict", f"prefix {p!r} overlaps migration"
                               f" {r['migration_id']} ({r['prefix']!r})")  # fmt: skip
    return list(touched.values())


async def _owner(db, who: Principal, raw: Any, sources: list[dict]) -> int:
    """The plan's owner: an AD user in the Owners or Writers group of every
    source it touches. A signed-in ArchiveAdmin may name themself."""
    if raw is None:
        if who.is_app:
            raise invalid("owner_identity_id", "an API client's plan names its owner")
        assert who.identity_id is not None
        return int(who.identity_id)
    oid = parse_id(raw, "owner_identity_id")
    if not who.is_app and who.identity_id is not None and oid == int(who.identity_id):
        return oid
    row = await (
        await db.execute(
            # group_sids is an array of the sid domain: read it as text[].
            "SELECT kind, group_sids::text[] AS groups FROM catalog.identity"
            " WHERE identity_id = %s",
            (oid,),
        )
    ).fetchone()
    if row is None or row["kind"] != "user":
        raise invalid("owner_identity_id", "is not a user the catalog knows")
    groups = set(row["groups"] or [])
    for s in sources:
        if not groups & ({s["owners_sid"], s["writers_sid"]} - {None}):
            raise invalid("owner_identity_id", "the owner must be in the Owners or Writers group"
                          f" of the source {s['prefix']!r}")  # fmt: skip
    return oid


_PLAN = "SELECT * FROM catalog.migration_plan WHERE plan_id = %s"


async def _load(db, plan_id: int, *, lock: bool = False) -> dict[str, Any]:
    p = await (await db.execute(_PLAN + (" FOR UPDATE" if lock else ""), (plan_id,))).fetchone()
    if p is None:
        raise not_found("plan")
    return p


def _hhmm(m: int | None) -> str | None:
    return None if m is None else f"{m // 60:02d}:{m % 60:02d}"


def wave_json(w: dict[str, Any], migrations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "seq": w["seq"],
        "name": w["name"],
        "prefixes": list(w["prefixes"]),
        "start_at": rfc3339(w["start_at"]),
        "window": None if w["window_start_min"] is None else {
            "start": _hhmm(w["window_start_min"]), "end": _hhmm(w["window_end_min"])},
        "blackouts": w["blackouts"],
        "after": w["after_seq"],
        "cutover": w["cutover"],
        "copier": w["copier"],
        "bandwidth_mbps": w["bandwidth_mbps"],
        "egress_gb_per_day": w["egress_gb_per_day"],
        "state": w["state"],
        "waiting_reason": w["waiting_reason"],
        "started_at": rfc3339(w["started_at"]),
        "verified_at": rfc3339(w["verified_at"]),
        "cut_over_at": rfc3339(w["cut_over_at"]),
        "migrations": [{"prefix": m["prefix"], "migration_id": str(m["migration_id"]),
                        "state": m["state"]} for m in migrations],  # fmt: skip
    }  # fmt: skip


async def plan_json(db, p: dict[str, Any]) -> dict[str, Any]:
    waves = await (
        await db.execute(
            "SELECT * FROM catalog.migration_wave WHERE plan_id = %s ORDER BY seq", (p["plan_id"],)
        )
    ).fetchall()
    links = await (
        await db.execute(
            "SELECT x.wave_id, x.prefix, x.migration_id, m.state"
            "  FROM catalog.migration_wave_prefix x"
            " JOIN catalog.migration_prefix m USING (migration_id)"
            " JOIN catalog.migration_wave w ON w.wave_id = x.wave_id WHERE w.plan_id = %s"
            " ORDER BY x.prefix",
            (p["plan_id"],),
        )
    ).fetchall()
    owner = await (
        await db.execute(
            "SELECT identity_id, display_name FROM catalog.identity WHERE identity_id = %s",
            (p["owner_identity_id"],),
        )
    ).fetchone()
    return {
        "plan_id": str(p["plan_id"]),
        "dataset_id": str(p["dataset_id"]),
        "name": p["name"],
        "state": p["state"],
        "owner": {
            "identity_id": str(p["owner_identity_id"]),
            "display_name": owner["display_name"] if owner else None,
        },  # fmt: skip
        "proposed_by": p["proposed_by"],
        "decided_by": p["decided_by"],
        "decided_at": rfc3339(p["decided_at"]),
        "decision_note": p["decision_note"],
        "version": int(p["entity_version"]),
        "created_at": rfc3339(p["created_at"]),
        "updated_at": rfc3339(p["updated_at"]),
        "waves": [
            wave_json(w, [x for x in links if x["wave_id"] == w["wave_id"]]) for w in waves
        ],  # fmt: skip
    }


async def create(ctx: Ctx) -> WebResult:
    """`POST /v1/migration-plans`."""
    who = ctx.who
    _maker(who)
    b = ctx.body
    extra = set(b) - PLAN_FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a plan field")
    dataset_id = parse_id(b.get("dataset_id"), "dataset_id")
    name = b.get("name")
    if not isinstance(name, str) or not 0 < len(name) <= 120:
        raise invalid("name", "must be 1 to 120 characters")
    raw = b.get("waves")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_WAVES:
        raise invalid("waves", f"must be 1 to {MAX_WAVES} waves")
    waves = [_wave(w, i + 1) for i, w in enumerate(raw)]
    async with ctx.svc.catalog.tx(who.actor) as db:
        ds = await (
            await db.execute(
                "SELECT dataset_id FROM catalog.dataset WHERE dataset_id = %s", (dataset_id,)
            )  # fmt: skip
        ).fetchone()
        if ds is None:
            raise invalid("dataset_id", "is not a dataset")
        # The owner's rule for API clients (5.21): the client's owner must see
        # the dataset, as for a policy proposal.
        await require_owner_access(db, who, dataset_id, ctx.now)
        sources = await _check_prefixes(db, dataset_id, waves)
        owner = await _owner(db, who, b.get("owner_identity_id"), sources)
        approved = not who.is_app
        p = await (
            await db.execute(
                "INSERT INTO catalog.migration_plan (dataset_id, name, state, owner_identity_id,"
                " proposed_by, decided_by, decided_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " RETURNING *",
                (
                    dataset_id,
                    name,
                    "approved" if approved else "proposed",
                    owner,
                    who.actor,
                    who.actor if approved else None,
                    ctx.now if approved else None,
                ),
            )  # fmt: skip
        ).fetchone()
        assert p is not None
        for w in waves:
            await db.execute(
                "INSERT INTO catalog.migration_wave (plan_id, seq, name, prefixes, start_at,"
                " window_start_min, window_end_min, blackouts, after_seq, cutover, copier,"
                " bandwidth_mbps, egress_gb_per_day) VALUES (%(plan)s, %(seq)s, %(name)s,"
                " %(prefixes)s, %(start_at)s, %(window_start_min)s, %(window_end_min)s,"
                " %(blackouts)s::jsonb, %(after_seq)s, %(cutover)s, %(copier)s,"
                " %(bandwidth_mbps)s, %(egress_gb_per_day)s)",
                {**w, "plan": p["plan_id"], "blackouts": _json(w["blackouts"])},
            )
        await audit(db, who, "migration_plan.create", dataset_id=dataset_id,
                    details={"plan_id": str(p["plan_id"]), "waves": len(waves),
                             "state": p["state"]})  # fmt: skip
        out = await plan_json(db, p)
    return WebResult(201, out)


def _json(v: Any) -> str:
    import json

    return json.dumps(v)


async def list_plans(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    limit = ctx.limit()
    after = ctx.cursor("plans")
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT * FROM catalog.migration_plan"
                " WHERE (%(ds)s::bigint IS NULL OR dataset_id = %(ds)s::bigint)"
                "   AND (%(after)s::bigint IS NULL OR plan_id < %(after)s::bigint)"
                "   AND (%(app)s::text IS NULL OR proposed_by = %(app)s::text)"
                " ORDER BY plan_id DESC LIMIT %(limit)s",
                {
                    "ds": ds,
                    "after": int(after[0]) if after else None,
                    "limit": limit + 1,
                    "app": ctx.who.actor if ctx.who.is_app else None,
                },  # fmt: skip
            )
        ).fetchall()
        more = len(rows) > limit
        rows = list(rows[:limit])
        items = [await plan_json(db, p) for p in rows]
    nxt = ctx.next_cursor("plans", [rows[-1]["plan_id"]]) if more else None
    return WebResult(200, page(items, nxt))


async def get_plan(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        p = await _load(db, ctx.path_id("plan_id"))
        if ctx.who.is_app and p["proposed_by"] != ctx.who.actor:
            raise not_found("plan")
        return WebResult(200, await plan_json(db, p))


async def _decide(ctx: Ctx, state: str) -> WebResult:
    who = ctx.who
    _admin_session(who)
    b = ctx.body
    extra = set(b) - {"note"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a field of a decision")
    note = b.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise invalid("note", "must be at most 500 characters")
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load(db, ctx.path_id("plan_id"), lock=True)
        if p["state"] != "proposed":
            raise WebError("migration_conflict", f"the plan is {p['state']}")
        if p["proposed_by"] == who.actor:
            raise forbidden("a proposal is decided by someone other than its proposer")
        if state == "approved":
            await _approvable(db, p, who, ctx.now)
        p = await (
            await db.execute(
                "UPDATE catalog.migration_plan SET state = %s, decided_by = %s, decided_at = %s,"
                " decision_note = %s, entity_version = entity_version + 1, updated_at = %s"
                " WHERE plan_id = %s RETURNING *",
                (state, who.actor, ctx.now, note, ctx.now, p["plan_id"]),
            )
        ).fetchone()
        assert p is not None
        await audit(db, who, f"migration_plan.{'approve' if state == 'approved' else 'reject'}",
                    dataset_id=p["dataset_id"], details={"plan_id": str(p["plan_id"])})  # fmt: skip
        out = await plan_json(db, p)
    return WebResult(200, out)


async def _approvable(db, p: dict[str, Any], who: Principal, now: datetime) -> None:
    """As for an API client's policy proposal (5.21, review of #43): nobody who
    answers for the client approves its plan (its owner, who registered it,
    the plan's named owner), the client must still be enabled, and its owner
    must still see the dataset."""
    if not str(p["proposed_by"]).startswith("app:"):
        return
    c = await (
        await db.execute(
            "SELECT state, owner_identity_id, created_by_identity_id FROM catalog.api_client"
            " WHERE app_id = %s",
            (str(p["proposed_by"])[4:],),
        )
    ).fetchone()
    mine = {int(p["owner_identity_id"])}
    if c is not None:
        mine |= {int(x) for x in (c["owner_identity_id"], c["created_by_identity_id"]) if x}
    if who.identity_id is not None and int(who.identity_id) in mine:
        raise WebError("self_approval", "you answer for the API client this plan came from (its"
                       " owner, who registered it, or the plan's owner), so another ArchiveAdmin"
                       " approves it")  # fmt: skip
    if c is None or c["state"] != "enabled":
        state = "gone" if c is None else c["state"]
        raise WebError("client_disabled", f"the API client this plan came from is {state}")
    if not await owner_can_see_dataset(db, int(c["owner_identity_id"]), p["dataset_id"], now):
        raise WebError("owner_lacks_access", "the API client's owner can no longer see this"
                       " dataset, so its plan can't be approved")  # fmt: skip


async def approve(ctx: Ctx) -> WebResult:
    return await _decide(ctx, "approved")


async def reject(ctx: Ctx) -> WebResult:
    return await _decide(ctx, "rejected")


async def cancel(ctx: Ctx) -> WebResult:
    """Waves not started are cancelled; started ones go on (cancel their
    migrations on their own pages: nothing here stops a copy)."""
    who = ctx.who
    _admin_session(who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "cancel takes no fields")
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load(db, ctx.path_id("plan_id"), lock=True)
        if p["state"] not in ("proposed", "approved"):
            raise WebError("migration_conflict", f"the plan is {p['state']}")
        await db.execute(
            "UPDATE catalog.migration_wave SET state = 'cancelled', updated_at = %s"
            " WHERE plan_id = %s AND state IN ('planned', 'waiting', 'blocked')",
            (ctx.now, p["plan_id"]),
        )
        p = await (
            await db.execute(
                "UPDATE catalog.migration_plan SET state = 'cancelled',"
                " entity_version = entity_version + 1, updated_at = %s WHERE plan_id = %s"
                " RETURNING *",
                (ctx.now, p["plan_id"]),
            )
        ).fetchone()
        assert p is not None
        await audit(db, who, "migration_plan.cancel", dataset_id=p["dataset_id"],
                    details={"plan_id": str(p["plan_id"])})  # fmt: skip
        out = await plan_json(db, p)
    return WebResult(200, out)


async def reschedule(ctx: Ctx) -> WebResult:
    """`PATCH /v1/migration-plans/{plan_id}/waves/{seq}`: the schedule view's
    drag or its keyboard and form alternative."""
    who = ctx.who
    _admin_session(who)
    b = ctx.body
    extra = set(b) - EDITABLE
    if extra:
        raise invalid(sorted(extra)[0], "can't be changed here")
    if "base_version" not in b:
        raise invalid("base_version", "is required (the plan's version you edited)")
    seq = int(ctx.request.path_params["seq"]) if str(
        ctx.request.path_params.get("seq", "")).isdigit() else 0  # fmt: skip
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load(db, ctx.path_id("plan_id"), lock=True)
        if b["base_version"] != int(p["entity_version"]):
            raise WebError("precondition_failed", "the plan changed since you loaded it")
        if p["state"] not in ("proposed", "approved"):
            raise WebError("migration_conflict", f"the plan is {p['state']}")
        waves = {w["seq"]: w for w in await (await db.execute(
            "SELECT * FROM catalog.migration_wave WHERE plan_id = %s FOR UPDATE",
            (p["plan_id"],))).fetchall()}  # fmt: skip
        w = waves.get(seq)
        if w is None:
            raise not_found("wave")
        # A blocked wave too: changing or dropping `after` unblocks it.
        if w["state"] not in ("planned", "waiting", "blocked"):
            raise WebError("migration_conflict", f"wave {seq} is {w['state']}: only a wave not"
                           " started can be rescheduled")  # fmt: skip
        merged = {
            "name": w["name"], "prefixes": list(w["prefixes"]),
            "start_at": b.get("start_at", rfc3339(w["start_at"])),
            "window": b["window"] if "window" in b else (None if w["window_start_min"] is None
                      else {"start": _hhmm(w["window_start_min"]),
                            "end": _hhmm(w["window_end_min"])}),
            "blackouts": b.get("blackouts", w["blackouts"]),
            "after": b.get("after", w["after_seq"]),
            "cutover": b.get("cutover", w["cutover"]), "copier": w["copier"],
            "bandwidth_mbps": w["bandwidth_mbps"], "egress_gb_per_day": w["egress_gb_per_day"],
        }  # fmt: skip
        nw = _wave(merged, seq)
        dep = waves.get(nw["after_seq"]) if nw["after_seq"] else None
        if dep is not None and nw["start_at"] < dep["start_at"]:
            raise invalid("start_at", f"wave {seq} waits for wave {dep['seq']}, which starts"
                          f" {rfc3339(dep['start_at'])}: it can't start before that")  # fmt: skip
        for other in waves.values():
            if other["after_seq"] == seq and other["start_at"] < nw["start_at"]:
                raise invalid("start_at", f"wave {other['seq']} waits for this one and starts"
                              f" {rfc3339(other['start_at'])}: move that one first")  # fmt: skip
        await db.execute(
            "UPDATE catalog.migration_wave SET start_at = %(start_at)s,"
            " window_start_min = %(window_start_min)s, window_end_min = %(window_end_min)s,"
            " blackouts = %(blackouts)s::jsonb, after_seq = %(after_seq)s, cutover = %(cutover)s,"
            " state = 'planned', waiting_reason = NULL, updated_at = %(now)s"
            " WHERE wave_id = %(wave_id)s",
            {**nw, "blackouts": _json(nw["blackouts"]), "now": ctx.now, "wave_id": w["wave_id"]},
        )
        p = await (
            await db.execute(
                "UPDATE catalog.migration_plan SET entity_version = entity_version + 1,"
                " updated_at = %s WHERE plan_id = %s RETURNING *",
                (ctx.now, p["plan_id"]),
            )
        ).fetchone()
        assert p is not None
        await audit(db, who, "migration_plan.reschedule", dataset_id=p["dataset_id"],
                    details={"plan_id": str(p["plan_id"]), "seq": seq,
                             "start_at": rfc3339(nw["start_at"]),
                             "fields": sorted(set(b) - {"base_version"})})  # fmt: skip
        out = await plan_json(db, p)
    return WebResult(200, out)


async def wave_cutover(ctx: Ctx) -> WebResult:
    """`POST .../waves/{seq}/cutover`: a manual wave's cutover, for its
    verified migrations only (decision 24)."""
    who = ctx.who
    _admin_session(who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "cutover takes no fields")
    seq = int(ctx.request.path_params["seq"]) if str(
        ctx.request.path_params.get("seq", "")).isdigit() else 0  # fmt: skip
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load(db, ctx.path_id("plan_id"))
        w = await (await db.execute(
            "SELECT * FROM catalog.migration_wave WHERE plan_id = %s AND seq = %s FOR UPDATE",
            (p["plan_id"], seq))).fetchone()  # fmt: skip
        if w is None:
            raise not_found("wave")
        if w["state"] != "verified":
            raise WebError("migration_conflict", f"wave {seq} is {w['state']}: it cuts over once"
                           " every file is verified")  # fmt: skip
        asked = await (await db.execute(
            "UPDATE catalog.migration_prefix m SET cutover_requested_at = %s,"
            " cutover_requested_by = %s, updated_at = %s"
            "  FROM catalog.migration_wave_prefix x WHERE x.migration_id = m.migration_id"
            "   AND x.wave_id = %s AND m.state = 'verified' AND m.cutover_requested_at IS NULL"
            " RETURNING m.migration_id",
            (ctx.now, who.actor, ctx.now, w["wave_id"]))).fetchall()  # fmt: skip
        with tuple_rows(db):
            for r in asked:
                await send(db, QUEUE, {"migration_id": int(r["migration_id"])})
        await audit(db, who, "migration_plan.cutover", dataset_id=p["dataset_id"],
                    details={"plan_id": str(p["plan_id"]), "seq": seq,
                             "migrations": len(asked)})  # fmt: skip
        out = await plan_json(db, p)
    return WebResult(200, out)


async def _wave_action(ctx: Ctx, action: str) -> WebResult:
    """`POST .../waves/{seq}/retry` (its failed migrations, back to pending)
    and `.../cancel` (its migrations not cut over; nothing is deleted, as for
    one migration, 5.17). The only way to retry or cancel a wave's
    migrations: they are the plan's."""
    who = ctx.who
    _admin_session(who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], f"{action} takes no fields")
    seq = int(ctx.request.path_params["seq"]) if str(
        ctx.request.path_params.get("seq", "")).isdigit() else 0  # fmt: skip
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load(db, ctx.path_id("plan_id"))
        w = await (await db.execute(
            "SELECT * FROM catalog.migration_wave WHERE plan_id = %s AND seq = %s FOR UPDATE",
            (p["plan_id"], seq))).fetchone()  # fmt: skip
        if w is None:
            raise not_found("wave")
        if action == "retry":
            sql = ("UPDATE catalog.migration_prefix m SET state = 'pending', last_error = NULL,"
                   " updated_at = %s FROM catalog.migration_wave_prefix x"
                   " WHERE x.migration_id = m.migration_id AND x.wave_id = %s"
                   "   AND m.state = 'failed' RETURNING m.migration_id")  # fmt: skip
            args: tuple[Any, ...] = (ctx.now, w["wave_id"])
        else:
            sql = ("UPDATE catalog.migration_prefix m SET state = 'cancelled', cancelled_at = %s,"
                   " cancelled_by = %s, updated_at = %s FROM catalog.migration_wave_prefix x"
                   " WHERE x.migration_id = m.migration_id AND x.wave_id = %s"
                   "   AND m.state NOT IN ('cut_over', 'cancelled')"
                   " RETURNING m.migration_id")  # fmt: skip
            args = (ctx.now, who.actor, ctx.now, w["wave_id"])
        done = await (await db.execute(sql, args)).fetchall()
        if not done:
            raise WebError("migration_conflict", f"wave {seq} has nothing to {action}")
        if action == "retry":
            with tuple_rows(db):
                for r in done:
                    await send(db, QUEUE, {"migration_id": int(r["migration_id"])})
        await audit(db, who, f"migration_plan.wave_{action}", dataset_id=p["dataset_id"],
                    details={"plan_id": str(p["plan_id"]), "seq": seq,
                             "migrations": len(done)})  # fmt: skip
        out = await plan_json(db, p)
    return WebResult(200, out)


async def wave_retry(ctx: Ctx) -> WebResult:
    return await _wave_action(ctx, "retry")


async def wave_cancel(ctx: Ctx) -> WebResult:
    return await _wave_action(ctx, "cancel")


async def split(ctx: Ctx) -> WebResult:
    """`GET /v1/migration-plans/split?source_id&by=count|bytes&waves=N`: the
    source's next-level prefixes with their counts, in N buckets of about the
    same size (largest first). Objects directly under the source prefix can
    move only with the whole prefix, so they're counted apart.

    Prefixes are folder names, so a signed-in caller sees only those of the
    folders they can read (a role never grants read, section 3.2); the rest
    are counted in `hidden`, without names, and left out of the waves. An app
    acts at dataset level, once its client's owner can see the dataset."""
    _reader(ctx.who)
    source_id = ctx.query_id("source_id")
    if source_id is None:
        raise invalid("source_id", "is required")
    by = ctx.query("by") or "bytes"
    if by not in ("bytes", "count"):
        raise invalid("by", "must be bytes or count")
    n_raw = ctx.query("waves") or "4"
    if not n_raw.isdigit() or not 1 <= int(n_raw) <= MAX_WAVES:
        raise invalid("waves", f"must be 1 to {MAX_WAVES}")
    n = int(n_raw)
    async with ctx.svc.catalog.read() as db:
        s = await (await db.execute("SELECT * FROM catalog.source WHERE source_id = %s",
                                    (source_id,))).fetchone()  # fmt: skip
        if s is None:
            raise not_found("source")
        await require_owner_access(db, ctx.who, s["dataset_id"], ctx.now)
        rows = await (await db.execute(
            """
            SELECT CASE WHEN position('/' IN substr(l.object_key, length(%(p)s) + 1)) = 0
                        THEN NULL
                        ELSE %(p)s || split_part(substr(l.object_key, length(%(p)s) + 1), '/', 1)
                             || '/' END AS prefix,
                   count(*) AS files, coalesce(sum(l.size), 0) AS bytes
              FROM catalog.location l
              JOIN catalog.file f ON f.content_version_id = l.version_id AND f.deleted_at IS NULL
             WHERE l.backend_id = %(b)s AND l.container = %(c)s AND l.state = 'committed'
               AND starts_with(l.object_key, %(p)s)
             GROUP BY 1 ORDER BY 1 NULLS FIRST
            """,
            {"p": s["prefix"], "b": s["backend_id"], "c": s["container"]})).fetchall()  # fmt: skip
        loose = next((r for r in rows if r["prefix"] is None), None)
        parts = [r for r in rows if r["prefix"] is not None]
        hidden = {"folders": 0, "files": 0, "bytes": 0}
        if not ctx.who.is_app:
            seen = []
            for r in parts:
                if await _can_read_folder(db, ctx.who, s, r["prefix"]):
                    seen.append(r)
                else:
                    hidden["folders"] += 1
                    hidden["files"] += int(r["files"])
                    hidden["bytes"] += int(r["bytes"])
            parts = seen
    key = "bytes" if by == "bytes" else "files"
    buckets: list[dict[str, Any]] = [{"prefixes": [], "files": 0, "bytes": 0}
                                     for _ in range(min(n, len(parts)) or 1)]  # fmt: skip
    for r in sorted(parts, key=lambda r: -int(r[key])):
        b = min(buckets, key=lambda b: b[key])
        b["prefixes"].append(r["prefix"])
        b["files"] += int(r["files"])
        b["bytes"] += int(r["bytes"])
    for b in buckets:
        b["prefixes"].sort()
    return WebResult(200, {
        "source_id": str(source_id),
        "prefix": s["prefix"],
        "by": by,
        "prefixes": [{"prefix": r["prefix"], "files": int(r["files"]), "bytes": int(r["bytes"])}
                     for r in parts],
        "loose": {"files": int(loose["files"]), "bytes": int(loose["bytes"])} if loose
                 else {"files": 0, "bytes": 0},
        "hidden": hidden,
        "waves": [b for b in buckets if b["prefixes"]],
    })  # fmt: skip


async def _can_read_folder(db, who: Principal, s: dict[str, Any], prefix: str) -> bool:
    """The dataset folder a next-level prefix of the source lands in: may the
    caller read it, as in a listing (FileScope: their SID, the folder's ACL,
    a covering share)?"""
    rest = prefix[len(s["prefix"]) :].strip("/")
    names = [x for x in (s["path"] or "").split("/") if x] + [rest]
    cur = await (await db.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
        (s["dataset_id"],))).fetchone()  # fmt: skip
    for name in names:
        if cur is None:
            return False
        cur = await (await db.execute(
            "SELECT file_id FROM catalog.file WHERE parent_id = %s AND name = %s"
            "   AND kind = 'dir' AND deleted_at IS NULL",
            (cur["file_id"], name))).fetchone()  # fmt: skip
    if cur is None:
        return False
    scope = await load_scope(db, who, int(cur["file_id"]))
    return scope.readable


__all__ = ["approve", "cancel", "create", "get_plan", "list_plans", "reject", "reschedule",
           "split", "wave_cancel", "wave_cutover", "wave_retry"]  # fmt: skip
