"""Tasks the worker runs for Settings and the migration wizard (migration 0044):
an AWS fetch, a Storage Mover deployment, a source scan.

`web` records the task (`catalog.cloud_task`) and queues it on ``cloud_task``
in the same transaction (decision 27). The worker (`cloud_jobs.py`) runs it,
because the identities it needs are the worker's: its web identity for AWS,
its managed identity for ARM, its S3 backends for a scan. The portal polls.
"""

from __future__ import annotations

import json
from typing import Any

from ..api.validate import rfc3339
from ..web.context import tuple_rows
from ..worker.queue import send

QUEUE = "cloud_task"
KINDS = ("aws_fetch", "mover_deploy", "source_scan")


def task_json(t: dict[str, Any]) -> dict[str, Any]:
    progress = {k: v for k, v in dict(t["progress"] or {}).items() if not k.startswith("_")}
    return {
        "task_id": str(t["task_id"]),
        "kind": t["kind"],
        "state": t["state"],
        "detail": t["detail"],
        "progress": progress,
        "result": dict(t["result"] or {}),
        "created_at": rfc3339(t["created_at"]),
        "updated_at": rfc3339(t["updated_at"]),
        "finished_at": rfc3339(t["finished_at"]),
    }


async def queue_task(db, kind: str, identity_id: int, params: dict[str, Any], now) -> dict:
    assert kind in KINDS
    t = await (
        await db.execute(
            "INSERT INTO catalog.cloud_task (kind, identity_id, params, created_at, updated_at)"
            " VALUES (%s, %s, %s::jsonb, %s, %s) RETURNING *",
            (kind, identity_id, json.dumps(params), now, now),
        )
    ).fetchone()
    with tuple_rows(db):
        await send(db, QUEUE, {"task_id": str(t["task_id"])})
    return t


async def get_task(db, task_id: int, kind: str | None = None) -> dict[str, Any] | None:
    row = await (
        await db.execute("SELECT * FROM catalog.cloud_task WHERE task_id = %s", (task_id,))
    ).fetchone()
    if row is None or (kind is not None and row["kind"] != kind):
        return None
    return row


async def latest(db, kind: str) -> dict[str, Any] | None:
    return await (
        await db.execute(
            "SELECT * FROM catalog.cloud_task WHERE kind = %s ORDER BY task_id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()
