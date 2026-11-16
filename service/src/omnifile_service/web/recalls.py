"""Recall planning and start-or-join for the portal (portal-api.md 5.5).

The same rules as the edge's `POST /recalls` (api/recalls.py): ETAs from
`capabilities()` through `policy.recall.plan_recall`, no time above the
no-promise size, and one active recall per (file, content version)
(`recall_one_active`). A new recall goes on `restores` and every new
requester on `notify`, in the same transaction (decision 27).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..api.recalls import promise
from ..api.validate import rfc3339
from ..backends.base import Capabilities, RestoreSpeed
from ..policy.cost import GiB
from ..policy.recall import PipelineEntry, account_of, offers, plan_recall
from ..worker.backends import capabilities_for
from . import rows
from .problems import WebError, not_found

STEPS = ("requested", "rehydrating", "downloading", "available")
STEP_OF = {"requested": 0, "rehydrating": 1, "copying": 1, "downloading": 2, "available": 3}

LOCATION_SQL = """
SELECT v.version_id, v.file_id, v.kind AS version_kind, v.state AS version_state, v.size,
       v.acl_id AS version_acl_id,
       CASE WHEN v.kind = 'content' THEN v.version_id ELSE v.content_version_id END
           AS content_id,
       l.location_id, l.backend_id, l.container, l.object_key, l.backend_version_id,
       l.logical_tier, l.storage_class, l.explicit_tier_set, l.tier_changed_at,
       b.kind AS backend_kind, b.config AS backend_config,
       r.state AS restore_state, r.expires_at AS restore_expires_at
  FROM catalog.file_version v
  LEFT JOIN catalog.location l
         ON l.version_id = CASE WHEN v.kind = 'content' THEN v.version_id
                                ELSE v.content_version_id END
        AND l.role = 'primary' AND l.state = 'committed'
  LEFT JOIN catalog.backend b ON b.backend_id = l.backend_id
  LEFT JOIN LATERAL (
        SELECT state, expires_at FROM catalog.restore
         WHERE location_id = l.location_id AND state <> 'cancelled'
         ORDER BY requested_at DESC, restore_id DESC LIMIT 1) r ON true
 WHERE v.version_id = %(v)s AND v.file_id = %(f)s
"""


async def version_location(db, file_id: int, version_id: int) -> dict[str, Any]:
    row = await (await db.execute(LOCATION_SQL, {"v": version_id, "f": file_id})).fetchone()
    if row is None:
        raise not_found("version")
    return row


def archived(loc: dict[str, Any], now: datetime) -> bool:
    return bool(rows.tier_state(loc, now)["archived"])


def caps_for(loc: dict[str, Any]) -> Capabilities | None:
    if loc.get("backend_kind") is None:
        return None
    try:
        caps = capabilities_for(loc["backend_kind"], loc["backend_config"] or {})
    except (ValueError, KeyError, TypeError):
        return None
    native = loc.get("storage_class")
    if native:
        fits = [o for o in caps.restore_options if native in o.from_classes]
        if fits:
            caps = replace(caps, restore_options=fits)
    return caps


async def high_backlog(db, loc: dict[str, Any]) -> list[PipelineEntry]:
    """Open High restores on the same account (the 10 GiB/h budget)."""
    account = account_of(loc["container"])
    found = await (
        await db.execute(
            "SELECT r.requested_at, coalesce(r.bytes, l.size, 0) AS size"
            "  FROM catalog.restore r JOIN catalog.location l ON l.location_id = r.location_id"
            " WHERE r.backend_id = %s AND r.speed = 'high'"
            "   AND r.state IN ('requested', 'in_progress')"
            "   AND split_part(l.container, '/', 1) = %s",
            (loc["backend_id"], account),
        )
    ).fetchall()
    return [PipelineEntry(r["requested_at"], int(r["size"])) for r in found]


def _fee(table: dict[str, Any] | None, native: str | None, size: int, high: bool):
    if not table or not native:
        return None
    cls = (table.get("classes") or {}).get(native) or {}
    per_gb = cls.get("retrieval_per_gb_high" if high else "retrieval_per_gb")
    if per_gb is None:
        return None
    try:
        # Per GB as the provider bills it: 2^30 bytes, as policy/cost.py.
        amount = Decimal(str(per_gb)) * Decimal(size) / Decimal(GiB)
    except ArithmeticError:
        return None
    return {"amount": format(amount.quantize(Decimal("0.01")), "f"),
            "currency": table.get("currency") or "USD"}  # fmt: skip


async def active_recall(db, file_id: int, content_id: int) -> dict[str, Any] | None:
    return await (
        await db.execute(
            "SELECT recall_id, state, eta FROM catalog.recall WHERE file_id = %s"
            " AND version_id = %s AND state = ANY(%s)",
            (file_id, content_id, list(rows.ACTIVE_RECALL)),
        )
    ).fetchone()


async def recall_options(
    db, loc: dict[str, Any], now: datetime, price_table: dict[str, Any] | None
) -> dict[str, Any]:
    needs = archived(loc, now) if loc.get("location_id") else False
    caps = caps_for(loc)
    size = int(loc.get("size") or 0)
    options: list[dict[str, Any]] = []
    no_promise = None
    if caps is not None:
        std = next((o for o in caps.restore_options if o.speed is RestoreSpeed.STANDARD), None)
        no_promise = std.no_promise_above_bytes if std else None
    if needs and caps is not None:
        native = loc.get("storage_class")
        if offers(caps, RestoreSpeed.STANDARD):
            plan = plan_recall(caps, size, "standard", now)
            options.append({
                "priority": "standard", "offered": True, "reason": None,
                "eta": rfc3339(plan.eta),
                "eta_confidence": "provider_sla" if plan.eta else "none",
                "fee_estimate": _fee(price_table, native, size, False),
            })  # fmt: skip
        if offers(caps, RestoreSpeed.HIGH):
            plan = plan_recall(caps, size, "high", now, high_backlog=await high_backlog(db, loc))
            offered = plan.speed is RestoreSpeed.HIGH and not plan.downgraded
            options.append({
                "priority": "high", "offered": offered,
                "reason": None if offered else (plan.reason or "High would not be faster"),
                "eta": rfc3339(plan.eta) if offered else None,
                "eta_confidence": "provider_sla" if offered and plan.eta else "none",
                "fee_estimate": _fee(price_table, native, size, True) if offered else None,
            })  # fmt: skip
        else:
            options.append({
                "priority": "high", "offered": False,
                "reason": "this storage class has no priority restore",
                "eta": None, "eta_confidence": "none", "fee_estimate": None,
            })  # fmt: skip
    ar = await active_recall(db, loc["file_id"], loc["content_id"]) if loc["content_id"] else None
    return {
        "file_id": str(loc["file_id"]),
        "version_id": str(loc["version_id"]),
        "size": size,
        "needs_restore": needs,
        "online": not needs and loc.get("location_id") is not None,
        "no_promise_above_bytes": no_promise,
        "active_recall": rows.active_recall(ar),
        "options": options,
    }


async def start_or_join(
    db,
    *,
    file: dict[str, Any],
    loc: dict[str, Any],
    priority: str,
    due_by: datetime | None,
    identity_id: int | None,
    app_id: str | None,
    now: datetime,
    hold: bool = False,
) -> tuple[dict[str, Any], bool]:
    """(recall row, joined). The requester is recorded via portal (a user)
    or api (an app), proof proven (Entra).

    `hold`: the caller is about to make a job that waits on this recall (a
    version restore, an annex placement), in this transaction. The catalog
    counts such jobs on the recall (`held_by_jobs`, migration 0059), and a
    recall with one is not ended when its file is replaced or deleted. A
    recall that is joined is locked here, so a commit that would end it waits
    for this transaction and then sees the job."""
    edge = await (
        await db.execute(
            "SELECT edge_id FROM catalog.dataset WHERE dataset_id = %s", (file["dataset_id"],)
        )
    ).fetchone()
    eta, _ = promise(
        loc["backend_kind"], loc["backend_config"] or {}, int(loc["size"] or 0), now,
        priority=priority, native_class=loc.get("storage_class"),
    )  # fmt: skip
    created = await (
        await db.execute(
            "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority, due_by, eta)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (file_id, version_id)"
            "   WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')"
            " DO NOTHING RETURNING *",
            (
                file["file_id"],
                loc["content_id"],
                edge["edge_id"] if edge else None,
                priority,
                due_by,
                eta,
            ),
        )  # fmt: skip
    ).fetchone()
    joined = created is None
    if joined:
        recall = await (
            await db.execute(
                "SELECT * FROM catalog.recall WHERE file_id = %s AND version_id = %s"
                " AND state = ANY(%s)" + (" FOR UPDATE" if hold else ""),
                (file["file_id"], loc["content_id"], list(rows.ACTIVE_RECALL)),
            )
        ).fetchone()
        if recall is None:
            raise WebError("request_in_progress", "retry", retry_after_s=1)
    else:
        recall = created
        await db.execute(
            "SELECT pgmq.send('restores', %s::jsonb)",
            (json.dumps({"recall_id": str(recall["recall_id"])}),),
        )
    added = await (
        await db.execute(
            "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof, app_id)"
            " VALUES (%s, %s, %s, 'proven', %s) ON CONFLICT DO NOTHING RETURNING requester_id",
            (recall["recall_id"], identity_id, "api" if app_id else "portal", app_id),
        )
    ).fetchone()
    if added is not None and identity_id is not None:
        await db.execute(
            "SELECT pgmq.send('notify', %s::jsonb)",
            (json.dumps({"recall_id": str(recall["recall_id"])}),),
        )
    return recall, joined


def steps(state: str, log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The four-step view from recall_state_log."""
    first: dict[int, datetime] = {}
    reached = 0
    for e in log:
        i = STEP_OF.get(e["state"])
        if i is not None:
            first.setdefault(i, e["at"])
            reached = max(reached, i)
    out = []
    failed = state in ("failed", "cancelled")
    current = STEP_OF.get(state, reached)
    for i, name in enumerate(STEPS):
        if state == "superseded":
            # Stopped, not failed: the steps it reached, and none after.
            status = "done" if i <= reached and i in first else "pending"
        elif failed:
            status = "done" if i <= reached and i < reached + 1 and i in first else "pending"
            if i == min(reached + 1, 3) and status != "done":
                status = "failed"
            if i == reached and reached == 3:
                status = "done"
        elif state == "available":
            status = "done"
        elif i < current:
            status = "done"
        elif i == current:
            status = "current"
        else:
            status = "pending"
        out.append({"step": name, "status": status, "at": rfc3339(first.get(i))})
    return out


SUPERSEDED_TEXT = {
    "replaced": "The file was replaced while it was being recalled.",
    "deleted": "The file was deleted while it was being recalled.",
}


async def superseded_by(db, r: dict[str, Any]) -> str | None:
    """Who replaced or deleted the file a `superseded` recall was for: the
    display name of the version's actor, when the edge proved who it was (an
    NFS `sec=sys` uid is only claimed, and names no one here). For callers
    who can read the file only: the portal shows them who uploads to it."""
    if r.get("superseded_by_version_id") is None:
        return None
    row = await (
        await db.execute(
            "SELECT i.display_name FROM catalog.file_version v"
            " JOIN catalog.identity i ON i.identity_id = v.actor_identity_id"
            " WHERE v.version_id = %s AND v.file_id = %s AND v.actor_proof = 'proven'",
            (r["superseded_by_version_id"], r["file_id"]),
        )
    ).fetchone()
    return row["display_name"] if row else None


async def recall_json(
    db, r: dict[str, Any], *, name: str | None, share_id: int | None, me: bool,
    parent_id: int | None = None,
) -> dict[str, Any]:  # fmt: skip
    """`name`, `share_id` and `parent_id` are None unless the caller can read the file."""
    superseded = None
    if r["state"] == "superseded":
        reason = r["superseded_reason"]
        superseded = {
            "reason": reason,
            "text": SUPERSEDED_TEXT.get(reason, "The file changed while it was being recalled."),
            # Only a caller who can read the file (`name` is theirs to see) is told who.
            "by": await superseded_by(db, r) if name is not None else None,
        }
    log = await (
        await db.execute(
            "SELECT state, at FROM catalog.recall_state_log WHERE recall_id = %s"
            " ORDER BY at, log_id",
            (r["recall_id"],),
        )
    ).fetchall()
    count = await (
        await db.execute(
            "SELECT count(*) AS n FROM catalog.recall_requester WHERE recall_id = %s",
            (r["recall_id"],),
        )
    ).fetchone()
    return {
        "recall_id": str(r["recall_id"]),
        "file_id": str(r["file_id"]),
        "version_id": str(r["version_id"]),
        "name": name,
        "share_id": None if share_id is None else str(share_id),
        "parent_id": None if parent_id is None else str(parent_id),
        "state": r["state"],
        "priority": r["priority"],
        "eta": rfc3339(r["eta"]),
        "eta_confidence": "provider_sla" if r["eta"] else "none",
        "due_by": rfc3339(r["due_by"]),
        "requested_at": rfc3339(r["requested_at"]),
        "completed_at": rfc3339(r["completed_at"]),
        "failure": "The recall could not be completed." if r["state"] == "failed" else None,
        "superseded": superseded,
        "steps": steps(r["state"], list(log)),
        "requesters": {"count": count["n"], "me": me},
    }
