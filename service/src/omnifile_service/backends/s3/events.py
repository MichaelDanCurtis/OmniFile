"""S3 events from EventBridge, pulled from SQS, as `ChangeRecord`s.

The bucket sends its events to EventBridge (the bucket's EventBridge setting);
a rule forwards them, unchanged, to an SQS queue that the service reads
through the SQS interface endpoint. Nothing is pushed into the private
network [A][V]. Each SQS message body is one EventBridge event:

    {"version": "0", "detail-type": "Object Restore Completed",
     "source": "aws.s3", "time": "2026-10-14T09:00:00Z",
     "detail": {"bucket": {"name": "b"},
                "object": {"key": "k", "size": 5, "version-id": "v1"},
                "restore-expiry-time": "2026-10-16T00:00:00Z",
                "source-storage-class": "GLACIER"}}

VERIFY on real AWS (docs/milestones/real-aws-checks.md): that keys arrive
unencoded (S3's direct notifications URL-encode them; EventBridge's are not
documented as encoded), and the exact field set of each detail-type.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..base import ChangeKind, ChangeRecord, ObjectRef

#: EventBridge S3 detail-types we act on. Tag and ACL events are ignored.
KINDS: Mapping[str, ChangeKind] = {
    "Object Created": ChangeKind.CREATED,
    "Object Deleted": ChangeKind.DELETED,
    "Object Restore Initiated": ChangeKind.RESTORE_STARTED,
    "Object Restore Completed": ChangeKind.RESTORE_COMPLETED,
    "Object Restore Expired": ChangeKind.RESTORE_EXPIRED,
    "Object Storage Class Changed": ChangeKind.TIER_CHANGED,
    "Object Access Tier Changed": ChangeKind.TIER_CHANGED,
}


def restore_ref(ref: ObjectRef) -> str:
    """A restore's provider reference: one per object version."""
    version = f"@{ref.version_id}" if ref.version_id else ""
    return f"s3-restore:{ref.container}/{ref.key}{version}"


def parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def to_record(backend_id: int, body: str | Mapping[str, Any]) -> ChangeRecord | None:
    """One SQS message body -> a record, or None if it isn't an S3 event we use.

    Raises `ValueError` for a body that claims to be an S3 event but can't be
    read, so the caller can log and drop it rather than loop on it.
    """
    event = json.loads(body) if isinstance(body, str) else dict(body)
    if event.get("source") != "aws.s3":
        return None
    kind = KINDS.get(str(event.get("detail-type", "")))
    if kind is None:
        return None
    try:
        detail = event["detail"]
        obj = detail["object"]
        ref = ObjectRef(
            backend_id, detail["bucket"]["name"], obj["key"], obj.get("version-id") or None
        )
        at = parse_time(event["time"])
    except (KeyError, TypeError) as e:
        raise ValueError(f"malformed S3 event: missing {e}") from None
    # Detail keys are the ones the change-feed worker reads
    # (worker/changefeed_sync.py): `expires_at`, `provider_ref`, `tombstone`.
    extra: dict[str, str] = {}
    for src, dst in (
        ("restore-expiry-time", "expires_at"),
        ("source-storage-class", "source_class"),
        ("deletion-type", "deletion_type"),
        ("reason", "reason"),
        ("request-id", "request_id"),
    ):
        if detail.get(src) is not None:
            extra[dst] = str(detail[src])
    if kind in (
        ChangeKind.RESTORE_STARTED,
        ChangeKind.RESTORE_COMPLETED,
        ChangeKind.RESTORE_EXPIRED,
    ):
        # The same reference `S3Backend.restore` puts on its handle.
        extra["provider_ref"] = restore_ref(ref)
    if kind is ChangeKind.DELETED and detail.get("deletion-type") == "Delete Marker Created":
        extra["tombstone"] = "true"  # a versioned delete keeps every version
    native = detail.get("destination-storage-class") or detail.get("destination-access-tier")
    if kind is ChangeKind.RESTORE_STARTED or kind is ChangeKind.RESTORE_COMPLETED:
        native = native or detail.get("source-storage-class")
    size = obj.get("size")
    return ChangeRecord(
        kind=kind,
        ref=ref,
        at=at,
        native_class=str(native) if native else None,
        size=int(size) if size is not None else None,
        detail=extra,
    )
