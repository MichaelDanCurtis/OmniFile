"""Settings' test sends: a Teams card, an email, an AWS role assumption.

`web` records the request (`catalog.settings_test`) and queues it on
``settings_test`` in the same transaction (decision 27). The worker runs it
(`worker_jobs.py`), because the identities that send cards, mail and assume
the AWS role are the worker's, and writes the outcome to the row. Cards and
mail go only to the person who asked. The portal polls the row.
"""

from __future__ import annotations

from typing import Any

from ..api.validate import rfc3339
from ..web.context import tuple_rows
from ..worker.queue import send

QUEUE = "settings_test"
KINDS = ("teams_card", "email", "aws_assume_role")


def test_json(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "test_id": str(t["test_id"]),
        "kind": t["kind"],
        "state": t["state"],
        "detail": t["detail"],
        "result": dict(t["result"] or {}),
        "created_at": rfc3339(t["created_at"]),
        "finished_at": rfc3339(t["finished_at"]),
    }


async def queue_test(db, kind: str, identity_id: int, params: dict[str, Any], now) -> dict:
    import json

    t = await (
        await db.execute(
            "INSERT INTO catalog.settings_test (kind, identity_id, params, created_at)"
            " VALUES (%s, %s, %s::jsonb, %s) RETURNING *",
            (kind, identity_id, json.dumps(params), now),
        )
    ).fetchone()
    with tuple_rows(db):
        await send(db, QUEUE, {"test_id": str(t["test_id"])})
    return t


async def get_test(db, test_id: int, identity_id: int) -> dict[str, Any] | None:
    return await (
        await db.execute(
            "SELECT * FROM catalog.settings_test WHERE test_id = %s AND identity_id = %s",
            (test_id, identity_id),
        )
    ).fetchone()
