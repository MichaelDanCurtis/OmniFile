"""Placements on a NAS through the control API (annex.md section 4.6; phase A3.3;
docs/milestones/results/wp14-a3.md, "What A3 writes to a NAS").

The edge never places a file by itself. Its annex unit asks for tickets, and
before the link that puts the file under its name (P4) it asks again
(`go`). Every answer checks every gate, here, and is audited:

- `POST /v0/edges/{id}/annex-placement-tickets` `{annex_id, in_hand, renew}`:
  the `ready` placements whose target is a folder of this annex's filer, at
  most 10. Each one that passes its gates becomes `writing`, with a ticket:
  the target, the names it may take in order, the descriptor to set, the
  version's size, CRC and mtime, and a read credential for its bytes. One
  that no longer passes them fails, with why. One already under way is
  handed over only if the edge doesn't list it in `in_hand` (it lost its
  journal), or lists it in `renew` (its read credential is running out): a
  new credential is minted only then, and audited.
- `POST /v0/edges/{id}/annex-placements/{placement_id}` `{step, ...}`: the
  edge's reports (`filled`, `read_back`, `linked`, `owned`, `done`,
  `failed`, `stuck`, `leftover`) and its question before P4, `go` `{acl,
  folder_acl, name}` -> `{proceed, reason}`.

A placement moves `ready -> writing -> verifying -> securing -> placing ->
placed`. A report out of order changes nothing and is answered
`not_in_order`: `go` needs a ticket first, `done` needs a `go`. A read-back
whose CRC isn't the version's fails the placement. A stuck placement goes back
to the state it came from at its next report.

The gates, at the ticket and again at `go`:
- the placement switches (the annex's and Settings') are on (a pause holds,
  never fails);
- the requester can still read the version (its captured ACL, deny-first);
- the annex's running return rules still allow that target for them;
- the requester can add a file to the target folder: at the ticket from the
  folder's captured ACL, at `go` from the folder's ACL as the edge reads it
  then (`folder_acl`), so a change during a long rehydration is caught;
- at `go`, the ACL the edge set on the staging file never lets more people
  read the file (decision 51), or `acl_mismatch`:
  - its read set is within the file's catalog read set. An NT entry for the
    descriptor's owner counts as the file's own owner only when that owner is
    the filer's registered placement account (`placement_sid`), which owns the
    file only until P5. Its group is taken literally, on every kind of ACL: a
    group's read must not depend on P5 succeeding, and over NFS the edge has
    compared the file's real group with the final one;
  - every deny in the catalog is in it too, with the same evaluation order. A
    server that drops a deny (one mapping NT ACLs to POSIX) would otherwise
    let a member of both groups read the file;
  - it gives WRITE_DAC or WRITE_OWNER to nobody who isn't a reader, the owner,
    or someone the catalog's ACL already gave them to: they could grant
    themselves read;
  - it allows no principal the edge couldn't map: whoever that is isn't known
    to be a reader of the file. Two exceptions, for read only unless said:
    the catalog's own raw ACL literally lets anyone read the file (`S-1-1-0`, a
    NULL DACL or POSIX's other class, not Authenticated Users or BUILTIN\\Users,
    which the read sets normalise to Everyone); or, at the file's origin on its
    own filer, the staged ACL is provably the capture of the version being
    placed (`same_as_capture`), which also covers WRITE_DAC and WRITE_OWNER.
    WRITE_DAC or WRITE_OWNER for an unmapped principal is otherwise always a
    mismatch, whoever may read the file.
  - a staged `S-1-1-0` needs a catalog ACL that literally has it.
- at `go`, the CRC of the edge's read-back is the version's (`crc_mismatch`).

`overwrite_keep_version` on a NAS (phase A3.4, `annex/replace.py`): at the file's
own name in its original folder, and nowhere else (decision 82), the file there is
replaced when it is exactly the annex's current, verified copy. The ticket then
carries `replace`, what that file must still be, and the edge asks `replace_go`
before it hides the file and `replace_delete_go` after the grace period (before the
new file's link) and again before the delete. It reports `replace_hidden`,
`replace_put_back`, `replace_aside` and `replaced`, each audited with the replaced
version and its CRC. Anything else takes a `-restored` name, and the outcome says
`overwrite_refused_<why>`. An older version at its origin takes the file's name only
then, or when the name is free and the rule says overwrite (decision 83); `done`
makes it the file's new head, referencing the placed version's blob, the replaced
version kept in its history. Otherwise it never takes the name: the head there is
another version, so the old bytes under it would make the NAS and the catalog
disagree.

`done` looks after the file the placement made (section 4.6, loops):
- back under its own name at the origin, the annex's file is present again as
  the copy it already has, held. Only if the size the edge journalled at P5 is
  the version's: otherwise the file isn't that copy, and the next scan sees a
  change. A tombstoned file is undeleted, unless its name has since been taken.
- under any other name in an annexed folder, it is a new annexed file: held,
  and referencing the version's blob, so it is not copied again.

`done` reports a fact, so it always lands. What it can't do (the head moved,
the catalog has something else there) it records as a note, and the next scan
sorts out the file.

A scan that meets a file a `placing` placement may be making is refused with
`placement_in_progress` until `done` says which it is.

An ArchiveAdmin may abandon a placement stuck after its `go`, once the edge has
said nothing new for `ABANDON_AFTER` (`web/routes_annex_returns.py`). It ends
as `failed: abandoned`. Whatever the edge reports later is refused and audited,
so the worst case is a recopy: the scan finds the file, which has no hold.
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..annex import replace as RP
from ..annex import returns as R
from ..annex import rules as annex_rules
from ..annex.acl import (
    AclError,
    acl_body,
    control_sids,
    deny_first_sets,
    folder_write_sets,
    literally_anyone_reads,
    parse_sd,
    unmapped_control,
    unmapped_reader,
)
from ..annex.placement_states import ENDED
from ..api.acls import access_sets
from ..catalog.read_set import (
    Folders,
    acl_sets,
    acls_hidden_blocks,
    captured,
    folders_of,
    group_readable,
    nas_folders,
    readable_sql_pos,
)
from ..web.routes_rewind import restored_name
from .context import Ctx
from .dataplane import Target
from .db import id_str
from .idempotency import Result
from .problems import ApiError
from .validate import req, req_id

TICKETS_PER_ASK = 10
TICKET_TTL = timedelta(minutes=30)
#: The replace's questions (A3.4): before the hide, and before the link and the delete.
REPLACE_ASKS = ("replace_go", "replace_delete_go")
#: The replace's reports: facts about the NAS, recorded whenever they come, even after the
#: placement ended (a refusal puts the old file back, and that is said afterwards).
REPLACE_FACTS = ("replace_hidden", "replace_put_back", "replace_aside", "replaced",
                 "replace_gone")  # fmt: skip
STEPS = ("go", "filled", "read_back", "linked", "owned", "done", "failed", "stuck", "leftover",
         *REPLACE_ASKS, *REPLACE_FACTS)  # fmt: skip
#: Where `go` may come from. `placing`: asked again after a restart, whose
#: every gate is checked again; it never skips one.
GO_FROM = ("writing", "verifying", "securing", "placing")
log = logging.getLogger(__name__)

#: States in which the fill may still need a read credential.
BEFORE_GO = ("writing", "verifying", "securing")
OUTCOMES = ("placed", "restored_name", "already_there", "replaced", "replaced_old_kept")
#: How often a placement says again that it can't have a read credential.
NO_CREDENTIAL_EVERY = timedelta(hours=1)
#: How many placements one ask may look at, whatever they turn out to need.
TICKET_ROWS_PER_ASK = 500
#: `go`'s reason when an SMB server's staged owner is an account nobody registered.
UNREGISTERED = "placement_account_not_registered"
#: The proof would hold, but for a server that doesn't show its POSIX ACLs.
ACL_HIDDEN = "acl_hidden"
#: Decision 64: the file's filer shows no ACLs and nobody has acknowledged it.
ACLS_HIDDEN_FILER = "acls_hidden_filer"
EVERYONE = "S-1-1-0"


def bad(detail: str) -> ApiError:
    return ApiError("invalid_request", detail)


async def _placer_annex(db, edge_id: int, annex_id: int) -> dict[str, Any]:
    a = await (
        await db.execute(
            "SELECT a.*, fl.protocol, fl.host, fl.export FROM catalog.annex a"
            " JOIN catalog.filer fl ON fl.filer_id = a.filer_id"
            " WHERE a.annex_id = %s AND a.edge_id = %s",
            (annex_id, edge_id),
        )
    ).fetchone()
    if a is None:
        raise ApiError("not_found", f"annex {annex_id} is not on this edge")
    return a


async def paused(db, annex_id: int, target_filer: int | None = None) -> bool:
    """The switches: Settings', the placement's annex's, and any annex's on the
    target's filer (a paused annex there means no placing there)."""
    row = await (
        await db.execute(
            "SELECT a.placements_paused OR d.placements_paused OR EXISTS ("
            "        SELECT 1 FROM catalog.annex x WHERE x.filer_id = %s AND x.placements_paused"
            "           AND x.state <> 'retired'"
            "       ) AS p"
            "  FROM catalog.annex a, catalog.annex_defaults d WHERE a.annex_id = %s",
            (target_filer, annex_id),
        )
    ).fetchone()
    return bool(row and row["p"])


async def _requester(db, identity_id: int) -> R.Requester:
    i = await (
        await db.execute(
            "SELECT sid::text AS sid, group_sids::text[] AS groups FROM catalog.identity"
            " WHERE identity_id = %s",
            (identity_id,),
        )
    ).fetchone()
    return R.Requester(i["sid"] if i else None, list((i or {}).get("groups") or []))


async def _source(db, p: dict[str, Any]) -> dict[str, Any] | None:
    """The annexed file, its catalog ACL, the folders above it (decision 63) and
    the version's readable bytes."""
    row = await (
        await db.execute(
            f"""
            SELECT f.file_id, f.dataset_id, af.path, af.nas_state, af.size AS annex_size,
                   v.size, v.mtime_ns, v.crc64nvme, v.state AS version_state, v.blake3,
                   f.content_version_id AS current_version_id,
                   f.parent_id AS f_parent_id, f.name AS f_name,
                   coalesce(f.uid, v.uid) AS f_uid, coalesce(f.gid, v.gid) AS f_gid,
                   v.uid AS version_uid, v.gid AS version_gid, v.mode AS version_mode,
                   v.acl_id AS version_acl_id,
                   af.purge_requested_at, af.purged_at, af.annex_id AS file_annex_id,
                   fa.path AS file_annex_path, fa.filer_id AS file_filer_id,
                   a.acl_id, a.raw_kind, a.raw, a.mode AS acl_mode, a.entries,
                   a.owner_sid::text AS owner_sid, a.group_sid::text AS group_sid,
                   {acl_sets("a")},
                   l.location_id, l.backend_id, l.container, l.object_key, l.backend_version_id,
                   l.logical_tier, b.kind AS backend_kind, b.config AS backend_config,
                   r.state AS restore_state, r.expires_at AS restore_expires_at,
                   t.container AS t_container, t.object_key AS t_object_key,
                   t.backend_version_id AS t_backend_version_id, t.location_id AS t_location_id
              FROM catalog.file f
              JOIN catalog.annex_file af ON af.file_id = f.file_id
              JOIN catalog.annex fa ON fa.annex_id = af.annex_id
              JOIN catalog.file_version v ON v.version_id = %(v)s AND v.file_id = f.file_id
              LEFT JOIN catalog.acl a ON a.acl_id = f.acl_id
              LEFT JOIN catalog.location l ON l.version_id = v.version_id
                   AND l.role = 'primary' AND l.state = 'committed'
              LEFT JOIN catalog.backend b ON b.backend_id = l.backend_id
              LEFT JOIN LATERAL (
                    SELECT * FROM catalog.restore WHERE location_id = l.location_id
                       AND state <> 'cancelled' ORDER BY requested_at DESC, restore_id DESC
                     LIMIT 1) r ON true
              LEFT JOIN catalog.location t ON t.location_id = r.target_location_id
                   AND t.state = 'committed'
             WHERE f.file_id = %(f)s
            """,
            {"f": p["file_id"], "v": p["version_id"]},
        )
    ).fetchone()
    if row is not None:
        row["folders"] = await folders_of(db, p["file_id"])
    return row


def _readable(src: dict[str, Any], now: datetime) -> Target | None:
    if src["location_id"] is None:
        return None
    if src["logical_tier"] != "archive":
        return Target(src["backend_id"], src["container"], src["object_key"],
                      src["backend_version_id"])  # fmt: skip
    live = src["restore_state"] == "completed" and (
        src["restore_expires_at"] is None or src["restore_expires_at"] > now
    )
    if not live:
        return None
    if src["t_location_id"] is not None:
        return Target(src["backend_id"], src["t_container"], src["t_object_key"],
                      src["t_backend_version_id"])  # fmt: skip
    return Target(src["backend_id"], src["container"], src["object_key"],
                  src["backend_version_id"])  # fmt: skip


def _descriptor(src: dict[str, Any]) -> dict[str, Any] | None:
    """The captured ACL as the edge sets it; None if it can't be put back in v1
    (a POSIX ACL with named entries: the catalog keeps their SIDs, not the
    NAS's ids)."""
    kind = src["raw_kind"]
    if kind in ("ntfs_sd", "nfs4"):
        if src["raw"] is None:
            return None
        return {"kind": kind, "raw": base64.b64encode(bytes(src["raw"])).decode(),
                "owner_sid": src["owner_sid"], "group_sid": src["group_sid"]}  # fmt: skip
    if kind == "posix":
        if any(e.get("tag") in ("user", "group") for e in src["entries"] or []):
            return None
        return {"kind": "posix", "mode": int(src["acl_mode"]) & 0o7777,
                "owner_sid": src["owner_sid"], "group_sid": src["group_sid"]}  # fmt: skip
    return None


#: `gates`' reason, with `ok`, when the placement may go on but the running rule no
#: longer says `overwrite_keep_version` for it: no replace (#98's S1).
RULE_NO_OVERWRITE = "rule_changed"


async def gates(
    db, p: dict[str, Any], src: dict[str, Any] | None, who: R.Requester, *, replace: bool = False
) -> tuple[str, str | None]:
    """(`ok` | `hold` | `fail`, reason). `replace`: also whether the running rule still
    says overwrite for this file and target; if not, (`ok`, `RULE_NO_OVERWRITE`)."""
    filer = p["target"].get("filer_id")
    if await paused(db, p["annex_id"], int(filer) if filer is not None else None):
        return "hold", "paused"
    if src is None or src["version_state"] != "committed" or src["crc64nvme"] is None:
        return "fail", "not_restorable"
    # A purge asked for or done: its name is spoken for. One a legal hold refused
    # ended there: the hold keeps the bytes, and doesn't stop them going back.
    if src["purge_requested_at"] is not None or src["purged_at"] is not None:
        return "fail", "file_purged"
    if src["acl_id"] is None:
        return "fail", "access_denied"
    can = await (
        await db.execute(
            f"SELECT {readable_sql_pos('%s::bigint', 'acl')} AS ok"
            "  FROM catalog.acl acl WHERE acl.acl_id = %s",
            (src["file_id"], who.sid or "S-1-0-0", list(who.groups), src["acl_id"]),
        )
    ).fetchone()
    if not can["ok"]:
        return "fail", "access_denied"
    # The running return rules still allow that target for them.
    if replace:
        # Held until this answer commits: an approval that replaces the running rule
        # set waits for it, so a yes is never given on a rule already replaced (C9).
        await db.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
            (annex_rules.ruleset_lock_key(p["annex_id"]),),
        )
    a = await (
        await db.execute(
            "SELECT a.*, r.body AS rs_body FROM catalog.annex a LEFT JOIN catalog.annex_ruleset r"
            " ON r.annex_id = a.annex_id AND r.state = 'active' WHERE a.annex_id = %s",
            (p["annex_id"],),
        )
    ).fetchone()
    facts = R.Facts(p["file_id"], p["annex_id"], src["path"], int(src["annex_size"] or 0),
                    src["nas_state"], src["owner_sid"], src["group_sid"])  # fmt: skip
    d = R.decide(a["rs_body"] or {"rules": []}, facts, who)
    allowed = False
    overwrites = d is not None and d.get("on_conflict") == "overwrite_keep_version"
    if d is not None and not d["exclude"]:
        for loc in [x for x in [d["default"], *d["choices"]] if x is not None]:
            cands = [loc] if p["chosen"] is None else ([p["chosen"]] if R.within(
                loc, p["chosen"]) else [])  # fmt: skip
            for c in cands:
                t = await R.resolve(db, c, facts, a)
                if t is not None and same_target(t, p["target"]):
                    allowed = True
    if not allowed:
        return "fail", "target_not_allowed"
    if (
        p["target"].get("origin")
        and src["nas_state"] not in R.ORIGIN_STATES
        and not (RP.wanted(p, src) and p["version_id"] != src["current_version_id"])
    ):
        # Decision 83: an older version may replace the file still there, when the rule
        # says overwrite. The current version is there already.
        return "fail", "file_present"
    if replace and not overwrites:
        return "ok", RULE_NO_OVERWRITE
    return "ok", None


def same_target(t: R.Target, target: dict[str, Any]) -> bool:
    return t.json() == {k: v for k, v in target.items() if k != "display"}


def older_at_origin(p: dict[str, Any], src: dict[str, Any] | None) -> bool:
    """An older version going back to the origin never takes the file's name,
    unless it replaces what is there (A3.4) or the name is free and the rule says
    overwrite: then `done` makes it the file's new head."""
    return bool(
        src is not None
        and p["target"].get("origin")
        and p["version_id"] != src["current_version_id"]
        and not own_name_ok(p, src)
    )


def own_name_ok(p: dict[str, Any], src: dict[str, Any] | None) -> bool:
    """The file's own name is this placement's to take, whatever its version: a
    replace was offered (or done), or the rule says overwrite and the file isn't
    on the NAS (its name is free as far as the catalog knows)."""
    if not RP.wanted(p, src):
        return False
    assert src is not None
    return p.get("replace_state") in RP.OWN_NAME or (
        p.get("replace_state") is None and src["nas_state"] in R.ORIGIN_STATES
    )


def allowed_name(p: dict[str, Any], name: Any, *, older: bool = False) -> bool:
    """One of the names the ticket listed: its own (not for an older version
    at its origin), `-restored`, or `-restored-<UTC time>` (whatever time the
    ticket was made)."""
    if not isinstance(name, str):
        return False
    n = p["target"]["name"]
    if name == restored_name(n, "restored", datetime(2000, 1, 1)) or (name == n and not older):
        return True
    t = restored_name(n, "restored_timestamped", datetime(2000, 1, 1, tzinfo=UTC))
    head, tail = t.split("-restored-2000-01-01-000000Z", 1)
    return bool(
        re.fullmatch(re.escape(head) + r"-restored-\d{4}-\d\d-\d\d-\d{6}Z" + re.escape(tail), name)
    )


def _target(p: dict[str, Any]) -> R.Target:
    t = p["target"]
    return R.Target("nas", t["folder"], t["name"], filer_id=int(t["filer_id"]),
                    origin=bool(t.get("origin")))  # fmt: skip


def names(p: dict[str, Any], now: datetime, *, older: bool = False) -> list[str]:
    """The names a placement may take, in order (section 4.6, decision 34)."""
    n = p["target"]["name"]
    own = [] if older else [n]
    return [*own, restored_name(n, "restored", now), restored_name(n, "restored_timestamped", now)]


async def _fail(db, ctx: Ctx, p: dict[str, Any], reason: str, detail: str = "") -> None:
    now = ctx.now
    await db.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = %s,"
        " fail_detail = left(%s, 500), done_at = %s, updated_at = %s WHERE placement_id = %s",
        (reason, detail or reason, now, now, p["placement_id"]),
    )
    await db.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
        " VALUES (%s, 'placement_failed', %s, %s, %s)",
        (p["annex_id"], p["file_id"], p["version_id"],
         Jsonb({"placement_id": id_str(p["placement_id"]), "reason": reason,
                "detail": detail[:500]})),
    )  # fmt: skip
    await _audit(db, ctx, p, "failed", "deny", {"reason": reason})


async def _audit(db, ctx: Ctx, p: dict[str, Any], action: str, decision: str,
                 details: dict[str, Any]) -> None:  # fmt: skip
    await db.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, file_id, version_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (ctx.edge.principal, f"annex.placement.{action}", decision, p["file_id"],
         p["version_id"], Jsonb({"placement_id": id_str(p["placement_id"]),
                                 "target": p["target"], "requester_identity_id":
                                 id_str(p["requester_identity_id"]), **details})),
    )  # fmt: skip


async def _ticket(ctx: Ctx, p: dict[str, Any], src: dict[str, Any], same_filer: bool,
                  now: datetime) -> dict[str, Any]:  # fmt: skip
    """One ticket, its read credential minted here. Never with a transaction open: a
    provider call with a transaction id held stops the edge feed for every edge."""
    target = _readable(src, now)
    assert target is not None
    plane = ctx.svc.planes.get(target.backend_id, src["backend_kind"], src["backend_config"] or {})
    from ..backends.base import EdgeAccess

    cred = await plane.credential(target, EdgeAccess.READ)
    t = p["target"]
    return {
        "placement_id": id_str(p["placement_id"]),
        "file_id": id_str(p["file_id"]),
        "version_id": id_str(p["version_id"]),
        "path": src["path"],
        "size": int(src["size"] or 0),
        "crc64": bytes(src["crc64nvme"]).hex(),
        "mtime_ns": None if src["mtime_ns"] is None else str(src["mtime_ns"]),
        "folder": t["folder"],
        "name": t["name"],
        "names": names(p, now, older=older_at_origin(p, src)),
        "origin": bool(t.get("origin")),
        # The file there may already be these bytes (A3.4's `same`): read back, as
        # for `same_content`, and left alone if so.
        "same_content": p["on_conflict"] == "same_content" or p.get("replace_state") == "same",
        # A3.4: the file at the name may be replaced, if it is still exactly this
        # (decisions 82 to 85). The edge checks it itself before every step.
        "replace": _replace_ticket(p),
        # In any folder but its own, the descriptor's inherited entries become
        # explicit and the DACL is protected (section 4.6).
        "protect": not t.get("origin"),
        "acl": _descriptor(src),
        # The NAS's own numeric owner and group, as the catalog last knew them
        # for the file, whatever version is being placed: the read set it is
        # judged by is the file's too, and an old version's owner may not be in
        # it. A tombstoned file keeps what it had when it went. A chown on the
        # NAS after the last scan isn't known here, for the same reason its ACL
        # isn't. Only on that NAS: on another they may be someone else's, so the
        # edge maps the SIDs there or fails `group_unmapped`.
        "uid": src["f_uid"] if same_filer else None,
        "gid": src["f_gid"] if same_filer else None,
        "read": cred,
        "expires_at": (now + TICKET_TTL).isoformat(),
    }


def _replace_ticket(p: dict[str, Any]) -> dict[str, Any] | None:
    e = p.get("replace_expect")
    if p.get("replace_state") != "offered" or e is None:
        return None
    return {
        "version_id": id_str(p["replaced_version_id"]),
        "nas_file_id": str(e["nas_file_id"]),
        "size": int(e["copied_size"]),
        "mtime_ns": str(e["copied_mtime_ns"]),
        "ctime_ns": str(e["copied_ctime_ns"]),
        "crc64": p["replaced_crc64"],
        "hidden_name": RP.hidden_name(p["placement_id"], p["target"]["name"]),
    }


async def _offer_replace(
    db,
    ctx: Ctx,
    p: dict[str, Any],
    src: dict[str, Any],
    now: datetime,
    refuse: str | None = None,
) -> bool:
    """Whether the ticket may go now; `False` holds it a while (the in-Azure read of
    the file there is under way). A replace offered, or why not, is recorded on the
    placement and audited."""
    state = p.get("replace_state")
    if state == "offered":
        # Asked again (an edge that lost its journal): the gate again, as at first.
        pass
    elif state is not None:
        return True  # decided already; a hidden file is never offered again
    status, why, row = await RP.decide(db, p, src, now)
    if refuse is not None and status in ("ok", "not_yet"):
        # The running rule no longer says overwrite (S1): no replace.
        status, why = "refused", refuse
    if status == "not_yet":
        return False
    if state == "offered" and p.get("replace_go_at") is not None and status != "ok":
        # A yes was given: the edge may have the file hidden. Only why is recorded;
        # the state waits for the edge's fact (the confirm pass, C8).
        await _refuse_replace(db, p, why or status, now)
        await _audit(db, ctx, p, "replace_refused", "deny",
                     _replace_details(p, reason=why or status, after_yes=True))  # fmt: skip
        return True
    sets: dict[str, Any] = {}
    if status == "ok":
        assert row is not None
        sets = {"replace_state": "offered", "replace_refused": None,
                "replaced_version_id": row["copied_version_id"],
                "replaced_crc64": bytes(row["crc64nvme"]).hex()}  # fmt: skip
        p["replace_expect"] = row
    elif status == "same":
        sets = {"replace_state": "same"}
    elif status == "refused":
        sets = {"replace_state": "refused", "replace_refused": why}
    elif state == "offered":
        sets = {"replace_state": None}
    if sets:
        cols = ", ".join(f"{k} = %({k})s" for k in sets)
        await db.execute(
            f"UPDATE catalog.annex_placement SET {cols}, updated_at = %(now)s"
            " WHERE placement_id = %(id)s",
            {**sets, "now": now, "id": p["placement_id"]},
        )
        p.update(sets)
    if status in ("ok", "refused"):
        await _audit(db, ctx, p, "replace_offered" if status == "ok" else "replace_refused",
                     "allow" if status == "ok" else "deny",
                     {"reason": why, "replaced_version_id": id_str(p.get("replaced_version_id")),
                      "replaced_crc64": p.get("replaced_crc64")})  # fmt: skip
    return True


def _id_list(body: dict[str, Any], key: str) -> set[int]:
    v = body.get(key, [])
    if not isinstance(v, list) or len(v) > 1000:
        raise bad(f"{key} must be a list of placement ids")
    try:
        return {int(x) for x in v}
    except (TypeError, ValueError):
        raise bad(f"{key} must be a list of placement ids") from None


async def _annex_filer(db, annex_id: int) -> dict[str, Any]:
    return await (
        await db.execute(
            "SELECT a.filer_id, fl.protocol FROM catalog.annex a"
            " JOIN catalog.filer fl ON fl.filer_id = a.filer_id WHERE a.annex_id = %s",
            (annex_id,),
        )
    ).fetchone()


async def post_tickets(ctx: Ctx) -> Result:
    annex_id = req_id(ctx.body, "annex_id")
    in_hand = _id_list(ctx.body, "in_hand")
    renew = _id_list(ctx.body, "renew")
    now = ctx.now
    due: list[tuple[dict[str, Any], dict[str, Any], bool, str]] = []
    failed: list[dict[str, Any]] = []
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        a = await _placer_annex(db, ctx.edge_id, annex_id)
        # Rows that take no ticket now (held, unreadable, in hand) don't use up
        # the ask: it goes on past them, up to a bound on how many it looks at.
        seen: list[int] = []
        while len(due) < TICKETS_PER_ASK and len(seen) < TICKET_ROWS_PER_ASK:
            rows = await (
                await db.execute(
                    """
                    SELECT p.* FROM catalog.annex_placement p
                      JOIN catalog.annex pa ON pa.annex_id = p.annex_id
                     WHERE (p.state IN ('ready', 'writing', 'verifying', 'securing')
                            OR (p.state = 'stuck' AND p.before_stuck = ANY(%(before)s)
                                AND p.placement_id = ANY(%(renew)s)))
                       AND p.target->>'kind' = 'nas'
                       AND (p.target->>'filer_id')::bigint = %(filer)s
                       AND p.placement_id <> ALL(%(seen)s)
                       AND (p.annex_id = %(annex)s OR (pa.filer_id <> %(filer)s AND %(annex)s = (
                            SELECT min(x.annex_id) FROM catalog.annex x
                             WHERE x.filer_id = %(filer)s AND x.state <> 'retired')))
                     ORDER BY p.placement_id = ANY(%(renew)s) DESC,
                              p.placement_id = ANY(%(in_hand)s), p.placement_id
                     LIMIT %(n)s FOR UPDATE OF p
                    """,
                    {
                        "filer": a["filer_id"],
                        "annex": annex_id,
                        "n": TICKETS_PER_ASK,
                        "before": list(BEFORE_GO),
                        "renew": list(renew),
                        # What is in hand needs nothing new: it never crowds out a
                        # placement nobody has started.
                        "in_hand": list(in_hand),
                        "seen": seen,
                    },
                )
            ).fetchall()
            if not rows:
                break
            for p in rows:
                if len(due) >= TICKETS_PER_ASK:
                    break
                seen.append(p["placement_id"])
                src = await _source(db, p)
                who = await _requester(db, p["requester_identity_id"])
                status, reason = await gates(
                    db, p, src, who, replace=p["on_conflict"] == "overwrite_keep_version"
                )
                if status == "hold":
                    continue
                no_overwrite = status == "ok" and reason == RULE_NO_OVERWRITE
                if no_overwrite:
                    reason = None
                same_filer = True
                if status == "ok":
                    # The file's own annex may be on another edge: its protocol
                    # and filer are all that's asked of it here.
                    pa = await _annex_filer(db, p["annex_id"])
                    same_filer = pa["filer_id"] == a["filer_id"]
                    if pa["protocol"] != a["protocol"] or _descriptor(src) is None:
                        status, reason = "fail", "acl_unsupported"
                if status == "ok" and _readable(src, now) is None:
                    continue  # the bytes aren't readable (yet): its recall comes first
                if status == "ok":
                    ok, why = await R.write_check(db, _target(p), who)
                    if not ok:
                        status, reason = "fail", "access_denied"
                if status == "fail":
                    await _fail(db, ctx, p, reason or "refused")
                    failed.append({"placement_id": id_str(p["placement_id"]), "reason": reason})
                    continue
                pid = p["placement_id"]
                fresh = p["state"] == "ready" or (pid not in in_hand and pid not in renew)
                if fresh and not await _offer_replace(
                    db, ctx, p, src, now, refuse=RULE_NO_OVERWRITE if no_overwrite else None
                ):
                    continue  # the file there is being read in Azure: asked again soon
                # In hand and renewed: the edge takes only the new credential, so its
                # ticket carries no replace.
                if p["state"] == "ready":
                    await db.execute(
                        "UPDATE catalog.annex_placement SET state = 'writing', updated_at = %s"
                        " WHERE placement_id = %s",
                        (now, pid),
                    )
                    action = "ticket"
                elif pid in renew:
                    action = "ticket_renewed"
                elif pid in in_hand:
                    continue  # under way, its credential good: no new one is minted
                else:
                    action = "ticket_again"
                due.append((p, src, same_filer, action))
    # After the commit, one at a time: one bad backend doesn't hold up every placement on
    # the filer. Without its credential a placement has no ticket: its state moved, the
    # next ask tries again, and only a ticket that was given is audited as one.
    out: list[dict[str, Any]] = []
    given: list[tuple[dict[str, Any], str]] = []
    no_credential: list[tuple[dict[str, Any], str]] = []
    for p, src, same_filer, action in due:
        try:
            out.append(await _ticket(ctx, p, src, same_filer, now))
            given.append((p, action))
        except Exception as e:  # noqa: BLE001 - whatever it was, it is this placement's alone
            no_credential.append((p, type(e).__name__, str(e)))
    if given or no_credential:
        async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
            for p, action in given:
                await _audit(db, ctx, p, action, "allow", {})
            for p, why, said in no_credential:
                if await _no_credential(db, p, why, now):
                    # As often as the problem: the edge asks every half minute.
                    log.error("ALERT annex placement %s: no read credential: %s: %s",
                              p["placement_id"], why, said)  # fmt: skip
    return Result(200, {"tickets": out, "failed": failed})


async def _no_credential(db, p: dict[str, Any], why: str, now: datetime) -> bool:
    """A problem on the annex's page, at most hourly: the edge asks every half minute.
    Whether it was said now."""
    pid = id_str(p["placement_id"])
    detail = (
        f"the read credential for this placement can't be had ({why}), so nothing is written:"
        " it is asked for again each time the edge asks. Check the file's storage backend, or"
        " cancel the placement"
    )
    cur = await db.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
        " SELECT %s, 'problem', %s, %s, %s WHERE NOT EXISTS ("
        "   SELECT 1 FROM catalog.annex_event e WHERE e.annex_id = %s AND e.kind = 'problem'"
        "    AND e.at > %s AND e.details->>'reason' = 'ticket_no_credential'"
        "    AND e.details->>'placement_id' = %s)",
        (p["annex_id"], p["file_id"], p["version_id"],
         Jsonb({"placement_id": pid, "reason": "ticket_no_credential", "detail": detail}),
         p["annex_id"], now - NO_CREDENTIAL_EVERY, pid),
    )  # fmt: skip
    return cur.rowcount > 0


def same_as_capture(value: Any, src: dict[str, Any], p: dict[str, Any], uid: Any, gid: Any,
                    file_filer: int | None, *, acls_shown: Any = False) -> bool:  # fmt: skip
    """Whether the staged ACL is provably what the catalog captured for the
    version being placed, so that nobody new can read it even if some principal
    in it has no SID: at the file's origin on its own filer, the same numeric
    uid and gid as the version's, the same mode, and the same ACL. An NFSv4 ACL is
    compared as raw bytes.

    POSIX, where the catalog holds no raw copy, rests on this. A POSIX ACL with
    only owner, owning group and other is fully determined by the uid, gid and
    mode, so with those the same the staged file is exactly the capture: the
    same accounts read it, and until P5 its owner class is the placement account
    rather than the file's owner, which is no wider. Any named entry forces a
    mask entry in a POSIX ACL (acl(5)), and that matters twice: with one, the mode's
    group bits are the mask, not the owning group's, so 0640 could mean an owning
    group with nothing. But `acl_body` keeps `mask` when it drops an unmapped named
    entry, so a capture that had any named entry, dropped or not, shows a tag
    other than the three, and is refused. A staged file with a named entry is
    refused too, whatever it names. So both sides are base entries or it fails.

    Accepted: a uid or gid reused on the same NAS for someone else since the
    capture passes this. The catalog can't see that, and neither can the NAS. The
    capture itself has the hidden-ACL blind spot for mapped groups: see `annex.md`."""
    if not p["target"].get("origin") or file_filer != int(p["target"]["filer_id"]):
        return False
    if not isinstance(uid, int) or not isinstance(gid, int) or isinstance(uid, bool):
        return False
    # The version's numbers, and the file's now: after a chown or chgrp the capture
    # says nothing about who the file's readers are.
    if src["version_uid"] != uid or src["version_gid"] != gid:
        return False
    if src["f_uid"] != uid or src["f_gid"] != gid:
        return False
    if src["version_acl_id"] is None or src["version_acl_id"] != src["acl_id"]:
        return False
    if src["version_mode"] is None or src["acl_mode"] is None:
        return False
    if not isinstance(value, dict) or not isinstance(value.get("mode"), int):
        return False
    # Setuid and setgid come back at P5: they say nothing about who reads.
    if value["mode"] & 0o777 != src["version_mode"] & 0o777:
        return False
    if src["acl_mode"] & 0o777 != src["version_mode"] & 0o777:
        return False
    kind = value.get("kind")
    if kind != src["raw_kind"]:
        return False
    if kind == "posix":
        # A server that hides POSIX ACLs looks base-only, and its 0640 may be a
        # hidden mask, so the edge says whether this one shows them. `group_sid`:
        # the same number with another mapping means the id map moved, and
        # nothing is taken on trust then.
        if not acls_shown:
            return False
        base = {"owner", "owning_group", "other"}
        tags = {e.get("tag") for e in [*(src["entries"] or []), *(value.get("entries") or [])]}
        return not tags - base and src["group_sid"] == value.get("group_sid")
    if kind == "nfs4":
        raw = value.get("raw")
        if src["raw"] is None or not isinstance(raw, str):
            return False
        try:
            return base64.b64decode(raw, validate=True) == bytes(src["raw"])
        except (ValueError, TypeError):
            return False
    return False


def staged_sets(
    value: Any, protocol: str, owner: str | None, placement_sid: str | None
) -> tuple[list[str], list[str], bool, set[str]]:
    """(read, deny, deny_first, control) of the ACL the edge read from the
    staging file. The staged group is taken as it is: what a group may read
    must not depend on P5 succeeding. An NT entry for the owner counts as the
    file's own owner only when that owner is the registered placement account."""
    body = acl_body(value, protocol)
    if body["raw_kind"] == "ntfs_sd":
        sd = parse_sd(body["raw"])
        aces = sd["dacl"]
        is_placer = bool(placement_sid) and sd["owner"] == placement_sid
        if is_placer and owner is None:
            # Whose it will be isn't known, so nothing can be proved about it.
            raise AclError("the catalog names no owner for the placement account to stand for")
        who = owner if is_placer else sd["owner"]
        if aces is not None and is_placer:
            aces = [{**a, "sid": owner if a["sid"] == sd["owner"] else a["sid"]} for a in aces]
        read, deny = deny_first_sets(who, aces)
        return read, deny, True, control_sids(who, aces) | ({who} if who else set())
    if body["raw_kind"] == "nfs4":
        aces = [{"type": e["ace"], "sid": e["sid"], "mask": e["mask"], "flags": e["flags"]}
                for e in body["entries"]]  # fmt: skip
        read, deny = deny_first_sets(owner, aces)
        return read, deny, True, control_sids(owner, aces) | ({owner} if owner else set())
    read, deny = access_sets(owner, body["group_sid"], body["entries"])
    return read, deny, False, {owner} if owner else set()


def within_catalog(value: Any, protocol: str, src: dict[str, Any], placement_sid: str | None,
                   *, proven: bool = False, covered: Iterable[bytes] = ()) -> bool:  # fmt: skip
    """Decision 51: the staged ACL never lets more people read the file. Fail
    closed: what can't be proved the same is a mismatch. `proven`: it is the
    capture of this version (`same_as_capture`).

    Decision 63: nor more than the folders above it let through. Everyone the
    staged ACL lets read or take control must pass `src["folders"]`, but for a
    folder whose ACL one of the target's own folders has too (`covered`, their
    hashes: the file sits below that one there), which keeps out the same
    people. So a file put back where it was passes; anywhere else, a folder that
    denies anyone, or doesn't let Everyone through, refuses it unless each SID
    is let through by name."""
    readers, denied, _ = captured(src)  # the captured ACL; the folders below
    # By the raw entries: the read sets call Authenticated Users Everyone.
    anyone = literally_anyone_reads(src["raw_kind"], src["entries"], sorted(readers))
    if not proven:
        # An unmapped principal with control can grant itself read.
        if unmapped_control(value):
            return False
        # Whoever an unmapped reader is, they're only known to be a reader of
        # this file if anyone at all may read it.
        if unmapped_reader(value) and not anyone:
            return False
    body = acl_body(value, protocol)
    if not anyone and literally_anyone_reads(body["raw_kind"], body["entries"], body["read"]):
        return False
    owner = src["owner_sid"]
    # The owner it will have is the file's: an NT descriptor's is judged below,
    # and on the others the edge reads it from the numbers it will set.
    if body["raw_kind"] != "ntfs_sd" and body["owner_sid"] != owner:
        return False
    read, deny, deny_first, control = staged_sets(value, protocol, owner, placement_sid)
    if not set(read) <= readers:
        return False
    folders = src.get("folders")
    if not isinstance(folders, Folders) or not folders.allow(set(read) | control, covered):
        return False
    # WRITE_DAC or WRITE_OWNER is read, one step later.
    had = control_sids(owner, _catalog_aces(src))
    if not control <= readers | had | ({owner} if owner else set()):
        return False
    if not denied:
        return True
    return denied <= set(deny) and deny_first == bool(src["deny_first"])


def _catalog_aces(src: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": e["ace"], "sid": e["sid"], "mask": e["mask"], "flags": e.get("flags", 0)}
            for e in (src["entries"] or []) if "ace" in e]  # fmt: skip


async def _go(db, ctx: Ctx, p: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str | None]:
    """Every gate again, right before P4."""
    src = await _source(db, p)
    who = await _requester(db, p["requester_identity_id"])
    status, reason = await gates(db, p, src, who)
    if status != "ok":
        return False, reason
    target_filer = await (
        await db.execute(
            "SELECT protocol, placement_sid FROM catalog.filer WHERE filer_id = %s",
            (int(p["target"]["filer_id"]),),
        )
    ).fetchone()
    protocol = target_filer["protocol"]
    # The folder's ACL as the edge reads it now: the requester may still add
    # a file there.
    try:
        fv = req(b, "folder_acl")
        fbody = acl_body(fv, protocol)
        write, wdeny = folder_write_sets(fv, fbody)
    except (AclError, ApiError) as e:
        return False, f"folder_acl_unreadable: {e}"[:64]
    if write is None:
        return False, "folder_acl_unreadable"
    can = await (
        await db.execute(
            "SELECT catalog.acl_can_write(%s::catalog.sid[], %s::catalog.sid[], %s, %s, %s) AS ok",
            (write, wdeny, who.sid or "S-1-0-0", list(who.groups), fbody["deny_first"]),
        )
    ).fetchone()
    if not can["ok"]:
        return False, "access_denied"
    # Decision 51: the staged ACL never lets more people read the file.
    try:
        value = req(b, "acl")
        file_filer = await (
            await db.execute(
                "SELECT filer_id FROM catalog.annex WHERE annex_id = %s", (src["file_annex_id"],)
            )
        ).fetchone()
        proven = same_as_capture(value, src, p, b.get("uid"), b.get("gid"),
                                 file_filer["filer_id"] if file_filer else None,
                                 acls_shown=b.get("posix_acls_shown") is True)  # fmt: skip
        placer = target_filer["placement_sid"]
        # The folders the placed file will sit below: its own, as the edge read it
        # now, and those above it, as last read (decision 63).
        covered = {bytes(fbody["sha256"])} | {
            f.sha256
            for f in await nas_folders(db, int(p["target"]["filer_id"]), p["target"]["folder"])
            if f.sha256 is not None
        }
        if not within_catalog(value, protocol, src, placer, proven=proven, covered=covered):
            if protocol == "smb" and _placer_unregistered(value, src, placer):
                return False, UNREGISTERED
            # Only if showing them is all it lacks. (With them shown, this would have
            # been proven above and passed.)
            if same_as_capture(value, src, p, b.get("uid"), b.get("gid"),
                               file_filer["filer_id"] if file_filer else None,
                               acls_shown=True) and within_catalog(
                value, protocol, src, placer, proven=True, covered=covered
            ):  # fmt: skip
                return False, ACL_HIDDEN
            return False, "acl_mismatch"
    except (AclError, ApiError, TypeError, ValueError, KeyError, IndexError, AttributeError) as e:
        return False, f"acl_unreadable: {e}"[:64]
    # Decision 64: from a filer that shows no ACLs, only a file its owner alone reads,
    # until an ArchiveAdmin acknowledges it.
    if await acls_hidden_blocks(db, p["file_id"]) and group_readable(
        staged_sets(value, protocol, src["owner_sid"], target_filer["placement_sid"])[0],
        src["owner_sid"],
    ):
        return False, ACLS_HIDDEN_FILER
    if not allowed_name(p, b.get("name"), older=older_at_origin(p, src)):
        return False, "name_not_allowed"
    # What the edge read back is what the file is, even if its report was lost.
    crc = b.get("crc64")
    if not isinstance(crc, str) or crc.lower() != bytes(src["crc64nvme"]).hex():
        return False, "crc_mismatch"
    return True, None


def _placer_unregistered(value: Any, src: dict[str, Any], placer: str | None) -> bool:
    """The staged file's owner over SMB is whoever placed it. If that isn't the
    file's owner or the registered account, this is why `go` refused."""
    sd = parse_sd(base64.b64decode(value["raw"], validate=True))
    return bool(sd["owner"]) and sd["owner"] not in (src["owner_sid"], placer)


def _scrub(v: Any) -> Any:
    """What Postgres can't hold (NUL, a lone surrogate) out of a request's text.
    A report's text is only ever noted, and a 500 here would be retried for ever."""
    if isinstance(v, str):
        return v.replace("\x00", "\ufffd").encode("utf-8", "replace").decode("utf-8")
    if isinstance(v, dict):
        return {_scrub(k): _scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    return v


async def post_step(ctx: Ctx) -> Result:
    pid = ctx.path_id("placement_id")
    b = _scrub(ctx.body)
    step = b.get("step")
    if step not in STEPS:
        raise bad("step must be one of " + ", ".join(STEPS))
    now = ctx.now
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        p = await (
            await db.execute(
                "SELECT p.* FROM catalog.annex_placement p"
                " JOIN catalog.annex a ON a.annex_id = p.annex_id"
                " JOIN catalog.filer fl ON fl.filer_id = (p.target->>'filer_id')::bigint"
                " WHERE p.placement_id = %s AND fl.edge_id = %s FOR UPDATE OF p",
                (pid, ctx.edge_id),
            )
        ).fetchone()
        if p is None:
            raise ApiError("not_found", f"placement {pid} is not this edge's")
        if step == "leftover":
            # After the end too: what a failed placement couldn't clean up.
            await db.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, file_id, details)"
                " VALUES (%s, 'problem', %s, %s)",
                (p["annex_id"], p["file_id"], Jsonb({"placement_id": id_str(pid),
                 "reason": "placement_leftover", "detail": str(b.get("detail"))[:500]})),
            )  # fmt: skip
            await _audit(db, ctx, p, "leftover", "deny", {"detail": str(b.get("detail"))[:500]})
            return Result(200, {"placement_id": id_str(pid), "proceed": False, "reason": None})
        if step in REPLACE_FACTS:
            # A fact about the NAS: recorded whenever it comes (after a refusal ended
            # the placement too: the old file was put back after it).
            took = await _replace_fact(db, ctx, p, step, b)
            return Result(200, {"placement_id": id_str(pid), "state": p["state"],
                                "proceed": False,
                                "reason": None if took else "not_in_order"})  # fmt: skip
        if p["state"] in ENDED:
            # Reported again after a restart: answered as it is. Nothing new
            # is ever allowed for a placement that ended. One abandoned while
            # the edge was away: what it says now is recorded, and changes
            # nothing (the next scan finds the file).
            if p["fail_reason"] in ("abandoned", "annex_retired"):
                await _audit(db, ctx, p, "late_report", "deny", {"step": step,
                             "name": b.get("name"), "outcome": b.get("outcome")})  # fmt: skip
                # In a folder no annex scans, nothing else would say so.
                name = b.get("name") if isinstance(b.get("name"), str) else None
                folder = p["target"].get("folder") or ""
                path = "/".join(x for x in [folder, name] if x) if name else None
                await db.execute(
                    "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id,"
                    " details) VALUES (%s, 'problem', %s, %s, %s, %s)",
                    (p["annex_id"], path, p["file_id"], p["version_id"],
                     Jsonb({"placement_id": id_str(pid), "reason": "placement_late_report",
                            "step": str(step), "filer_id": p["target"].get("filer_id"),
                            "ended_as": p["fail_reason"],
                            "detail": "the edge reported after this placement ended;"
                                      " the file may be on the NAS, and isn't held"})),
                )  # fmt: skip
            return Result(200, {"placement_id": id_str(pid), "state": p["state"],
                                "proceed": False, "reason": p["fail_reason"]})  # fmt: skip
        if not (step == "stuck" and p["state"] == "stuck"):
            await db.execute(
                "UPDATE catalog.annex_placement SET last_report_at = %s WHERE placement_id = %s",
                (now, pid),
            )
        if p["state"] == "stuck" and step not in ("stuck", "failed"):
            # The problem cleared: it goes on from its real step.
            p["state"] = p["before_stuck"] or "writing"
            await db.execute(
                "UPDATE catalog.annex_placement SET state = %s, before_stuck = NULL,"
                " fail_detail = NULL, last_edge_report = NULL, updated_at = %s"
                " WHERE placement_id = %s",
                (p["state"], now, pid),
            )
        proceed, reason = True, None
        if step == "go":
            if p["state"] not in GO_FROM:
                proceed, reason = False, "not_in_order"
                await _audit(db, ctx, p, "go", "deny", {"reason": reason, "name": b.get("name")})
                return Result(200, {"placement_id": id_str(pid), "state": p["state"],
                                    "proceed": False, "reason": reason})  # fmt: skip
            proceed, reason = await _go(db, ctx, p, b)
            await _audit(db, ctx, p, "go", "allow" if proceed else "deny",
                         {"reason": reason, "name": b.get("name")})  # fmt: skip
            if proceed:
                await db.execute(
                    "UPDATE catalog.annex_placement SET state = 'placing', crc64 = %s,"
                    " updated_at = %s WHERE placement_id = %s",
                    (b["crc64"].lower(), now, pid),
                )
            elif reason != "paused":
                detail = ""
                if reason == UNREGISTERED:
                    detail = (
                        "the account placing files on this NAS isn't registered: an ArchiveAdmin"
                        " registers its SID on the filer (PATCH /v1/filers/"
                        f"{p['target']['filer_id']}, placement_sid)"
                    )
                elif reason == ACL_HIDDEN:
                    detail = (
                        "someone it can't name may read this file, and that is allowed only for an"
                        " exact copy of what was captured. This NAS doesn't show POSIX ACLs to the"
                        " edge, so 0640 may mean something else and the copy can't be proved"
                        " exact. Give the file's group a SID in AD, or place it by hand"
                    )
                elif reason == ACLS_HIDDEN_FILER:
                    detail = (
                        "the NAS this file was captured from shows no ACLs, so who its group"
                        " is may be wider than it reads (decision 64). Only a copy its owner"
                        " alone reads is placed until an ArchiveAdmin acknowledges it on the"
                        " filer"
                    )
                await _fail(db, ctx, p, reason or "refused", detail)
        elif step in REPLACE_ASKS:
            hold, proceed, reason = await _replace_ask(db, ctx, p, step, b)
            return Result(200, {"placement_id": id_str(pid), "state": p["state"],
                                "proceed": proceed, "reason": reason, "hold": hold})  # fmt: skip
        elif step == "filled":
            cur = await db.execute(
                "UPDATE catalog.annex_placement SET state = 'verifying', updated_at = %s"
                " WHERE placement_id = %s AND state = 'writing'",
                (now, pid),
            )
            if cur.rowcount:
                await _audit(db, ctx, p, "filled", "allow", {})
            else:
                proceed, reason = False, "not_in_order"
        elif step == "read_back":
            proceed, reason = await _read_back(db, ctx, p, b)
        elif step in ("linked", "owned"):
            if p["state"] == "placing":
                keys = ("name", "ino", "method", "owner_restored")
                await _audit(db, ctx, p, step, "allow", {k: b.get(k) for k in keys})
            else:
                proceed, reason = False, "not_in_order"
        elif step == "failed":
            reason = str(b.get("reason") or "failed")[:64]
            await _fail(db, ctx, p, reason, str(b.get("detail") or "")[:500])
        elif step == "stuck":
            # Where it is, for an abandon to say what the file was left like.
            where = {"at": b.get("at"), "name": b.get("name"), "reason": b.get("reason")}
            await db.execute(
                "UPDATE catalog.annex_placement SET before_stuck = CASE WHEN state = 'stuck'"
                " THEN before_stuck ELSE state END, state = 'stuck',"
                " fail_detail = left(%s, 500), last_edge_report = %s, updated_at = %s"
                " WHERE placement_id = %s",
                (str(b.get("reason") or "stuck"), Jsonb(where), now, pid),
            )
            if p["state"] != "stuck":
                await db.execute(
                    "INSERT INTO catalog.annex_event (annex_id, kind, file_id, details)"
                    " VALUES (%s, 'problem', %s, %s)",
                    (p["annex_id"], p["file_id"], Jsonb({"placement_id": id_str(pid),
                     "reason": "placement_stuck", "detail": str(b.get("reason"))[:500]})),
                )  # fmt: skip
                await _audit(db, ctx, p, "stuck", "deny", {"reason": b.get("reason")})
        elif p["state"] != "placing":  # done, with no `go` before it
            proceed, reason = False, "not_in_order"
            await _audit(db, ctx, p, "done", "deny", {"reason": reason})
        else:
            await _done(db, ctx, p, b)
    return Result(200, {"placement_id": id_str(pid), "proceed": proceed, "reason": reason})


def _replace_details(p: dict[str, Any], **more: Any) -> dict[str, Any]:
    """What every replace audit row and event carries: the replaced version and its CRC."""
    return {"replaced_version_id": id_str(p.get("replaced_version_id")),
            "replaced_crc64": p.get("replaced_crc64"), **more}  # fmt: skip


async def _replace_ask(
    db, ctx: Ctx, p: dict[str, Any], step: str, b: dict[str, Any]
) -> tuple[bool, bool, str | None]:
    """`replace_go` (before the old file is hidden) and `replace_delete_go` (after
    the grace, before the new file's link, and again before the delete): every gate
    again. (hold, proceed, reason): a hold is waited out with the old file as it is;
    any other refusal is definite, and the edge puts the old file back. A gate of the
    placement itself that fails ends the placement, as at `go`."""
    now = ctx.now
    want = "offered" if step == "replace_go" else "hidden"
    phase = b.get("phase") if step == "replace_delete_go" else None
    if step == "replace_delete_go" and phase not in ("place", "delete"):
        raise bad("phase must be place or delete")

    async def answer(hold: bool, proceed: bool, reason: str | None) -> tuple:
        await _audit(db, ctx, p, step, "allow" if proceed else "deny",
                     _replace_details(p, reason=reason, hold=hold, phase=phase))  # fmt: skip
        return hold, proceed, reason

    if p["state"] != "placing" or p.get("replace_state") != want:
        return await answer(False, False, "not_in_order")
    if phase == "delete" and p.get("replace_place_at") is None:
        return await answer(False, False, "not_in_order")
    if phase == "delete" and b.get("name") != p["target"]["name"]:
        # The new file isn't at the file's own name: nothing replaced the old one,
        # which is never deleted (#98's E1). The edge keeps it.
        await _refuse_replace(db, p, "not_origin_name", now)
        return await answer(False, False, "not_origin_name")
    src = await _source(db, p)
    who = await _requester(db, p["requester_identity_id"])
    status, reason = await gates(db, p, src, who, replace=True)
    if status == "hold":
        return await answer(True, False, reason)
    if status == "ok" and reason == RULE_NO_OVERWRITE:
        # The running rule no longer says overwrite (S1): at `replace_go` the file
        # takes a `-restored` name, at `place` the old file goes back, at `delete`
        # it is kept aside. Never deleted.
        await _refuse_replace(db, p, reason, now, nothing_hidden=step == "replace_go")
        return await answer(False, False, reason)
    if status != "ok":
        if phase == "delete":
            # The new file has its name already: the placement goes on, and the old
            # file is kept (`replace_aside`), never deleted on a refusal.
            await _refuse_replace(db, p, reason or "refused", now)
            return await answer(False, False, reason)
        await _fail(db, ctx, p, reason or "refused")
        p["state"] = "failed"
        return await answer(False, False, reason)
    rs, why, _ = await RP.decide(db, p, src, now, recheck=True)
    if rs == "not_yet":
        return await answer(True, False, why)
    if rs != "ok":
        why = why or "changed"
        await _refuse_replace(db, p, why, now, nothing_hidden=step == "replace_go")
        return await answer(False, False, why)
    if step == "replace_delete_go":
        # The service keeps the grace too, whatever the edge does.
        a = await (
            await db.execute(
                "SELECT grace_minutes FROM catalog.annex WHERE annex_id = %s", (p["annex_id"],)
            )
        ).fetchone()
        grace = timedelta(minutes=max(1, int(a["grace_minutes"] if a else 15)))
        hidden_at = p.get("replace_hidden_at")
        if hidden_at is None or hidden_at + grace > now:
            return await answer(True, False, "grace")
    # Each yes is recorded: the facts that follow are taken only after theirs (S3).
    col = {"replace_go": "replace_go_at", "place": "replace_place_at",
           "delete": "replace_delete_at"}[phase or step]  # fmt: skip
    await db.execute(
        f"UPDATE catalog.annex_placement SET {col} = %s, updated_at = %s WHERE placement_id = %s",
        (now, now, p["placement_id"]),
    )
    p[col] = now
    return await answer(False, True, None)


async def _refuse_replace(
    db, p: dict[str, Any], why: str, now: datetime, *, nothing_hidden: bool = False
) -> None:
    """A definite no to a replace: why, and, before the hide, its end (after it, the
    edge's report of where it put the old file ends it). Once a `replace_go` yes is
    recorded the edge may have hidden the file, so only why is recorded: the edge's
    fact closes the replace (the confirm pass, C7)."""
    sets: dict[str, Any] = {"replace_refused": why[:64]}
    if nothing_hidden and p.get("replace_go_at") is None:
        sets["replace_state"] = "refused"
    cols = ", ".join(f"{k} = %({k})s" for k in sets)
    await db.execute(
        f"UPDATE catalog.annex_placement SET {cols}, updated_at = %(now)s"
        " WHERE placement_id = %(id)s",
        {**sets, "now": now, "id": p["placement_id"]},
    )
    p.update(sets)


#: Where each fact is taken from (#98's S3), besides a replay of itself: the hide only
#: after a `replace_go` yes, the delete only after a delete-phase yes; the old file put
#: back, kept aside or gone from the offer (the edge found it changed, or a user's file
#: under the hidden name, before it said hidden) or from the hide. `refused`: the
#: service's own no to `replace_go`, after which the edge says it took `-restored`.
FACT_FROM = {
    "replace_hidden": ("offered",),
    "replace_put_back": ("offered", "hidden", "refused"),
    "replace_aside": ("offered", "hidden"),
    "replace_gone": ("offered", "hidden"),
    "replaced": ("hidden",),
}
#: The replace state each fact leaves.
FACT_STATE = {
    "replace_hidden": "hidden",
    "replace_put_back": "put_back",
    "replace_aside": "kept_aside",
    "replace_gone": "gone",
    "replaced": "replaced",
}


async def _replace_fact(db, ctx: Ctx, p: dict[str, Any], step: str, b: dict[str, Any]) -> bool:
    """What the edge did to the old file. Each is recorded once, and only in its order:
    a replay of the fact recorded is a no-op (`True`); a fact out of order is refused,
    audited and changes nothing (`False`). Taken after the placement ended too: the
    old file may be put back after that."""
    now = ctx.now
    target = p["target"]
    folder = target.get("folder") or ""
    own = target.get("name")
    path = "/".join(x for x in [folder, own] if x)
    state = p.get("replace_state")
    sets: dict[str, Any] = {}
    kind = step
    extra: dict[str, Any] = {}
    if step == "replace_hidden":
        name = b.get("hidden_name")
        if name != RP.hidden_name(p["placement_id"], own):
            raise bad("hidden_name must be this placement's .omnifile-replacing- name")
    elif step == "replace_aside":
        name = b.get("name")
        if not (isinstance(name, str) and 0 < len(name.encode()) <= 255 and "/" not in name
                and "\x00" not in name and "-replaced-" in name):  # fmt: skip
            raise bad("name must be the old file's -replaced- name")
    if state == FACT_STATE[step]:
        return True  # recorded already: a replay
    allowed = state in FACT_FROM[step]
    if step == "replace_hidden" and p.get("replace_go_at") is None:
        allowed = False
    if step == "replaced" and p.get("replace_delete_at") is None:
        allowed = False
    if not allowed:
        await _audit(db, ctx, p, step, "deny",
                     _replace_details(p, reason="not_in_order", replace_state=state))  # fmt: skip
        return False
    if step == "replace_hidden":
        sets = {"replace_state": "hidden", "replace_hidden_name": name, "replace_hidden_at": now}
        extra = {"hidden_name": name}
    elif step == "replace_put_back":
        why = str(b.get("reason") or p.get("replace_refused") or "put_back")[:64]
        sets = {"replace_state": "put_back", "replace_refused": why}
        extra = {"reason": why}
    elif step == "replace_gone":
        sets = {"replace_state": "gone"}
    elif step == "replace_aside":
        why = str(b.get("reason") or "changed")[:64]
        sets = {"replace_state": "kept_aside", "replaced_aside_name": name, "replace_refused": why}
        aside = "/".join(x for x in [folder, name] if x)
        extra = {"reason": why, "kept_as": aside}
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
            " VALUES (%s, 'problem', %s, %s, %s, %s)",
            (p["annex_id"], path, p["file_id"], p["version_id"],
             Jsonb(_replace_details(p, placement_id=id_str(p["placement_id"]),
                   reason="replace_kept_aside", kept_as=aside, why=why,
                   detail=f"{path} was to be replaced by an older version. The file that was"
                          f" there couldn't be deleted ({why}), so it is kept as {aside}."
                          " Nothing was deleted."))),
        )  # fmt: skip
    elif step == "replaced":
        sets = {"replace_state": "replaced"}
    cols = ", ".join(f"{k} = %({k})s" for k in sets)
    await db.execute(
        f"UPDATE catalog.annex_placement SET {cols}, updated_at = %(now)s"
        " WHERE placement_id = %(id)s",
        {**sets, "now": now, "id": p["placement_id"]},
    )
    p.update(sets)
    await db.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (p["annex_id"], kind, path, p["file_id"], p["version_id"],
         Jsonb(_replace_details(p, placement_id=id_str(p["placement_id"]), **extra))),
    )  # fmt: skip
    await _audit(db, ctx, p, step, "deny" if step == "replace_aside" else "allow",
                 _replace_details(p, **extra))  # fmt: skip
    return True


async def _read_back(db, ctx: Ctx, p: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str | None]:
    """The edge's read-back: the CRC is the version's, or the placement fails."""
    if p["state"] not in ("writing", "verifying"):
        return False, "not_in_order"
    src = await _source(db, p)
    want = bytes(src["crc64nvme"]).hex() if src and src["crc64nvme"] is not None else None
    crc = b.get("crc64")
    if not isinstance(crc, str) or crc.lower() != want:
        await _fail(db, ctx, p, "crc_mismatch", f"read back {crc!r}"[:500])
        return False, "crc_mismatch"
    await db.execute(
        "UPDATE catalog.annex_placement SET state = 'securing', crc64 = %s, updated_at = %s"
        " WHERE placement_id = %s",
        (crc, ctx.now, p["placement_id"]),
    )
    await _audit(db, ctx, p, "read_back", "allow", {"crc64": crc})
    return True, None


async def _done(db, ctx: Ctx, p: dict[str, Any], b: dict[str, Any]) -> None:
    now = ctx.now
    # An edge of another version may say what this one doesn't know, and a 400
    # here would be retried for ever. What can't be used is noted, and claims
    # nothing for the file, which the next scan finds as it is.
    note: dict[str, Any] = {}
    outcome = b.get("outcome")
    if outcome not in OUTCOMES:
        note["outcome_reported"] = str(outcome)[:64].replace("\x00", "")
        outcome = "unknown"
    name = b.get("name")
    name_ok = (
        isinstance(name, str) and 0 < len(name) <= 255 and "/" not in name and "\x00" not in name
    )
    if not name_ok:
        note["name_reported"] = str(name)[:255].replace("\x00", "")
        name = None
    src = await _source(db, p)
    # `done` reports a fact: the file has this name. If the ticket's names no
    # longer cover it (the head moved since), it is noted, and nothing is
    # claimed for it.
    allowed = name_ok and allowed_name(p, name, older=older_at_origin(p, src))
    folder = p["target"]["folder"]
    path = "/".join(x for x in [folder, name] if x) if name_ok else None
    hold_until = now + timedelta(days=p["hold_days"]) if p["hold_days"] else None
    attrs = _attrs_or_none(b.get("attrs"))
    # What the edge journalled at P5 is the version's bytes, or the file isn't
    # that copy: a user changed it, and the next scan sees the change. For
    # `already_there` the edge read that file back and compared its size and CRC.
    is_copy = (
        attrs is not None
        and src is not None
        and attrs.get("nas_file_id") is not None
        and attrs.get("size") == src["size"]
    ) or (outcome == "already_there" and src is not None)
    ino = str(attrs["nas_file_id"]) if attrs and attrs.get("nas_file_id") is not None else None
    stored = outcome
    if outcome == "restored_name" and p.get("replace_state") in ("refused", "put_back",
                                                                  "kept_aside"):  # fmt: skip
        # An overwrite that didn't happen says why (A3.4).
        stored = f"overwrite_refused_{p.get('replace_refused') or 'refused'}"[:64]
    unconfirmed = outcome == "replaced" and p.get("replace_state") not in ("replaced", "gone")
    if unconfirmed:
        # `replaced` without the edge's fact that the old file went (S8): never
        # recorded as a replace. Nothing is claimed for the file; the next scan
        # finds what is at the name.
        note["replace_unconfirmed"] = p.get("replace_state")
        stored = "replace_unconfirmed"
    await db.execute(
        "UPDATE catalog.annex_placement SET state = 'placed', outcome = %s, final_name = %s,"
        " final_path = %s, hold_until = %s, placed_ino = %s, done_at = %s, updated_at = %s"
        " WHERE placement_id = %s",
        (stored, name, path, hold_until, ino, now, now, p["placement_id"]),
    )
    a = await (
        await db.execute("SELECT * FROM catalog.annex WHERE annex_id = %s", (p["annex_id"],))
    ).fetchone()
    kind = "placed"
    if not allowed:
        note["name_not_allowed_now"] = True
    # `done` reports a fact, so whatever goes wrong claiming the file for the
    # catalog is noted, and the placement still ends. Each claim has a savepoint.
    if outcome == "unknown" or unconfirmed:
        pass  # what it means isn't known, so nothing is claimed for the file
    elif p["target"].get("origin") and name == p["target"]["name"]:
        # An older version under the file's own name (A3.4: it replaced the file there,
        # or the name was free and the rule says overwrite) is the file's new head.
        older = src is not None and p["version_id"] != src["current_version_id"]
        try:
            async with db.transaction():
                if older and outcome == "already_there":
                    came_back = False  # those bytes are the head already: nothing to claim
                elif older:
                    head = (
                        allowed
                        and is_copy
                        and await _replaced(ctx, db, p, src, attrs or {}, hold_until, now)
                    )
                    came_back = bool(head)
                    if head:
                        note["new_head_version_id"] = id_str(head)
                        note.update(_replace_details(p))
                else:
                    came_back = (
                        allowed
                        and is_copy
                        and await _returned(ctx, db, p, attrs or {}, hold_until, now)
                    )
        except (_rejected(), ApiError, psycopg.Error, TypeError, ValueError, KeyError) as e:
            log.exception("annex placement %s: done couldn't claim the file", p["placement_id"])
            came_back = False
            note["not_claimed"] = type(e).__name__
        if came_back:
            kind = "returned"
        else:
            note["not_the_copy"] = True
            await db.execute(
                "UPDATE catalog.annex_file SET hold_until = greatest(hold_until, %s),"
                " updated_at = now() WHERE annex_id = %s AND file_id = %s",
                (hold_until, p["annex_id"], p["file_id"]),
            )
    elif allowed and outcome != "already_there" and attrs is not None:
        try:
            async with db.transaction():
                note["annexed_as"] = await _adopt(ctx, db, p, src, path, attrs, hold_until, is_copy)
        except (_rejected(), ApiError, psycopg.Error, TypeError, ValueError, KeyError) as e:
            # Something else is there in the catalog: the scan reports the clash.
            if not isinstance(e, _rejected() | ApiError):
                log.exception("annex placement %s: done couldn't annex it", p["placement_id"])
            note["not_annexed"] = str(getattr(e, "code", type(e).__name__))
    await db.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (p["annex_id"], kind, path, p["file_id"], p["version_id"],
         Jsonb({"placement_id": id_str(p["placement_id"]), "outcome": outcome,
                "filer_id": p["target"]["filer_id"], "owner_restored": b.get("owner_restored"),
                "pointer_deleted": b.get("pointer_deleted"), **note})),
    )  # fmt: skip
    if p["recall_id"] is not None:
        await db.execute(
            "UPDATE catalog.recall SET state = 'available', completed_at = %s, updated_at = %s"
            " WHERE recall_id = %s AND state = 'downloading'",
            (now, now, p["recall_id"]),
        )
    await _audit(db, ctx, p, "placed", "allow", {"outcome": outcome, "path": path,
                                                 "annex": a["name"] if a else None})  # fmt: skip


def _attrs_or_none(value: Any) -> dict[str, Any] | None:
    """What the edge journalled at P5, if it is shaped like attributes: whatever
    arrives, `done` lands, and a value a column can't hold claims nothing."""
    if not isinstance(value, dict):
        return None
    for k in ("size", "mtime_ns", "ctime_ns"):
        v = value.get(k)
        if v is not None and (
            not isinstance(v, int) or isinstance(v, bool) or not -(2**63) <= v < 2**63
        ):
            return None
    ino = value.get("nas_file_id")
    if ino is not None and (not isinstance(ino, str) or "\x00" in ino or len(ino) > 255):
        return None
    return value


def _rejected() -> type[Exception]:
    from .annex import Rejected

    return Rejected


async def _returned(ctx: Ctx, db, p: dict[str, Any], attrs: dict[str, Any],
                    hold_until: datetime | None, now: datetime) -> bool:  # fmt: skip
    """Back under its own name where it came from: the annex's file again, as
    the copy it already has (no recopy), held from leaving again, its age
    counted from now. A tombstoned file comes back too, unless its name has
    been taken since: then it stays as it is, and the scan reports the clash."""
    # The version's location first, as `copied` does and the verify worker's trigger
    # does: a verification between that trigger and its commit isn't lost then.
    await db.execute(
        "SELECT 1 FROM catalog.location WHERE version_id = %s AND file_id = %s"
        " AND role = 'primary' AND state = 'committed' FOR SHARE",
        (p["version_id"], p["file_id"]),
    )
    # A purge asked for after its `go` and still pending: taken, like
    # a name someone else has. The constraint on `annex_file` says the same.
    af = await (
        await db.execute(
            "SELECT purge_requested_at, purged_at FROM catalog.annex_file"
            " WHERE annex_id = %s AND file_id = %s FOR UPDATE",
            (p["annex_id"], p["file_id"]),
        )
    ).fetchone()
    if af is None or af["purge_requested_at"] is not None or af["purged_at"] is not None:
        return False
    f = await (
        await db.execute(
            "SELECT parent_id, name, deleted_at FROM catalog.file WHERE file_id = %s FOR UPDATE",
            (p["file_id"],),
        )
    ).fetchone()
    if f["deleted_at"] is not None:
        taken = await (
            await db.execute(
                "SELECT 1 FROM catalog.file WHERE parent_id = %s AND name = %s"
                " AND deleted_at IS NULL AND file_id <> %s",
                (f["parent_id"], f["name"], p["file_id"]),
            )
        ).fetchone()
        if taken is not None:
            return False
        await db.execute(
            "UPDATE catalog.file SET deleted_at = NULL WHERE file_id = %s", (p["file_id"],)
        )
        # The head is still the tombstone: a version that says it is back, or
        # as-of listings miss a file that's there.
        from .annex import Batch

        await Batch(ctx, db, {}).version(
            p["file_id"], "metadata", f"annex:{p['annex_id']}:returned:{p['placement_id']}"
        )
    # A verified version is on the NAS again: its refusals, and an ArchiveAdmin's stop
    # and acknowledgement of them, end here as at every other verification (migration
    # 0058). Its location is locked above, so this is what the UPDATE below sees too.
    verified = await (
        await db.execute(
            "SELECT 1 FROM catalog.location l WHERE l.version_id = %s AND l.role = 'primary'"
            "   AND l.state = 'committed' AND l.verified LIMIT 1",
            (p["version_id"],),
        )
    ).fetchone()
    if verified is not None:
        await db.execute(
            "UPDATE catalog.annex_file SET copy_refusals = 0, copy_refused_version_id = NULL,"
            " copy_stopped_at = NULL, copy_stopped_by = NULL, copy_refused_ack_at = NULL,"
            " copy_refused_ack_by = NULL WHERE annex_id = %s AND file_id = %s",
            (p["annex_id"], p["file_id"]),
        )
    await db.execute(
        "UPDATE catalog.annex_file SET nas_state = 'present', removed_at = NULL,"
        " purge_refused_at = NULL,"
        " gone_since = NULL, hold_until = greatest(hold_until, %s), first_seen = %s,"
        " nas_file_id = coalesce(%s, nas_file_id),"
        " size = coalesce(%s, size), mtime_ns = coalesce(%s, mtime_ns),"
        " ctime_ns = coalesce(%s, ctime_ns), copied_version_id = %s,"
        # As its location says, and nothing else: unverified stays unverified, and the
        # worker's trigger marks it when it is read back.
        " verified_version_id = (SELECT l.version_id FROM catalog.location l"
        "   WHERE l.version_id = %s AND l.role = 'primary' AND l.state = 'committed'"
        "     AND l.verified LIMIT 1),"
        " verified_at = (SELECT l.verified_at FROM catalog.location l"
        "   WHERE l.version_id = %s AND l.role = 'primary' AND l.state = 'committed'"
        "     AND l.verified LIMIT 1),"
        " copied_size = coalesce(%s, copied_size),"
        " copied_mtime_ns = coalesce(%s, copied_mtime_ns),"
        " copied_ctime_ns = coalesce(%s, copied_ctime_ns), updated_at = now()"
        " WHERE annex_id = %s AND file_id = %s",
        (hold_until, now, attrs.get("nas_file_id"), attrs.get("size"), attrs.get("mtime_ns"),
         attrs.get("ctime_ns"), p["version_id"], p["version_id"], p["version_id"],
         attrs.get("size"), attrs.get("mtime_ns"),
         attrs.get("ctime_ns"), p["annex_id"], p["file_id"]),
    )  # fmt: skip
    return True


async def _replaced(ctx: Ctx, db, p: dict[str, Any], src: dict[str, Any], attrs: dict[str, Any],
                    hold_until: datetime | None, now: datetime) -> int | None:  # fmt: skip
    """An older version under the file's own name at its origin (A3.4; decision 34's
    overwrite, on a NAS): a new head version that references the placed version's
    blob (no copy), the replaced version kept in the history. The annex's file is
    present again as that copy, held. None if it can't be claimed (purged, or the
    name taken in the catalog): the next scan finds the file as it is."""
    from .data import commit_version, insert_version

    await db.execute(
        "SELECT 1 FROM catalog.location WHERE version_id = %s AND file_id = %s"
        " AND role = 'primary' AND state = 'committed' FOR SHARE",
        (p["version_id"], p["file_id"]),
    )
    af = await (
        await db.execute(
            "SELECT purge_requested_at, purged_at FROM catalog.annex_file"
            " WHERE annex_id = %s AND file_id = %s FOR UPDATE",
            (p["annex_id"], p["file_id"]),
        )
    ).fetchone()
    if af is None or af["purge_requested_at"] is not None or af["purged_at"] is not None:
        return None
    if src["location_id"] is None:
        return None
    f = await (
        await db.execute(
            "SELECT * FROM catalog.file WHERE file_id = %s FOR UPDATE", (p["file_id"],)
        )  # fmt: skip
    ).fetchone()
    if f["deleted_at"] is not None:
        taken = await (
            await db.execute(
                "SELECT 1 FROM catalog.file WHERE parent_id = %s AND name = %s"
                " AND deleted_at IS NULL AND file_id <> %s",
                (f["parent_id"], f["name"], p["file_id"]),
            )
        ).fetchone()
        if taken is not None:
            return None
        await db.execute(
            "UPDATE catalog.file SET deleted_at = NULL WHERE file_id = %s", (p["file_id"],)
        )
    size = int(src["size"])
    mtime = attrs.get("mtime_ns") if attrs.get("mtime_ns") is not None else src["mtime_ns"]
    vid = await insert_version(db, ctx, f, f"placement-{p['placement_id']}-replace", size, mtime,
                               src["blake3"], src["crc64nvme"])  # fmt: skip
    await db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
        " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
        " crc64nvme, etag, verified, verified_at, verified_by, committed_at)"
        " SELECT %s, %s, backend_id, container, object_key, backend_version_id, 'primary',"
        " 'committed', logical_tier, storage_class, %s, crc64nvme, etag, verified,"
        " verified_at, verified_by, now() FROM catalog.location WHERE location_id = %s",
        (p["file_id"], vid, size, src["location_id"]),
    )
    await commit_version(db, p["file_id"], vid, size, mtime, src["blake3"], src["crc64nvme"],
                         remote=True)  # fmt: skip
    verified = await (
        await db.execute(
            "SELECT verified_at FROM catalog.location WHERE version_id = %s AND role = 'primary'"
            " AND state = 'committed' AND verified LIMIT 1",
            (vid,),
        )
    ).fetchone()
    if verified is not None:
        await db.execute(
            "UPDATE catalog.annex_file SET copy_refusals = 0, copy_refused_version_id = NULL,"
            " copy_stopped_at = NULL, copy_stopped_by = NULL, copy_refused_ack_at = NULL,"
            " copy_refused_ack_by = NULL WHERE annex_id = %s AND file_id = %s",
            (p["annex_id"], p["file_id"]),
        )
    await db.execute(
        "UPDATE catalog.annex_file SET nas_state = 'present', removed_at = NULL,"
        " purge_refused_at = NULL, gone_since = NULL, hold_until = greatest(hold_until, %s),"
        " first_seen = %s, nas_file_id = coalesce(%s, nas_file_id), size = %s,"
        " mtime_ns = coalesce(%s, mtime_ns), ctime_ns = coalesce(%s, ctime_ns),"
        " copied_version_id = %s, verified_version_id = %s, verified_at = %s,"
        " copied_size = %s, copied_mtime_ns = coalesce(%s, copied_mtime_ns),"
        " copied_ctime_ns = coalesce(%s, copied_ctime_ns), copied_at = now(), updated_at = now()"
        " WHERE annex_id = %s AND file_id = %s",
        (hold_until, now, attrs.get("nas_file_id"), size, attrs.get("mtime_ns"),
         attrs.get("ctime_ns"), vid, vid if verified else None,
         verified["verified_at"] if verified else None, size, attrs.get("mtime_ns"),
         attrs.get("ctime_ns"), p["annex_id"], p["file_id"]),
    )  # fmt: skip
    return vid


async def _adopt(ctx: Ctx, db, p: dict[str, Any], src: dict[str, Any], path: str,
                 attrs: dict[str, Any], hold_until: datetime | None,
                 is_copy: bool) -> str | None:  # fmt: skip
    """A placed file under another name, in a folder an annex covers: a new
    annexed file, held. It references the version's blob when it is that
    copy and in the same dataset, so it isn't copied again."""
    from .annex import ANNEX_SQL, Batch

    annexes = await (
        await db.execute(
            ANNEX_SQL + " WHERE a.filer_id = %s AND a.state <> 'retired'"
            " AND (a.path = '' OR %s LIKE replace(replace(a.path, '%%', '\\%%'), '_', '\\_')"
            "      || '/%%') ORDER BY length(a.path) DESC LIMIT 1",
            (int(p["target"]["filer_id"]), path),
        )
    ).fetchall()
    if not annexes:
        return None  # a restore folder no annex scans: nothing removes it
    a = annexes[0]
    rel = path[len(a["path"]) + 1 :] if a["path"] else path
    version = src if is_copy and a["dataset_id"] == src["dataset_id"] else None
    return await Batch(ctx, db, a).adopt_placed(rel, p, attrs, hold_until, version)
