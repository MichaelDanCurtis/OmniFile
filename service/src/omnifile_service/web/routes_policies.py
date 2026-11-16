"""Datasets, policies, the live preview and proposals (portal-api.md 5.7).

Anti-pattern 7: a proposal (from Prizmanyx, any AI, or a DataSteward) never
applies itself. The only ways a policy version becomes active are an
ArchiveAdmin saving one (`POST /v1/datasets/{id}/policies`) or approving a
proposal someone else made. The catalog enforces both too: every policy
names its approver, and a proposal can't be approved by its proposer
(`proposal_not_self_approved`, `policy_guard`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..api.validate import parse_id, rfc3339
from ..apiclients import registry as api_clients
from ..apiclients.access import require_owner_access
from ..policy.scheduler import reschedule_dataset
from . import rows
from .access import share_roles
from .context import Ctx, Principal, WebResult, audit, page
from .facts import dataset_facts
from .problems import WebError, forbidden, invalid, not_found, role_required

PROPOSAL_STATES = ("pending", "approved", "rejected", "withdrawn", "superseded")


# --- who may read policies ------------------------------------------------------------


async def _can_read_policies(db, who: Principal, dataset_id: int) -> None:
    if who.is_app:
        if who.has("Archive.Read", "Policy.Propose"):
            return
        raise role_required("Archive.Read")
    if who.has("ArchiveAdmin", "DataSteward"):
        return
    shares = await (
        await db.execute("SELECT * FROM catalog.share WHERE dataset_id = %s", (dataset_id,))
    ).fetchall()
    if any(share_roles(who, s).owner for s in shares):
        return
    raise role_required("ArchiveAdmin", "DataSteward", "a share Owner")


def _require_identity(who: Principal) -> int:
    if who.identity_id is None:
        raise forbidden("this needs an AD identity (an on-premises SID)")
    return who.identity_id


# --- JSON ------------------------------------------------------------------------------


async def _people(db, ids: list[int | None]) -> dict[int, dict[str, Any]]:
    want = [i for i in ids if i is not None]
    found = await (
        await db.execute(
            "SELECT identity_id, display_name FROM catalog.identity WHERE identity_id = ANY(%s)",
            (want,),
        )
    ).fetchall()
    return {r["identity_id"]: r for r in found}


def _identity(people: dict[int, dict[str, Any]], identity_id: int) -> dict[str, Any]:
    return rows.person(
        identity_id=identity_id, display_name=(people.get(identity_id) or {}).get("display_name")
    )


async def policy_json(db, p: dict[str, Any]) -> dict[str, Any]:
    people = await _people(db, [p["approved_by_identity_id"]])
    return {
        "policy_id": str(p["policy_id"]),
        "dataset_id": str(p["dataset_id"]),
        "version": p["version"],
        "body": p["body"],
        "state": p["state"],
        "proposal_id": rows.sid_(p["proposal_id"]),
        "approved_by": _identity(people, p["approved_by_identity_id"]),
        "approved_at": rfc3339(p["approved_at"]),
        "note": p.get("note"),
    }


async def _active(db, dataset_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    return await (
        await db.execute(
            "SELECT * FROM catalog.policy WHERE dataset_id = %s AND state = 'active'"
            + (" FOR UPDATE" if lock else ""),
            (dataset_id,),
        )
    ).fetchone()


async def proposal_json(db, p: dict[str, Any]) -> dict[str, Any]:
    active = await _active(db, p["dataset_id"])
    base = None
    if p["base_policy_id"] is not None:
        base = await (
            await db.execute(
                "SELECT version FROM catalog.policy WHERE policy_id = %s", (p["base_policy_id"],)
            )
        ).fetchone()
    people = await _people(db, [p["proposed_by_identity_id"], p["decided_by_identity_id"]])
    proposer = (
        rows.person(app=p["proposed_by_app"])
        if p["proposed_by_kind"] == "application"
        else _identity(people, p["proposed_by_identity_id"])
    )
    decided = None
    if p["decided_by_identity_id"] is not None:
        decided = _identity(people, p["decided_by_identity_id"])
    return {
        "proposal_id": str(p["proposal_id"]),
        "dataset_id": str(p["dataset_id"]),
        "base_version": base["version"] if base else None,
        "stale": p["state"] == "pending"
        and (
            p["base_policy_id"] != (active["policy_id"] if active else None)
            or p.get("stale_reason") is not None
        ),
        "stale_reason": p.get("stale_reason") if p["state"] == "pending" else None,
        "body": p["body"],
        "rationale": p["rationale"],
        "proposed_by": proposer,
        "state": p["state"],
        "decided_by": decided,
        "decided_at": rfc3339(p["decided_at"]),
        "decision_note": p["decision_note"],
        "created_at": rfc3339(p["created_at"]),
        "share_overrides": list(p.get("share_overrides") or []),
        "api_client": await api_clients.ref_json(
            db, p.get("api_client_id"), p.get("owner_identity_id")
        ),
    }


def diff(active: Mapping[str, Any] | None, proposed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Field by field, `notify.*` one level down."""

    def flat(b: Mapping[str, Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in (b or {}).items():
            if isinstance(v, Mapping):
                for k2, v2 in v.items():
                    out[f"{k}.{k2}"] = v2
            else:
                out[k] = v
        return out

    a, p = flat(active), flat(proposed)
    return [
        {"field": k, "active": a.get(k), "proposed": p.get(k)}
        for k in sorted(set(a) | set(p))
        if a.get(k) != p.get(k)
    ]


# --- datasets and policies ------------------------------------------------------------


async def list_datasets(ctx: Ctx) -> WebResult:
    who = ctx.who
    if not who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")
    limit = ctx.limit()
    after = ctx.cursor("datasets")
    async with ctx.svc.catalog.read() as db:
        found = await (
            await db.execute(
                "SELECT d.*, b.kind AS backend_kind, b.private_network,"
                " (SELECT count(*) FROM catalog.policy_proposal q"
                "   WHERE q.dataset_id = d.dataset_id AND q.state = 'pending') AS pending"
                "  FROM catalog.dataset d JOIN catalog.backend b ON b.backend_id = d.backend_id"
                " WHERE d.dataset_id > %s ORDER BY d.dataset_id LIMIT %s",
                (int(after[0]) if after else 0, limit + 1),
            )
        ).fetchall()
        more = len(found) > limit
        found = list(found[:limit])
        items = []
        for d in found:
            shares = await (
                await db.execute(
                    "SELECT share_id, name, worm_enabled FROM catalog.share"
                    " WHERE dataset_id = %s ORDER BY share_id",
                    (d["dataset_id"],),
                )
            ).fetchall()
            active = await _active(db, d["dataset_id"])
            summary = None
            if active is not None:
                body = active["body"] or {}
                summary = {
                    "version": active["version"],
                    "mode": body.get("mode", "scheduled") if "archive_day" in body else "m1",
                    "archive_day": body.get("archive_day"),
                }
            items.append({
                "dataset_id": str(d["dataset_id"]),
                "name": d["name"],
                "backend_id": str(d["backend_id"]),
                "backend_kind": d["backend_kind"],
                "edge_id": None if d["edge_id"] is None else str(d["edge_id"]),
                "key_prefix": d["key_prefix"],
                "private_network": bool(d["private_network"]),
                "storage_layout": d["storage_layout"],
                "migration_state": d["migration_state"],
                "shares": [{"share_id": str(s["share_id"]), "name": s["name"],
                            "worm_enabled": bool(s["worm_enabled"])} for s in shares],
                "active_policy": summary,
                "pending_proposals": d["pending"],
            })  # fmt: skip
    nxt = ctx.next_cursor("datasets", [found[-1]["dataset_id"]]) if more else None
    return WebResult(200, page(items, nxt))


async def _dataset_exists(db, dataset_id: int) -> None:
    row = await (
        await db.execute("SELECT 1 FROM catalog.dataset WHERE dataset_id = %s", (dataset_id,))
    ).fetchone()
    if row is None:
        raise not_found("dataset")


async def get_active_policy(ctx: Ctx) -> WebResult:
    ds = ctx.path_id("dataset_id")
    async with ctx.svc.catalog.read() as db:
        await _dataset_exists(db, ds)
        await _can_read_policies(db, ctx.who, ds)
        p = await _active(db, ds)
        if p is None:
            raise not_found("active policy")
        return WebResult(200, await policy_json(db, p))


async def list_policies(ctx: Ctx) -> WebResult:
    ds = ctx.path_id("dataset_id")
    limit = ctx.limit()
    after = ctx.cursor("policies")
    async with ctx.svc.catalog.read() as db:
        await _dataset_exists(db, ds)
        await _can_read_policies(db, ctx.who, ds)
        found = await (
            await db.execute(
                "SELECT * FROM catalog.policy WHERE dataset_id = %s"
                " AND (%s::int IS NULL OR version < %s::int) ORDER BY version DESC LIMIT %s",
                (ds, after[0] if after else None, after[0] if after else None, limit + 1),
            )
        ).fetchall()
        more = len(found) > limit
        found = list(found[:limit])
        items = [await policy_json(db, p) for p in found]
    nxt = ctx.next_cursor("policies", [found[-1]["version"]]) if more else None
    return WebResult(200, page(items, nxt))


def _body(b: dict[str, Any]) -> dict[str, Any]:
    body = b.get("body")
    if not isinstance(body, dict):
        raise invalid("body", "must be a policy body (an object)")
    return body


def _note(b: dict[str, Any], field: str = "note", *, required: bool = False) -> str | None:
    v = b.get(field)
    if v is None:
        if required:
            raise invalid(field, "is required")
        return None
    if not isinstance(v, str) or len(v) > 4000 or (required and not v):
        raise invalid(field, "must be text of at most 4000 characters")
    return v


def parse_share_overrides(overrides: Any) -> list[dict[str, Any]] | None:
    """`share_overrides` of a preview or a proposal ("what users see")."""
    if overrides is None:
        return None
    if (
        not isinstance(overrides, list)
        or len(overrides) > 64
        or not all(isinstance(o, dict) and "share_id" in o for o in overrides)
    ):
        raise invalid("share_overrides", "up to 64 objects with share_id")
    for i, o in enumerate(overrides):
        parse_id(o["share_id"], f"share_overrides[{i}].share_id")
        if set(o) - {"share_id", "on_open", "wait_threshold_minutes"}:
            raise invalid(f"share_overrides[{i}]", "takes on_open and wait_threshold_minutes")
        if "on_open" in o and o["on_open"] not in ("wait", "fail", "auto"):
            raise invalid(f"share_overrides[{i}].on_open", "must be wait, fail or auto")
        t = o.get("wait_threshold_minutes")
        if t is not None and (isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 1440):
            raise invalid(f"share_overrides[{i}].wait_threshold_minutes", "1 to 1440, or null")
    return overrides


async def _check_override_shares(db, dataset_id: int, overrides: list[dict[str, Any]]) -> None:
    ids = {int(o["share_id"]) for o in overrides}
    found = await (
        await db.execute(
            "SELECT share_id FROM catalog.share WHERE dataset_id = %s AND share_id = ANY(%s)",
            (dataset_id, list(ids)),
        )
    ).fetchall()
    if len(found) != len(ids):
        raise invalid("share_overrides", "every share must belong to the dataset")


async def _apply_share_overrides(
    db, who: Principal, dataset_id: int, overrides: list[dict[str, Any]], proposal_id: int
) -> None:
    """On approval only (anti-pattern 7): the proposal's open policy per share."""
    for o in overrides:
        s = await (
            await db.execute(
                "SELECT * FROM catalog.share WHERE share_id = %s AND dataset_id = %s FOR UPDATE",
                (int(o["share_id"]), dataset_id),
            )
        ).fetchone()
        if s is None:
            raise invalid("share_overrides", "a share of this proposal no longer exists")
        on_open = o.get("on_open", s["on_open"])
        if "wait_threshold_minutes" in o:
            threshold = o["wait_threshold_minutes"]
        else:
            threshold = s["wait_threshold_minutes"] if on_open == "auto" else None
        if on_open != "auto":
            threshold = None
        if on_open == "auto" and threshold is None:
            raise invalid("share_overrides", f"share {s['name']}: auto needs a threshold")
        await db.execute(
            "UPDATE catalog.share SET on_open = %s, wait_threshold_minutes = %s"
            " WHERE share_id = %s",
            (on_open, threshold, s["share_id"]),
        )
        await audit(db, who, "share.settings", dataset_id=dataset_id, share_id=s["share_id"],
                    details={"on_open": on_open, "wait_threshold_minutes": threshold,
                             "proposal_id": str(proposal_id)})  # fmt: skip


async def _preview(ctx: Ctx, db, dataset_id: int, body: Mapping[str, Any], **kw):
    facts, _ = await dataset_facts(db, dataset_id, **kw)
    return ctx.svc.preview.policy_preview(body, facts)


async def _require_valid(ctx: Ctx, db, dataset_id: int, body: Mapping[str, Any]) -> None:
    result = await _preview(ctx, db, dataset_id, body)
    errors = [c for c in result.get("checks", []) if c.get("severity") == "error"]
    if errors or not result.get("valid", False):
        raise WebError(
            "policy_invalid", "the policy has errors; see checks", checks=result.get("checks", [])
        )


async def _activate(
    ctx: Ctx, db, dataset_id: int, body: Mapping[str, Any], approver: int, *,
    note: str | None, proposal_id: int | None,
) -> dict[str, Any]:  # fmt: skip
    """Supersede the active version and insert the new one, then re-plan."""
    await db.execute(
        "UPDATE catalog.policy SET state = 'superseded' WHERE dataset_id = %s AND state = 'active'",
        (dataset_id,),
    )
    import json

    p = await (
        await db.execute(
            "INSERT INTO catalog.policy (dataset_id, version, body, state, proposal_id,"
            " approved_by_identity_id, approved_at, note)"
            " SELECT %s, coalesce(max(version), 0) + 1, %s::jsonb, 'active', %s, %s, %s, %s"
            "   FROM catalog.policy WHERE dataset_id = %s RETURNING *",
            (dataset_id, json.dumps(body), proposal_id, approver, ctx.now, note, dataset_id),
        )
    ).fetchone()
    await reschedule_dataset(db, dataset_id, ctx.now)
    return p


async def create_policy(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"body", "base_version", "note"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a policy field")
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    me = _require_identity(who)
    body = _body(b)
    if "base_version" not in b:
        raise invalid("base_version", "is required (null when there is no active policy)")
    base = b["base_version"]
    if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
        raise invalid("base_version", "must be an integer or null")
    note = _note(b)
    ds = ctx.path_id("dataset_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        lock = await (
            await db.execute(
                "SELECT dataset_id FROM catalog.dataset WHERE dataset_id = %s FOR UPDATE", (ds,)
            )
        ).fetchone()
        if lock is None:
            raise not_found("dataset")
        active = await _active(db, ds, lock=True)
        if (active["version"] if active else None) != base:
            raise WebError("version_conflict", "the active policy changed; reload and edit again")
        await _require_valid(ctx, db, ds, body)
        p = await _activate(ctx, db, ds, body, me, note=note, proposal_id=None)
        await audit(db, who, "policy.create", dataset_id=ds,
                    details={"version": p["version"], "base_version": base})  # fmt: skip
        out = await policy_json(db, p)
    return WebResult(201, out)


async def preview_policy(ctx: Ctx) -> WebResult:
    who = ctx.who
    if who.is_app:
        if not who.has("Policy.Propose"):
            raise role_required("Policy.Propose")
    elif not who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")
    b = ctx.body
    extra = set(b) - {"dataset_id", "body", "share_overrides", "assumptions"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a preview field")
    if "dataset_id" not in b:
        raise invalid("dataset_id", "is required")
    ds = parse_id(b["dataset_id"], "dataset_id")
    body = _body(b)
    overrides = parse_share_overrides(b.get("share_overrides"))
    assumptions = b.get("assumptions")
    if assumptions is not None and not isinstance(assumptions, dict):
        raise invalid("assumptions", "must be an object")
    async with ctx.svc.catalog.read() as db:
        result = await _preview(
            ctx, db, ds, body, assumptions=assumptions, share_overrides=overrides
        )
    return WebResult(200, result)


# --- proposals ----------------------------------------------------------------------


def _proposal_reader(who: Principal) -> None:
    if who.is_app:
        if not who.has("Policy.Propose"):
            raise role_required("Policy.Propose")
    elif not who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")


async def _load_proposal(db, who: Principal, pid: int, *, lock: bool = False):
    p = await (
        await db.execute(
            "SELECT * FROM catalog.policy_proposal WHERE proposal_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (pid,),
        )
    ).fetchone()
    if p is None or (who.is_app and p["proposed_by_app"] != who.app_id):
        raise not_found("proposal")
    return p


async def submit_proposal(ctx: Ctx) -> WebResult:
    who = ctx.who
    _proposal_reader(who)
    b = ctx.body
    extra = set(b) - {"dataset_id", "body", "rationale", "base_version", "share_overrides"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a proposal field")
    if "dataset_id" not in b:
        raise invalid("dataset_id", "is required")
    ds = parse_id(b["dataset_id"], "dataset_id")
    body = _body(b)
    rationale = _note(b, "rationale", required=True)
    identity = None if who.is_app else _require_identity(who)
    client = who.api_client if who.is_app else None
    import json

    async with ctx.svc.catalog.tx(who.actor) as db:
        await _dataset_exists(db, ds)
        # An API client's owner must see the dataset (portal-api.md 5.21).
        await require_owner_access(db, who, ds, ctx.now)
        overrides = parse_share_overrides(b.get("share_overrides")) or []
        await _check_override_shares(db, ds, overrides)
        if "base_version" in b and b["base_version"] is not None:
            v = b["base_version"]
            if isinstance(v, bool) or not isinstance(v, int):
                raise invalid("base_version", "must be an integer or null")
            base = await (
                await db.execute(
                    "SELECT policy_id FROM catalog.policy WHERE dataset_id = %s AND version = %s",
                    (ds, v),
                )
            ).fetchone()
            if base is None:
                raise WebError("version_conflict", f"the dataset has no policy version {v}")
            base_id = base["policy_id"]
        elif "base_version" in b:
            base_id = None
        else:
            active = await _active(db, ds)
            base_id = active["policy_id"] if active else None
        p = await (
            await db.execute(
                "INSERT INTO catalog.policy_proposal (dataset_id, base_policy_id, body, rationale,"
                " proposed_by_kind, proposed_by_identity_id, proposed_by_app, created_at,"
                " share_overrides, api_client_id, owner_identity_id, key_id)"
                " VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)"
                " RETURNING *",
                (
                    ds,
                    base_id,
                    json.dumps(body),
                    rationale,
                    "application" if who.is_app else "identity",
                    identity,
                    who.app_id,
                    ctx.now,
                    json.dumps(overrides),
                    client["api_client_id"] if client else None,
                    client["owner_identity_id"] if client else None,
                    who.key_id if client else None,
                ),
            )  # fmt: skip
        ).fetchone()
        await audit(db, who, "policy.propose", dataset_id=ds,
                    details={"proposal_id": str(p["proposal_id"])})  # fmt: skip
        out = await proposal_json(db, p)
    return WebResult(201, out)


async def list_proposals(ctx: Ctx) -> WebResult:
    who = ctx.who
    _proposal_reader(who)
    ds = ctx.query_id("dataset_id")
    state = ctx.query("state")
    if state is not None and state not in PROPOSAL_STATES:
        raise invalid("state", "is not a proposal state")
    limit = ctx.limit()
    after = ctx.cursor("proposals")
    async with ctx.svc.catalog.read() as db:
        found = await (
            await db.execute(
                "SELECT * FROM catalog.policy_proposal"
                " WHERE (%(ds)s::bigint IS NULL OR dataset_id = %(ds)s::bigint)"
                "   AND (%(state)s::text IS NULL OR state = %(state)s::text)"
                "   AND (%(app)s::text IS NULL OR proposed_by_app = %(app)s::text)"
                "   AND (%(after)s::bigint IS NULL OR proposal_id < %(after)s::bigint)"
                " ORDER BY proposal_id DESC LIMIT %(limit)s",
                {
                    "ds": ds,
                    "state": state,
                    "app": who.app_id if who.is_app else None,
                    "after": int(after[0]) if after else None,
                    "limit": limit + 1,
                },
            )  # fmt: skip
        ).fetchall()
        more = len(found) > limit
        found = list(found[:limit])
        items = [await proposal_json(db, p) for p in found]
    nxt = ctx.next_cursor("proposals", [found[-1]["proposal_id"]]) if more else None
    return WebResult(200, page(items, nxt))


async def get_proposal(ctx: Ctx) -> WebResult:
    who = ctx.who
    _proposal_reader(who)
    async with ctx.svc.catalog.read() as db:
        p = await _load_proposal(db, who, ctx.path_id("proposal_id"))
        out = await proposal_json(db, p)
        active = await _active(db, p["dataset_id"])
        out["diff"] = diff(active["body"] if active else None, p["body"])
        try:
            out["preview"] = await _preview(
                ctx, db, p["dataset_id"], p["body"],
                share_overrides=list(p["share_overrides"] or []) or None,
            )  # fmt: skip
        except WebError as e:
            if e.code != "unavailable":
                raise
    return WebResult(200, out)


def _pending(p: dict[str, Any]) -> None:
    if p["state"] != "pending":
        raise WebError("proposal_not_pending", f"the proposal is {p['state']}")


async def approve_proposal(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"note", "accept_stale"}
    if extra:
        raise invalid(sorted(extra)[0], "is not an approval field")
    note = _note(b)
    accept_stale = b.get("accept_stale", False)
    if not isinstance(accept_stale, bool):
        raise invalid("accept_stale", "must be a boolean")
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    me = _require_identity(who)
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load_proposal(db, who, ctx.path_id("proposal_id"), lock=True)
        _pending(p)
        if p["proposed_by_identity_id"] == me:
            raise WebError("self_approval", "a proposal can't be approved by its proposer")
        # An API client's proposal (portal-api.md 5.21): nobody answerable for the client
        # approves it, and the client and its owner must still be in good standing.
        await api_clients.check_approvable(db, p, me, ctx.now)
        await db.execute(
            "SELECT dataset_id FROM catalog.dataset WHERE dataset_id = %s FOR UPDATE",
            (p["dataset_id"],),
        )
        active = await _active(db, p["dataset_id"], lock=True)
        if p["base_policy_id"] != (active["policy_id"] if active else None) and not accept_stale:
            raise WebError("version_conflict", "the active policy changed since this proposal;"
                           " review it and approve with accept_stale")  # fmt: skip
        await _require_valid(ctx, db, p["dataset_id"], p["body"])
        p = await (
            await db.execute(
                "UPDATE catalog.policy_proposal SET state = 'approved',"
                " decided_by_identity_id = %s, decided_at = %s, decision_note = %s"
                " WHERE proposal_id = %s RETURNING *",
                (me, ctx.now, note, p["proposal_id"]),
            )
        ).fetchone()
        pol = await _activate(ctx, db, p["dataset_id"], p["body"], me, note=note,
                              proposal_id=p["proposal_id"])  # fmt: skip
        await _apply_share_overrides(db, who, p["dataset_id"], list(p["share_overrides"] or []),
                                     p["proposal_id"])  # fmt: skip
        await audit(db, who, "policy.approve", dataset_id=p["dataset_id"],
                    details={"proposal_id": str(p["proposal_id"]), "version": pol["version"],
                             "stale_accepted": accept_stale})  # fmt: skip
        out = {"proposal": await proposal_json(db, p), "policy": await policy_json(db, pol)}
    return WebResult(200, out)


async def reject_proposal(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"note"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a rejection field")
    note = _note(b, required=True)
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    me = _require_identity(who)
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load_proposal(db, who, ctx.path_id("proposal_id"), lock=True)
        _pending(p)
        if p["proposed_by_identity_id"] == me:
            raise WebError("self_approval", "withdraw your own proposal instead")
        p = await (
            await db.execute(
                "UPDATE catalog.policy_proposal SET state = 'rejected',"
                " decided_by_identity_id = %s, decided_at = %s, decision_note = %s"
                " WHERE proposal_id = %s RETURNING *",
                (me, ctx.now, note, p["proposal_id"]),
            )
        ).fetchone()
        await audit(db, who, "policy.reject", dataset_id=p["dataset_id"],
                    details={"proposal_id": str(p["proposal_id"])})  # fmt: skip
        out = await proposal_json(db, p)
    return WebResult(200, out)


async def withdraw_proposal(ctx: Ctx) -> WebResult:
    who = ctx.who
    _proposal_reader(who)
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await _load_proposal(db, who, ctx.path_id("proposal_id"), lock=True)
        mine = (
            p["proposed_by_app"] == who.app_id
            if who.is_app
            else who.identity_id is not None and p["proposed_by_identity_id"] == who.identity_id
        )
        if not mine:
            raise forbidden("only the proposer can withdraw a proposal")
        _pending(p)
        p = await (
            await db.execute(
                "UPDATE catalog.policy_proposal SET state = 'withdrawn' WHERE proposal_id = %s"
                " RETURNING *",
                (p["proposal_id"],),
            )
        ).fetchone()
        await audit(db, who, "policy.withdraw", dataset_id=p["dataset_id"],
                    details={"proposal_id": str(p["proposal_id"])})  # fmt: skip
        out = await proposal_json(db, p)
    return WebResult(200, out)
