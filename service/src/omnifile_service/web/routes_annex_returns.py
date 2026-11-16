"""Getting an annexed file back (portal-api.md 5.25; annex.md sections 3.6,
4.5 and 4.6; phase A3.1).

- `GET /v1/files/{file_id}/return-targets[?requester=SID]`: where the
  annex's return rules let this requester have the file placed, default
  first, each with the write check, today's name conflict and free space.
- `POST /v1/placements` `{file_id, version_id?, place_at, on_conflict?,
  priority?, note?, requester?}`: a placement. It brings the bytes back
  first if they are in Archive (the usual recall, `priority`), then waits as
  `ready` for its writer: the edge for a NAS folder (phase A3.3), the
  service for a share (A3.2). A second request for the same file and target
  joins the open one.
- `GET /v1/placements/{placement_id}`: its progress (the requester's, and an
  ArchiveAdmin's).
- `POST /v1/annexes/{annex_id}/return-preview` `{body?}`: the return rules
  over the annex's removed files (the builder's Back half). ArchiveAdmins and
  apps only, as previews of the outbound rules are: counts can tell what is
  in folders the caller can't read.

Who may ask: anyone the file's captured ACL lets read it (access-control.md
section 5); an API client with `Recall.Request`, for a person it names, who
must be able to read it (`422 requester_required` otherwise; decision 52).
Where it may land is a separate check: the requester's own right to add a
file there (`annex/returns.py`), because the edge writes with its own
account. A target the requester can't write to isn't offered, and is
refused if asked for (`403 target_not_allowed`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from ..annex import returns as R
from ..annex import rules as annex_rules
from ..annex.placement_states import ENDED
from ..api.validate import parse_id, rfc3339
from ..apiclients.access import require_owner_access
from ..catalog.read_set import readable_row_sql
from .access import acl_params
from .context import Ctx, Principal, WebResult, audit, client_addr
from .problems import WebError, invalid, not_found, role_required
from .recalls import archived, recall_options, start_or_join, version_location

PLACE_FIELDS = {"file_id", "version_id", "place_at", "on_conflict", "priority", "note",
                "requester"}  # fmt: skip
#: Conflict choices from the least to the most that a placement may do: a
#: requester may choose a gentler one than the rule's, never a harsher one.
CONFLICT_ORDER = ("same_content", "restored_name", "overwrite_keep_version")
#: How long the edge must have said nothing new before an ArchiveAdmin may
#: abandon a placement that has its `go`. A day: longer than an overnight outage
#: of the service or the NAS, after which a live edge carries on by itself.
ABANDON_AFTER = timedelta(hours=24)


def _annex_rows():
    from . import routes_annex

    return routes_annex


async def _requester(db, ctx: Ctx, sid: Any) -> tuple[int, R.Requester, str]:
    """(identity_id, requester, via): the signed-in person, or the person an
    app names."""
    who = ctx.who
    if not who.is_app:
        if who.identity_id is None or who.sid is None:
            raise WebError("forbidden", "your AD identity is not known yet; sign in again")
        acl_params(who)  # 503 groups_pending while groups are being resolved
        if sid is not None:
            raise invalid("requester", "is for API clients; you ask for yourself")
        return who.identity_id, R.Requester(who.sid, list(who.groups)), "portal"
    if not who.has("Recall.Request"):
        raise role_required("Recall.Request")
    if sid is None:
        raise WebError("requester_required", "name the person the file is for (requester: a SID)")
    if not isinstance(sid, str) or not annex_rules._SID.match(sid):
        raise invalid("requester", "a SID")
    row = await (
        await db.execute(
            "SELECT identity_id, kind, sid::text AS sid, group_sids::text[] AS groups"
            "  FROM catalog.identity WHERE sid = %s",
            (sid,),
        )
    ).fetchone()
    if row is None or row["kind"] != "user":
        raise WebError("requester_required", "requester is not a person the catalog knows")
    return row["identity_id"], R.Requester(row["sid"], list(row["groups"] or [])), "api"


async def _app_owner(db, ctx: Ctx, file_id: int) -> None:
    """For an API client: registered, and its owner able to see the file's
    dataset, before anything about the file or its readers is looked at
    (#67 review: otherwise an app whose owner lost access learns whether a
    person can read a file, or places it)."""
    if not ctx.who.is_app:
        return
    row = await (
        await db.execute("SELECT dataset_id FROM catalog.file WHERE file_id = %s", (file_id,))
    ).fetchone()
    if row is None:
        raise not_found()
    await require_owner_access(db, ctx.who, row["dataset_id"], ctx.now)


async def _annexed(db, file_id: int, who: R.Requester) -> tuple[dict, dict, R.Facts]:
    """(file, annex, facts) of a file the requester may read; 404 otherwise
    (a file they can't read looks like no file)."""
    row = await (
        await db.execute(
            f"""
            SELECT f.*, af.annex_id, af.path AS annex_path, af.nas_state,
                   coalesce(af.copied_size, af.size) AS annex_size,
                   acl.owner_sid::text AS acl_owner, acl.group_sid::text AS acl_group,
                   (acl.acl_id IS NOT NULL AND {readable_row_sql("f.file_id")}) AS readable
              FROM catalog.file f
              JOIN catalog.annex_file af ON af.file_id = f.file_id
              LEFT JOIN catalog.acl acl ON acl.acl_id = f.acl_id
             WHERE f.file_id = %(f)s AND af.purged_at IS NULL
               AND af.purge_requested_at IS NULL
            """,
            {"f": file_id, "sid": who.sid or "S-1-0-0", "groups": list(who.groups)},
        )
    ).fetchone()
    if row is None or not row["readable"]:
        raise not_found()
    a = await (
        await db.execute(
            "SELECT a.*, r.version AS rs_version, r.body AS rs_body FROM catalog.annex a"
            " LEFT JOIN catalog.annex_ruleset r ON r.annex_id = a.annex_id AND r.state = 'active'"
            " WHERE a.annex_id = %s",
            (row["annex_id"],),
        )
    ).fetchone()
    facts = R.Facts(row["file_id"], row["annex_id"], row["annex_path"], int(row["annex_size"] or 0),
                    row["nas_state"], row["acl_owner"], row["acl_group"])  # fmt: skip
    return row, a, facts


def _targets_json(out: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for t in out["targets"]:
        sp = t.get("space")
        items.append({
            "location": t["location"],
            "default": t["default"],
            "on_conflict": t["on_conflict"],
            "hold_days": t["hold_days"],
            "allowed": t["allowed"],
            "why_not": t.get("why_not"),
            "target": t.get("target"),
            "display": t.get("display"),
            "name_taken": t.get("name_taken"),
            # A3.4: an older version of the file may replace it there (decision 83).
            "replace_older": bool(t.get("replace_older")),
            "free_bytes": sp["free_bytes"] if sp else None,
            "space_seen_at": rfc3339(sp["observed_at"]) if sp else None,
        })  # fmt: skip
    return items


async def get_return_targets(ctx: Ctx) -> WebResult:
    file_id = ctx.path_id("file_id")
    async with ctx.svc.catalog.read() as db:
        await _app_owner(db, ctx, file_id)
        _, who, _ = await _requester(db, ctx, ctx.query("requester"))
        f, a, facts = await _annexed(db, file_id, who)
        out = await R.targets_for(db, a["rs_body"] or {"rules": []}, facts, who, a)
    return WebResult(200, {
        "file_id": str(file_id),
        "annex_id": str(a["annex_id"]),
        "nas_state": facts.nas_state,
        "rule": out["rule"],
        "ruleset_version": a["rs_version"],
        "items": _targets_json(out),
    })  # fmt: skip


def placement_json(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "placement_id": str(p["placement_id"]),
        "annex_id": str(p["annex_id"]),
        "file_id": str(p["file_id"]),
        "version_id": str(p["version_id"]),
        "recall_id": None if p["recall_id"] is None else str(p["recall_id"]),
        "requester_identity_id": str(p["requester_identity_id"]),
        "request_via": p["request_via"],
        "note": p["note"],
        "rule": p["rule"],
        "ruleset_version": p["ruleset_version"],
        "target": p["target"],
        "display": (p.get("target") or {}).get("display"),
        "chosen": p["chosen"],
        "on_conflict": p["on_conflict"],
        "hold_days": p["hold_days"],
        "state": p["state"],
        "outcome": p["outcome"],
        "fail_reason": p["fail_reason"],
        "fail_detail": p["fail_detail"],
        "final_path": p["final_path"],
        # A3.4: a replace at the origin, and what became of the file that was there.
        "replace": None
        if p.get("replace_state") is None
        else {
            "state": p["replace_state"],
            "refused": p.get("replace_refused"),
            "replaced_version_id": None
            if p.get("replaced_version_id") is None
            else str(p["replaced_version_id"]),
            "hidden_name": p.get("replace_hidden_name"),
            "kept_as": p.get("replaced_aside_name"),
        },
        "requested_at": rfc3339(p["requested_at"]),
        "updated_at": rfc3339(p["updated_at"]),
        "done_at": rfc3339(p["done_at"]),
    }


async def request_placement(ctx: Ctx) -> WebResult:
    b = ctx.body
    extra = set(b) - PLACE_FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a placement field")
    if "file_id" not in b:
        raise invalid("file_id", "is required")
    file_id = parse_id(b["file_id"], "file_id")
    if "place_at" not in b:
        raise invalid("place_at", "is required: 'default' or one of the file's return targets")
    note = b.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise invalid("note", "up to 500 characters")
    now = ctx.now
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        await _app_owner(db, ctx, file_id)
        identity_id, who, via = await _requester(db, ctx, b.get("requester"))
        f, a, facts = await _annexed(db, file_id, who)
        if a["state"] == "retired":
            raise WebError("version_conflict", "the annex is retired")
        body = a["rs_body"] or {"rules": []}
        d = R.decide(body, facts, who)
        if d is None or d["exclude"]:
            raise WebError("target_not_allowed", "the annex's return rules place this file nowhere")
        locs = [x for x in [d["default"], *d["choices"]] if x is not None]
        if b["place_at"] == "default":
            if d["default"] is None:
                raise WebError("target_not_allowed", "the rules have no default target; pick one")
            loc = d["default"]
        else:
            try:
                loc = annex_rules.location(b["place_at"], "place_at")
            except annex_rules.RulesError as e:
                raise invalid(e.field, str(e)) from None
            if not any(R.within(x, loc) for x in locs):
                raise WebError("target_not_allowed", "the annex's return rules don't allow that"
                                                     " target for you")  # fmt: skip
        conflict = b.get("on_conflict", d["on_conflict"])
        if conflict not in CONFLICT_ORDER:
            raise invalid("on_conflict", "one of " + ", ".join(CONFLICT_ORDER))
        if CONFLICT_ORDER.index(conflict) > CONFLICT_ORDER.index(d["on_conflict"]):
            raise WebError(
                "target_not_allowed",
                f"the rule's choice on a name conflict is {d['on_conflict']}; you can't go further",
            )
        asked = parse_id(b["version_id"], "version_id") if "version_id" in b else None
        if (
            loc.get("origin")
            and facts.nas_state not in R.ORIGIN_STATES
            and not (
                # Decision 83: an older version may replace the file still there, when the
                # rule says overwrite (A3.4). The current version is there already.
                conflict == "overwrite_keep_version"
                and asked is not None
                and asked != f["content_version_id"]
            )
        ):
            raise WebError("file_present", "the file is still on the NAS where it came from")
        t = await R.resolve(db, loc, facts, a)
        if t is None:
            raise WebError("target_not_allowed", "that location no longer exists")
        ok, why = await R.write_check(db, t, who)
        if ok and t.kind == "share":
            why = await R.share_acl_problem(db, file_id)
            ok = why is None
        if not ok:
            raise WebError("target_not_allowed", f"you can't have it placed there: {why}")
        vid = parse_id(b["version_id"], "version_id") if "version_id" in b else None
        vid = vid or f["content_version_id"]
        if vid is None:
            raise WebError("recall_not_needed", "the file has no stored bytes")
        vloc = await version_location(db, file_id, vid)
        if vloc["version_state"] != "committed" or vloc["location_id"] is None:
            raise not_found("version")
        target = {**t.json(), "display": await R.display(db, t)}
        existing = await (
            await db.execute(
                "SELECT * FROM catalog.annex_placement WHERE file_id = %s"
                " AND md5(target::text) = md5(%s::jsonb::text) AND state <> ALL(%s)",
                (file_id, Jsonb(target), list(ENDED)),
            )
        ).fetchone()
        if existing is not None:
            out = placement_json(existing)
            out["joined"] = True
            return WebResult(200, out)
        recall_id, state = None, "ready"
        if archived(vloc, now):
            priority = b.get("priority")
            options = (await recall_options(db, vloc, now, None))["options"]
            offered = [o["priority"] for o in options if o["offered"]] or ["standard"]
            if priority is None:
                raise invalid("priority", "the file is in Archive: pick one of the recall options"
                                          f" ({', '.join(offered)})")  # fmt: skip
            if priority not in offered:
                raise invalid("priority", f"is not offered for this file ({', '.join(offered)})")
            recall, _ = await start_or_join(
                db, file=f, loc=vloc, priority=priority, due_by=None,
                identity_id=identity_id, app_id=ctx.who.app_id if ctx.who.is_app else None,
                # A placement is of this version: it holds the recall it waits on,
                # which goes on if the annex copies a newer one meanwhile (0059).
                now=now, hold=True,
            )  # fmt: skip
            recall_id, state = recall["recall_id"], "rehydrating"
        elif "priority" in b:
            raise invalid("priority", "the file is online: it needs no recall first")
        client = (ctx.who.api_client or {}) if ctx.who.is_app else {}
        p = await (
            await db.execute(
                """
                INSERT INTO catalog.annex_placement
                    (annex_id, file_id, version_id, recall_id, requester_identity_id,
                     request_via, api_client_id, note, rule, ruleset_version, target, chosen,
                     on_conflict, hold_days, notify, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    a["annex_id"],
                    file_id,
                    vloc["content_id"],
                    recall_id,
                    identity_id,
                    via,
                    client.get("api_client_id"),
                    note,
                    d["rule"],
                    a["rs_version"],
                    Jsonb(target),
                    None if b["place_at"] == "default" else Jsonb(loc),
                    conflict,
                    d["hold_days"],
                    Jsonb(d["notify"]),
                    state,
                ),
            )  # fmt: skip
        ).fetchone()
        if state == "ready" and t.kind == "share":
            # Into a share: the worker places it at once (A3.2).
            await db.execute(
                "SELECT pgmq.send('portal', %s::jsonb)",
                (Jsonb({"kind": "annex_placement", "placement_id": str(p["placement_id"])}),),
            )
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
            " VALUES (%s, 'recall_requested', %s, %s, %s, %s)",
            (a["annex_id"], facts.path, file_id, vloc["content_id"],
             Jsonb({"placement_id": str(p["placement_id"]), "by": ctx.who.actor,
                    "requester_identity_id": str(identity_id), "target": target,
                    "rule": d["rule"]})),
        )  # fmt: skip
        await audit(
            db, ctx.who, "annex.placement.request", dataset_id=f["dataset_id"],
            file_id=file_id, version_id=vloc["content_id"], client_addr=client_addr(ctx.request),
            details={"placement_id": str(p["placement_id"]), "annex_id": str(a["annex_id"]),
                     "target": target, "on_conflict": conflict, "rule": d["rule"],
                     "requester_identity_id": str(identity_id), "via": via},
        )  # fmt: skip
    out = placement_json(p)
    out["joined"] = False
    return WebResult(201, out)


async def get_placement(ctx: Ctx) -> WebResult:
    pid = ctx.path_id("placement_id")
    who: Principal = ctx.who
    async with ctx.svc.catalog.read() as db:
        p = await (
            await db.execute(
                "SELECT * FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
            )
        ).fetchone()
        if p is None:
            raise not_found("placement")
        mine = not who.is_app and who.identity_id == p["requester_identity_id"]
        app_mine = (
            who.is_app
            and who.api_client is not None
            and (str(who.api_client.get("api_client_id")) == str(p["api_client_id"]))
        )
        if not (mine or app_mine or (not who.is_app and who.has("ArchiveAdmin"))):
            raise not_found("placement")
    return WebResult(200, placement_json(p))


async def cancel_placement(ctx: Ctx) -> WebResult:
    """The way out of a placement that can't go on (a stuck one, or one waiting
    for its bytes): its requester's or an ArchiveAdmin's. Once it has its `go`
    the file may be under its name, so a `placing` one can't be cancelled. The
    edge cleans up what it made when it next hears."""
    pid = ctx.path_id("placement_id")
    who: Principal = ctx.who
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await (
            await db.execute(
                "SELECT * FROM catalog.annex_placement WHERE placement_id = %s FOR UPDATE", (pid,)
            )
        ).fetchone()
        if p is None:
            raise not_found("placement")
        mine = not who.is_app and who.identity_id == p["requester_identity_id"]
        if not (mine or (not who.is_app and who.has("ArchiveAdmin"))):
            raise not_found("placement")
        stuck_placing = p["state"] == "stuck" and p["before_stuck"] == "placing"
        if p["state"] in (*ENDED, "placing") or stuck_placing:
            raise WebError("placement_not_cancellable", f"the placement is {p['state']}")
        p = await (
            await db.execute(
                "UPDATE catalog.annex_placement SET state = 'cancelled',"
                " done_at = now(), updated_at = now() WHERE placement_id = %s RETURNING *",
                (pid,),
            )
        ).fetchone()
        await audit(db, who, "annex.placement.cancelled", file_id=p["file_id"],
                    version_id=p["version_id"], details={"placement_id": str(pid)})  # fmt: skip
    return WebResult(200, placement_json(p))


def left_like(p: dict[str, Any]) -> str:
    """What an abandoned placement left on the NAS, as far as the edge said, for
    an admin to put right. Nothing goes back to fix it afterwards."""
    report = p["last_edge_report"] or {}
    at, name = report.get("at"), report.get("name")
    folder = (p["target"] or {}).get("folder") or ""
    where = "/".join(x for x in [folder, name] if x) if name else "its name"
    if at in ("linked", "owned"):
        return (
            f"the file is under {where}, and the placement account may still own it, because"
            " the step that sets its owner didn't finish: set its owner and ACL by hand"
        )
    if at in ("linking", "placeholder", "placeholder_make"):
        return (
            f"the file may be under {where}, with the placement account as owner; check it, and"
            " set its owner and ACL by hand if it is there. The edge removes its staged copy"
        )
    if report:
        return f"the edge last said it was at {at}: it removes its staged copy when it next hears"
    return "the edge never said where it was: the file may be on the NAS; the next scan finds it"


async def abandon_placement(ctx: Ctx) -> WebResult:
    """The way out of a placement stuck after its `go`, when the edge has said
    nothing new for a day: its journal was lost, say, or it is stuck at the link.
    An ArchiveAdmin's, audited. It ends, so whatever the edge reports later is
    refused and audited, and changes nothing. The file may be on the NAS: the
    next scan finds it like any other, so the worst case is a recopy, and the
    file has no hold."""
    pid = ctx.path_id("placement_id")
    who: Principal = ctx.who
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    now = ctx.now
    async with ctx.svc.catalog.tx(who.actor) as db:
        p = await (
            await db.execute(
                "SELECT * FROM catalog.annex_placement WHERE placement_id = %s FOR UPDATE", (pid,)
            )
        ).fetchone()
        if p is None:
            raise not_found("placement")
        after_go = p["state"] == "placing" or (
            p["state"] == "stuck" and p["before_stuck"] == "placing"
        )
        if not after_go:
            raise WebError(
                "placement_not_abandonable",
                f"the placement is {p['state']}: only one that has its go can be abandoned;"
                " before that, cancel it",
            )
        last = p["last_report_at"] or p["updated_at"]
        if now - last < ABANDON_AFTER:
            raise WebError(
                "placement_not_abandonable",
                f"the edge reported at {rfc3339(last)}: it can be abandoned"
                f" {rfc3339(last + ABANDON_AFTER)}",
            )
        p = await (
            await db.execute(
                "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'abandoned',"
                " fail_detail = %s, done_at = %s, updated_at = %s WHERE placement_id = %s"
                " RETURNING *",
                (f"abandoned by {who.actor}", now, now, pid),
            )
        ).fetchone()
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
            " VALUES (%s, 'problem', %s, %s, %s)",
            (p["annex_id"], p["file_id"], p["version_id"],
             Jsonb({"placement_id": str(pid), "reason": "placement_abandoned",
                    "detail": left_like(p), "edge_said": p["last_edge_report"]})),
        )  # fmt: skip
        await audit(db, who, "annex.placement.abandoned", file_id=p["file_id"],
                    version_id=p["version_id"], details={"placement_id": str(pid),
                    "last_report_at": rfc3339(last), "left": left_like(p),
                    "edge_said": p["last_edge_report"]})  # fmt: skip
    return WebResult(200, placement_json(p))


async def return_preview(ctx: Ctx) -> WebResult:
    r = _annex_rows()
    b = ctx.body or {}
    if set(b) - {"body"}:
        raise invalid(sorted(set(b) - {"body"})[0], "is not a preview field")
    who = ctx.who
    if not (who.is_app or who.has("ArchiveAdmin")):
        raise WebError("role_required", "previews over an annex's files are an ArchiveAdmin's")
    async with ctx.svc.catalog.read() as db:
        a = await r._annex(db, ctx.path_id("annex_id"), who)
        await require_owner_access(db, who, a["dataset_id"], ctx.now)
        if b.get("body") is None:
            row = await (
                await db.execute(
                    "SELECT body FROM catalog.annex_ruleset WHERE annex_id = %s"
                    " AND state = 'active'",
                    (a["annex_id"],),
                )
            ).fetchone()
            body = row["body"] if row else {"rules": []}
        else:
            try:
                body = annex_rules.normalize(b["body"], a["dataset_id"])
            except annex_rules.RulesError as e:
                raise invalid(e.field, str(e)) from None
        report = await R.preview(db, body, a)
    report["checks"] = annex_rules.return_checks(body)
    return WebResult(200, report)
