"""Recall batches (portal-api.md section 5.19): recalls of paths or a whole
dataset by a due date, planned file by file. Ported from line B (8344f8a).

The rules, per file:
- **With a priority given:** the files that offer it use it; the others are
  `skipped`, with a reason.
- **Otherwise, with `due_by`:** the cheapest offered priority whose ETA meets
  `due_by`. If none does, the fastest offered one, and the file counts as
  `late`.
- **Otherwise:** `standard`.

ETAs and fees come from `web.recalls.recall_options` (`capabilities()` and
the price tables): no time above the no-promise size, and no High where it
can't be faster (anti-pattern 5).

The request plans small batches itself, so its `202` carries the ETA and the
cost. The worker (`portal` queue, kind `recall_batch`) plans the rest in
chunks and then starts or joins each planned file's one active recall
(`recall_one_active`), with requester `via = api` and the app's id.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from ..api.validate import rfc3339
from ..policy import cost as cost_mod
from .facts import price_override
from .recalls import archived, recall_options, start_or_join, version_location

#: Batches up to this many files are planned in the request.
INLINE_PLAN_MAX = 1000
MAX_FILES = 1_000_000
MAX_PATHS = 100
#: Files planned or started per worker transaction.
CHUNK = 500

EXPAND_SQL = """
WITH RECURSIVE t(file_id, kind) AS (
    SELECT file_id, kind FROM catalog.file WHERE file_id = ANY(%(roots)s) AND deleted_at IS NULL
    UNION ALL
    SELECT c.file_id, c.kind FROM catalog.file c JOIN t ON c.parent_id = t.file_id
     WHERE t.kind = 'dir' AND c.deleted_at IS NULL
)
SELECT DISTINCT t.file_id FROM t
 WHERE t.kind = 'file'
   AND NOT EXISTS (SELECT 1 FROM catalog.recall_batch_item i
                    WHERE i.batch_id = %(batch)s AND i.file_id = t.file_id)
 ORDER BY t.file_id LIMIT %(n)s
"""

PATH_SQL = """
SELECT coalesce('/' || string_agg(a.name, '/' ORDER BY a.depth DESC), '/') AS path
  FROM catalog.file_ancestors(%s) a WHERE a.parent_id IS NOT NULL
"""


def split_path(value: str) -> list[str] | None:
    """A dataset path's names, or None if it isn't one."""
    if not value.startswith("/") or "//" in value.rstrip("/") or len(value) > 4096:
        return None
    names = [n for n in value.strip("/").split("/") if n]
    if any(n in (".", "..") or "\x00" in n for n in names):
        return None
    return names


async def resolve(db, dataset_id: int, names: list[str]) -> int | None:
    """The live entry at a dataset path, or None."""
    row = await (
        await db.execute(
            "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
            (dataset_id,),
        )
    ).fetchone()
    for name in names:
        if row is None:
            return None
        row = await (
            await db.execute(
                "SELECT file_id FROM catalog.file WHERE parent_id = %s AND name = %s"
                "   AND deleted_at IS NULL",
                (row["file_id"], name),
            )
        ).fetchone()
    return None if row is None else int(row["file_id"])


async def roots(db, batch: dict[str, Any]) -> list[int]:
    if batch["paths"] is None:
        root = await resolve(db, batch["dataset_id"], [])
        return [] if root is None else [root]
    out = []
    for p in batch["paths"]:
        names = split_path(p)
        fid = None if names is None else await resolve(db, batch["dataset_id"], names)
        if fid is not None:
            out.append(fid)
    return out


async def count_files(db, root_ids: list[int], cap: int) -> int:
    row = await (
        await db.execute(
            "SELECT count(*) AS n FROM (" + EXPAND_SQL + ") x",
            {"roots": root_ids, "batch": 0, "n": cap + 1},
        )
    ).fetchone()
    return int(row["n"])


async def next_files(db, batch: dict[str, Any], root_ids: list[int], n: int) -> list[int]:
    found = await (
        await db.execute(EXPAND_SQL, {"roots": root_ids, "batch": batch["batch_id"], "n": n})
    ).fetchall()
    return [int(r["file_id"]) for r in found]


async def _price_table(db, kind: str) -> dict[str, Any] | None:
    try:
        return cost_mod.load_price_table(kind, await price_override(db, kind)).to_json()
    except Exception:  # noqa: BLE001 - fees are an estimate; no table, no fee
        return None


def choose(options: list[dict[str, Any]], *, asked: str | None, due_by: datetime | None):
    """(option, late, reason): the option to use, or None with a reason."""
    offered = [o for o in options if o["offered"]]
    if not offered:
        return None, False, "no recall option is offered for this file"
    if asked is not None:
        o = next((o for o in offered if o["priority"] == asked), None)
        return (o, False, None) if o else (None, False, f"{asked} is not offered for this file")
    if due_by is None:
        o = next((o for o in offered if o["priority"] == "standard"), offered[0])
        return o, False, None
    # Options are listed cheapest first (standard, then high).
    for o in offered:
        eta = o["eta"]
        if eta is not None and _ts(eta) <= due_by:
            return o, False, None
    timed = [o for o in offered if o["eta"] is not None]
    fastest = min(timed, key=lambda o: _ts(o["eta"])) if timed else offered[0]
    return fastest, True, None


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


async def plan_files(db, batch: dict[str, Any], file_ids: list[int], now: datetime,
                     tables: dict[str, Any]) -> None:  # fmt: skip
    """One recall_batch_item per file: planned, online or skipped. Each fee
    keeps the currency of the price table that gave it."""
    for fid in file_ids:
        f = await (
            await db.execute("SELECT * FROM catalog.file WHERE file_id = %s", (fid,))
        ).fetchone()
        path = (await (await db.execute(PATH_SQL, (fid,))).fetchone())["path"]
        status, prio, reason, eta, late, fee, vid = "skipped", None, None, None, False, None, None
        currency = None
        size = int(f["size"] or 0) if f else 0
        if f is None or f["content_version_id"] is None:
            reason = "the file has no stored bytes yet"
        else:
            vid = f["content_version_id"]
            loc = await version_location(db, fid, vid)
            if loc["location_id"] is None:
                reason = "the file has no stored bytes yet"
            elif not archived(loc, now):
                status = "online"
            else:
                kind = loc["backend_kind"]
                if kind not in tables:
                    tables[kind] = await _price_table(db, kind)
                opts = (await recall_options(db, loc, now, tables[kind]))["options"]
                o, late, reason = choose(opts, asked=batch["priority"], due_by=batch["due_by"])
                if o is not None:
                    status, prio = "planned", o["priority"]
                    eta = _ts(o["eta"]) if o["eta"] else None
                    if o["fee_estimate"]:
                        fee = Decimal(o["fee_estimate"]["amount"])
                        currency = o["fee_estimate"]["currency"] or "USD"
        await db.execute(
            "INSERT INTO catalog.recall_batch_item (batch_id, file_id, version_id, path, status,"
            " priority, reason, eta, late, fee, fee_currency, size)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (batch["batch_id"], fid, vid, path, status, prio, reason, eta, late, fee, currency,
             size),
        )  # fmt: skip


async def refresh(db, batch_id: int) -> dict[str, Any]:
    """Recompute the batch's counts, ETA and costs from its items: one fee
    total per currency, never an amount added across currencies."""
    return await (
        await db.execute(
            """
            UPDATE catalog.recall_batch b SET
                files = x.files, bytes = x.bytes, planned = x.planned, started = x.started,
                joined = x.joined, online = x.online, skipped = x.skipped, late = x.late,
                eta = x.eta, eta_known = x.eta_known, updated_at = now(),
                costs = coalesce((
                    SELECT jsonb_agg(jsonb_build_object('amount', c.amount::text,
                                                        'currency', c.currency)
                                     ORDER BY c.currency)
                      FROM (SELECT i2.fee_currency AS currency, sum(i2.fee) AS amount
                              FROM catalog.recall_batch_item i2
                             WHERE i2.batch_id = b.batch_id AND i2.fee IS NOT NULL
                             GROUP BY i2.fee_currency) c), '[]'::jsonb)
              FROM (SELECT count(*) AS files, coalesce(sum(i.size), 0) AS bytes,
                           count(*) FILTER (WHERE i.status = 'planned') AS planned,
                           count(*) FILTER (WHERE i.status = 'started') AS started,
                           count(*) FILTER (WHERE i.status = 'joined') AS joined,
                           count(*) FILTER (WHERE i.status = 'online') AS online,
                           count(*) FILTER (WHERE i.status = 'skipped') AS skipped,
                           count(*) FILTER (WHERE i.late) AS late,
                           max(i.eta) AS eta,
                           coalesce(NOT bool_or(i.eta IS NULL AND i.status IN
                                    ('planned', 'started', 'joined')), true) AS eta_known
                      FROM catalog.recall_batch_item i
                     WHERE i.batch_id = %s) x
             WHERE b.batch_id = %s
            RETURNING b.*
            """,
            (batch_id, batch_id),
        )
    ).fetchone()


async def start_planned(db, batch: dict[str, Any], now: datetime, limit: int) -> int:
    """Start or join the recalls of up to `limit` planned items; returns how many."""
    items = await (
        await db.execute(
            "SELECT * FROM catalog.recall_batch_item WHERE batch_id = %s AND status = 'planned'"
            " ORDER BY file_id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch["batch_id"], limit),
        )
    ).fetchall()
    for it in items:
        f = await (
            await db.execute("SELECT * FROM catalog.file WHERE file_id = %s", (it["file_id"],))
        ).fetchone()
        loc = await version_location(db, it["file_id"], it["version_id"])
        if f is None or f["deleted_at"] is not None or loc["location_id"] is None:
            status, recall_id, reason = "skipped", None, "the file is gone"
        elif not archived(loc, now):
            status, recall_id, reason = "online", None, None
        else:
            recall, joined = await start_or_join(
                db, file=f, loc=loc, priority=it["priority"], due_by=batch["due_by"],
                identity_id=None, app_id=batch["app_id"], now=now,
            )  # fmt: skip
            status = "joined" if joined else "started"
            recall_id, reason = recall["recall_id"], None
        await db.execute(
            "UPDATE catalog.recall_batch_item SET status = %s, recall_id = %s,"
            " reason = coalesce(%s, reason) WHERE batch_id = %s AND file_id = %s",
            (status, recall_id, reason, batch["batch_id"], it["file_id"]),
        )
    return len(items)


async def step(db, batch_id: int, now: datetime) -> bool:
    """One worker step on the caller's transaction: plan a chunk, or start a
    chunk. Returns True while there is more to do."""
    batch = await (
        await db.execute(
            "SELECT * FROM catalog.recall_batch WHERE batch_id = %s FOR UPDATE", (batch_id,)
        )
    ).fetchone()
    if batch is None or batch["state"] == "done":
        return False
    if batch["state"] == "planning":
        ids = await next_files(db, batch, await roots(db, batch), CHUNK)
        if ids:
            await plan_files(db, batch, ids, now, {})
        else:
            await db.execute(
                "UPDATE catalog.recall_batch SET state = 'starting' WHERE batch_id = %s",
                (batch_id,),
            )
        await refresh(db, batch_id)
        return True
    if await start_planned(db, batch, now, CHUNK) == 0:
        await db.execute(
            "UPDATE catalog.recall_batch SET state = 'done', done_at = %s WHERE batch_id = %s",
            (now, batch_id),
        )
        await refresh(db, batch_id)
        return False
    await refresh(db, batch_id)
    return True


def _money(amount: Decimal | None, currency: str | None) -> dict[str, str] | None:
    if amount is None:
        return None
    return {"amount": format(Decimal(amount).quantize(Decimal("0.01")), "f"),
            "currency": currency or "USD"}  # fmt: skip


def batch_json(b: dict[str, Any], items: list[dict[str, Any]] | None = None,
               next_cursor: str | None = None) -> dict[str, Any]:  # fmt: skip
    eta_known = bool(b["eta_known"]) and b["state"] != "planning"
    costs = [_money(Decimal(c["amount"]), c["currency"]) for c in b["costs"] or []]
    out: dict[str, Any] = {
        "batch_id": str(b["batch_id"]),
        "dataset_id": str(b["dataset_id"]),
        "paths": None if b["paths"] is None else list(b["paths"]),
        "due_by": rfc3339(b["due_by"]),
        "priority": b["priority"],
        "state": b["state"],
        "counts": {
            k: int(b[k])
            for k in ("files", "planned", "started", "joined", "online", "skipped", "late")
        },
        "bytes": int(b["bytes"]),
        "eta": rfc3339(b["eta"]) if eta_known else None,
        "eta_confidence": "provider_sla" if eta_known and b["eta"] else "none",
        # One total per currency; `cost` is that total when there is one
        # currency, and null when fees are in several (never added across).
        "costs": costs,
        "cost": costs[0] if len(costs) == 1 else None,
        "created_at": rfc3339(b["created_at"]),
        "done_at": rfc3339(b["done_at"]),
    }
    if items is not None:
        out["items"] = [
            {
                "file_id": str(i["file_id"]),
                "path": i["path"],
                "status": i["status"],
                "priority": i["priority"],
                "reason": i["reason"],
                "eta": rfc3339(i["eta"]),
                "late": bool(i["late"]),
                "fee": _money(i["fee"], i["fee_currency"]),
                "recall_id": None if i["recall_id"] is None else str(i["recall_id"]),
            }
            for i in items
        ]
        out["next_cursor"] = next_cursor
    return out
