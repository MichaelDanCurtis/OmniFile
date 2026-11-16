"""The edge event feed (control-api.md section 5) and the snapshot (section 6).

Cursor: "<txid>.<event_id>", opaque to the edge, starting at "0.0". After a
restore of the catalog (migration 0019) it gains a third part, the feed's
incarnation; a cursor from another incarnation gets `reset_required`. Events
are read with catalog.feed_read below the horizon (the oldest running
transaction), so a late-committing transaction is never skipped. When a page
comes back short, every event below the horizon has been seen and `next`
moves to "<horizon>.0", past events this edge does not receive.

Events are thin in the catalog; each is served with the entity's *current*
state, and its current `entity_version`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

from .. import exports
from ..backends.base import EdgeAccess
from .context import Ctx
from .data import read_credential
from .dataplane import Target
from .db import id_str
from .idempotency import Result
from .problems import ApiError
from .rows import (
    ACL_SELECT,
    DATASET_SELECT,
    FILE_SELECT,
    RECALL_SELECT,
    SHARE_SELECT,
    acl_row,
    dataset_row,
    file_row,
    recall_row,
    share_row,
)
from .validate import cursor, cursor_incarnation, opt_id, query_int, req, req_id

#: The read SAS of an export's parts (control-api.md section 6).
EXPORT_SAS_TTL = timedelta(hours=24)

Cursor = tuple[int, int]


def fmt_cursor(c: Cursor, incarnation: int = 0) -> str:
    base = f"{c[0]}.{c[1]}"
    return base if incarnation == 0 else f"{base}.{incarnation}"


async def feed_incarnation(db) -> int:
    row = await (await db.execute("SELECT incarnation FROM catalog.feed_retention")).fetchone()
    return int(row["incarnation"]) if row else 0


def stale_incarnation(raw: str, current: int) -> bool:
    """A cursor issued before the catalog was restored (not "0.0", which
    means everything kept, in any incarnation)."""
    return raw != "0.0" and cursor_incarnation(raw) != current


async def edge_datasets(db, edge_id: int) -> list[int]:
    rows = await (
        await db.execute(
            # An annex's dataset holds copies of NAS files the edge never
            # serves (annex.md section 6.3): no placeholders, so no feed.
            "SELECT dataset_id FROM catalog.dataset d WHERE edge_id = %s"
            " AND NOT EXISTS (SELECT 1 FROM catalog.annex a WHERE a.dataset_id = d.dataset_id)",
            (edge_id,),
        )
    ).fetchall()
    return [r["dataset_id"] for r in rows]


async def horizon(db) -> int:
    row = await (await db.execute("SELECT catalog.feed_horizon()::text AS h")).fetchone()
    return int(row["h"])


async def retention_floor(db) -> Cursor:
    row = await (
        await db.execute(
            "SELECT dropped_through_txid::text AS t, dropped_through_event AS e"
            "  FROM catalog.feed_retention"
        )
    ).fetchone()
    return (int(row["t"]), row["e"]) if row else (0, 0)


async def read_page(db, edge_id: int, after: Cursor, limit: int) -> tuple[list[dict], Cursor]:
    h = await horizon(db)
    datasets = await edge_datasets(db, edge_id)
    rows = await (
        await db.execute(
            "SELECT txid::text AS txid, event_id, dataset_id, entity, entity_id, op,"
            "       origin_edge_id, origin_op_id, detail"
            "  FROM catalog.feed_read(%s::xid8, %s::bigint, %s::xid8, %s::bigint, %s::bigint[],"
            "                        %s::integer)",
            (str(after[0]), after[1], str(h), edge_id, datasets, limit),
        )
    ).fetchall()
    if len(rows) >= limit:
        last = rows[-1]
        nxt = (int(last["txid"]), last["event_id"])
    else:
        nxt = max(after, (h, 0))
    return rows, nxt


async def count_after(db, edge_id: int, after: Cursor, cap: int = 10_000) -> int:
    rows, _ = await read_page(db, edge_id, after, cap)
    return len(rows)


async def build_events(ctx: Ctx, db, rows: list[dict]) -> list[dict[str, Any]]:
    """Join each thin event to its entity's current state."""
    ids: dict[str, set[int]] = {}
    for r in rows:
        ids.setdefault(r["entity"], set()).add(r["entity_id"])

    async def fetch(sql: str, key: str, entity: str) -> dict[int, dict]:
        wanted = sorted(ids.get(entity, ()))
        if not wanted:
            return {}
        alias = {"file": "f.", "recall": "r.", "dataset": "d."}.get(entity, "")
        res = await (
            await db.execute(f"{sql} WHERE {alias}{key} = ANY(%s::bigint[])", (wanted,))
        ).fetchall()
        return {x[key]: x for x in res}

    files = await fetch(FILE_SELECT, "file_id", "file")
    shares = await fetch(SHARE_SELECT, "share_id", "share")
    datasets = await fetch(DATASET_SELECT, "dataset_id", "dataset")
    acls = await fetch(ACL_SELECT, "acl_id", "acl")
    recalls = await fetch(RECALL_SELECT, "recall_id", "recall")
    await speak_the_edges_states(db, ctx.edge_id, recalls)
    creds = await recall_credentials(
        ctx, db, [k for k, v in recalls.items() if v["state"] == "downloading"]
    )

    out: list[dict[str, Any]] = []
    for r in rows:
        entity, eid = r["entity"], r["entity_id"]
        origin = (
            {"edge_id": id_str(r["origin_edge_id"]), "op_id": r["origin_op_id"]}
            if r["origin_edge_id"] is not None and r["origin_op_id"]
            else None
        )
        ev: dict[str, Any] = {"entity_id": id_str(eid), "origin": origin}
        if entity == "file" and eid in files:
            row = file_row(files[eid])
            ev["entity_version"] = row["entity_version"]
            if r["op"] == "conflict":
                detail = r["detail"] or {}
                ev["type"] = "file.conflict"
                ev["conflict"] = {
                    "op_id": detail.get("op_id", r["origin_op_id"] or ""),
                    "reason": detail.get("reason", "conflict"),
                    "current": row,
                }
            else:
                ev["type"] = (
                    "file.tombstone"
                    if row["deleted"]
                    else "file.rename"
                    if r["op"] == "rename"
                    else "file.upsert"
                )
                ev["file"] = row
        elif entity == "share" and r["op"] == "delete":
            detail = r["detail"] or {}
            ev.update(type="share.delete", share_id=id_str(eid))
            ev["entity_version"] = id_str(detail.get("entity_version", 0))
        elif entity == "share" and eid in shares:
            ev.update(type="share.upsert", share=share_row(shares[eid]))
            ev["entity_version"] = ev["share"]["entity_version"]
        elif entity == "dataset" and eid in datasets:
            ev.update(type="dataset.upsert", dataset=dataset_row(datasets[eid]))
            ev["entity_version"] = ev["dataset"]["entity_version"]
        elif entity == "acl" and eid in acls:
            # ACLs are immutable (content-addressed): version 1 forever.
            ev.update(type="acl.upsert", acl=acl_row(acls[eid]), entity_version="1")
        elif entity == "recall" and eid in recalls:
            ev.update(type="recall.state", recall=recall_row(recalls[eid], creds.get(eid)))
            ev["entity_version"] = ev["recall"]["entity_version"]
        else:
            continue  # an entity this edge doesn't replicate, or one that is gone
        out.append(ev)
    return out


async def speak_the_edges_states(db, edge_id: int, recalls: dict[int, dict]) -> None:
    """`superseded` (migration 0059) is sent as `cancelled` to an edge that has not
    said it knows it (`recall_superseded` in its heartbeat).

    Edges are updated after the service, site by site. One that predates the state
    reads it as unknown: it ends nothing, and a reader of a `wait` share that still
    holds the file open would be told to wait for good. `cancelled` is a state
    every edge knows, and to an edge the two mean the same: stop, and let the next
    read look again. An edge that has not sent a heartbeat yet is treated as old."""
    ended = [r for r in recalls.values() if r["state"] == "superseded"]
    if not ended:
        return
    row = await (
        await db.execute(
            "SELECT coalesce((health->>'recall_superseded')::bigint, 0) >= 1 AS knows"
            "  FROM catalog.edge WHERE edge_id = %s",
            (edge_id,),
        )
    ).fetchone()
    if row and row["knows"]:
        return
    for r in ended:
        r["state"] = "cancelled"


async def recall_credentials(ctx: Ctx, db, recall_ids: list[int]) -> dict[int, dict]:
    """Read credentials for recalls in `downloading`: the restored copy when the
    restore made one (Azure rehydrate target, WORM recall container), else the
    version's primary location."""
    if not recall_ids:
        return {}
    rows = await (
        await db.execute(
            """
            SELECT r.recall_id, l.location_id, l.file_id, l.version_id, l.crc64nvme, l.etag,
                   l.backend_id, l.container, l.object_key, l.backend_version_id,
                   b.kind, b.config
              FROM catalog.recall r
              LEFT JOIN catalog.restore rs ON rs.restore_id = r.restore_id
              JOIN catalog.location l ON l.location_id = coalesce(
                     rs.target_location_id,
                     (SELECT p.location_id FROM catalog.location p
                       WHERE p.version_id = r.version_id AND p.role = 'primary'
                         AND p.state = 'committed' LIMIT 1))
              JOIN catalog.backend b ON b.backend_id = l.backend_id
             WHERE r.recall_id = ANY(%s::bigint[])
            """,
            (recall_ids,),
        )
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        plane = ctx.svc.planes.get(r["backend_id"], r["kind"], r["config"] or {})
        target = Target(r["backend_id"], r["container"], r["object_key"], r["backend_version_id"])
        out[r["recall_id"]] = await read_credential(ctx, r, plane, target)
    return out


async def get_feed(ctx: Ctx) -> Result:
    q = ctx.request.query_params
    raw = q.get("after")
    after = cursor(raw)
    limit = query_int(ctx.request, "limit", 500, 1, 1000)
    wait = query_int(ctx.request, "wait", 0, 0, 30)
    deadline = time.monotonic() + wait
    while True:
        async with ctx.svc.catalog.read() as db:
            inc = await feed_incarnation(db)
            floor = await retention_floor(db)
            if (floor > (0, 0) and after <= floor) or stale_incarnation(raw or "", inc):
                return Result(200, {"events": [], "next": raw, "reset_required": True})
            rows, nxt = await read_page(db, ctx.edge_id, after, limit)
            if rows or time.monotonic() >= deadline:
                events = await build_events(ctx, db, rows)
                return Result(
                    200,
                    {"events": events, "next": fmt_cursor(nxt, inc), "reset_required": False},
                )
        after = max(after, nxt)
        await asyncio.sleep(
            min(ctx.svc.settings.feed_poll_seconds, max(0.0, deadline - time.monotonic()))
        )


async def ack_feed(ctx: Ctx) -> Result:
    raw = req(ctx.body, "cursor")
    c = cursor(raw, "cursor")
    async with ctx.svc.catalog.tx(ctx.edge.principal) as db:
        await db.execute(
            "UPDATE catalog.edge SET feed_acked_cursor = %s, feed_acked_at = now()"
            " WHERE edge_id = %s",
            (fmt_cursor(c, cursor_incarnation(raw)), ctx.edge_id),
        )
    return Result(204)


async def get_snapshot(ctx: Ctx) -> Result:
    q = dict(ctx.request.query_params)
    dataset_id = opt_id(q, "dataset_id")
    if dataset_id is None:
        raise ApiError("invalid_request", "dataset_id is required")
    dir_id = opt_id(q, "dir_id")
    after = opt_id(q, "after_file_id")
    limit = query_int(ctx.request, "limit", 5000, 1, 10000)
    async with ctx.svc.catalog.read() as db:
        owned = await (
            await db.execute(
                DATASET_SELECT + " WHERE d.dataset_id = %s AND d.edge_id = %s",
                (dataset_id, ctx.edge_id),
            )
        ).fetchone()
        if owned is None:
            raise ApiError("not_found", "no such dataset on this edge")
        out: dict[str, Any] = {}
        if after is None:
            # Taken before the rows are read: replaying from it is harmless.
            out["as_of"] = fmt_cursor((await horizon(db), 0), await feed_incarnation(db))
            out["dataset"] = dataset_row(owned)
            shares = await (
                await db.execute(
                    SHARE_SELECT + " WHERE dataset_id = %s AND edge_id = %s ORDER BY share_id",
                    (dataset_id, ctx.edge_id),
                )
            ).fetchall()
            out["shares"] = [share_row(s) for s in shares]
            acls = await (
                await db.execute(
                    ACL_SELECT + " WHERE dataset_id = %s ORDER BY acl_id", (dataset_id,)
                )
            ).fetchall()
            out["acls"] = [acl_row(a) for a in acls]
        cond = " WHERE f.dataset_id = %s AND f.deleted_at IS NULL AND f.file_id > %s"
        args: list[Any] = [dataset_id, after or 0]
        if dir_id is not None:
            cond += " AND f.parent_id = %s"
            args.append(dir_id)
        rows = await (
            await db.execute(
                FILE_SELECT + cond + " ORDER BY f.file_id LIMIT %s", (*args, limit + 1)
            )
        ).fetchall()
    out["files"] = [file_row(r) for r in rows[:limit]]
    out["more"] = len(rows) > limit
    return Result(200, out)


async def start_snapshot_export(ctx: Ctx) -> Result:
    """`POST snapshot-exports`: a bulk export of a large dataset (section 6,
    `exports.py`). 409 `layout_conflict` when the dataset's backend has no
    export area: the edge pages through `snapshot` instead."""
    dataset_id = req_id(ctx.body, "dataset_id")
    async with ctx.svc.catalog.tx(ctx.edge.principal) as db:
        ds = await (
            await db.execute(
                DATASET_SELECT + " WHERE d.dataset_id = %s AND d.edge_id = %s",
                (dataset_id, ctx.edge_id),
            )
        ).fetchone()
        if ds is None:
            raise ApiError("not_found", "no such dataset on this edge")
        container = exports.container_for(ds["backend_kind"], ds["backend_config"] or {})
        if container is None:
            raise ApiError(
                "layout_conflict", "this dataset's backend has no export area: page the snapshot"
            )
        # Taken before any row is read: replaying the feed from it is harmless.
        as_of = fmt_cursor((await horizon(db), 0), await feed_incarnation(db))
        export_id = await exports.start(
            db,
            edge_id=ctx.edge_id,
            dataset_id=dataset_id,
            backend_id=ds["backend_id"],
            container=container,
            as_of=as_of,
        )
    return Result(202, {"export_id": id_str(export_id), "state": "running", "as_of": as_of})


async def get_snapshot_export(ctx: Ctx) -> Result:
    """`GET snapshot-exports/{export_id}`: the state, and once ready each
    part with a read SAS valid for 24 hours, its rows, size and SHA-256."""
    export_id = ctx.path_id("export_id")
    async with ctx.svc.catalog.read() as db:
        ex = await (
            await db.execute(
                "SELECT e.*, b.kind AS backend_kind, b.config AS backend_config"
                "  FROM catalog.snapshot_export e"
                "  JOIN catalog.backend b ON b.backend_id = e.backend_id"
                " WHERE e.export_id = %s AND e.edge_id = %s",
                (export_id, ctx.edge_id),
            )
        ).fetchone()
    if ex is None:
        raise ApiError("not_found", "no such export for this edge")
    out: dict[str, Any] = {
        "export_id": id_str(ex["export_id"]),
        "state": ex["state"],
        "as_of": ex["as_of"],
    }
    if ex["state"] == "ready":
        plane = ctx.svc.planes.get(ex["backend_id"], ex["backend_kind"], ex["backend_config"] or {})
        parts = []
        for p in ex["parts"]:
            cred = await plane.credential(
                Target(ex["backend_id"], ex["container"], p["key"]),
                EdgeAccess.READ,
                ttl=EXPORT_SAS_TTL,
            )
            parts.append(
                {"url": cred["url"], "rows": p["rows"], "bytes": p["bytes"], "sha256": p["sha256"]}
            )
        out["parts"] = parts
    return Result(200, out)


#: XOR, over rows, of the first 8 bytes of SHA-256("<file_id>:<entity_version>").
_DIGEST_SQL = """
SELECT count(*) AS live_rows,
       coalesce(bit_xor(('x' || encode(substring(sha256(convert_to(
           f.file_id::text || ':' || f.entity_version::text, 'UTF8')) FROM 1 FOR 8), 'hex'))
           ::bit(64)::bigint), 0) AS x
  FROM catalog.file f
 WHERE f.dataset_id = %s AND f.deleted_at IS NULL
"""


async def get_digest(ctx: Ctx) -> Result:
    """`GET digest` (control-api.md §6): the order-independent digest of a
    dataset's live rows, or of one directory's direct children, computed now.
    `as_of` is taken first: an edge compares only once its cursor is at or
    past it, and acts only on a mismatch that two digests a quiet interval
    apart agree on (writes racing the comparison change the digest)."""
    q = dict(ctx.request.query_params)
    dataset_id = opt_id(q, "dataset_id")
    if dataset_id is None:
        raise ApiError("invalid_request", "dataset_id is required")
    dir_id = opt_id(q, "dir_id")
    async with ctx.svc.catalog.read() as db:
        owned = await (
            await db.execute(
                "SELECT 1 FROM catalog.dataset WHERE dataset_id = %s AND edge_id = %s",
                (dataset_id, ctx.edge_id),
            )
        ).fetchone()
        if owned is None:
            raise ApiError("not_found", "no such dataset on this edge")
        as_of = fmt_cursor((await horizon(db), 0), await feed_incarnation(db))
        sql, args = _DIGEST_SQL, [dataset_id]
        if dir_id is not None:
            sql += " AND f.parent_id = %s"
            args.append(dir_id)
        row = await (await db.execute(sql, args)).fetchone()
    return Result(
        200,
        {
            "as_of": as_of,
            "live_rows": int(row["live_rows"]),
            "digest": f"{int(row['x']) & 0xFFFFFFFFFFFFFFFF:016x}",
        },
    )
