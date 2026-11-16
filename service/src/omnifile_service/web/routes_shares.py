"""Shares: listing, settings, the access screen and WORM (portal-api.md 5.2, 5.6)."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import psycopg

from ..catalog.read_set import readable_row_sql
from ..catalog.restricted import RESTRICTED_ACL_EXISTS
from ..retention import worm
from ..retention.common import RetentionError
from ..retention.depth import set_rewind_depth, set_rewind_max
from ..retention.worm import QUIET_WORM_DEFAULT_S
from .access import acl_params, share_roles
from .context import Ctx, Principal, WebResult, audit, etag, if_match, page, tuple_rows
from .problems import WebError, forbidden, invalid, not_found
from .rows import share_json

log = logging.getLogger("omnifile_service.web.shares")

_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
NFS_SEC = ("sys", "krb5", "krb5i", "krb5p")
SETTINGS_FIELDS = {
    "on_open", "wait_threshold_minutes", "rewind_depth_days", "rewind_depth_max_days",
    "nfs_sec", "nfs_clients", "root_squash", "host_trust_override", "quiet_s",
    "quiet_s_confirmed",
}  # fmt: skip
OWNER_FIELDS = {
    "on_open", "wait_threshold_minutes", "rewind_depth_days", "quiet_s", "quiet_s_confirmed",
}  # fmt: skip
#: A share's quiet period (decision 67; migration 0050 and the edge's `quiet.rs`
#: hold the same bounds). Below 10 s, today's period, an ordinary writeback stall
#: commits a partial version. Above 600 s the commit's further wait outlasts the
#: edge's looks at it.
QUIET_MIN_S = 10
QUIET_MAX_S = 600


async def _root_id(db, s: dict[str, Any]) -> int:
    if s["root_dir_id"] is not None:
        return s["root_dir_id"]
    row = await (
        await db.execute(
            "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
            (s["dataset_id"],),
        )
    ).fetchone()
    return row["file_id"]


async def _root_readable(db, who: Principal, root_id: int) -> bool:
    if who.is_app or who.sid is None or who.groups_source == "pending":
        return False
    row = await (
        await db.execute(
            f"SELECT {readable_row_sql('f.file_id')} AS ok FROM catalog.file f"
            " JOIN catalog.acl acl ON acl.acl_id = f.acl_id WHERE f.file_id = %(f)s",
            {**acl_params(who), "f": root_id},
        )
    ).fetchone()
    return bool(row and row["ok"])


async def restricted_groups(db, dataset_id: int) -> bool:
    """Does any folder of the dataset carry an ACL that Everyone and Domain
    Users can't read? (host-trusted shares then need the recorded override)."""
    row = await (
        await db.execute("SELECT " + RESTRICTED_ACL_EXISTS.format(ds="%s") + " AS r", (dataset_id,))
    ).fetchone()
    return bool(row["r"])


async def share_checks(ctx: Ctx, db, s: dict[str, Any]) -> list[dict[str, Any]]:
    # Read from the ACLs as they are now, so a folder restricted after the
    # share was made shows here and in the list (threat model T7). A recorded
    # host-trust override is the ArchiveAdmin's answer to it.
    restricted = (
        "sys" in (s.get("nfs_sec") or [])
        and not s.get("host_trust_override")
        and await restricted_groups(db, s["dataset_id"])
    )
    try:
        return ctx.svc.preview.share_checks(s, restricted_groups=restricted)
    except WebError as e:
        if e.code != "unavailable":
            raise
        log.warning("share checks unavailable: %s", e.detail)
        return []


async def group_names(db, sids: list[str | None]) -> dict[str, dict[str, Any]]:
    wanted = [s for s in sids if s]
    found = await (
        await db.execute(
            "SELECT sid, display_name, sam_account_name FROM catalog.identity"
            " WHERE sid = ANY(%s::text[])",
            (wanted,),
        )
    ).fetchall()
    return {r["sid"]: r for r in found}


async def visible(db, who: Principal, s: dict[str, Any]) -> tuple[bool, Any, bool, int]:
    roles = share_roles(who, s)
    root = await _root_id(db, s)
    can_root = await _root_readable(db, who, root)
    ok = who.has("ArchiveAdmin", "DataSteward") or roles.owner or roles.writer or roles.reader
    return ok or can_root, roles, can_root, root


async def share_view(ctx: Ctx, db, s: dict[str, Any]) -> dict[str, Any]:
    ok, roles, can_root, root = await visible(db, ctx.who, s)
    if not ok:
        raise not_found("share")
    names = await group_names(
        db, [s["owners_group_sid"], s["writers_group_sid"], s["readers_group_sid"]]
    )
    ds = await (
        await db.execute(
            "SELECT d.name, (SELECT max(completed_at) FROM catalog.acl_scan"
            " WHERE dataset_id = d.dataset_id AND kind = 'full') AS scan,"
            " (SELECT coalesce('/' || string_agg(a.name, '/' ORDER BY a.depth DESC), '/')"
            "    FROM catalog.file_ancestors(%s) a WHERE a.parent_id IS NOT NULL) AS path"
            " FROM catalog.dataset d WHERE d.dataset_id = %s",
            (root, s["dataset_id"]),
        )
    ).fetchone()
    edge = await (
        await db.execute(
            "SELECT name, health FROM catalog.edge WHERE edge_id = %s", (s["edge_id"],)
        )
    ).fetchone()
    return share_json(
        s,
        ctx.who,
        edge=edge,
        roles=roles,
        can_read_root=can_root,
        checks=await share_checks(ctx, db, s),
        names=names,
        root_dir_id=root,
        dataset_name=ds["name"],
        last_full_acl_scan_at=ds["scan"],
        path=ds["path"],
    )


async def list_shares(ctx: Ctx) -> WebResult:
    limit = ctx.limit()
    after = ctx.cursor("shares")
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT * FROM catalog.share WHERE share_id > %s ORDER BY share_id",
                (int(after[0]) if after else 0,),
            )
        ).fetchall()
        items: list[dict[str, Any]] = []
        last = None
        more = False
        for s in rows:
            if (await visible(db, ctx.who, s))[0]:
                if len(items) == limit:
                    more = True
                    break
                items.append(await share_view(ctx, db, s))
                last = s["share_id"]
    return WebResult(200, page(items, ctx.next_cursor("shares", [last]) if more else None))


async def _load(db, share_id: int, *, lock: bool = False) -> dict[str, Any]:
    s = await (
        await db.execute(
            "SELECT * FROM catalog.share WHERE share_id = %s" + (" FOR UPDATE" if lock else ""),
            (share_id,),
        )
    ).fetchone()
    if s is None:
        raise not_found("share")
    return s


def _with_etag(body: dict[str, Any]) -> WebResult:
    return WebResult(200, body, headers={"ETag": body["etag"]})


async def get_share(ctx: Ctx) -> WebResult:
    async with ctx.svc.catalog.read() as db:
        s = await _load(db, ctx.path_id("share_id"))
        return _with_etag(await share_view(ctx, db, s))


# --- settings -------------------------------------------------------------------


def parse_settings(b: dict[str, Any]) -> dict[str, Any]:
    if not b:
        raise invalid("body", "at least one setting is required")
    unknown = set(b) - SETTINGS_FIELDS
    if unknown:
        raise invalid(sorted(unknown)[0], "is not a share setting")
    out: dict[str, Any] = {}
    if "on_open" in b:
        if b["on_open"] not in ("wait", "fail", "auto"):
            raise invalid("on_open", "must be wait, fail or auto")
        out["on_open"] = b["on_open"]
    if "wait_threshold_minutes" in b:
        v = b["wait_threshold_minutes"]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 1440):
            raise invalid("wait_threshold_minutes", "must be 1 to 1440, or null")
        out["wait_threshold_minutes"] = v
    for f in ("rewind_depth_days", "rewind_depth_max_days"):
        if f in b:
            v = b[f]
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 36500:
                raise invalid(f, "must be 0 to 36500")
            out[f] = v
    if "nfs_sec" in b:
        v = b["nfs_sec"]
        if not isinstance(v, list) or not v or any(x not in NFS_SEC for x in v):
            raise invalid("nfs_sec", "must be a non-empty list of sys, krb5, krb5i, krb5p")
        out["nfs_sec"] = sorted(set(v), key=NFS_SEC.index)
    if "nfs_clients" in b:
        v = b["nfs_clients"]
        if (
            not isinstance(v, list)
            or len(v) > 256
            or any(not isinstance(x, str) or not x or len(x) > 255 for x in v)
        ):
            raise invalid("nfs_clients", "must be up to 256 host names or subnets")
        out["nfs_clients"] = v
    if "quiet_s" in b:
        v = b["quiet_s"]
        if v is not None and (
            isinstance(v, bool) or not isinstance(v, int) or not QUIET_MIN_S <= v <= QUIET_MAX_S
        ):
            raise invalid(
                "quiet_s",
                f"must be {QUIET_MIN_S} to {QUIET_MAX_S} seconds, or null for the default",
            )
        out["quiet_s"] = v
    if "quiet_s_confirmed" in b:
        if not isinstance(b["quiet_s_confirmed"], bool):
            raise invalid("quiet_s_confirmed", "must be a boolean")
        out["quiet_s_confirmed"] = b["quiet_s_confirmed"]
    if "root_squash" in b:
        if not isinstance(b["root_squash"], bool):
            raise invalid("root_squash", "must be a boolean")
        out["root_squash"] = b["root_squash"]
    if "host_trust_override" in b:
        v = b["host_trust_override"]
        if (
            not isinstance(v, dict)
            or set(v) != {"enabled", "reason"}
            or not isinstance(v["enabled"], bool)
            or not isinstance(v["reason"], str)
            or not 1 <= len(v["reason"]) <= 1000
        ):
            raise invalid("host_trust_override", "needs enabled (boolean) and a reason")
        out["host_trust_override"] = v
    return out


def _check_field_roles(who: Principal, owner: bool, fields: set[str]) -> None:
    admin = who.has("ArchiveAdmin")
    for f in sorted(fields):
        if admin:
            continue
        if f in OWNER_FIELDS and owner:
            continue
        raise forbidden(f"{f} needs the ArchiveAdmin role" + (
            "" if f not in OWNER_FIELDS else " or the share's Owners group"))  # fmt: skip


def _proposed(s: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    out = dict(s)
    for k, v in p.items():
        out[k] = v["enabled"] if k == "host_trust_override" else v
    if p.get("on_open") and p["on_open"] != "auto" and "wait_threshold_minutes" not in p:
        out["wait_threshold_minutes"] = None
    out["trust_level"] = "host_trusted" if "sys" in (out["nfs_sec"] or []) else "proven"
    return out


async def patch_settings(ctx: Ctx) -> WebResult:
    who = ctx.who
    p = parse_settings(ctx.body)
    sid = ctx.path_id("share_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        s = await _load(db, sid, lock=True)
        ok, roles, _, _ = await visible(db, who, s)
        if not ok:
            raise not_found("share")
        if_match(ctx.request, s["entity_version"])
        _check_field_roles(who, roles.owner, set(p))
        new = _proposed(s, p)
        if new["on_open"] == "auto" and new["wait_threshold_minutes"] is None:
            raise invalid("wait_threshold_minutes", "auto needs a threshold in minutes")
        # Decision 67 allows a WORM share below its default, but it's asked for in
        # so many words: a partial version there is kept until retention ends.
        short = (
            "quiet_s" in p
            and s["worm_enabled"]
            and p["quiet_s"] is not None
            and p["quiet_s"] < QUIET_WORM_DEFAULT_S
        )
        if short and not p.get("quiet_s_confirmed"):
            raise invalid(
                "quiet_s_confirmed",
                f"below {QUIET_WORM_DEFAULT_S} s on a WORM share needs quiet_s_confirmed: a"
                " copy that pauses can leave a half-written version kept until retention ends",
            )
        try:
            async with db.transaction():
                with tuple_rows(db):
                    if "rewind_depth_max_days" in p:
                        await set_rewind_max(db, sid, p["rewind_depth_max_days"], actor=who.actor)
                    if "rewind_depth_days" in p:
                        await set_rewind_depth(db, sid, p["rewind_depth_days"], actor=who.actor)
                await db.execute(
                    "UPDATE catalog.share SET on_open = %s, wait_threshold_minutes = %s,"
                    " nfs_sec = %s, nfs_clients = %s, root_squash = %s, host_trust_override = %s,"
                    " quiet_s = %s WHERE share_id = %s",
                    (new["on_open"], new["wait_threshold_minutes"], new["nfs_sec"],
                     new["nfs_clients"], new["root_squash"], new["host_trust_override"],
                     new["quiet_s"], sid),
                )  # fmt: skip
        except RetentionError as e:
            raise WebError("invalid_request", str(e), errors=[
                {"field": "rewind_depth_days", "message": str(e)}]) from None  # fmt: skip
        except psycopg.errors.CheckViolation as e:
            name = getattr(e.diag, "constraint_name", None) or "share"
            raise WebError(
                "invalid_request",
                f"the catalog refuses these settings ({name})",
                errors=[{"field": name, "message": _constraint_message(name)}],
            ) from None
        details = {k: v for k, v in p.items() if k != "quiet_s_confirmed"}
        if "quiet_s" in p:
            # Null is the share's default.
            details["quiet_s"] = {"old": s["quiet_s"], "new": p["quiet_s"]}
            if short:
                details["quiet_s"]["confirmed_below_worm_default"] = True
        await audit(db, who, "share.settings", dataset_id=s["dataset_id"], share_id=sid,
                    details=details)  # fmt: skip
        s = await _load(db, sid)
        body = await share_view(ctx, db, s)
    return _with_etag(body)


def _constraint_message(name: str) -> str:
    return {
        "share_host_trusted_needs_clients": "sec=sys needs named NFS clients (never *)"
        " and root_squash",
        "share_nfs_needs_sec": "an NFS share needs at least one security flavour",
        "share_auto_needs_threshold": "auto needs a threshold; wait and fail take none",
        "share_rewind_within_max": "the rewind depth must be within the maximum",
        "share_quiet_range": f"must be {QUIET_MIN_S} to {QUIET_MAX_S} seconds, or null",
    }.get(name, "violates a share rule")


async def preview_settings(ctx: Ctx) -> WebResult:
    who = ctx.who
    p = parse_settings(ctx.body)
    async with ctx.svc.catalog.read() as db:
        s = await _load(db, ctx.path_id("share_id"))
        ok, roles, _, _ = await visible(db, who, s)
        if not ok:
            raise not_found("share")
        if not (who.has("ArchiveAdmin") or roles.owner):
            raise forbidden("previewing settings needs ArchiveAdmin or the share's Owners")
        checks = await share_checks(ctx, db, _proposed(s, p))
    return WebResult(200, {"checks": checks})


# --- access screen ---------------------------------------------------------------


def _sid_field(b: dict[str, Any], f: str, *, required: bool) -> str | None:
    if f not in b:
        if required:
            raise invalid(f, "is required")
        return None
    v = b[f]
    if v is None and not required:
        return None
    if not isinstance(v, str) or not _SID.match(v):
        raise invalid(f, "must be a SID")
    return v


async def put_access(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"owners_group_sid", "writers_group_sid", "readers_group_sid"}
    if extra:
        raise invalid(sorted(extra)[0], "is not an access field")
    owners = _sid_field(b, "owners_group_sid", required=True)
    writers = _sid_field(b, "writers_group_sid", required=False)
    readers = _sid_field(b, "readers_group_sid", required=False)
    sid = ctx.path_id("share_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        s = await _load(db, sid, lock=True)
        ok, roles, _, _ = await visible(db, who, s)
        if not ok:
            raise not_found("share")
        if_match(ctx.request, s["entity_version"])
        admin = who.has("ArchiveAdmin")
        if owners != s["owners_group_sid"] and not admin:
            raise forbidden("only an ArchiveAdmin changes the Owners group")
        if not (admin or roles.owner):
            raise forbidden("the access screen needs ArchiveAdmin or the share's Owners")
        await db.execute(
            "UPDATE catalog.share SET owners_group_sid = %s, writers_group_sid = %s,"
            " readers_group_sid = %s WHERE share_id = %s",
            (owners, writers, readers, sid),
        )
        await audit(
            db, who, "share.access", dataset_id=s["dataset_id"], share_id=sid,
            details={
                "old": {"owners": s["owners_group_sid"], "writers": s["writers_group_sid"],
                        "readers": s["readers_group_sid"]},
                "new": {"owners": owners, "writers": writers, "readers": readers},
            },
        )  # fmt: skip
        body = await share_view(ctx, db, await _load(db, sid))
    return _with_etag(body)


# --- WORM (decision 31) -------------------------------------------------------------


async def _worm_target(ctx: Ctx, db, *, reauth: bool) -> dict[str, Any]:
    who = ctx.who
    s = await _load(db, ctx.path_id("share_id"), lock=True)
    if not (await visible(db, who, s))[0]:
        raise not_found("share")
    if not who.has("ArchiveAdmin"):
        raise WebError("role_required", "WORM needs the ArchiveAdmin role")
    if reauth:
        ctx.svc.auth.require_recent_auth(who)
    if_match(ctx.request, s["entity_version"])
    return s


def _int_field(b: dict[str, Any], f: str, lo: int, hi: int, default: int | None = None) -> int:
    v = b.get(f, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise invalid(f, f"must be an integer from {lo} to {hi}")
    return v


def _confirm(b: dict[str, Any], s: dict[str, Any]) -> str:
    typed = b.get("confirm_name")
    if not isinstance(typed, str) or len(typed) > 255:
        raise invalid("confirm_name", "type the share's name")
    if typed != s["name"]:
        raise WebError("confirmation_mismatch", "the typed name is not this share's name")
    return typed


async def worm_enable(ctx: Ctx) -> WebResult:
    b = ctx.body
    extra = set(b) - {"retention_days", "confirm_name", "window_hours", "quiet_s_confirmed"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a WORM field")
    quiet_confirmed = b.get("quiet_s_confirmed", False)
    if not isinstance(quiet_confirmed, bool):
        raise invalid("quiet_s_confirmed", "must be a boolean")
    days = _int_field(b, "retention_days", 1, 36500)
    hours = _int_field(b, "window_hours", 1, 24, default=24)
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        s = await _worm_target(ctx, db, reauth=True)
        typed = _confirm(b, s)
        if s["worm_enabled"]:
            raise WebError("worm_violation", f"WORM is already on ({s['worm_mode']})")
        # Policy saves and approvals lock the dataset row too, so a Smart
        # policy can't become active between this check and the enable.
        await db.execute(
            "SELECT 1 FROM catalog.dataset WHERE dataset_id = %s FOR UPDATE", (s["dataset_id"],)
        )
        pol = await (
            await db.execute(
                "SELECT body->>'mode' AS mode FROM catalog.policy"
                " WHERE dataset_id = %s AND state = 'active'",
                (s["dataset_id"],),
            )
        ).fetchone()
        if pol is not None and pol["mode"] == "smart":
            raise WebError("layout_conflict", "a WORM share can't use Smart tier; change the"
                           " dataset's policy first")  # fmt: skip
        # The service clock, the one the worker's auto-lock sweep compares
        # the deadline with (retention.worm.lock_due_shares).
        now = ctx.now
        try:
            with tuple_rows(db):
                await worm.enable_worm(
                    db, s["share_id"], retention_days=days, confirmation=typed,
                    actor=ctx.who.actor, now=now, test_window=timedelta(hours=hours),
                    quiet_confirmed=quiet_confirmed,
                )  # fmt: skip
        except RetentionError as e:
            raise WebError("worm_violation", str(e)) from None
        body = await share_view(ctx, db, await _load(db, s["share_id"]))
    return _with_etag(body)


async def worm_cancel(ctx: Ctx) -> WebResult:
    b = ctx.body
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        s = await _worm_target(ctx, db, reauth=True)
        _confirm(b, s)
        closed = WebError("worm_window_closed", f"WORM is {s['worm_mode'] or 'off'}: it can be"
                          " turned off only inside its validation window")  # fmt: skip
        if s["worm_mode"] != "unlocked_test":
            raise closed
        # The window ends at the deadline, whether or not the auto-lock sweep
        # has run yet (the same rule as retention.worm.lock_due_shares).
        row = await (
            await db.execute(
                "SELECT coalesce(worm_lock_deadline,"
                " worm_enabled_at + interval '24 hours', created_at + interval '24 hours')"
                " AS deadline FROM catalog.share WHERE share_id = %s",
                (s["share_id"],),
            )
        ).fetchone()
        now = ctx.now
        if row["deadline"] <= now:
            raise closed
        try:
            with tuple_rows(db):
                await worm.cancel_worm_test(db, s["share_id"], actor=ctx.who.actor, now=now)
        except RetentionError as e:
            raise WebError("worm_window_closed", str(e)) from None
        body = await share_view(ctx, db, await _load(db, s["share_id"]))
    return _with_etag(body)


async def worm_extend(ctx: Ctx) -> WebResult:
    days = _int_field(ctx.body, "retention_days", 1, 36500)
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        s = await _worm_target(ctx, db, reauth=False)
        try:
            with tuple_rows(db):
                await worm.extend_worm_retention(db, s["share_id"], days, actor=ctx.who.actor)
        except RetentionError as e:
            raise WebError("worm_violation", str(e)) from None
        body = await share_view(ctx, db, await _load(db, s["share_id"]))
    return _with_etag(body)


__all__ = ["etag"]
