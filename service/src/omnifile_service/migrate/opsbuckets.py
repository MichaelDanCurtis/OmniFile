"""Batch Operations write manifests and completion reports into OmniFile's
operations buckets. A registered source is read-only to OmniFile
(catalog-seeding.md section 10), so a job whose manifest or report bucket is
also a source bucket is never started: registration refuses such a source,
and this check covers a config changed afterwards."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg


def batch_buckets(backend: Any) -> list[str]:
    batch = getattr(getattr(backend, "config", None), "batch", None)
    if batch is None:
        return []
    return sorted({b for b in (batch.manifest_bucket, batch.report_bucket) if b})


async def batch_writes_source(
    conn: psycopg.AsyncConnection, backend: Any, m: Mapping[str, Any], now: datetime
) -> bool:
    """True (and the migration's `last_error` set) when the backend's Batch
    manifest or report bucket is a registered source bucket."""
    buckets = batch_buckets(backend)
    if not buckets:
        return False
    cur = await conn.execute(
        "SELECT container FROM catalog.source WHERE backend_id = %s AND container = ANY(%s)"
        " LIMIT 1",
        (m["src_backend_id"], buckets),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    await conn.execute(
        "UPDATE catalog.migration_prefix SET last_error = %s, updated_at = %s"
        " WHERE migration_id = %s",
        (f"Batch manifest/report bucket {row[0]} is a source of a dataset: no Batch job is"
         " started (configure a separate operations bucket)", now, m["migration_id"]),
    )  # fmt: skip
    return True
