"""`POST /v1/recall-batches` and `GET /v1/recall-batches/{batch_id}`
(portal-api.md section 5.19; the planning is `web/recall_batches.py`).

Apps only, with `Recall.Request`: a batch acts at dataset level, unfiltered by
ACL (section 2.2), so a signed-in user recalls through `POST /v1/recalls`,
file by file, on what they can read. Only the app that made a batch reads it;
another app gets `404`.
"""

from __future__ import annotations

from ..api.validate import parse_id, rfc3339, timestamp
from ..worker.queue import send
from . import recall_batches as rb
from .context import Ctx, Principal, WebResult, audit, tuple_rows
from .problems import WebError, forbidden, invalid, not_found, role_required

QUEUE = "portal"
PRIORITIES = ("standard", "high")


def _app(who: Principal) -> None:
    if not who.is_app:
        raise forbidden("recall batches act on whole datasets and are for apps; recall files"
                        " you can read with POST /v1/recalls")  # fmt: skip
    if not who.has("Recall.Request"):
        raise role_required("Recall.Request")


async def create(ctx: Ctx) -> WebResult:
    who = ctx.who
    _app(who)
    b = ctx.body
    extra = set(b) - {"dataset_id", "paths", "due_by", "priority"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a field of a recall batch")
    if "dataset_id" not in b:
        raise invalid("dataset_id", "is required")
    dataset_id = parse_id(b["dataset_id"], "dataset_id")
    paths = b.get("paths")
    if paths is not None:
        if not isinstance(paths, list) or not paths:
            raise invalid("paths", "must be a non-empty list, or absent for the whole dataset")
        if len(paths) > rb.MAX_PATHS:
            raise WebError("batch_too_large", f"at most {rb.MAX_PATHS} paths per batch")
        norm = []
        for p in paths:
            names = rb.split_path(p) if isinstance(p, str) else None
            if names is None:
                raise invalid("paths", "each must be a dataset path starting with '/'")
            norm.append("/" + "/".join(names))
        paths = list(dict.fromkeys(norm))
    due_by = timestamp(b["due_by"], "due_by") if b.get("due_by") is not None else None
    priority = b.get("priority")
    if priority is not None and priority not in PRIORITIES:
        raise invalid("priority", "must be standard or high, or null for the cheapest on time")
    now = ctx.now
    async with ctx.svc.catalog.tx(who.actor) as db:
        ds = await (
            await db.execute("SELECT 1 FROM catalog.dataset WHERE dataset_id = %s", (dataset_id,))
        ).fetchone()
        if ds is None:
            raise not_found("dataset")
        for p in paths or []:
            if await rb.resolve(db, dataset_id, rb.split_path(p) or []) is None:
                raise not_found(f"path {p}")
        batch = await (
            await db.execute(
                "INSERT INTO catalog.recall_batch (dataset_id, app_id, paths, due_by, priority)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (dataset_id, who.app_id, paths, due_by, priority),
            )
        ).fetchone()
        root_ids = await rb.roots(db, batch)
        n = await rb.count_files(db, root_ids, rb.MAX_FILES)
        if n > rb.MAX_FILES:
            raise WebError("batch_too_large", f"the paths hold more than {rb.MAX_FILES:,} files")
        if n <= rb.INLINE_PLAN_MAX:
            ids = await rb.next_files(db, batch, root_ids, rb.INLINE_PLAN_MAX)
            await rb.plan_files(db, batch, ids, now, {})
            await db.execute(
                "UPDATE catalog.recall_batch SET state = 'starting' WHERE batch_id = %s",
                (batch["batch_id"],),
            )
            batch = await rb.refresh(db, batch["batch_id"])
        else:
            batch = await (
                await db.execute(
                    "UPDATE catalog.recall_batch SET files = %s WHERE batch_id = %s RETURNING *",
                    (n, batch["batch_id"]),
                )
            ).fetchone()
        with tuple_rows(db):
            await send(db, QUEUE, {"kind": "recall_batch", "batch_id": str(batch["batch_id"])})
        await audit(db, who, "recall.batch", dataset_id=dataset_id,
                    details={"batch_id": str(batch["batch_id"]), "paths": paths,
                             "due_by": rfc3339(due_by), "priority": priority,
                             "files": n})  # fmt: skip
        body = rb.batch_json(batch)
    return WebResult(202, body)


async def get_batch(ctx: Ctx) -> WebResult:
    """The batch and a page of its items by file id."""
    who = ctx.who
    _app(who)
    batch_id = ctx.path_id("batch_id")
    kind = f"recall-batch:{batch_id}"
    after = ctx.cursor(kind)
    limit = ctx.limit()
    async with ctx.svc.catalog.read() as db:
        b = await (
            await db.execute(
                "SELECT * FROM catalog.recall_batch WHERE batch_id = %s AND app_id = %s",
                (batch_id, who.app_id),
            )
        ).fetchone()
        if b is None:
            raise not_found("recall batch")
        found = await (
            await db.execute(
                "SELECT * FROM catalog.recall_batch_item WHERE batch_id = %s AND file_id > %s"
                " ORDER BY file_id LIMIT %s",
                (batch_id, int(after[0]) if after else 0, limit + 1),
            )
        ).fetchall()
    more = len(found) > limit
    found = list(found[:limit])
    nxt = ctx.next_cursor(kind, [found[-1]["file_id"]]) if more else None
    return WebResult(200, rb.batch_json(b, found, nxt))
