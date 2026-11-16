"""Filers and annexes (portal-api.md section 5.23; docs/design/annex.md, phase A1).

- `GET/POST /v1/filers`, `GET /v1/filers/{filer_id}`, `GET …/health`: register a
  NAS (any NFS or SMB server; ONTAP and PowerScale are generic ones) on an
  edge. Each filer gets one `backend` row of kind `nas`; the service never
  reads or writes NAS bytes.
- `GET/POST /v1/annexes`, `GET/PATCH /v1/annexes/{annex_id}`: an annex in Copy
  mode (the only mode in A1): a directory of a filer whose files the edge
  copies into a dataset. Its rule set is the fixed Copy template, approved by
  the annex's Owners at creation.
- `GET /v1/annexes/{annex_id}/events`, `…/files`, `…/timeline`, `…/problems`:
  the Annex page.
- `POST /v1/annexes/{annex_id}/files/{file_id}/purge`: an ArchiveAdmin purges
  the kept copy of a file deleted on the NAS (decisions 40 and 51).
- `GET/PUT /v1/settings/annex-defaults`: the quiet period and scan schedules
  new annexes start with.

Who:
- Filers: ArchiveAdmin registers; ArchiveAdmin and DataSteward read; apps with
  `Archive.Read` or `Archive.Adopt` read.
- Annexes: an ArchiveAdmin, or a DataSteward in the annex's Owners group,
  creates one; apps with `Archive.Adopt` too, naming the person who owns it
  (`owner`, a SID the catalog knows, in the Owners group: `422 owner_required`,
  `403 owner_lacks_access`). ArchiveAdmins and DataStewards see every annex,
  Owners theirs. Settings: Owners and ArchiveAdmins.
- Files and their events are shown as the NAS's own ACLs allow (decision 28):
  each file's captured ACL decides, roles never grant read. Apps act at
  dataset level.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from ..annex import rules as annex_rules
from ..annex.acl import EVERYONE, EVERYONE_LIKE, AclError, valid_sid
from ..annex.placement_states import open_sql
from ..annex.readback import MAX_REFUSALS, alerting
from ..annex.rules import copy_template
from ..api.annex import PLACEMENT_REPORT_FRESH
from ..api.validate import rfc3339
from ..apiclients.access import owner_can_see_dataset, require_owner_access
from ..catalog.read_set import narrowing, readable_page_sql
from .access import acl_params
from .context import Ctx, Principal, WebResult, audit, etag, if_match, page
from .problems import WebError, forbidden, invalid, not_found, role_required

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
VENDORS = ("generic", "ontap", "powerscale")
PROTOCOLS = ("smb", "nfs3", "nfs4")
CREDENTIAL_MODES = ("machine_account", "service_keytab", "sec_sys")
EVENT_SOURCES = ("scan", "smb_notify")
MODES = ("copy", "archive_after", "move_now", "custom")
SETTINGS = {
    "quiet_minutes": (1, 1440),
    "quick_scan_minutes": (1, 10080),
    "full_scan_minutes": (5, 43200),
    "reconcile_days": (1, 90),
}
FILER_FIELDS = {"name", "vendor", "host", "protocol", "export", "edge_id", "credential_mode",
                "event_source"}  # fmt: skip
ANNEX_FIELDS = {"name", "filer_id", "path", "mode", "dataset_id", "dataset_path", "owners_sid",
                "owner", "settings", "archive_after_days", "rules", "rationale"}  # fmt: skip
PATCH_FIELDS = {"state", "settings", "deleted_retention_days", "removals_paused",
                "placements_paused",
                "pointer_files", "grace_minutes", "removal_rate_per_hour",
                "removal_window"}  # fmt: skip
#: Removal settings A2.2's pipeline obeys (recorded from A2.1): only an
#: ArchiveAdmin loosens them; any Owner may pause.
REMOVAL_LIMITS = {"grace_minutes": (1, 1440), "removal_rate_per_hour": (1, 100000)}
DEFAULT_REMOVALS = {"removal_rate_per_hour": (1, 100000), "grace_minutes": (1, 1440),
                    "dry_run_days": (1, 90)}  # fmt: skip
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
EVENT_KINDS_PROBLEMS = ("skipped", "copy_failed", "problem", "drift", "purge_refused")


# --- who ------------------------------------------------------------------------


def _admin_user(who: Principal) -> None:
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def _filer_reader(who: Principal) -> None:
    if who.is_app:
        if not who.has("Archive.Read", "Archive.Adopt"):
            raise role_required("Archive.Read", "Archive.Adopt")
    elif not who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")


def _sees(who: Principal, a: dict[str, Any]) -> bool:
    if who.is_app:
        return who.has("Archive.Read", "Archive.Adopt")
    return who.has("ArchiveAdmin", "DataSteward") or a["owners_sid"] in who.all_sids


def _owns(who: Principal, a: dict[str, Any]) -> bool:
    if who.is_app:
        return who.has("Archive.Adopt")
    return who.has("ArchiveAdmin") or a["owners_sid"] in who.all_sids


# --- shapes -----------------------------------------------------------------------


def filer_json(f: dict[str, Any], who: Principal | None = None) -> dict[str, Any]:
    return {
        "filer_id": str(f["filer_id"]),
        "name": f["name"],
        "vendor": f["vendor"],
        "host": f["host"],
        "protocol": f["protocol"],
        "export": f["export"],
        "edge_id": str(f["edge_id"]),
        "credential_mode": f["credential_mode"],
        # sec=sys: the NAS trusts the edge's address, not a proven identity.
        "host_trusted": f["credential_mode"] == "sec_sys",
        "event_source": f["event_source"],
        "health": f["health"],
        "health_checked_at": rfc3339(f["health_checked_at"]),
        "created_at": rfc3339(f["created_at"]),
        # The account that writes placed files: at `go`, the only staged owner
        # that counts as the file's own owner.
        "placement_sid": f["placement_sid"],
        # What a person sees before registering it (portal-api.md): an
        # ArchiveAdmin's, who alone can act on it.
        "placement_account_report": _report_json(f)
        if who is not None and not who.is_app and who.has("ArchiveAdmin")
        else None,
        # Decision 65: whether its share or export limits access beyond the files'
        # permissions, as an ArchiveAdmin answered; unanswered hides its annexes' files.
        "access": _access_json(f),
        # Decision 64: the server shows no ACLs (since when), and who acknowledged it.
        "acls": {
            "hidden_since": rfc3339(f.get("acls_hidden_since")),
            "acknowledged_by": f.get("acls_hidden_ack_by"),
            "acknowledged_at": rfc3339(f.get("acls_hidden_ack_at")),
            "blocks_grants": f.get("acls_hidden_since") is not None
            and f.get("acls_hidden_ack_at") is None,
        },
    }


def _access_json(f: dict[str, Any]) -> dict[str, Any]:
    limited = f.get("access_limited")
    groups = f.get("access_groups") or []
    if isinstance(groups, str):  # a catalog.sid[] read with SELECT *: '{S-1-...,S-1-...}'
        groups = [g for g in groups.strip("{}").split(",") if g]
    return {
        "answered": limited is not None,
        "limited": limited,
        "groups": list(groups),
        "confirmed_by": f.get("access_confirmed_by"),
        "confirmed_at": rfc3339(f.get("access_confirmed_at")),
    }


def _access_body(b: dict[str, Any]) -> tuple[bool, list[str]]:
    """`{limited, groups}`: limited with the AD groups the share lets in, or not."""
    if not isinstance(b, dict):
        raise invalid("access", "an object: {limited, groups}")
    _only(b, {"limited", "groups"}, "access")
    limited = b.get("limited")
    if not isinstance(limited, bool):
        raise invalid("access.limited", "true or false")
    groups = b.get("groups", [])
    if not isinstance(groups, list) or len(groups) > 100:
        raise invalid("access.groups", "a list of at most 100 SIDs")
    out = []
    for g in groups:
        try:
            sid = valid_sid(g, "access.groups")
        except AclError as e:
            raise invalid("access.groups", str(e)) from None
        if sid == EVERYONE or sid in EVERYONE_LIKE:
            raise invalid("access.groups", "AD groups, not Everyone: that limits nothing")
        if sid not in out:
            out.append(sid)
    if limited and not out:
        raise invalid("access.groups", "name the AD groups the share or export lets in")
    if not limited and out:
        raise invalid("access.groups", "only when access is limited")
    return limited, sorted(out)


#: What winbind calls one account. It names a machine account a user.
PLACEMENT_KINDS = ("user", "computer")


def _report_problem(f: dict[str, Any], now: datetime) -> tuple[str, str] | None:
    """(code, why) a report can't be registered from, or None. On every filer
    read too, so a screen says what the route would."""
    if f["protocol"] != "smb":
        return "not_smb", "only an SMB filer has a placement account"
    report, at = f["placement_account"], f["placement_account_at"]
    if report is None or at is None:
        return "no_report", "this filer's edge hasn't reported its placement account"
    if report.get("error") or not report.get("sid"):
        return "no_report", f"this filer's edge can't name its account: {report.get('error')}"
    if now - at > PLACEMENT_REPORT_FRESH:
        return "stale_report", (
            f"this filer's edge last reported its account at {rfc3339(at)}, more than"
            f" {int(PLACEMENT_REPORT_FRESH.total_seconds() // 3600)} hours ago"
        )
    if report.get("kind") not in PLACEMENT_KINDS:
        return "not_an_account", f"the reported account is a {report.get('kind')}, not one account"
    return None


def _report_json(f: dict[str, Any]) -> dict[str, Any] | None:
    report, at = f["placement_account"], f["placement_account_at"]
    if f["protocol"] != "smb":
        return None
    problem = _report_problem(f, datetime.now(UTC))
    r = report or {}
    return {
        "sid": r.get("sid"),
        "kind": r.get("kind"),
        "name": r.get("name"),
        "error": r.get("error"),
        "reported_at": rfc3339(at),
        "can_register": problem is None,
        "why_not": problem[0] if problem else None,
    }


def _sid_or_none(b: dict[str, Any], key: str) -> str | None:
    v = b.get(key)
    if v is None:
        return None
    try:
        return valid_sid(v, key)
    except AclError as e:
        raise invalid(key, str(e)) from None


def annex_json(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "annex_id": str(a["annex_id"]),
        "name": a["name"],
        "mode": a["mode"],
        "state": a["state"],
        "filer_id": str(a["filer_id"]),
        "filer_name": a["filer_name"],
        "unc": _where(a),
        "path": a["path"],
        "dataset_id": str(a["dataset_id"]),
        "dataset_name": a["dataset_name"],
        "dataset_path": a["source_path"],
        "edge_id": str(a["edge_id"]),
        "owners_sid": a["owners_sid"],
        "settings": {k: a[k] for k in SETTINGS},
        "deleted_retention_days": a["deleted_retention_days"],
        "ruleset_version": a["ruleset_version"],
        # `proposed` until a person approves an app's annex (anti-pattern 7).
        "ruleset_state": a["ruleset_state"],
        # What the A2.2 removal pipeline will obey; A2.1 removes nothing.
        # The placements' kill switch (A3.3): an ArchiveAdmin's, audited.
        "placements_paused": a["placements_paused"],
        "removals": {
            "paused": a["removals_paused"],
            "dry_run_until": rfc3339(a["dry_run_until"]),
            "rate_per_hour": a["removal_rate_per_hour"],
            "window": a["removal_window"],
            "grace_minutes": a["grace_minutes"],
            "pointer_files": a["pointer_files"],
            # Live: an ArchiveAdmin confirmed the dry run of the running
            # version, and nothing pauses them (decision 59).
            "enabled": bool(a["removals_live"]),
            "confirmed_version": a["removals_confirmed_version"],
            "confirmed_at": rfc3339(a["removals_confirmed_at"]),
        },
        "pending_proposals": a["pending_proposals"],
        "host_trusted": a["credential_mode"] == "sec_sys",
        "baseline": {
            "at": rfc3339(a["baseline_at"]),
            "files": a["baseline_files"],
            "bytes": a["baseline_bytes"],
        },
        "last_quick_scan_at": rfc3339(a["last_quick_scan_at"]),
        "last_full_scan_at": rfc3339(a["last_full_scan_at"]),
        "last_reconcile_at": rfc3339(a["last_reconcile_at"]),
        "last_event_at": rfc3339(a["last_event_at"]),
        "created_at": rfc3339(a["created_at"]),
        "entity_version": a["entity_version"],
    }


def _where(a: dict[str, Any]) -> str:
    """Where the annex is, as people write it: \\\\host\\share\\path, or host:/export/path."""
    sub = a["path"]
    if a["protocol"] == "smb":
        return (
            "\\\\" + a["host"] + "\\" + a["export"] + ("\\" + sub.replace("/", "\\") if sub else "")
        )
    export = a["export"].rstrip("/")
    return f"{a['host']}:{export}" + (f"/{sub}" if sub else "")


ANNEX_SQL = """
SELECT a.*, s.path AS source_path, fl.name AS filer_name, fl.host, fl.protocol, fl.export,
       fl.access_limited,
       fl.credential_mode, d.name AS dataset_name,
       (SELECT r.state FROM catalog.annex_ruleset r WHERE r.annex_id = a.annex_id
         ORDER BY r.version DESC LIMIT 1) AS ruleset_state,
       (SELECT count(*) FROM catalog.annex_ruleset r WHERE r.annex_id = a.annex_id
         AND r.state = 'proposed') AS pending_proposals,
       (a.state = 'active' AND NOT a.removals_paused
        AND NOT (SELECT removals_paused FROM catalog.annex_defaults)
        AND a.dry_run_until IS NOT NULL AND a.dry_run_until <= now()
        AND a.removals_confirmed_version = a.ruleset_version
        AND coalesce((SELECT r.removes AND r.decided_as_archive_admin
                       FROM catalog.annex_ruleset r
                       WHERE r.annex_id = a.annex_id AND r.state = 'active'), false))
           AS removals_live
  FROM catalog.annex a
  JOIN catalog.source s ON s.source_id = a.source_id
  JOIN catalog.filer fl ON fl.filer_id = a.filer_id
  JOIN catalog.dataset d ON d.dataset_id = a.dataset_id
"""


def _only(body: dict[str, Any], allowed: set[str], what: str) -> None:
    extra = set(body) - allowed
    if extra:
        raise invalid(sorted(extra)[0], f"is not a {what} field")


def _id(body: dict[str, Any], field: str) -> int:
    v = body.get(field)
    if isinstance(v, str) and v.isdigit() and len(v) <= 19:
        return int(v)
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v
    raise invalid(field, "an id is required")


def _enum(body: dict[str, Any], field: str, allowed: tuple[str, ...], default: str) -> str:
    v = body.get(field, default)
    if v not in allowed:
        raise invalid(field, "one of " + ", ".join(allowed))
    return v


def _path(v: Any, field: str) -> str:
    """'' or a relative folder path: names separated by '/', no '.', '..'."""
    if v is None or v in ("", "/"):
        return ""
    if not isinstance(v, str) or len(v) > 1024:
        raise invalid(field, "a folder path of at most 1024 characters")
    parts = [p for p in v.strip("/").split("/")]
    if any(p in ("", ".", "..") or "\x00" in p or "\\" in p for p in parts):
        raise invalid(field, "a folder path like surveys/2026, with no '.' or '..'")
    return "/".join(parts)


def _settings(v: Any, base: dict[str, int]) -> dict[str, int]:
    if v is None:
        return dict(base)
    if not isinstance(v, dict):
        raise invalid("settings", "an object")
    out = dict(base)
    for k, n in v.items():
        if k not in SETTINGS:
            raise invalid(f"settings.{k}", "is not an annex setting")
        lo, hi = SETTINGS[k]
        if isinstance(n, bool) or not isinstance(n, int) or not lo <= n <= hi:
            raise invalid(f"settings.{k}", f"an integer from {lo} to {hi}")
        out[k] = n
    if out["full_scan_minutes"] < out["quick_scan_minutes"]:
        raise invalid("settings.full_scan_minutes", "at least the quick scan's interval")
    return out


# --- filers -----------------------------------------------------------------------


async def list_filers(ctx: Ctx) -> WebResult:
    _filer_reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        rows = await (await db.execute("SELECT * FROM catalog.filer ORDER BY name")).fetchall()
    return WebResult(200, page([filer_json(r, ctx.who) for r in rows], None))


async def _filer(db, filer_id: int) -> dict[str, Any]:
    row = await (
        await db.execute("SELECT * FROM catalog.filer WHERE filer_id = %s", (filer_id,))
    ).fetchone()
    if row is None:
        raise not_found("filer")
    return row


async def get_filer(ctx: Ctx) -> WebResult:
    _filer_reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        f = await _filer(db, ctx.path_id("filer_id"))
    return WebResult(200, filer_json(f, ctx.who), headers={"ETag": etag(f["entity_version"])})


async def get_filer_health(ctx: Ctx) -> WebResult:
    """The edge's last mount check (it reports one each time an annex unit
    mounts the filer, and with every scan's failure)."""
    _filer_reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        f = await _filer(db, ctx.path_id("filer_id"))
        n = await (
            await db.execute(
                "SELECT count(*) AS n FROM catalog.annex WHERE filer_id = %s"
                " AND state <> 'retired'",
                (f["filer_id"],),
            )
        ).fetchone()
    health = f["health"] or {}
    return WebResult(
        200,
        {
            "filer_id": str(f["filer_id"]),
            "checked_at": rfc3339(f["health_checked_at"]),
            "ok": health.get("ok") if f["health"] is not None else None,
            "read_only": health.get("read_only"),
            "readable": health.get("readable"),
            "sec": health.get("sec"),
            "error": health.get("error"),
            "annexes": n["n"],
        },
    )


async def add_filer(ctx: Ctx) -> WebResult:
    """`POST /v1/filers`: a NAS on one of the edges, with its `nas` backend."""
    _admin_user(ctx.who)
    b = ctx.body
    _only(b, FILER_FIELDS, "filer")
    name = b.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise invalid("name", "1 to 63 letters, digits, '.', '_' or '-'")
    host = b.get("host")
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise invalid("host", "a host name or address")
    protocol = _enum(b, "protocol", PROTOCOLS, "")
    export = b.get("export")
    if not isinstance(export, str) or not export or len(export) > 1024 or "\x00" in export:
        raise invalid("export", "the SMB share name or the NFS export path")
    if protocol == "smb" and any(c in export for c in '/\\:*?"<>|'):
        raise invalid("export", "an SMB share name, like geo")
    if protocol != "smb" and not export.startswith("/"):
        raise invalid("export", "an NFS export path, like /export/geo")
    vendor = _enum(b, "vendor", VENDORS, "generic")
    mode = _enum(b, "credential_mode", CREDENTIAL_MODES, "machine_account")
    if mode == "sec_sys" and protocol == "smb":
        raise invalid("credential_mode", "sec_sys is for NFS; SMB uses Kerberos")
    source = _enum(b, "event_source", EVENT_SOURCES, "smb_notify" if protocol == "smb" else "scan")
    if source == "smb_notify" and protocol != "smb":
        raise invalid("event_source", "SMB change notify needs an SMB filer")
    edge_id = _id(b, "edge_id")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        edge = await (
            await db.execute("SELECT state FROM catalog.edge WHERE edge_id = %s", (edge_id,))
        ).fetchone()
        if edge is None or edge["state"] == "revoked":
            raise invalid("edge_id", "no such edge")
        taken = await (
            await db.execute(
                "SELECT name FROM catalog.filer WHERE name = %s"
                " OR (host = %s AND protocol = %s AND export = %s)",
                (name, host, protocol, export),
            )
        ).fetchone()
        if taken is not None:
            raise WebError("name_taken", f"filer {taken['name']} is that name or that export")
        backend = await (
            await db.execute(
                "SELECT catalog.nas_backend_add(%s, %s) AS backend_id",
                (
                    f"nas-{name}",
                    Jsonb({"host": host, "protocol": protocol, "export": export, "vendor": vendor}),
                ),
            )
        ).fetchone()
        if backend["backend_id"] is None:
            raise WebError("name_taken", f"a backend named nas-{name} exists")
        f = await (
            await db.execute(
                "INSERT INTO catalog.filer (name, vendor, host, protocol, export, event_source,"
                " edge_id, credential_mode, backend_id, created_by)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    name,
                    vendor,
                    host,
                    protocol,
                    export,
                    source,
                    edge_id,
                    mode,
                    backend["backend_id"],
                    ctx.who.actor,
                ),
            )  # fmt: skip
        ).fetchone()
        await audit(db, ctx.who, "filer.add", details=filer_json(f))
    return WebResult(201, filer_json(f), headers={"ETag": etag(f["entity_version"])})


async def _check_placement_account(db, f: dict[str, Any], sid: str | None, now: datetime) -> None:
    """At `go` this account's entries count as the file's own owner's, and stay
    after P5, so it must be the one account this filer's edge places files with:
    what that edge currently reports, a user or a machine account, and no other.
    Each refusal says which it was (fail closed)."""
    if sid is None:
        return
    if not sid.startswith("S-1-5-21-"):
        raise invalid("placement_sid", "well_known: the SID of one domain account, not this one")
    problem = _report_problem(f, now)
    if problem is not None:
        raise invalid("placement_sid", f"{problem[0]}: {problem[1]}")
    reported = f["placement_account"]["sid"]
    if sid != reported:
        raise invalid("placement_sid", f"different_sid: this filer's edge reports {reported}")
    # The catalog may know better, if it knows this SID at all.
    row = await (
        await db.execute("SELECT kind FROM catalog.identity WHERE sid = %s", (sid,))
    ).fetchone()
    if row is not None and row["kind"] not in PLACEMENT_KINDS:
        raise invalid("placement_sid", f"not_an_account: the catalog says it is a {row['kind']}")


async def patch_filer(ctx: Ctx) -> WebResult:
    """`PATCH /v1/filers/{id}` `{placement_sid}`: which account places files on
    this NAS, or null for none. ArchiveAdmin, freshly signed in, with If-Match,
    and audited with the old value: at `go` that account's entries count as the
    file's own owner's. Only what the filer's own edge currently reports can be
    registered, and a person does it: the report never registers anything."""
    _admin_user(ctx.who)
    ctx.svc.auth.require_recent_auth(ctx.who)
    b = ctx.body
    _only(b, {"placement_sid", "access", "acls_hidden_acknowledged"}, "filer")
    if not {"placement_sid", "access", "acls_hidden_acknowledged"} & set(b):
        raise invalid("placement_sid", "required: a SID, or null to clear it; or access")
    ack = b.get("acls_hidden_acknowledged")
    if "acls_hidden_acknowledged" in b and ack is not True:
        raise invalid("acls_hidden_acknowledged", "true: a report that the ACLs show clears it")
    sid = _sid_or_none(b, "placement_sid") if "placement_sid" in b else None
    access = _access_body(b["access"]) if "access" in b else None
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        f = await (
            await db.execute(
                "SELECT * FROM catalog.filer WHERE filer_id = %s FOR UPDATE",
                (ctx.path_id("filer_id"),),
            )
        ).fetchone()
        if f is None:
            raise not_found("filer")
        if_match(ctx.request, f["entity_version"])
        if "placement_sid" in b:
            await _check_placement_account(db, f, sid, ctx.now)
            old = f["placement_sid"]
            f = await (
                await db.execute(
                    "UPDATE catalog.filer SET placement_sid = %s,"
                    " entity_version = entity_version + 1 WHERE filer_id = %s RETURNING *",
                    (sid, f["filer_id"]),
                )
            ).fetchone()
            await audit(db, ctx.who, "filer.placement_account",
                        details={"filer_id": str(f["filer_id"]), "placement_sid": sid,
                                 "was": old, "reported": f["placement_account"]})  # fmt: skip
        if ack is True:
            if f["acls_hidden_since"] is None:
                raise invalid(
                    "acls_hidden_acknowledged",
                    "this filer's NAS shows its ACLs: nothing to acknowledge",
                )
            f = await (
                await db.execute(
                    "UPDATE catalog.filer SET acls_hidden_ack_by = %s, acls_hidden_ack_at = %s,"
                    " entity_version = entity_version + 1 WHERE filer_id = %s RETURNING *",
                    (ctx.who.actor, ctx.now, f["filer_id"]),
                )
            ).fetchone()
            # Decision 64: that access there rests on mode bits alone, with their name.
            await audit(db, ctx.who, "filer.acls_hidden",
                        details={"filer_id": str(f["filer_id"]),
                                 "hidden_since": rfc3339(f["acls_hidden_since"])})  # fmt: skip
        if access is not None:
            was = _access_json(f)
            f = await (
                await db.execute(
                    "UPDATE catalog.filer SET access_limited = %s, access_groups = %s,"
                    " access_confirmed_by = %s, access_confirmed_at = %s,"
                    " entity_version = entity_version + 1 WHERE filer_id = %s RETURNING *",
                    (access[0], access[1], ctx.who.actor, ctx.now, f["filer_id"]),
                )
            ).fetchone()
            # Decision 65: the answer, with their name, every time it is confirmed.
            await audit(db, ctx.who, "filer.access",
                        details={"filer_id": str(f["filer_id"]), "limited": access[0],
                                 "groups": access[1], "was": was})  # fmt: skip
    return WebResult(200, filer_json(f, ctx.who), headers={"ETag": etag(f["entity_version"])})


# --- annexes ------------------------------------------------------------------------


async def _annex(db, annex_id: int, who: Principal, *, lock: bool = False) -> dict[str, Any]:
    row = await (
        await db.execute(
            ANNEX_SQL + " WHERE a.annex_id = %s" + (" FOR UPDATE OF a" if lock else ""),
            (annex_id,),
        )
    ).fetchone()
    if row is None or not _sees(who, row):
        raise not_found("annex")
    return row


async def _owner_identity(
    db, who: Principal, b: dict[str, Any], owners_sid: str, dataset_id: int, now: datetime
) -> int:
    """The person who owns the annex's rule set (annex.md section 4.3): the
    signed-in user, or, for an app, the `owner` it names. For an app both
    that owner and the API client's own owner must be able to see the
    dataset (apiclients/access.py), and the owner must be in the Owners
    group, which is a group: never the owner alone."""
    if not who.is_app:
        if who.identity_id is None:
            raise forbidden("your AD identity is not known yet; sign in again")
        if not who.has("ArchiveAdmin") and owners_sid not in who.all_sids:
            raise WebError("owner_lacks_access", "you are not in the annex's Owners group")
        return who.identity_id
    owner = b.get("owner")
    if owner is None:
        raise WebError("owner_required", "name the person who owns this annex (owner: a SID)")
    if not isinstance(owner, str) or not _SID.match(owner):
        raise invalid("owner", "a SID")
    if owner == owners_sid:
        raise invalid("owners_sid", "the AD group that owns the annex, not its owner alone")
    await require_owner_access(db, who, dataset_id, now)
    row = await (
        await db.execute(
            "SELECT identity_id, kind, sid, group_sids FROM catalog.identity WHERE sid = %s",
            (owner,),
        )
    ).fetchone()
    if row is None or row["kind"] != "user":
        raise WebError("owner_lacks_access", "owner is not a person the catalog knows")
    if owners_sid not in (row["group_sids"] or []):
        raise WebError("owner_lacks_access", "owner is not in the annex's Owners group")
    if not await owner_can_see_dataset(db, row["identity_id"], dataset_id, now):
        raise WebError(
            "owner_lacks_access",
            "owner can't see the dataset: they need ArchiveAdmin or DataSteward (seen at a"
            " portal sign-in in the last 30 days), or to be an Owner of one of its shares",
        )
    return row["identity_id"]


async def add_annex(ctx: Ctx) -> WebResult:
    """`POST /v1/annexes`: Copy mode. The edge's unit starts on its next poll."""
    who, b = ctx.who, ctx.body
    if who.is_app:
        if not who.has("Archive.Adopt"):
            raise role_required("Archive.Adopt")
    elif not who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")
    _only(b, ANNEX_FIELDS, "annex")
    name = b.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise invalid("name", "1 to 63 letters, digits, '.', '_' or '-'")
    mode = b.get("mode", "copy")
    if mode not in MODES:
        raise invalid("mode", "one of " + ", ".join(MODES))
    days = b.get("archive_after_days")
    if mode == "archive_after" and (isinstance(days, bool) or not isinstance(days, int)
                                    or not 1 <= days <= 36500):  # fmt: skip
        raise invalid("archive_after_days", "Archive after N days: N from 1 to 36500")
    if mode == "custom" and not isinstance(b.get("rules"), dict):
        raise invalid("rules", "Custom rules need a rule set body")
    rationale = b.get("rationale")
    if rationale is not None and (not isinstance(rationale, str) or len(rationale) > 2000):
        raise invalid("rationale", "up to 2000 characters")
    owners = b.get("owners_sid")
    if not isinstance(owners, str) or not _SID.match(owners):
        raise invalid("owners_sid", "the AD group that owns the annex, as a SID")
    path = _path(b.get("path"), "path")
    ds_path = _path(b.get("dataset_path"), "dataset_path")
    filer_id, dataset_id = _id(b, "filer_id"), _id(b, "dataset_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        owner_id = await _owner_identity(db, who, b, owners, dataset_id, ctx.now)
        defaults = await (await db.execute("SELECT * FROM catalog.annex_defaults")).fetchone()
        settings = _settings(b.get("settings"), {k: defaults[k] for k in SETTINGS})
        f = await (
            await db.execute("SELECT * FROM catalog.filer WHERE filer_id = %s", (filer_id,))
        ).fetchone()
        if f is None:
            raise invalid("filer_id", "no such filer")
        ds = await (
            await db.execute(
                "SELECT d.dataset_id, d.edge_id, b.kind,"
                " (SELECT count(*) FROM catalog.share s"
                "   WHERE s.dataset_id = d.dataset_id) AS shares"
                " FROM catalog.dataset d JOIN catalog.backend b ON b.backend_id = d.backend_id"
                " WHERE d.dataset_id = %s FOR UPDATE OF d",
                (dataset_id,),
            )
        ).fetchone()
        if ds is None:
            raise invalid("dataset_id", "no such dataset")
        if ds["kind"] in ("s3", "nas"):
            raise invalid("dataset_id", "copies go to Azure in v1: pick an Azure dataset")
        if ds["edge_id"] != f["edge_id"]:
            raise invalid("dataset_id", "the dataset must be on the filer's edge")
        if ds["shares"]:
            # A1: an annex's copies are never served by the edge (section 6.3).
            raise WebError("dataset_shared", "the dataset is shared; give the annex its own")
        prefix = f"{path}/" if path else ""
        # One directory, one annex: none inside another on the same export.
        overlap = await (
            await db.execute(
                "SELECT a.name FROM catalog.annex a WHERE a.filer_id = %s AND a.state <> 'retired'"
                " AND (a.path = %s OR %s = '' OR a.path = ''"
                "      OR left(%s, length(a.path) + 1) = a.path || '/'"
                "      OR left(a.path, length(%s) + 1) = %s || '/')",
                (filer_id, path, path, path, path, path),
            )
        ).fetchone()
        if overlap is not None:
            raise WebError("source_conflict", f"annex {overlap['name']} covers that directory")
        taken = await (
            await db.execute("SELECT 1 FROM catalog.annex WHERE name = %s", (name,))
        ).fetchone()
        if taken is not None:
            raise WebError("name_taken", f"an annex named {name} exists")
        src = await (
            await db.execute(
                "INSERT INTO catalog.source (dataset_id, path, backend_id, container, prefix,"
                " owners_sid, kind) VALUES (%s, %s, %s, %s, %s, %s, 'annex')"
                " ON CONFLICT (backend_id, container, prefix) DO NOTHING RETURNING source_id",
                (dataset_id, ds_path, f["backend_id"], f["export"], prefix, owners),
            )
        ).fetchone()
        if src is None:
            raise WebError("source_conflict", "that directory is already a source")
        a = await (
            await db.execute(
                "INSERT INTO catalog.annex (name, source_id, filer_id, dataset_id, edge_id, path,"
                " mode, owners_sid, quiet_minutes, quick_scan_minutes, full_scan_minutes,"
                " reconcile_days, ruleset_version, created_by)"
                " VALUES (%s, %s, %s, %s, %s, %s, 'copy', %s, %s, %s, %s, %s, 1, %s)"
                " RETURNING annex_id",
                (
                    name,
                    src["source_id"],
                    filer_id,
                    dataset_id,
                    f["edge_id"],
                    path,
                    owners,
                    settings["quiet_minutes"],
                    settings["quick_scan_minutes"],
                    settings["full_scan_minutes"],
                    settings["reconcile_days"],
                    who.actor,
                ),
            )  # fmt: skip
        ).fetchone()
        annex_id = a["annex_id"]
        # The rule set for the chosen mode (annex.md section 4.4). A person
        # creating a Copy annex approves it by creating it (they are an Owner,
        # or an ArchiveAdmin). An app's stays `proposed`, and the edge doesn't
        # start it, until a person approves it (anti-pattern 7). A removing
        # mode starts copying at once under the Copy template, and its own
        # rule set waits as a proposal for an ArchiveAdmin (decision 5).
        from . import routes_annex_rules as rr

        a_row = await _annex(db, annex_id, _AllSeeing)
        if mode == "custom":
            wanted = rr._checked(b["rules"], a_row)
        else:
            try:
                wanted = annex_rules.normalize(
                    annex_rules.template(mode, dataset_id, days), dataset_id
                )
            except annex_rules.RulesError as e:
                raise invalid(e.field, str(e)) from None
            problems = annex_rules.checks(wanted, quiet_minutes=a_row["quiet_minutes"])
            if problems:
                raise WebError("rules_check_failed", "the rule set can't be saved as it is",
                               rule_checks=problems)  # fmt: skip
        await rr.require_locations(db, who, a_row, wanted, owner_id)
        if who.is_app:
            await db.execute("UPDATE catalog.annex SET ruleset_version = NULL WHERE annex_id = %s",
                             (annex_id,))  # fmt: skip
            await rr.insert_proposal(db, who, a_row, wanted, owner_id, rationale=rationale,
                                     version=1)  # fmt: skip
        else:
            await db.execute(
                "INSERT INTO catalog.annex_ruleset (annex_id, version, body, state,"
                " owner_identity_id, proposed_via, decided_by_identity_id, decided_at,"
                " decision_note) VALUES (%s, 1, %s, 'active', %s, 'portal', %s, now(), %s)",
                (annex_id, Jsonb(annex_rules.normalize(copy_template(dataset_id), dataset_id)),
                 owner_id, owner_id, "the Copy template, approved at creation"),
            )  # fmt: skip
            if mode != "copy":
                await rr.insert_proposal(db, who, a_row, wanted, owner_id, rationale=rationale)
            if mode in ("archive_after", "move_now"):
                await db.execute(
                    "UPDATE catalog.annex SET pointer_files = %s WHERE annex_id = %s",
                    (mode == "archive_after", annex_id),
                )
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, details) VALUES (%s, 'created', %s)",
            (annex_id, Jsonb({"by": who.actor, "mode": "copy"})),
        )
        row = await _annex(db, annex_id, _AllSeeing)
        await audit(db, who, "annex.create", dataset_id=dataset_id,
                    details={"annex_id": str(annex_id), "name": name, "filer_id": str(filer_id),
                             "path": path, "mode": "copy", "owners_sid": owners})  # fmt: skip
    return WebResult(201, annex_json(row), headers={"ETag": etag(row["entity_version"])})


class _AllSeeingPrincipal:
    is_app = True

    @staticmethod
    def has(*_roles: str) -> bool:
        return True


_AllSeeing: Any = _AllSeeingPrincipal()


async def list_annexes(ctx: Ctx) -> WebResult:
    who = ctx.who
    if who.is_app and not who.has("Archive.Read", "Archive.Adopt"):
        raise role_required("Archive.Read", "Archive.Adopt")
    async with ctx.svc.catalog.read() as db:
        rows = await (await db.execute(ANNEX_SQL + " ORDER BY a.name")).fetchall()
    return WebResult(200, page([annex_json(r) for r in rows if _sees(who, r)], None))


async def get_annex(ctx: Ctx) -> WebResult:
    async with ctx.svc.catalog.read() as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        c = await (
            await db.execute(
                """
                SELECT count(*) FILTER (WHERE gone_since IS NULL) AS present,
                       count(*) FILTER (WHERE gone_since IS NULL AND copied_version_id IS NOT NULL
                                        AND copied_size = size AND copied_mtime_ns = mtime_ns)
                           AS copied,
                       count(*) FILTER (WHERE gone_since IS NULL AND verified_version_id IS NOT NULL
                                        AND verified_version_id = copied_version_id) AS verified,
                       count(*) FILTER (WHERE gone_since IS NOT NULL AND purged_at IS NULL) AS gone,
                       count(*) FILTER (WHERE purged_at IS NOT NULL) AS purged,
                       coalesce(sum(size) FILTER (WHERE gone_since IS NULL), 0) AS bytes,
                       coalesce(sum(copied_size) FILTER (WHERE gone_since IS NULL
                                AND verified_version_id = copied_version_id), 0)
                           AS bytes_verified
                  FROM catalog.annex_file WHERE annex_id = %s
                """,
                (a["annex_id"],),
            )
        ).fetchone()
        problems = await (
            await db.execute(
                "SELECT count(*) AS n FROM catalog.annex_event WHERE annex_id = %s"
                " AND kind = ANY(%s) AND at > now() - interval '30 days'",
                (a["annex_id"], list(EVENT_KINDS_PROBLEMS)),
            )
        ).fetchone()
        f = await _filer(db, a["filer_id"])
    out = annex_json(a)
    out["counts"] = {
        "present": c["present"],
        "copied": c["copied"],
        "verified": c["verified"],
        "pending": max(c["present"] - c["copied"], 0),
        "gone": c["gone"],
        "purged": c["purged"],
        "bytes": int(c["bytes"]),
        "bytes_verified": int(c["bytes_verified"]),
        "problems_30d": problems["n"],
    }
    out["filer"] = filer_json(f, ctx.who)
    out["can_manage"] = _owns(ctx.who, a)
    out["can_purge"] = not ctx.who.is_app and ctx.who.has("ArchiveAdmin")
    return WebResult(200, out, headers={"ETag": etag(a["entity_version"])})


async def patch_annex(ctx: Ctx) -> WebResult:
    """Settings, pause and resume, retire. The mode never changes (a new
    annex is a new rule set; A2 brings the others)."""
    b = ctx.body
    _only(b, PATCH_FIELDS, "annex settings")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who, lock=True)
        if not _owns(ctx.who, a):
            raise forbidden("only the annex's Owners and ArchiveAdmins change it")
        # An app's change, retiring included, is its client owner's to make.
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        if_match(ctx.request, a["entity_version"])
        sets: dict[str, Any] = {}
        if "settings" in b:
            sets.update(_settings(b["settings"], {k: a[k] for k in SETTINGS}))
        if "deleted_retention_days" in b:
            v = b["deleted_retention_days"]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)
                                  or not 1 <= v <= 36500):  # fmt: skip
                raise invalid("deleted_retention_days", "null (keep until purged) or 1 to 36500")
            if v is not None and (ctx.who.is_app or not ctx.who.has("ArchiveAdmin")):
                # A retention purges copies with nobody looking: ArchiveAdmin's
                # call, made with a fresh sign-in.
                raise role_required("ArchiveAdmin")
            if v is not None:
                ctx.svc.auth.require_recent_auth(ctx.who)
            sets["deleted_retention_days"] = v
        if "removals_paused" in b:
            v = b["removals_paused"]
            if not isinstance(v, bool):
                raise invalid("removals_paused", "true or false")
            if not v and a["removals_paused"]:
                # Resuming removals is an ArchiveAdmin's call, freshly signed in.
                if ctx.who.is_app or not ctx.who.has("ArchiveAdmin"):
                    raise role_required("ArchiveAdmin")
                ctx.svc.auth.require_recent_auth(ctx.who)
            sets["removals_paused"] = v
        if "placements_paused" in b:
            v = b["placements_paused"]
            if not isinstance(v, bool):
                raise invalid("placements_paused", "true or false")
            if ctx.who.is_app or not ctx.who.has("ArchiveAdmin"):
                raise role_required("ArchiveAdmin")
            sets["placements_paused"] = v
        if "pointer_files" in b:
            if not isinstance(b["pointer_files"], bool):
                raise invalid("pointer_files", "true or false")
            sets["pointer_files"] = b["pointer_files"]
        for k, (lo, hi) in REMOVAL_LIMITS.items():
            if k in b:
                v = b[k]
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    raise invalid(k, f"an integer from {lo} to {hi}")
                if ctx.who.is_app or not ctx.who.has("ArchiveAdmin"):
                    raise role_required("ArchiveAdmin")
                sets[k] = v
        if "removal_window" in b:
            w = b["removal_window"]
            if w is not None and (not isinstance(w, dict) or set(w) - {"start", "end", "tz"}
                                  or not all(isinstance(w.get(k), str) and _HHMM.match(w[k])
                                             for k in ("start", "end"))):  # fmt: skip
                raise invalid("removal_window", "null, or {start: HH:MM, end: HH:MM, tz?}")
            if ctx.who.is_app or not ctx.who.has("ArchiveAdmin"):
                raise role_required("ArchiveAdmin")
            sets["removal_window"] = Jsonb(w) if w is not None else None
        if "state" in b:
            want = b["state"]
            if want not in ("active", "paused", "retired"):
                raise invalid("state", "active, paused or retired")
            if a["state"] == "retired":
                raise WebError("version_conflict", "a retired annex stays retired")
            if want == "active" and a["state"] in ("registered", "baseline"):
                want = a["state"]  # resuming before the first scan is a no-op
            sets["state"] = want
            if want == "retired":
                await _end_placements(ctx, db, a)
        if sets:
            cols = ", ".join(f"{k} = %({k})s" for k in sets)
            await db.execute(
                f"UPDATE catalog.annex SET {cols} WHERE annex_id = %(id)s",
                {**sets, "id": a["annex_id"]},
            )
            shown = {k: (v.obj if isinstance(v, Jsonb) else v) for k, v in sets.items()}
            await db.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, details)"
                " VALUES (%s, 'settings', %s)",
                (a["annex_id"], Jsonb({"by": ctx.who.actor, **shown})),
            )
            await audit(db, ctx.who, "annex.settings", dataset_id=a["dataset_id"],
                        details={"annex_id": str(a["annex_id"]), **shown})  # fmt: skip
        a = await _annex(db, a["annex_id"], _AllSeeing)
    return WebResult(200, annex_json(a), headers={"ETag": etag(a["entity_version"])})


async def _placer_gone(ctx: Ctx, db, a: dict[str, Any]) -> None:
    """Placements past their `go` that this annex's unit was placing for another
    annex, on a filer another annex is left on. That one won't take them up, since
    they have no ticket to give, and this unit has stopped: they sit until an
    ArchiveAdmin abandons them. Say so."""
    rows = await (
        await db.execute(
            "SELECT p.placement_id, p.annex_id, p.file_id, p.version_id"
            "  FROM catalog.annex_placement p JOIN catalog.annex pa ON pa.annex_id = p.annex_id"
            # On another filer's file: an annex on this filer places its own.
            " WHERE p.annex_id <> %(a)s AND pa.filer_id <> %(f)s"
            "   AND (p.target->>'filer_id')::bigint = %(f)s"
            "   AND p.target->>'kind' = 'nas'"
            "   AND (p.state = 'placing' OR (p.state = 'stuck' AND p.before_stuck = 'placing'))"
            "   AND %(a)s < ALL (SELECT x.annex_id FROM catalog.annex x WHERE x.filer_id = %(f)s"
            "                     AND x.state <> 'retired' AND x.annex_id <> %(a)s)"
            "   AND EXISTS (SELECT 1 FROM catalog.annex x WHERE x.filer_id = %(f)s"
            "                AND x.state <> 'retired' AND x.annex_id <> %(a)s)",
            {"a": a["annex_id"], "f": a["filer_id"]},
        )
    ).fetchall()
    for p in rows:
        detail = (
            f"annex {a['annex_id']} was placing it and is retired: it had its go, so the file may"
            " already be under its name, and nothing will finish it. An ArchiveAdmin may abandon"
            " it after 24 hours with no report"
        )
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
            " VALUES (%s, 'problem', %s, %s, %s)",
            (p["annex_id"], p["file_id"], p["version_id"],
             Jsonb({"placement_id": str(p["placement_id"]), "reason": "placement_placer_retired",
                    "detail": detail})),
        )  # fmt: skip


async def _end_placements(ctx: Ctx, db, a: dict[str, Any]) -> None:
    """A retired annex's unit stops, so nothing will finish what it was placing
    or clean up after it. Its own placements end, and any other annex's that
    target its filer when no other annex there is left to place them. Each is a
    problem for the file's annex: a staged copy stays in the placer's staging
    folder on the NAS, which only the placement and read accounts can open, until
    the NAS's owners remove it, and one that has its `go` may be under its name."""
    rows = await (
        await db.execute(
            "SELECT p.placement_id, p.annex_id, p.file_id, p.version_id, p.state, p.before_stuck,"
            "       p.target, (p.target->>'filer_id')::bigint AS target_filer,"
            "       (SELECT min(x.annex_id) FROM catalog.annex x"
            "         WHERE x.filer_id = (p.target->>'filer_id')::bigint AND x.state <> 'retired'"
            "           AND x.annex_id <> %(a)s) AS other_placer"
            "  FROM catalog.annex_placement p"
            f" WHERE {open_sql('p')} AND p.target->>'kind' = 'nas'"
            "   AND (p.annex_id = %(a)s OR ((p.target->>'filer_id')::bigint = %(f)s"
            "        AND NOT EXISTS (SELECT 1 FROM catalog.annex x WHERE x.filer_id = %(f)s"
            "                         AND x.state <> 'retired' AND x.annex_id <> %(a)s)))"
            " FOR UPDATE OF p",
            {"a": a["annex_id"], "f": a["filer_id"]},
        )
    ).fetchall()
    await _placer_gone(ctx, db, a)
    for p in rows:
        was = p["before_stuck"] if p["state"] == "stuck" else p["state"]
        await db.execute(
            "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'annex_retired',"
            " fail_detail = %s, done_at = now(), updated_at = now() WHERE placement_id = %s",
            (f"was {p['state']}", p["placement_id"]),
        )
        # Who was placing it: the annex on the target's filer, this one if none else.
        placer = a["annex_id"] if p["target_filer"] == a["filer_id"] else p["other_placer"]
        if p["state"] in ("requested", "rehydrating", "ready"):
            detail = "not started: nothing on the NAS"
        elif p["target_filer"] != a["filer_id"] and placer is not None:
            detail = f"annex {placer} was placing it, and cleans up when it next hears"
            if was == "placing":
                detail += ". It had its go, so the file may already be under its name"
        else:
            detail = (
                f"a staged copy may remain in .omnifile-staging-{placer}-* at the top of the"
                " export, open only to the placement and read accounts; nothing removes it"
            )
            if was == "placing":
                detail += ". It had its go, so the file may already be under its name"
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
            " VALUES (%s, 'problem', %s, %s, %s)",
            (p["annex_id"], p["file_id"], p["version_id"],
             Jsonb({"placement_id": str(p["placement_id"]), "reason": "placement_annex_retired",
                    "detail": detail})),
        )  # fmt: skip
        await audit(db, ctx.who, "annex.placement.annex_retired", file_id=p["file_id"],
                    version_id=p["version_id"], dataset_id=a["dataset_id"],
                    details={"placement_id": str(p["placement_id"]), "was": p["state"],
                             "detail": detail})  # fmt: skip


def _visible_sql(who: Principal, owner: bool) -> tuple[str, dict[str, Any]]:
    """Events and files a caller may see: apps all (dataset level); users a
    file only through its captured ACL, and events without a file (scans,
    mounts, and path-only ones such as skipped or drift, whose paths
    `_event_json` hides from all but ArchiveAdmins: access-control.md
    section 5)."""
    if who.is_app:
        return "true", {}
    params = acl_params(who)
    sql = (
        "(CASE WHEN e.file_id IS NULL THEN true"
        " ELSE EXISTS (SELECT 1 FROM catalog.file vf JOIN catalog.acl acl"
        " ON acl.acl_id = vf.acl_id WHERE vf.file_id = e.file_id AND "
        + readable_page_sql("vf.file_id")
        + ") END)"
    )
    return sql, params


async def list_events(ctx: Ctx) -> WebResult:
    kind = ctx.query("kind")
    if kind is not None and not re.fullmatch(r"[a-z][a-z_]{0,39}(,[a-z][a-z_]{0,39})*", kind):
        raise invalid("kind", "event kinds, comma-separated")
    since, until = _time(ctx.query("since"), "since"), _time(ctx.query("until"), "until")
    limit = ctx.limit()
    after = ctx.cursor("annex_events")
    async with ctx.svc.catalog.read() as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        # An app reads what its client's owner may see (section 5.21).
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        vis, params = _visible_sql(ctx.who, _owns(ctx.who, a))
        rows = await (
            await db.execute(
                "SELECT e.* FROM catalog.annex_event e WHERE e.annex_id = %(a)s"
                " AND (%(kinds)s::text[] IS NULL OR e.kind = ANY(%(kinds)s::text[]))"
                " AND (%(since)s::timestamptz IS NULL OR e.at >= %(since)s::timestamptz)"
                " AND (%(until)s::timestamptz IS NULL OR e.at < %(until)s::timestamptz)"
                " AND (%(at)s::timestamptz IS NULL OR (e.at, e.event_id)"
                "      < (%(at)s::timestamptz, %(eid)s::bigint))"
                f" AND {vis} ORDER BY e.at DESC, e.event_id DESC LIMIT %(limit)s",
                {
                    "a": a["annex_id"],
                    "kinds": kind.split(",") if kind else None,
                    "since": since,
                    "until": until,
                    "at": after[0] if after else None,
                    "eid": after[1] if after else None,
                    "limit": limit + 1,
                    **params,
                },
            )  # fmt: skip
        ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = ctx.next_cursor("annex_events", [rows[-1]["at"].isoformat(), rows[-1]["event_id"]]) \
        if more else None  # fmt: skip
    hide = _hides_paths(ctx.who)
    return WebResult(200, page([_event_json(r, hide) for r in rows], nxt))


def _hides_paths(who: Principal) -> bool:
    """Paths the catalog can't check against an ACL (a NAS path with no
    catalog file: skipped, drift) are shown to ArchiveAdmins and apps (their
    access is the dataset's) only; others see the kind and the reason."""
    return not (who.is_app or who.has("ArchiveAdmin"))


def _time(v: str | None, field: str) -> datetime | None:
    if v is None:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise invalid(field, "an RFC 3339 time") from None
    if t.tzinfo is None:
        raise invalid(field, "an RFC 3339 time with its offset")
    return t


def _event_json(r: dict[str, Any], hide_paths: bool = False) -> dict[str, Any]:
    details = r["details"]
    path = r["path"]
    if hide_paths and r["file_id"] is None:
        # Whether or not the row has a path: a problem may name a folder in its
        # details (folder_unreadable, above the annex root too; the review of #83).
        path = None
        d = details or {}
        details = {k: d[k] for k in ("reason", "drift", "scan") if k in d} or None
    return {
        "event_id": str(r["event_id"]),
        "at": rfc3339(r["at"]),
        "kind": r["kind"],
        "path": path,
        "file_id": None if r["file_id"] is None else str(r["file_id"]),
        "version_id": None if r["version_id"] is None else str(r["version_id"]),
        "size": r["size"],
        "observed_at": rfc3339(r["observed_at"]),
        "details": details,
    }


async def list_files(ctx: Ctx) -> WebResult:
    state = ctx.query("state")
    if state is not None and state not in ("present", "gone", "purged"):
        raise invalid("state", "present, gone or purged")
    prefix = ctx.query("path") or ""
    if len(prefix) > 4096:
        raise invalid("path", "too long")
    limit = ctx.limit()
    after = ctx.cursor("annex_files")
    async with ctx.svc.catalog.read() as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        if ctx.who.is_app:
            vis, params = "true", {}
        else:
            params = acl_params(ctx.who)
            vis = "(acl.acl_id IS NOT NULL AND " + readable_page_sql("f.file_id") + ")"
        rows = await (
            await db.execute(
                "SELECT af.*, f.remote_tier, f.content_version_id, f.restore_state,"
                "       l.verified AS loc_verified, l.logical_tier"
                "  FROM catalog.annex_file af"
                "  JOIN catalog.file f ON f.file_id = af.file_id"
                "  LEFT JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
                "  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id"
                " WHERE af.annex_id = %(a)s"
                "   AND (%(prefix)s = '' OR left(af.path, length(%(prefix)s)) = %(prefix)s)"
                "   AND (%(state)s::text IS NULL"
                "        OR (%(state)s = 'present' AND af.gone_since IS NULL)"
                "        OR (%(state)s = 'gone' AND af.gone_since IS NOT NULL"
                "            AND af.purged_at IS NULL)"
                "        OR (%(state)s = 'purged' AND af.purged_at IS NOT NULL))"
                "   AND (%(ap)s::text IS NULL OR (af.path, af.file_id) > (%(ap)s, %(af)s::bigint))"
                f"   AND {vis} ORDER BY af.path, af.file_id LIMIT %(limit)s",
                {
                    "a": a["annex_id"],
                    "prefix": prefix,
                    "state": state,
                    "ap": after[0] if after else None,
                    "af": after[1] if after else None,
                    "limit": limit + 1,
                    **params,
                },
            )  # fmt: skip
        ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        # Decision 63: the folders above each file that narrow who may read it.
        narrow = await narrowing(db, [r["file_id"] for r in rows])
    nxt = ctx.next_cursor("annex_files", [rows[-1]["path"], rows[-1]["file_id"]]) if more else None
    return WebResult(200, page([{**_file_json(r), "folders_narrow": narrow.get(r["file_id"], [])}
                                for r in rows], nxt))  # fmt: skip


def _file_json(r: dict[str, Any]) -> dict[str, Any]:
    copied = r["copied_version_id"] is not None
    current = copied and (r["copied_size"], r["copied_mtime_ns"]) == (r["size"], r["mtime_ns"])
    if r["purged_at"] is not None:
        copy = "purged"
    elif not copied:
        copy = "pending"
    elif r["verified_version_id"] == r["copied_version_id"]:
        copy = "verified" if current else "changed"
    else:
        copy = "copied" if current else "changed"
    return {
        "file_id": str(r["file_id"]),
        "path": r["path"],
        "size": r["size"],
        "mtime_ns": None if r["mtime_ns"] is None else str(r["mtime_ns"]),
        "nas_state": r["nas_state"],
        "copy_state": copy,
        "tier": r["logical_tier"] or r["remote_tier"],
        "first_seen": rfc3339(r["first_seen"]),
        "last_seen": rfc3339(r["last_seen"]),
        # Loops (section 4.6): held until, and pinned until an Owner looks.
        "hold_until": rfc3339(r["hold_until"]),
        "pinned_by_breaker": r["pinned_by_breaker"],
        "gone_since": rfc3339(r["gone_since"]),
        "baseline": r["baseline"],
        "copied_at": rfc3339(r["copied_at"]),
        "verified_at": rfc3339(r["verified_at"]),
        "version_id": None if r["copied_version_id"] is None else str(r["copied_version_id"]),
        "purge_requested_at": rfc3339(r["purge_requested_at"]),
        # A legal hold or WORM refused it. Nothing is waiting: it can be asked again.
        "purge_refused_at": rfc3339(r["purge_refused_at"]),
        "purged_at": rfc3339(r["purged_at"]),
        **_refused_json(r),
    }


def _refused_json(r: dict[str, Any]) -> dict[str, Any]:
    """A copy Azure's read-back refused (annex.md section 3.3; migration 0058): in a
    row, the last, and an ArchiveAdmin's stop and acknowledgement."""
    return {
        "copy_refusals": r["copy_refusals"],
        "copy_refused_at": rfc3339(r["copy_refused_at"]),
        "copy_stopped": None if r["copy_stopped_at"] is None else {
            "by": r["copy_stopped_by"], "at": rfc3339(r["copy_stopped_at"])},
        "copy_refused_ack": None if r["copy_refused_ack_at"] is None else {
            "by": r["copy_refused_ack_by"], "at": rfc3339(r["copy_refused_ack_at"])},
    }  # fmt: skip


async def get_timeline(ctx: Ctx) -> WebResult:
    """Arrivals and departures per UTC day, with copies and verifications."""
    raw = ctx.query("days") or "30"
    if not raw.isdigit() or not 1 <= int(raw) <= 366:
        raise invalid("days", "1 to 366")
    days = int(raw)
    async with ctx.svc.catalog.read() as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        start = (ctx.now - timedelta(days=days - 1)).date()
        rows = await (
            await db.execute(
                "SELECT (e.at AT TIME ZONE 'UTC')::date AS day, e.kind, count(*) AS n,"
                "       coalesce(sum(e.size), 0) AS bytes"
                "  FROM catalog.annex_event e"
                " WHERE e.annex_id = %s AND e.at >= %s::date"
                "   AND e.kind IN ('arrived', 'left', 'changed', 'copied', 'verified')"
                " GROUP BY 1, 2",
                (a["annex_id"], start),
            )
        ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        d = by_day.setdefault(r["day"].isoformat(), {})
        d[r["kind"]] = r["n"]
        d[r["kind"] + "_bytes"] = int(r["bytes"])
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        d = by_day.get(day, {})
        out.append({
            "date": day,
            "arrived": d.get("arrived", 0),
            "left": d.get("left", 0),
            "changed": d.get("changed", 0),
            "copied": d.get("copied", 0),
            "verified": d.get("verified", 0),
            "bytes_copied": d.get("copied_bytes", 0),
        })  # fmt: skip
    return WebResult(200, {
        "annex_id": str(a["annex_id"]),
        "baseline": {"at": rfc3339(a["baseline_at"]), "files": a["baseline_files"],
                     "bytes": a["baseline_bytes"]},
        "days": out,
    })  # fmt: skip


#: The folders of an annex and those above its root (decision 63), on its filer.
READ_SET_SQL = """
SELECT count(*) AS folders,
       count(*) FILTER (WHERE n.error IS NOT NULL OR s.search_sids IS NULL) AS unreadable
  FROM catalog.nas_folder_acl n LEFT JOIN catalog.acl s ON s.acl_id = n.acl_id
 WHERE n.filer_id = %(filer)s AND n.gone_at IS NULL
   AND (%(root)s = '' OR n.path = '' OR n.path = %(root)s
        OR left(n.path, length(%(root)s) + 1) = %(root)s || '/'
        OR left(%(root)s, length(n.path) + 1) = n.path || '/')
"""


#: Refused copies `GET …/problems` lists, at most; `copies_refused_total` says how many
#: there are.
REFUSED_LISTED = 200


async def get_problems(ctx: Ctx) -> WebResult:
    """Drift from reconciliation and the copier's problems, newest first."""
    async with ctx.svc.catalog.read() as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        await require_owner_access(db, ctx.who, a["dataset_id"], ctx.now)
        owner = _owns(ctx.who, a)
        vis, params = _visible_sql(ctx.who, owner)
        events = await (
            await db.execute(
                # A copy_refused problem is put right once a later version verifies.
                "SELECT e.*, (e.kind = 'problem' AND e.details->>'reason' = 'copy_refused'"
                "   AND EXISTS (SELECT 1 FROM catalog.annex_file af"
                "                WHERE af.annex_id = e.annex_id AND af.file_id = e.file_id"
                "                  AND af.verified_version_id >= e.version_id)) AS put_right"
                "  FROM catalog.annex_event e WHERE e.annex_id = %(a)s"
                " AND e.kind = ANY(%(kinds)s) AND e.at > now() - interval '30 days'"
                f" AND {vis} ORDER BY e.at DESC, e.event_id DESC LIMIT 200",
                {"a": a["annex_id"], "kinds": list(EVENT_KINDS_PROBLEMS), **params},
            )
        ).fetchall()
        drift = []
        hide = _hides_paths(ctx.who)
        if owner or ctx.who.has("DataSteward"):
            prefix = f"{a['path']}/" if a["path"] else ""
            drift = await (
                await db.execute(
                    "SELECT d.* FROM catalog.reconcile_drift d"
                    "  JOIN catalog.filer fl ON fl.backend_id = d.backend_id"
                    " WHERE d.source = 'annex' AND fl.filer_id = %s AND d.container = %s"
                    "   AND left(d.object_key, length(%s)) = %s"
                    " ORDER BY d.detected_at DESC LIMIT 200",
                    (a["filer_id"], a["export"], prefix, prefix),
                )
            ).fetchall()
        folders = await (
            await db.execute(READ_SET_SQL, {"filer": a["filer_id"], "root": a["path"]})
        ).fetchone()
        # Copies the read-back refuses, however long ago (migration 0058): a file whose
        # copying an ArchiveAdmin stopped stays here until a version verifies or it is
        # resumed, never dropped with the 30 days' events. ArchiveAdmins (who act on
        # them) and apps see every one; others the files they may read.
        rvis = "true" if not hide else vis
        refused_where = (
            " FROM catalog.annex_file e WHERE e.annex_id = %(a)s AND e.gone_since IS NULL"
            "   AND (e.copy_refusals > 0 OR e.copy_stopped_at IS NOT NULL)"
            f"  AND {rvis}"
        )
        refused = await (
            await db.execute(
                "SELECT e.*" + refused_where + " ORDER BY e.copy_stopped_at IS NULL,"
                f" e.copy_refused_at DESC, e.file_id LIMIT {REFUSED_LISTED}",
                {"a": a["annex_id"], **params},
            )
        ).fetchall()
        # How many there are in all, so a list cut short never looks complete.
        refused_total = len(refused)
        if refused_total == REFUSED_LISTED:
            refused_total = (
                await (
                    await db.execute(
                        "SELECT count(*) AS n" + refused_where, {"a": a["annex_id"], **params}
                    )
                ).fetchone()
            )["n"]
    counts: dict[str, int] = {}
    for d in drift:
        if d["resolved_at"] is None and not (d["observed"] or {}).get("resolved_by"):
            counts[d["kind"]] = counts.get(d["kind"], 0) + 1
    return WebResult(200, {
        "annex_id": str(a["annex_id"]),
        # Decision 63: the folders above the annex's files, read so far.
        "read_set": {"ready": a["folders_read_at"] is not None,
                     # Decision 65: the filer's question; unanswered hides every file.
                     "filer_answered": a["access_limited"] is not None,
                     "read_at": rfc3339(a["folders_read_at"]),
                     "folders": folders["folders"], "unreadable": folders["unreadable"]},
        # Unresolved drift by kind: what everyone who sees the annex may know.
        "drift_counts": counts,
        # Each drift names a NAS path: ArchiveAdmins and apps only.
        "drift": [] if hide else [{
            "drift_id": str(d["drift_id"]),
            "kind": d["kind"],
            "path": d["object_key"][len(a["path"]) + 1:] if a["path"] else d["object_key"],
            "expected": d["expected"],
            "observed": d["observed"],
            "detected_at": rfc3339(d["detected_at"]),
            "last_seen_at": rfc3339(d["last_seen_at"]),
            "resolved": d["resolved_at"] is not None
            or bool((d["observed"] or {}).get("resolved_by")),
        } for d in drift],
        "events": [{**_event_json(e, hide), **({"resolved": True} if e["put_right"] else {})}
                   for e in events],
        # Stopped first. `alerting`: at the bound or stopped, and not acknowledged, so
        # the alert is up for it (readback.ALERTING_SQL).
        "copies_refused": [{"file_id": str(r["file_id"]), "path": r["path"],
                            "at_bound": r["copy_refusals"] >= MAX_REFUSALS,
                            "alerting": alerting(r["copy_refusals"],
                                                 r["copy_stopped_at"] is not None,
                                                 r["copy_refused_ack_at"] is not None),
                            **_refused_json(r)} for r in refused],
        # All of them, of which `copies_refused` lists at most REFUSED_LISTED.
        "copies_refused_total": refused_total,
    })  # fmt: skip


async def purge_file(ctx: Ctx) -> WebResult:
    """An ArchiveAdmin purges the kept copy of a file deleted on the NAS,
    with a fresh sign-in (the copy is the last one)."""
    _admin_user(ctx.who)
    ctx.svc.auth.require_recent_auth(ctx.who)
    file_id = ctx.path_id("file_id")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        af = await (
            await db.execute(
                "SELECT * FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s FOR UPDATE",
                (a["annex_id"], file_id),
            )
        ).fetchone()
        if af is None:
            raise not_found("annex file")
        if af["gone_since"] is None:
            raise WebError("file_present", "the file is still on the NAS: its copy is kept")
        if af["purge_requested_at"] is None:
            await db.execute(
                "UPDATE catalog.annex_file SET purge_requested_at = now(), purge_requested_by = %s,"
                " purge_refused_at = NULL WHERE annex_id = %s AND file_id = %s",
                (ctx.who.actor, a["annex_id"], file_id),
            )
            await audit(db, ctx.who, "annex.purge.request", dataset_id=a["dataset_id"],
                        file_id=file_id, details={"annex_id": str(a["annex_id"]),
                                                  "path": af["path"]})  # fmt: skip
        # Asked before and still open too: its message may have gone with a crash on
        # an older version, and a second one finds nothing to do.
        if af["purged_at"] is None:
            await db.execute(
                "SELECT pgmq.send('retention', %s)",
                (Jsonb({"op": "annex_purge", "annex_id": str(a["annex_id"]),
                        "file_id": str(file_id)}),),
            )  # fmt: skip
        af = await (
            await db.execute(
                "SELECT af.*, NULL::text AS remote_tier, NULL::text AS logical_tier"
                " FROM catalog.annex_file af WHERE annex_id = %s AND file_id = %s",
                (a["annex_id"], file_id),
            )
        ).fetchone()
    return WebResult(202, _file_json(af))


async def clear_breaker(ctx: Ctx) -> WebResult:
    """An Owner looked at a file the loop breaker pinned (section 4.6): it may
    leave again, and the breaker counts from now. Audited."""
    file_id = ctx.path_id("file_id")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
        if ctx.who.is_app or not _owns(ctx.who, a):
            raise role_required("ArchiveAdmin", "the annex's Owners")
        af = await (
            await db.execute(
                "SELECT pinned_by_breaker, path FROM catalog.annex_file"
                " WHERE annex_id = %s AND file_id = %s FOR UPDATE",
                (a["annex_id"], file_id),
            )
        ).fetchone()
        if af is None:
            raise not_found("annex file")
        was_pinned = af["pinned_by_breaker"]
        if was_pinned:
            await db.execute(
                "UPDATE catalog.annex_file SET pinned_by_breaker = false,"
                " breaker_cleared_at = now(), updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (a["annex_id"], file_id),
            )
        # On a file that wasn't pinned, it did nothing, and the row says so.
        await audit(db, ctx.who, "annex.breaker.cleared", dataset_id=a["dataset_id"],
                    file_id=file_id, details={"annex_id": str(a["annex_id"]), "path": af["path"],
                                              "was_pinned": was_pinned})  # fmt: skip
        af = await (
            await db.execute(
                "SELECT af.*, NULL::text AS remote_tier, NULL::text AS logical_tier"
                " FROM catalog.annex_file af WHERE annex_id = %s AND file_id = %s",
                (a["annex_id"], file_id),
            )
        ).fetchone()
    return WebResult(200, _file_json(af))


async def _refused_file(db, ctx: Ctx) -> tuple[dict[str, Any], dict[str, Any]]:
    """The annex and its file, locked, for an ArchiveAdmin's stop, resume or
    acknowledgement; only a copy the read-back refused (or one stopped)."""
    _admin_user(ctx.who)
    ctx.svc.auth.require_recent_auth(ctx.who)
    file_id = ctx.path_id("file_id")
    a = await _annex(db, ctx.path_id("annex_id"), ctx.who)
    af = await (
        await db.execute(
            "SELECT * FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s FOR UPDATE",
            (a["annex_id"], file_id),
        )
    ).fetchone()
    if af is None:
        raise not_found("annex file")
    return a, af


async def _file_after(db, a: dict[str, Any], file_id: int) -> WebResult:
    af = await (
        await db.execute(
            "SELECT af.*, NULL::text AS remote_tier, NULL::text AS logical_tier"
            " FROM catalog.annex_file af WHERE annex_id = %s AND file_id = %s",
            (a["annex_id"], file_id),
        )
    ).fetchone()
    return WebResult(200, _file_json(af))


async def stop_copy(ctx: Ctx) -> WebResult:
    """An ArchiveAdmin stops copies of a file the read-back keeps refusing (migration
    0058): its edge's reconciliation doesn't copy the same bytes again until a version
    verifies or the admin resumes. A fresh sign-in; audited."""
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a, af = await _refused_file(db, ctx)
        if af["copy_refusals"] == 0 and af["copy_stopped_at"] is None:
            raise WebError("copy_not_refused", "no read-back has refused this file's copy")
        was = af["copy_stopped_at"] is not None
        if not was:
            await db.execute(
                "UPDATE catalog.annex_file SET copy_stopped_at = now(), copy_stopped_by = %s,"
                " updated_at = now() WHERE annex_id = %s AND file_id = %s",
                (ctx.who.actor, a["annex_id"], af["file_id"]),
            )
        await audit(db, ctx.who, "annex.copy.stopped", dataset_id=a["dataset_id"],
                    file_id=af["file_id"], details={"annex_id": str(a["annex_id"]),
                                                    "path": af["path"], "was_stopped": was,
                                                    "refusals": af["copy_refusals"]})  # fmt: skip
        return await _file_after(db, a, af["file_id"])


async def resume_copy(ctx: Ctx) -> WebResult:
    """The ArchiveAdmin says so: its edge may copy the file again (its next
    reconciliation, or one the service asks for at the next refusal). Audited."""
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a, af = await _refused_file(db, ctx)
        was = af["copy_stopped_at"] is not None
        if was:
            await db.execute(
                "UPDATE catalog.annex_file SET copy_stopped_at = NULL, copy_stopped_by = NULL,"
                " updated_at = now() WHERE annex_id = %s AND file_id = %s",
                (a["annex_id"], af["file_id"]),
            )
        # On a file that wasn't stopped, it did nothing, and the row says so.
        details = {"annex_id": str(a["annex_id"]), "path": af["path"], "was_stopped": was,
                   "stopped_by": af["copy_stopped_by"]}  # fmt: skip
        await audit(db, ctx.who, "annex.copy.resumed", dataset_id=a["dataset_id"],
                    file_id=af["file_id"], details=details)  # fmt: skip
        return await _file_after(db, a, af["file_id"])


async def ack_copy_refused(ctx: Ctx) -> WebResult:
    """An ArchiveAdmin has seen the refusals: the file leaves the gauge
    `omnifile.annex.copies_refused`, so the alert quiets for it alone, until a version
    verifies. It stays on Problems. A fresh sign-in; audited."""
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        a, af = await _refused_file(db, ctx)
        if af["copy_refusals"] == 0:
            raise WebError("copy_not_refused", "no read-back has refused this file's copy")
        was = af["copy_refused_ack_at"] is not None
        if not was:
            await db.execute(
                "UPDATE catalog.annex_file SET copy_refused_ack_at = now(),"
                " copy_refused_ack_by = %s, updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (ctx.who.actor, a["annex_id"], af["file_id"]),
            )
        await audit(db, ctx.who, "annex.copy.refused_ack", dataset_id=a["dataset_id"],
                    file_id=af["file_id"], details={"annex_id": str(a["annex_id"]),
                                                    "path": af["path"], "was_acknowledged": was,
                                                    "refusals": af["copy_refusals"]})  # fmt: skip
        return await _file_after(db, a, af["file_id"])


# --- defaults ------------------------------------------------------------------------


def _defaults_json(r: dict[str, Any]) -> dict[str, Any]:
    return {**{k: r[k] for k in SETTINGS}, **{k: r[k] for k in DEFAULT_REMOVALS},
            "removals_paused": r["removals_paused"],
            "placements_paused": r["placements_paused"], "updated_at": rfc3339(r["updated_at"]),
            "updated_by": r["updated_by"]}  # fmt: skip


async def get_defaults(ctx: Ctx) -> WebResult:
    _filer_reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        r = await (await db.execute("SELECT * FROM catalog.annex_defaults")).fetchone()
    return WebResult(200, _defaults_json(r), headers={"ETag": etag(r["entity_version"])})


async def put_defaults(ctx: Ctx) -> WebResult:
    _admin_user(ctx.who)
    b = ctx.body
    _only(b, set(SETTINGS) | set(DEFAULT_REMOVALS) | {"removals_paused", "placements_paused"},
          "annex default")  # fmt: skip
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        r = await (await db.execute("SELECT * FROM catalog.annex_defaults FOR UPDATE")).fetchone()
        if_match(ctx.request, r["entity_version"])
        s = _settings({k: v for k, v in b.items() if k in SETTINGS},
                      {k: r[k] for k in SETTINGS})  # fmt: skip
        for k, (lo, hi) in DEFAULT_REMOVALS.items():
            v = b.get(k, r[k])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise invalid(k, f"an integer from {lo} to {hi}")
            s[k] = v
        paused = b.get("removals_paused", r["removals_paused"])
        if not isinstance(paused, bool):
            raise invalid("removals_paused", "true or false")
        if r["removals_paused"] and not paused:
            # "Resume all removals": freshly signed in.
            ctx.svc.auth.require_recent_auth(ctx.who)
        s["removals_paused"] = paused
        placements = b.get("placements_paused", r["placements_paused"])
        if not isinstance(placements, bool):
            raise invalid("placements_paused", "true or false")
        s["placements_paused"] = placements
        r = await (
            await db.execute(
                "UPDATE catalog.annex_defaults SET quiet_minutes = %(quiet_minutes)s,"
                " quick_scan_minutes = %(quick_scan_minutes)s,"
                " full_scan_minutes = %(full_scan_minutes)s, reconcile_days = %(reconcile_days)s,"
                " removal_rate_per_hour = %(removal_rate_per_hour)s,"
                " grace_minutes = %(grace_minutes)s, dry_run_days = %(dry_run_days)s,"
                " removals_paused = %(removals_paused)s,"
                " placements_paused = %(placements_paused)s, updated_by = %(by)s RETURNING *",
                {**s, "by": ctx.who.actor},
            )
        ).fetchone()
        await audit(db, ctx.who, "annex.defaults", details=s)
    return WebResult(200, _defaults_json(r), headers={"ETag": etag(r["entity_version"])})
