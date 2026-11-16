"""Drift rows: one open difference between the catalog and a backend."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

DRIFT_KINDS = (
    "missing",  # the catalog has a committed copy the backend doesn't
    "unexpected",  # the backend has an object the catalog doesn't know
    "size_mismatch",
    "tier_mismatch",
    "retention_mismatch",  # the backend retains a version for less than the catalog
    "hold_mismatch",
    "deleted_externally",  # the change feed showed a delete the service didn't make
    "feed_record_failed",  # a change-feed record that kept failing was skipped
    "unmappable",  # seeding: the object's key can't be a POSIX path
    "path_conflict",  # seeding: a file and a folder want the same path
    "checksum_mismatch",  # migration: a copy's CRC64-NVME differs from its source
)


async def record_drift(
    conn: psycopg.AsyncConnection,
    *,
    source: str,
    backend_id: int,
    container: str,
    key: str,
    version: str | None,
    kind: str,
    now: datetime,
    location_id: int | None = None,
    expected: Mapping[str, Any] | None = None,
    observed: Mapping[str, Any] | None = None,
    run_id: int | None = None,
) -> int:
    """Open a drift row, or refresh the open one. Returns 1 if newly opened."""
    if kind not in DRIFT_KINDS:
        raise ValueError(f"unknown drift kind {kind!r}")
    cur = await conn.execute(
        """
        INSERT INTO catalog.reconcile_drift
            (first_run_id, last_run_id, source, backend_id, container, object_key,
             backend_version_id, location_id, kind, expected, observed, detected_at, last_seen_at)
        VALUES (%(run)s, %(run)s, %(source)s, %(bid)s, %(container)s, %(key)s, %(ver)s, %(lid)s,
                %(kind)s, %(expected)s, %(observed)s, %(now)s, %(now)s)
        ON CONFLICT (backend_id, container, object_key, kind, coalesce(backend_version_id, ''))
            WHERE resolved_at IS NULL
        DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at,
                      last_run_id = coalesce(EXCLUDED.last_run_id,
                                             catalog.reconcile_drift.last_run_id),
                      observed = EXCLUDED.observed
        RETURNING (xmax = 0) AS inserted
        """,
        {"run": run_id, "source": source, "bid": backend_id, "container": container,
         "key": key, "ver": version, "lid": location_id, "kind": kind,
         "expected": Jsonb(dict(expected or {})), "observed": Jsonb(dict(observed or {})),
         "now": now},
    )  # fmt: skip
    row = await cur.fetchone()
    return 1 if row and row[0] else 0


async def open_drift(
    conn: psycopg.AsyncConnection, backend_id: int | None = None, container: str | None = None
) -> list[dict]:
    """Open drift rows (newest first), optionally for one container."""
    cur = await conn.execute(
        """
        SELECT drift_id, source, backend_id, container, object_key, backend_version_id,
               location_id, kind, expected, observed, detected_at, last_seen_at
          FROM catalog.reconcile_drift
         WHERE resolved_at IS NULL
           AND (%(bid)s::bigint IS NULL OR backend_id = %(bid)s::bigint)
           AND (%(c)s::text IS NULL OR container = %(c)s::text)
         ORDER BY detected_at DESC, drift_id DESC
        """,
        {"bid": backend_id, "c": container},
    )
    names = [d.name for d in cur.description or []]
    return [dict(zip(names, r, strict=True)) for r in await cur.fetchall()]
