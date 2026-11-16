"""Bulk snapshot exports (control-api.md section 6).

For a dataset too large to page through, the worker writes its snapshot as
zstd-compressed NDJSON parts to blob storage, one part per `snapshot_export`
queue message, each queueing the next (as the seeder does), so no
transaction is held across 10^8 rows. Each line is one object with one
key: ``{"dataset": …}``, then ``{"share": …}`` lines, ``{"acl": …}`` lines,
and ``{"file": …}`` lines in `file_id` order, in the snapshot's row shapes.
The first three only in the first part. The export's `as_of` is taken when it
starts, before any row is read.

Parts go to the ``omnifile-exports`` container of the dataset's primary
account, never the user-data container (whose change feed is the catalog's).
A dataset on another kind of backend (S3) has no export area: the edge pages
through `snapshot` instead. Exports are kept for `KEEP`; the sweep then
deletes their parts and marks them expired.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import psycopg
import zstandard
from psycopg.rows import dict_row

from .api.rows import (
    ACL_SELECT,
    DATASET_SELECT,
    FILE_SELECT,
    SHARE_SELECT,
    acl_row,
    dataset_row,
    file_row,
    share_row,
)
from .backends.base import ObjectNotFound, ObjectRef, StorageBackend
from .worker.queue import Defer

log = logging.getLogger(__name__)

QUEUE = "snapshot_export"
CONTAINER = "omnifile-exports"
#: Rows per part (the contract's "about 1M").
PART_ROWS = 1_000_000
#: Rows read per query while a part is written.
READ_BATCH = 10_000
#: How long an export and its parts are kept (the read SAS lasts 24 h).
KEEP = timedelta(hours=48)
#: How soon a step looks again when another holder of its message is writing.
STEP_BUSY_SECONDS = 60.0
ZSTD_LEVEL = 3
#: Uncompressed bytes handed to the compressor at a time.
FLUSH_BYTES = 1 << 20

GetBackend = Callable[[int], StorageBackend]


def container_for(kind: str, config: Mapping[str, Any]) -> str | None:
    """The export container for a dataset's backend, or None when the backend
    has no export area (S3, or a sim with the S3 flavour)."""
    if kind == "azure_blob":
        account = config.get("primary_account")
        return f"{account}/{CONTAINER}" if account else None
    if kind == "sim" and config.get("flavour", "azure_blob") == "azure_blob":
        account = str(config.get("container", "live/data")).split("/", 1)[0]
        return f"{account}/{CONTAINER}"
    return None


async def start(
    db: psycopg.AsyncConnection,
    *,
    edge_id: int,
    dataset_id: int,
    backend_id: int,
    container: str,
    as_of: str,
) -> int:
    """Record an export and queue its first part, in the caller's transaction."""
    row = await (
        await db.execute(
            "INSERT INTO catalog.snapshot_export"
            " (edge_id, dataset_id, as_of, backend_id, container, prefix)"
            " VALUES (%s, %s, %s, %s, %s, '') RETURNING export_id",
            (edge_id, dataset_id, as_of, backend_id, container),
        )
    ).fetchone()
    export_id = int(row[0] if isinstance(row, tuple) else row["export_id"])
    await db.execute(
        "UPDATE catalog.snapshot_export SET prefix = %s WHERE export_id = %s",
        (f"edge-{edge_id}/export-{export_id}/", export_id),
    )
    await db.execute(
        "SELECT pgmq.send(%s, %s::jsonb)", (QUEUE, json.dumps({"export_id": export_id}))
    )
    return export_id


def _line(kind: str, row: Mapping[str, Any]) -> bytes:
    return json.dumps({kind: row}, separators=(",", ":")).encode() + b"\n"


async def step(conn: psycopg.AsyncConnection, get_backend: GetBackend, export_id: int) -> None:
    """Write the export's next part, on `conn`'s transaction. Queues the part
    after it, or marks the export ready.

    Writers of one export take turns on an advisory lock, not its row: a row lock
    takes a transaction id, which would hold the edge feed back for the whole upload.
    The rows it reads are read during the upload, so the read locks stay."""
    cur = conn.cursor(row_factory=dict_row)
    (mine,) = await (
        await conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (f"omnifile:export:{export_id}",)
        )
    ).fetchone()  # type: ignore[misc]
    if not mine:
        raise Defer(STEP_BUSY_SECONDS)
    ex = await (
        await cur.execute(
            "SELECT * FROM catalog.snapshot_export WHERE export_id = %s", (export_id,)
        )
    ).fetchone()
    if ex is None or ex["state"] != "running":
        return  # expired or gone meanwhile: nothing to write
    parts: list[dict[str, Any]] = list(ex["parts"])
    first = not parts
    dataset_id = ex["dataset_id"]
    state: dict[str, Any] = {"rows": 0, "last": int(ex["after_file_id"]), "full": False}
    sha = hashlib.sha256()
    size = 0

    async def lines() -> AsyncIterator[bytes]:
        if first:
            d = await (
                await cur.execute(DATASET_SELECT + " WHERE d.dataset_id = %s", (dataset_id,))
            ).fetchone()
            yield _line("dataset", dataset_row(d))
            state["rows"] += 1
            for s in await (
                await cur.execute(
                    SHARE_SELECT + " WHERE dataset_id = %s AND edge_id = %s ORDER BY share_id",
                    (dataset_id, ex["edge_id"]),
                )
            ).fetchall():
                yield _line("share", share_row(s))
                state["rows"] += 1
            for a in await (
                await cur.execute(
                    ACL_SELECT + " WHERE dataset_id = %s ORDER BY acl_id", (dataset_id,)
                )
            ).fetchall():
                yield _line("acl", acl_row(a))
                state["rows"] += 1
        files = 0
        while files < PART_ROWS:
            n = min(READ_BATCH, PART_ROWS - files)
            rows = await (
                await cur.execute(
                    FILE_SELECT + " WHERE f.dataset_id = %s AND f.deleted_at IS NULL"
                    " AND f.file_id > %s ORDER BY f.file_id LIMIT %s",
                    (dataset_id, state["last"], n),
                )
            ).fetchall()
            for r in rows:
                yield _line("file", file_row(r))
            files += len(rows)
            state["rows"] += len(rows)
            if rows:
                state["last"] = int(rows[-1]["file_id"])
            if len(rows) < n:
                return
        state["full"] = True

    async def compressed() -> AsyncIterator[bytes]:
        z = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compressobj()
        buf: list[bytes] = []
        held = 0
        async for line in lines():
            buf.append(line)
            held += len(line)
            if held >= FLUSH_BYTES:
                yield z.compress(b"".join(buf))
                buf, held = [], 0
        yield z.compress(b"".join(buf)) + z.flush()

    async def hashed() -> AsyncIterator[bytes]:
        """What is uploaded is what is hashed and counted."""
        nonlocal size
        async for out in compressed():
            if out:
                sha.update(out)
                size += len(out)
                yield out

    key = f"{ex['prefix']}part-{len(parts):05d}.ndjson.zst"
    backend = get_backend(ex["backend_id"])
    await backend.put(ex["container"], key, hashed(), None, {})
    # The expiry sweep may have ended it during the upload. That leaves this one part
    # behind: an export only expires running if it has been stuck for two days.
    now_state = await (
        await cur.execute(
            "SELECT state FROM catalog.snapshot_export WHERE export_id = %s FOR UPDATE",
            (export_id,),
        )
    ).fetchone()
    if now_state is None or now_state["state"] != "running":
        return
    parts.append({"key": key, "rows": state["rows"], "bytes": size, "sha256": sha.hexdigest()})
    more = state["full"] and (
        await (
            await cur.execute(
                "SELECT 1 FROM catalog.file WHERE dataset_id = %s AND deleted_at IS NULL"
                " AND file_id > %s LIMIT 1",
                (dataset_id, state["last"]),
            )
        ).fetchone()
        is not None
    )
    await cur.execute(
        "UPDATE catalog.snapshot_export SET parts = %s::jsonb, after_file_id = %s,"
        " state = CASE WHEN %s THEN state ELSE 'ready' END,"
        " finished_at = CASE WHEN %s THEN NULL ELSE now() END"
        " WHERE export_id = %s",
        (json.dumps(parts), state["last"], more, more, export_id),
    )
    if more:
        await cur.execute(
            "SELECT pgmq.send(%s, %s::jsonb)", (QUEUE, json.dumps({"export_id": export_id}))
        )
    log.info(
        "snapshot export %s: part %s, %s rows, %s bytes%s",
        export_id,
        len(parts) - 1,
        state["rows"],
        size,
        "" if more else " (ready)",
    )


async def expire(conn: psycopg.AsyncConnection, now: datetime, get_backend: GetBackend) -> int:
    """Delete the parts of exports older than `KEEP` and mark them expired.
    Runs on an autocommit connection."""
    cur = conn.cursor(row_factory=dict_row)
    old = await (
        await cur.execute(
            "SELECT export_id FROM catalog.snapshot_export"
            " WHERE state <> 'expired' AND created_at < %s ORDER BY export_id",
            (now - KEEP,),
        )
    ).fetchall()
    n = 0
    for o in old:
        # No transaction is open during the deletes: an id held across them would stop
        # the edge feed. A crash before the mark repeats them on the next sweep.
        ex = await (
            await cur.execute(
                "SELECT * FROM catalog.snapshot_export WHERE export_id = %s", (o["export_id"],)
            )
        ).fetchone()
        if ex is None or ex["state"] == "expired":
            continue
        backend = get_backend(ex["backend_id"])
        for p in ex["parts"]:
            try:
                await backend.delete(ObjectRef(ex["backend_id"], ex["container"], p["key"]))
            except ObjectNotFound:
                pass
        await cur.execute(
            "UPDATE catalog.snapshot_export SET state = 'expired',"
            " finished_at = coalesce(finished_at, now()) WHERE export_id = %s"
            " AND state <> 'expired'",
            (ex["export_id"],),
        )
        n += 1
    return n


__all__ = ["CONTAINER", "KEEP", "PART_ROWS", "QUEUE", "container_for", "expire", "start", "step"]
