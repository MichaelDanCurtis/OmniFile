"""Catalog rows in their wire shapes: FileRow, ShareRow, DatasetRow, AclRow, RecallRow.

The feed and the snapshot serve the same shapes. Fields the spec types as
non-null are omitted when the catalog has NULL; ids go out as strings.
"""

from __future__ import annotations

from typing import Any

from .db import id_str
from .validate import rfc3339

# FileRow: the file, its content version's primary location and backend.
FILE_SELECT = """
SELECT f.file_id, f.dataset_id, f.parent_id, f.name, f.kind, f.symlink_target, f.size,
       f.mtime_ns, f.uid, f.gid, f.mode, f.owner_sid, f.acl_id, f.state, f.resident_bytes,
       f.content_version_id, f.crc64nvme, f.restore_state, f.restore_expires_at,
       f.stub_version_id, f.stub_resident_bytes, f.deleted_at, f.entity_version,
       d.storage_layout,
       l.location_id AS loc_id, l.backend_id AS loc_backend_id, l.container AS loc_container,
       l.object_key AS loc_key, l.backend_version_id AS loc_bvid,
       l.logical_tier AS loc_tier, l.storage_class AS loc_class,
       b.kind AS backend_kind, b.config AS backend_config
  FROM catalog.file f
  JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
  LEFT JOIN catalog.backend b ON b.backend_id = l.backend_id
"""


def wire_backend_kind(kind: str, config: dict[str, Any] | None) -> str:
    """The sim stands in for a provider; on the wire it is that provider."""
    if kind == "sim":
        flavour = (config or {}).get("flavour", "azure_blob")
        return "s3" if flavour == "s3" else "azure_blob"
    return kind


def default_tier(storage_layout: str) -> str:
    """New blobs take the account default: Smart on the two-account live account
    (uploads never set a tier), Hot on a single (WORM) account."""
    return "smart" if storage_layout == "two_account" else "hot"


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        out[key] = value


def file_row(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "file_id": id_str(r["file_id"]),
        "dataset_id": id_str(r["dataset_id"]),
        "parent_id": id_str(r["parent_id"]),
        "name": r["name"],
        "kind": r["kind"],
        "symlink_target": r["symlink_target"],
        "size": r["size"],
        "state": r["state"],
        "resident_bytes": r["resident_bytes"],
        "deleted": r["deleted_at"] is not None,
        "entity_version": id_str(r["entity_version"]),
    }
    if r["mtime_ns"] is not None:
        out["mtime_ns"] = str(r["mtime_ns"])
    _put(out, "uid", r["uid"])
    _put(out, "gid", r["gid"])
    _put(out, "mode", r["mode"])
    _put(out, "owner_sid", r["owner_sid"])
    _put(out, "acl_id", id_str(r["acl_id"]))
    _put(out, "version_id", id_str(r["content_version_id"]))
    if r["crc64nvme"] is not None:
        out["crc64nvme"] = bytes(r["crc64nvme"]).hex()
    out["location"] = location(r)
    out["stub"] = (
        {
            "version_id": id_str(r["stub_version_id"]),
            "resident_bytes": r["stub_resident_bytes"] or 0,
        }
        if r["stub_version_id"] is not None
        else None
    )
    return out


def location(r: dict[str, Any]) -> dict[str, Any] | None:
    if r.get("loc_id") is None:
        return None
    tier = r["loc_tier"] or default_tier(r["storage_layout"])
    archived = tier == "archive"
    restore = r["restore_state"] or ("archived" if archived else "not_needed")
    out: dict[str, Any] = {
        "backend_id": id_str(r["loc_backend_id"]),
        "backend_kind": wire_backend_kind(r["backend_kind"], r["backend_config"]),
        "container": r["loc_container"],
        "object_key": r["loc_key"],
        "backend_version_id": r["loc_bvid"],
        "logical_tier": tier,
        "archived": archived,
        "restore_state": restore,
        "restore_expires_at": rfc3339(r["restore_expires_at"]),
    }
    _put(out, "storage_class", r["loc_class"])
    return out


SHARE_SELECT = "SELECT * FROM catalog.share"


def share_row(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "share_id": id_str(r["share_id"]),
        "dataset_id": id_str(r["dataset_id"]),
        "name": r["name"],
        "root_dir_id": id_str(r["root_dir_id"]),
        "protocols": list(r["protocols"]),
        "nfs_sec": list(r["nfs_sec"]),
        "nfs_clients": list(r["nfs_clients"]),
        "root_squash": r["root_squash"],
        "trust_level": r["trust_level"],
        "host_trust_override": r["host_trust_override"],
        "nfs_export_id": r["nfs_export_id"],
        "smb_encrypt": r["smb_encrypt"],
        "owners_group_sid": r["owners_group_sid"],
        "on_open": r["on_open"],
        "wait_threshold_minutes": r["wait_threshold_minutes"],
        "quiet_s": r["quiet_s"],
        "rewind": {
            "depth_days": r["rewind_depth_days"],
            "max_days": r["rewind_depth_max_days"],
            "labels": r["rewind_labels"],
        },
        "worm": {
            "enabled": r["worm_enabled"],
            "mode": r["worm_mode"],
            "retention_days": r["worm_retention_days"],
            "locked_at": rfc3339(r["worm_locked_at"]),
            "lock_deadline": rfc3339(r["worm_lock_deadline"]),
        },
        "entity_version": id_str(r["entity_version"]),
    }
    _put(out, "writers_group_sid", r["writers_group_sid"])
    _put(out, "readers_group_sid", r["readers_group_sid"])
    return out


DATASET_SELECT = """
SELECT d.*, b.kind AS backend_kind, b.config AS backend_config, b.private_network
  FROM catalog.dataset d JOIN catalog.backend b ON b.backend_id = d.backend_id
"""


def dataset_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": id_str(r["dataset_id"]),
        "name": r["name"],
        "backend": {
            "kind": wire_backend_kind(r["backend_kind"], r["backend_config"]),
            "private_network": r["private_network"],
        },
        "storage_layout": r["storage_layout"],
        "migration_state": r["migration_state"],
        "entity_version": id_str(r["entity_version"]),
    }


ACL_SELECT = "SELECT * FROM catalog.acl"


def acl_row(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "acl_id": id_str(r["acl_id"]),
        "dataset_id": id_str(r["dataset_id"]),
        "sha256": bytes(r["sha256"]).hex(),
        "mode": r["mode"],
        "entries": r["entries"],
    }
    _put(out, "owner_sid", r["owner_sid"])
    _put(out, "group_sid", r["group_sid"])
    return out


RECALL_SELECT = """
SELECT r.*,
       coalesce((SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                    'sid', i.sid, 'via', q.via, 'proof', q.proof)) ORDER BY q.requester_id)
                   FROM catalog.recall_requester q
                   LEFT JOIN catalog.identity i ON i.identity_id = q.identity_id
                  WHERE q.recall_id = r.recall_id), '[]'::jsonb) AS requesters
  FROM catalog.recall r
"""


def recall_row(r: dict[str, Any], read_credential: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "recall_id": id_str(r["recall_id"]),
        "file_id": id_str(r["file_id"]),
        "version_id": id_str(r["version_id"]),
        "state": r["state"],
        "eta": rfc3339(r["eta"]),
        "requesters": r["requesters"],
        "error": r["failure"],
        "read_credential": read_credential,
        "entity_version": id_str(r["entity_version"]),
    }
