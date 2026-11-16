"""Catalog rows -> the JSON shapes of service/openapi/portal-api.yaml."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from ..api.validate import rfc3339
from ..retention.worm import QUIET_WORM_DEFAULT_S
from .access import ShareRoles
from .context import Principal, etag

ACTIVE_RECALL = ("requested", "rehydrating", "copying", "downloading")
#: What a share with no quiet period of its own takes (decision 67), until its
#: edge reports its own in the heartbeat: the defaults of the edge's
#: `[commit] quiet_s` and `[commit] worm_quiet_s`.
QUIET_DEFAULT_S = 10


def quiet_view(s: dict[str, Any], edge: dict[str, Any] | None) -> dict[str, Any]:
    """The share's quiet period and its edge's defaults. `quiet_supported` says
    the edge sets a period per share at all (it reports its defaults); it doesn't
    say the edge has this share's latest value yet, which comes in the feed."""
    health = (edge or {}).get("health") or {}
    key = "quiet_worm_default_s" if s["worm_enabled"] else "quiet_default_s"
    reported = health.get(key)
    supported = isinstance(reported, int) and not isinstance(reported, bool)
    fallback = QUIET_WORM_DEFAULT_S if s["worm_enabled"] else QUIET_DEFAULT_S
    return {
        "quiet_s": s.get("quiet_s"),
        "quiet_default_s": reported if supported else fallback,
        "quiet_supported": supported,
        "edge_name": (edge or {}).get("name"),
    }


def sid_(v: int | None) -> str | None:
    return None if v is None else str(v)


def day(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def ns(v: int | None) -> str | None:
    return None if v is None else str(v)


def mode_str(mode: int | None) -> str | None:
    return None if mode is None else f"{mode:04o}"


def mtime(v: int | None) -> str | None:
    if v is None:
        return None
    return rfc3339(datetime.fromtimestamp(v / 1e9, tz=UTC))


def tier_state(loc: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """TierState from a location row (logical_tier, storage_class,
    explicit_tier_set, tier_changed_at) plus its newest restore
    (restore_state, restore_expires_at)."""
    if loc is None or loc.get("logical_tier") is None and loc.get("storage_class") is None:
        return {"logical": None, "native_class": None, "archived": False,
                "restore_state": None, "restore_expires_at": None,
                "explicit_tier_set": False, "tier_changed_at": None}  # fmt: skip
    rs = loc.get("restore_state")
    rs = "in_progress" if rs == "requested" else rs
    exp = loc.get("restore_expires_at")
    live_restore = rs == "completed" and (exp is None or exp > now)
    return {
        "logical": loc.get("logical_tier"),
        "native_class": loc.get("storage_class"),
        "archived": loc.get("logical_tier") == "archive" and not live_restore,
        "restore_state": rs if rs in ("in_progress", "completed", "expired", "failed") else None,
        "restore_expires_at": rfc3339(exp),
        "explicit_tier_set": bool(loc.get("explicit_tier_set")),
        "tier_changed_at": rfc3339(loc.get("tier_changed_at")),
    }


def person(
    *, identity_id: int | None = None, display_name: str | None = None, app: str | None = None
) -> dict[str, Any]:
    """PersonRef. `identity_id` lets the SPA recognise the caller's own rows
    (compare with Me.identity_id); the server still decides what they may do."""
    if app is not None:
        return {"kind": "application", "display_name": None, "app_id": app, "identity_id": None}
    if identity_id is not None:
        return {"kind": "identity", "display_name": display_name, "app_id": None,
                "identity_id": str(identity_id)}  # fmt: skip
    return {"kind": "system", "display_name": None, "app_id": None, "identity_id": None}


def group_ref(sid: str | None, names: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if sid is None:
        return None
    n = names.get(sid) or {}
    return {
        "sid": sid,
        "display_name": n.get("display_name"),
        "sam_account_name": n.get("sam_account_name"),
    }


def worm_state(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(s["worm_enabled"]),
        "mode": s.get("worm_mode"),
        "retention_days": s.get("worm_retention_days"),
        "enabled_at": rfc3339(s.get("worm_enabled_at")),
        "lock_deadline": rfc3339(s.get("worm_lock_deadline")),
        "locked_at": rfc3339(s.get("worm_locked_at")),
        "policy_state": s.get("worm_policy_state") or "none",
    }


def share_json(
    s: dict[str, Any],
    who: Principal,
    *,
    roles: ShareRoles,
    can_read_root: bool,
    checks: list[dict[str, Any]],
    names: dict[str, dict[str, Any]],
    root_dir_id: int,
    dataset_name: str | None = None,
    last_full_acl_scan_at: datetime | None = None,
    path: str | None = None,
    edge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    admin = who.has("ArchiveAdmin")
    return {
        "share_id": str(s["share_id"]),
        "name": s["name"],
        "dataset_id": str(s["dataset_id"]),
        "dataset_name": dataset_name,
        "edge_id": str(s["edge_id"]),
        "root_dir_id": str(root_dir_id),
        "path": path or "/",
        "protocols": list(s["protocols"]),
        "nfs_sec": list(s["nfs_sec"]),
        "nfs_clients": list(s["nfs_clients"]) if admin else [],
        "root_squash": bool(s["root_squash"]),
        "trust_level": s["trust_level"],
        "host_trust_override": bool(s["host_trust_override"]),
        "nfs_export_id": s["nfs_export_id"],
        "smb_encrypt": bool(s["smb_encrypt"]),
        "groups": {
            "owners": group_ref(s["owners_group_sid"], names),
            "writers": group_ref(s.get("writers_group_sid"), names),
            "readers": group_ref(s.get("readers_group_sid"), names),
        },
        "on_open": s["on_open"],
        "wait_threshold_minutes": s.get("wait_threshold_minutes"),
        **quiet_view(s, edge),
        "rewind": {
            "depth_days": s["rewind_depth_days"],
            "max_days": s["rewind_depth_max_days"],
            "labels": s.get("rewind_labels") or {},
        },
        "worm": worm_state(s),
        "last_full_acl_scan_at": rfc3339(last_full_acl_scan_at),
        "my_access": {
            "owner": roles.owner,
            "writer": roles.writer,
            "reader": roles.reader,
            "can_read_root": can_read_root,
        },
        "checks": checks,
        "etag": etag(s["entity_version"]),
    }


def pin_summary(pin: dict[str, Any] | None, file_id: int) -> dict[str, Any] | None:
    if pin is None:
        return None
    return {
        "pin_id": str(pin["pin_id"]),
        "tier": pin["logical_tier"],
        "until": rfc3339(pin["until"]),
        "override_archive": bool(pin["override_archive"]),
        "inherited_from": None if pin["file_id"] == file_id else str(pin["file_id"]),
    }


def holds_json(holds: list[dict[str, Any]], file_id: int) -> dict[str, Any]:
    keep = [h for h in holds if h["kind"] == "keep_online"]
    until = None
    if keep and all(h["until"] is not None for h in keep):
        until = rfc3339(max(h["until"] for h in keep))
    return {
        "keep_online": bool(keep),
        "keep_online_until": until,
        "legal": any(h["kind"] == "legal" for h in holds),
        "items": [
            {
                "hold_id": str(h["hold_id"]),
                "kind": h["kind"],
                "until": rfc3339(h["until"]),
                "inherited_from": None if h["file_id"] == file_id else str(h["file_id"]),
                "set_by_identity_id": sid_(h.get("set_by_identity_id")),
            }
            for h in holds
        ],
    }


def active_recall(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"recall_id": str(r["recall_id"]), "state": r["state"], "eta": rfc3339(r["eta"])}


def entry(
    f: dict[str, Any],
    *,
    loc: dict[str, Any] | None,
    pin: dict[str, Any] | None,
    holds: list[dict[str, Any]],
    recall: dict[str, Any] | None,
    now: datetime,
    move_hold: dict[str, Any] | None = None,
    read_back_failed_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "file_id": str(f["file_id"]),
        "name": f["name"],
        "kind": f["kind"],
        "symlink_target": f.get("symlink_target"),
        "size": f["size"],
        "mtime": mtime(f.get("mtime_ns")),
        "mtime_ns": ns(f.get("mtime_ns")),
        "state": f["state"],
        "resident_bytes": f["resident_bytes"],
        "version_id": sid_(f.get("content_version_id")),
        "tier": tier_state(loc, now),
        "archive_at": day(f.get("archive_at")),
        # While a move is held the scheduler's fields only say when it looks
        # again, under a label that isn't what will happen: `move_hold` says it.
        "next_action": None if move_hold else f.get("next_action"),
        "next_action_at": None if move_hold else rfc3339(f.get("next_action_at")),
        "move_hold": move_hold,
        "read_back_failed_at": rfc3339(read_back_failed_at),
        "pin": pin_summary(pin, f["file_id"]),
        "holds": holds_json(holds, f["file_id"]),
        "active_recall": active_recall(recall),
    }


def hold_json(
    h: dict[str, Any], name: str | None = None, *, for_file: int | None = None
) -> dict[str, Any]:
    """`for_file`: the file a `?file_id=` listing asked about; a hold on a folder
    above it names that folder in `inherited_from`."""
    return {
        "hold_id": str(h["hold_id"]),
        "file_id": str(h["file_id"]),
        "name": name if name is not None else h.get("name") or "",
        "kind": h["kind"],
        "until": rfc3339(h["until"]),
        "reason": h["reason"],
        "set_by": person(
            identity_id=h.get("set_by_identity_id"),
            display_name=h.get("setter_name"),
            app=h.get("set_by_app"),
        ),
        "set_at": rfc3339(h["set_at"]),
        "released_at": rfc3339(h["released_at"]),
        "inherited_from": (
            str(h["file_id"]) if for_file is not None and h["file_id"] != for_file else None
        ),
    }


def pin_json(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "pin_id": str(p["pin_id"]),
        "file_id": str(p["file_id"]),
        "name": p.get("name") or "",
        "kind": p.get("kind") or "file",
        "tier": p["logical_tier"],
        "until": rfc3339(p["until"]),
        "override_archive": bool(p["override_archive"]),
        "reason": p.get("reason"),
        "set_by": person(
            identity_id=p.get("set_by_identity_id"), display_name=p.get("setter_name")
        ),
        "set_at": rfc3339(p["set_at"]),
        "released_at": rfc3339(p["released_at"]),
    }
