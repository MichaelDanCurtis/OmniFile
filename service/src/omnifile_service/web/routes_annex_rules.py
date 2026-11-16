"""Annex rule sets and dry runs in the portal and the partner API
(portal-api.md section 5.23; annex.md sections 4.3 and 4.5; phase A2.1).

- `GET /v1/annexes/{annex_id}/rulesets`, `GET …/rulesets/{version}`:
  versions, with the running one, proposals, and each proposal's dry run.
- `POST /v1/annexes/{annex_id}/rulesets` `{body, rationale?, owner?}`: a
  proposal. It never applies anything. It names a person who owns it (an
  app's must name one), is checked (`annex/rules.py`), and gets a dry run.
- `POST …/rulesets/{version}/approve` and `…/reject`: a person decides.
  - An ArchiveAdmin approves every proposal that removes anything; the
    annex's Owners may approve the others. A removing set that adds or
    widens a removal, or changes any arrived, changed or age rule (an age
    rule removes what the arrival rules copied), starts a fresh dry run.
    Both are computed again at approval, against the running set then.
  - Nobody approves their own: not the person a proposal names, nor anyone
    answerable for the app it came from.
  - At approval the named person's access, and an app's client and its
    owner, are checked again.
  - A rule set that adds or widens a removal runs in dry run first
    (`dry_run_until`, decision 5), and A2.1 removes nothing in any case.
- `POST /v1/annexes/{annex_id}/dry-runs` `{body}` and `GET …/dry-runs/{run_id}`:
  a preview the edge computes over its listing (the rule builder's counts).
  A newer draft replaces the person's older ones; runs the edge doesn't
  take or answer in time fail (`annex/dry_runs.py`).

The edge's listing is unfiltered, so counts can tell what is on the NAS
(access-control.md section 5), and an annex's folders don't carry the NAS's
ACLs yet (they get the Owners' ACL): previews are ArchiveAdmins' and apps'.
Anyone else gets `403 role_required` for a preview and `report_hidden` for a
report. Capturing the NAS folder ACLs is what would reopen them to Owners.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from ..annex import dry_runs
from ..annex import rules as annex_rules
from ..api.validate import rfc3339
from ..apiclients import registry as api_clients
from ..apiclients.access import owner_can_see_dataset, require_owner_access
from .context import Ctx, Principal, WebResult, audit, etag, if_match, page
from .problems import WebError, forbidden, invalid, not_found, role_required

PROPOSE_FIELDS = {"body", "rationale", "owner"}


class _AppLike:
    """At approval, a share target is checked for the person the proposal
    names, as an app's named owner is: by their SID and groups."""

    is_app = True


DECIDE_FIELDS = {"note"}


def _routes():
    from . import routes_annex

    return routes_annex


def _sees_paths(who: Principal) -> bool:
    return who.is_app or who.has("ArchiveAdmin")


async def may_preview(db, who: Principal, a: dict[str, Any], body: dict[str, Any]) -> bool:
    """Whether `who` may see counts for `body` over the annex's listing: only
    ArchiveAdmins and apps, until an annex's folders carry the NAS's ACLs
    (today they carry the Owners' ACL, so no folder check can stand in)."""
    return _sees_paths(who)


def report_json(report: dict[str, Any] | None, who: Principal) -> dict[str, Any] | None:
    return report if _sees_paths(who) else None


def dry_run_json(r: dict[str, Any], who: Principal, *, visible: bool = True) -> dict[str, Any]:
    return {
        "run_id": str(r["run_id"]),
        "annex_id": str(r["annex_id"]),
        "ruleset_version": r["ruleset_version"],
        "state": r["state"],
        "requested_at": rfc3339(r["requested_at"]),
        "done_at": rfc3339(r["done_at"]),
        "report": report_json(r["report"], who) if visible else None,
        "report_hidden": not visible and r["report"] is not None,
        "error": r["error"],
    }


async def visible_run_json(db, who: Principal, a: dict[str, Any], r: dict[str, Any]):
    return dry_run_json(r, who, visible=await may_preview(db, who, a, r["body"]))


def ruleset_json(r: dict[str, Any], *, body: bool = False) -> dict[str, Any]:
    out = {
        "version": r["version"],
        "state": r["state"],
        "mode": annex_rules.mode_of(r["body"]),
        "removes": r["removes"],
        "needs_archive_admin": r["needs_archive_admin"],
        # A3.4 (decision 85): the approval page marks these "replaces files on the NAS".
        "replaces_on_nas": annex_rules.replaces_on_nas(r["body"]),
        "owner_identity_id": str(r["owner_identity_id"]),
        "owner_name": r.get("owner_name"),
        "proposed_via": r["proposed_via"],
        "proposed_by_app": r["proposed_by_app"],
        "rationale": r["rationale"],
        "created_at": rfc3339(r["created_at"]),
        "decided_at": rfc3339(r["decided_at"]),
        "decided_by_identity_id": None
        if r["decided_by_identity_id"] is None
        else str(r["decided_by_identity_id"]),
        "decision_note": r["decision_note"],
        "dry_run_id": None if r["dry_run_id"] is None else str(r["dry_run_id"]),
    }
    if body:
        out["body"] = r["body"]
    return out


RULESET_SQL = (
    "SELECT r.*, i.display_name AS owner_name FROM catalog.annex_ruleset r"
    " LEFT JOIN catalog.identity i ON i.identity_id = r.owner_identity_id"
)


async def _running(db, annex_id: int) -> dict[str, Any] | None:
    return await (
        await db.execute(
            "SELECT * FROM catalog.annex_ruleset WHERE annex_id = %s AND state = 'active'",
            (annex_id,),
        )
    ).fetchone()


def _checked(b: Any, a: dict[str, Any]) -> dict[str, Any]:
    """The body, normalized, passing every check; else 400 / 422."""
    try:
        body = annex_rules.normalize(b, a["dataset_id"])
    except annex_rules.RulesError as e:
        raise invalid(e.field, str(e)) from None
    problems = annex_rules.checks(body, quiet_minutes=a["quiet_minutes"])
    if problems:
        raise WebError("rules_check_failed", "the rule set can't be saved as it is",
                       rule_checks=problems)  # fmt: skip
    return body


def _within(path: str, root: str) -> str | None:
    """`path` relative to `root` (both relative to an export), or None."""
    if root == "":
        return path
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return None


async def location_checks(
    db, a: dict[str, Any], body: dict[str, Any], sids: list[str] | None
) -> list[dict[str, str]]:
    """Section 4.2's checks of the return rules' locations against the
    catalog: each names an annex, filer or share that exists, on this
    annex's edge (a NAS folder: the edge that mounts it places the file), a
    share its proposer can write to (`sids`: the proposer's, None for an
    ArchiveAdmin), and a folder in another annex that its own rules wouldn't
    empty again at once. A filer folder inside an annex is that annex's."""
    out: list[dict[str, str]] = []

    def bad(rule: str, check: str, message: str) -> None:
        out.append({"rule": rule, "check": check, "message": message})

    for r in annex_rules.returns(body["rules"]):
        for loc in annex_rules.targets(r):
            if loc["kind"] == "share_folder":
                s = await (
                    await db.execute(
                        "SELECT share_id, owners_group_sid, writers_group_sid FROM catalog.share"
                        " WHERE share_id = %s",
                        (int(loc["share_id"]),),
                    )
                ).fetchone()
                if s is None:
                    bad(r["id"], "unknown_location", f"no share {loc['share_id']}")
                elif sids is not None and not (
                    {s["owners_group_sid"], s["writers_group_sid"]} & set(sids)
                ):
                    bad(r["id"], "location_unreachable",
                        f"share {loc['share_id']}: its proposer can't write to it")  # fmt: skip
                continue
            if annex_rules.in_this_annex(loc):
                continue  # annex/rules.py checks those
            if "annex_id" in loc:
                other = await (
                    await db.execute(
                        "SELECT annex_id, filer_id, edge_id, path, state FROM catalog.annex"
                        " WHERE annex_id = %s",
                        (int(loc["annex_id"]),),
                    )
                ).fetchone()
                if other is None or other["state"] == "retired":
                    bad(r["id"], "unknown_location", f"no annex {loc['annex_id']}")
                    continue
                inside = [(other, loc["path"])]
            else:
                f = await (
                    await db.execute(
                        "SELECT filer_id, edge_id FROM catalog.filer WHERE filer_id = %s",
                        (int(loc["filer_id"]),),
                    )
                ).fetchone()
                if f is None:
                    bad(r["id"], "unknown_location", f"no filer {loc['filer_id']}")
                    continue
                annexes = await (
                    await db.execute(
                        "SELECT annex_id, filer_id, edge_id, path, state FROM catalog.annex"
                        " WHERE filer_id = %s AND state <> 'retired'",
                        (f["filer_id"],),
                    )
                ).fetchall()
                if not annexes:
                    bad(r["id"], "location_unreachable",
                        f"filer {loc['filer_id']} has no annex, so no edge mounts it")  # fmt: skip
                    continue
                other = annexes[0]
                inside = [(x, rel) for x in annexes
                          if (rel := _within(loc["path"], x["path"])) is not None]  # fmt: skip
            if other["edge_id"] != a["edge_id"]:
                bad(r["id"], "location_unreachable",
                    "a NAS folder on another edge: the annex's edge places its files")  # fmt: skip
                continue
            for x, rel in inside:
                if x["annex_id"] == a["annex_id"]:
                    rules = body["rules"]
                else:
                    running = await _running(db, x["annex_id"])
                    rules = running["body"]["rules"] if running else []
                for check, msg in annex_rules.target_problems(r, rel, rules):
                    bad(r["id"], check, f"in annex {x['annex_id']}: {msg}")
    return out


async def proposer_sids(db, who: Principal, owner_id: int) -> list[str] | None:
    """The SIDs a share's write check uses for a proposal: the person's own
    (an app's named owner), or None for an ArchiveAdmin in the portal."""
    if not who.is_app:
        return None if who.has("ArchiveAdmin") else list(who.all_sids)
    row = await (
        await db.execute(
            "SELECT sid::text AS sid, group_sids::text[] AS groups FROM catalog.identity"
            " WHERE identity_id = %s",
            (owner_id,),
        )
    ).fetchone()
    if row is None:
        return []
    return [x for x in [row["sid"], *(row["groups"] or [])] if x]


async def require_locations(db, who, a, body, owner_id: int) -> None:
    problems = await location_checks(db, a, body, await proposer_sids(db, who, owner_id))
    if problems:
        raise WebError("rules_check_failed", "the rule set can't be saved as it is",
                       rule_checks=problems)  # fmt: skip


async def new_dry_run(
    db, who: Principal, annex_id: int, body: dict[str, Any], version: int | None
) -> dict[str, Any]:
    return await (
        await db.execute(
            "INSERT INTO catalog.annex_dry_run (annex_id, ruleset_version, body, requested_by)"
            " VALUES (%s, %s, %s, %s) RETURNING *",
            (annex_id, version, Jsonb(body), who.actor),
        )
    ).fetchone()


async def insert_proposal(
    db,
    who: Principal,
    a: dict[str, Any],
    body: dict[str, Any],
    owner_id: int,
    *,
    rationale: str | None,
    version: int | None = None,
) -> dict[str, Any]:
    """A proposed rule set and its dry run."""
    running = await _running(db, a["annex_id"])
    if version is None:
        nxt = await (
            await db.execute(
                "SELECT coalesce(max(version), 0) + 1 AS v FROM catalog.annex_ruleset"
                " WHERE annex_id = %s",
                (a["annex_id"],),
            )
        ).fetchone()
        version = nxt["v"]
    client = (who.api_client or {}) if who.is_app else {}
    rs = await (
        await db.execute(
            "INSERT INTO catalog.annex_ruleset (annex_id, version, body, state, owner_identity_id,"
            " proposed_via, proposed_by_app, api_client_id, key_id, rationale, removes,"
            " needs_archive_admin)"
            " VALUES (%s, %s, %s, 'proposed', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (
                a["annex_id"],
                version,
                Jsonb(body),
                owner_id,
                "api" if who.is_app else "portal",
                who.actor if who.is_app else None,
                client.get("api_client_id"),
                who.key_id if who.is_app else None,
                rationale,
                annex_rules.removes(body),
                annex_rules.needs_archive_admin(body, running["body"] if running else None),
            ),
        )  # fmt: skip
    ).fetchone()
    # The annex's ETag moves with every proposal, so an approval made from a
    # page that hasn't seen it is refused (If-Match).
    await db.execute("UPDATE catalog.annex SET state = state WHERE annex_id = %s", (a["annex_id"],))
    run = None
    await dry_runs.expire(db, a["annex_id"])
    if await dry_runs.waiting(db, a["annex_id"]) < dry_runs.MAX_WAITING:
        # Throttled like the builder's: past it, the proposal has no run, and
        # the approver's page asks for one when it opens.
        run = await new_dry_run(db, who, a["annex_id"], body, version)
        await db.execute(
            "UPDATE catalog.annex_ruleset SET dry_run_id = %s WHERE annex_id = %s AND version = %s",
            (run["run_id"], a["annex_id"], version),
        )
    await db.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, details) VALUES (%s, 'settings', %s)",
        (a["annex_id"], Jsonb({"by": who.actor, "ruleset_proposed": version,
                                "removes": rs["removes"]})),
    )  # fmt: skip
    await audit(db, who, "annex.ruleset.propose", dataset_id=a["dataset_id"],
                details={"annex_id": str(a["annex_id"]), "version": version,
                         "removes": rs["removes"],
                         "needs_archive_admin": rs["needs_archive_admin"]})  # fmt: skip
    rs = dict(rs)
    rs["dry_run_id"] = run["run_id"] if run else None
    return rs


async def _still_has_access(
    db, owner_id: int, a: dict[str, Any], ctx: Ctx, *, via_app: bool
) -> None:
    """Section 4.3: the person a proposal names must still be an Owner of the
    annex or able to see its dataset, at approval as at proposal. For an
    app's proposal, both (as `_owner_identity` asked when it was made)."""
    row = await (
        await db.execute(
            "SELECT group_sids::text[] AS groups, sid::text AS sid FROM catalog.identity"
            " WHERE identity_id = %s",
            (owner_id,),
        )
    ).fetchone()
    in_owners = row is not None and (
        a["owners_sid"] in (row["groups"] or []) or a["owners_sid"] == row["sid"]
    )
    sees = await owner_can_see_dataset(db, owner_id, a["dataset_id"], ctx.now)
    ok = (in_owners and sees) if via_app else (in_owners or sees)
    if not ok:
        raise WebError("owner_lacks_access", "the person this proposal names no longer has"
                       " access to the annex, so it can't be approved")  # fmt: skip


async def decide(
    db, ctx: Ctx, a: dict[str, Any], rs: dict[str, Any], *, approve: bool, note: str | None
) -> None:
    """Approve or reject one proposal (the caller has locked the annex)."""
    who = ctx.who
    if who.is_app:
        raise forbidden("a person decides on a rule set, in the portal")
    if who.identity_id is None:
        raise forbidden("your AD identity is not known yet; sign in again")
    if rs["state"] != "proposed":
        raise WebError("version_conflict", f"rule set {rs['version']} is {rs['state']}")
    r = _routes()
    if not r._owns(who, a):
        raise forbidden("only the annex's Owners and ArchiveAdmins decide on its rule sets")
    if not approve:
        await db.execute(
            "UPDATE catalog.annex_ruleset SET state = 'rejected', decided_by_identity_id = %s,"
            " decided_at = now(), decision_note = %s WHERE annex_id = %s AND version = %s",
            (who.identity_id, note or "rejected in the portal", a["annex_id"], rs["version"]),
        )
        await audit(db, who, "annex.ruleset.reject", dataset_id=a["dataset_id"],
                    details={"annex_id": str(a["annex_id"]), "version": rs["version"]})  # fmt: skip
        return
    running = await _running(db, a["annex_id"])
    old = running["body"] if running else None
    # Computed again now: the running set may have changed since it was
    # proposed. Anti-pattern 7, and decision 5: a removal is an
    # ArchiveAdmin's call, and A2.2's gates read who approved it.
    if (
        rs["needs_archive_admin"] or annex_rules.needs_archive_admin(rs["body"], old)
    ) and not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    # Nobody approves their own proposal: not the person it names (who made
    # it, in the portal), nor anyone answerable for the app it came from.
    if rs["owner_identity_id"] == who.identity_id:
        raise WebError("self_approval", "this proposal is yours (you are the person it names),"
                       " so another Owner or an ArchiveAdmin approves it")  # fmt: skip
    if rs["proposed_via"] == "api":
        if rs["api_client_id"] is None:
            raise WebError("client_not_registered", "the app that proposed this is not registered")
        client = await api_clients.load(db, int(rs["api_client_id"]))
        await api_clients.check_approvable(
            db,
            {
                "api_client_id": rs["api_client_id"],
                "key_id": rs["key_id"],
                "owner_identity_id": client["owner_identity_id"] if client else 0,
                "dataset_id": a["dataset_id"],
            },
            who.identity_id,
            ctx.now,
        )
    via_app = rs["proposed_via"] == "api"
    await _still_has_access(db, rs["owner_identity_id"], a, ctx, via_app=via_app)
    # The checks again: the quiet period, the targets and the other annexes'
    # rules may have changed since. A share target is checked for the
    # person the proposal names.
    problems = annex_rules.checks(rs["body"], quiet_minutes=a["quiet_minutes"])
    problems += await location_checks(
        db, a, rs["body"], await proposer_sids(db, _AppLike(), rs["owner_identity_id"])
    )
    if problems:
        raise WebError("rules_check_failed", "the rule set no longer passes its checks",
                       rule_checks=problems)  # fmt: skip
    widened = annex_rules.widens(rs["body"], old)
    # A replace question in flight holds this shared (A3.4, the confirm pass of #98,
    # C9): the running rule set changes only after its answer commits.
    await db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (annex_rules.ruleset_lock_key(a["annex_id"]),),
    )
    await db.execute(
        "UPDATE catalog.annex_ruleset SET state = 'superseded' WHERE annex_id = %s"
        " AND (state = 'active' OR (state = 'proposed' AND version <> %s))",
        (a["annex_id"], rs["version"]),
    )
    await db.execute(
        "UPDATE catalog.annex_ruleset SET state = 'active', decided_by_identity_id = %s,"
        " decided_at = now(), decision_note = %s, decided_as_archive_admin = %s"
        " WHERE annex_id = %s AND version = %s",
        (
            who.identity_id,
            note or "approved in the portal",
            who.has("ArchiveAdmin"),
            a["annex_id"],
            rs["version"],
        ),  # fmt: skip
    )
    sets: dict[str, Any] = {"ruleset_version": rs["version"],
                            "mode": annex_rules.mode_of(rs["body"])}  # fmt: skip
    if widened:
        days = await (
            await db.execute("SELECT dry_run_days FROM catalog.annex_defaults")
        ).fetchone()
        sets["dry_run_until"] = ctx.now + timedelta(days=days["dry_run_days"])
    cols = ", ".join(f"{k} = %({k})s" for k in sets)
    await db.execute(
        f"UPDATE catalog.annex SET {cols} WHERE annex_id = %(id)s", {**sets, "id": a["annex_id"]}
    )
    await db.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, details) VALUES (%s, 'settings', %s)",
        (a["annex_id"], Jsonb({"by": who.actor, "ruleset_approved": rs["version"],
                                "dry_run_until": rfc3339(sets.get("dry_run_until"))})),
    )  # fmt: skip
    await audit(db, who, "annex.ruleset.approve", dataset_id=a["dataset_id"],
                details={"annex_id": str(a["annex_id"]), "version": rs["version"],
                         "removes": rs["removes"], "proposed_by_app": rs["proposed_by_app"],
                         "dry_run_until": rfc3339(sets.get("dry_run_until"))})  # fmt: skip


# --- routes ---------------------------------------------------------------------------------


async def list_rulesets(ctx: Ctx) -> WebResult:
    r = _routes()
    async with ctx.svc.catalog.read() as db:
        a = await r._annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        rows = await (
            await db.execute(
                RULESET_SQL + " WHERE r.annex_id = %s ORDER BY r.version DESC", (a["annex_id"],)
            )  # fmt: skip
        ).fetchall()
    return WebResult(200, page([ruleset_json(x) for x in rows], None))


async def get_ruleset(ctx: Ctx) -> WebResult:
    r = _routes()
    version = ctx.path_id("version")
    async with ctx.svc.catalog.read() as db:
        a = await r._annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        rs = await (
            await db.execute(
                RULESET_SQL + " WHERE r.annex_id = %s AND r.version = %s", (a["annex_id"], version)
            )  # fmt: skip
        ).fetchone()
        if rs is None:
            raise not_found("rule set")
        run = None
        if rs["dry_run_id"] is not None:
            run = await (
                await db.execute(
                    "SELECT * FROM catalog.annex_dry_run WHERE run_id = %s", (rs["dry_run_id"],)
                )
            ).fetchone()
        run_json = await visible_run_json(db, ctx.who, a, run) if run else None
        running = await _running(db, a["annex_id"])
    out = ruleset_json(rs, body=True)
    out["checks"] = annex_rules.checks(rs["body"], quiet_minutes=a["quiet_minutes"])
    out["dry_run"] = run_json
    # What approving it would change: the running set's body, to compare.
    out["running_version"] = running["version"] if running else None
    out["running_body"] = running["body"] if running and running["version"] != version else None
    return WebResult(200, out)


async def propose_ruleset(ctx: Ctx) -> WebResult:
    r = _routes()
    who, b = ctx.who, ctx.body
    extra = set(b) - PROPOSE_FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a proposal field")
    rationale = b.get("rationale")
    if rationale is not None and (not isinstance(rationale, str) or len(rationale) > 2000):
        raise invalid("rationale", "up to 2000 characters")
    if who.is_app and not who.has("Archive.Adopt"):
        raise role_required("Archive.Adopt")
    async with ctx.svc.catalog.tx(who.actor) as db:
        a = await r._annex(db, ctx.path_id("annex_id"), who, lock=True)
        if not r._owns(who, a):
            raise forbidden("only the annex's Owners and ArchiveAdmins propose its rules")
        if a["state"] == "retired":
            raise WebError("version_conflict", "a retired annex has no rules to change")
        body = _checked(b.get("body"), a)
        owner_id = await r._owner_identity(db, who, b, a["owners_sid"], a["dataset_id"], ctx.now)
        await require_locations(db, who, a, body, owner_id)
        rs = await insert_proposal(db, who, a, body, owner_id, rationale=rationale)
        rs = await (
            await db.execute(
                RULESET_SQL + " WHERE r.annex_id = %s AND r.version = %s",
                (a["annex_id"], rs["version"]),
            )  # fmt: skip
        ).fetchone()
    return WebResult(201, ruleset_json(rs, body=True))


async def _decide_route(ctx: Ctx, approve: bool) -> WebResult:
    r = _routes()
    b = ctx.body or {}
    extra = set(b) - DECIDE_FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a decision field")
    note = b.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise invalid("note", "up to 2000 characters")
    version = ctx.path_id("version")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a = await r._annex(db, ctx.path_id("annex_id"), ctx.who, lock=True)
        if_match(ctx.request, a["entity_version"])
        rs = await (
            await db.execute(
                "SELECT * FROM catalog.annex_ruleset WHERE annex_id = %s AND version = %s"
                " FOR UPDATE",
                (a["annex_id"], version),
            )
        ).fetchone()
        if rs is None:
            raise not_found("rule set")
        await decide(db, ctx, a, rs, approve=approve, note=note)
        a = await r._annex(db, a["annex_id"], r._AllSeeing)
    return WebResult(200, r.annex_json(a), headers={"ETag": etag(a["entity_version"])})


async def approve_version(ctx: Ctx) -> WebResult:
    return await _decide_route(ctx, True)


async def reject_version(ctx: Ctx) -> WebResult:
    return await _decide_route(ctx, False)


async def create_dry_run(ctx: Ctx) -> WebResult:
    """A preview of a draft (the builder's live counts) or of the running
    rule set. Checks are returned with it; a draft that fails them can still
    be previewed."""
    r = _routes()
    b = ctx.body
    if set(b) - {"body"}:
        raise invalid(sorted(set(b) - {"body"})[0], "is not a dry-run field")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a = await r._annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        if not r._owns(ctx.who, a) and not ctx.who.has("DataSteward"):
            raise forbidden("the annex's Owners, DataStewards and ArchiveAdmins preview its rules")
        if b.get("body") is None:
            running = await _running(db, a["annex_id"])
            if running is None:
                raise WebError("version_conflict", "the annex has no running rule set")
            body = running["body"]
        else:
            try:
                body = annex_rules.normalize(b["body"], a["dataset_id"])
            except annex_rules.RulesError as e:
                raise invalid(e.field, str(e)) from None
        if not await may_preview(db, ctx.who, a, body):
            # The listing is unfiltered: counts by name, size, owner or
            # content can tell what is in folders the caller can't read.
            raise WebError(
                "role_required",
                "previews are an ArchiveAdmin's until the annex's folders carry the NAS's ACLs",
            )
        await dry_runs.expire(db, a["annex_id"])
        if b.get("body") is not None:
            await dry_runs.supersede_drafts(db, a["annex_id"], ctx.who.actor)
        if await dry_runs.waiting(db, a["annex_id"]) >= dry_runs.MAX_WAITING:
            raise WebError("throttled", "20 previews are waiting for the edge; try again soon")
        run = await new_dry_run(db, ctx.who, a["annex_id"], body, None)
    out = dry_run_json(run, ctx.who)
    out["checks"] = annex_rules.checks(body, quiet_minutes=a["quiet_minutes"])
    return WebResult(202, out)


async def get_dry_run(ctx: Ctx) -> WebResult:
    r = _routes()
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a = await r._annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        await dry_runs.expire(db, a["annex_id"])
        run = await (
            await db.execute(
                "SELECT * FROM catalog.annex_dry_run WHERE annex_id = %s AND run_id = %s",
                (a["annex_id"], ctx.path_id("run_id")),
            )
        ).fetchone()
        if run is None:
            raise not_found("dry run")
        out = await visible_run_json(db, ctx.who, a, run)
    return WebResult(200, out)
