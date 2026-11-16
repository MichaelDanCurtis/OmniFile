"""Versioning depth (decision 30): who sets it, and pruning beyond it.

- The share's **Owners** set `rewind_depth_days`; the **ArchiveAdmin** sets
  the maximum, `rewind_depth_max_days`. The catalog refuses a depth above the
  maximum (`share_rewind_within_max`) and audits every change (0006).
- **Pruning** marks versions older than the depth `pruned` in the catalog and
  queues the backend delete of their bytes on the `retention` queue: the
  worker's lifecycle-equivalent job (`worker/retention_jobs.py`). A native
  lifecycle rule (`lifecycle_policy`) is the backstop on Azure.

A version is prunable when all of these hold:
- it is committed and was superseded more than `depth` days ago, where the
  depth is the deepest of the shares covering the folder it was in
  (`catalog.rewind_depth_days`), plus a grace period;
- it is not on legal hold and its retention has ended, by the worker's clock
  **and** the database's (the catalog's own guards check again and refuse);
- its bytes are not still needed: no unpruned metadata or rename version
  carries it as its content, it is not the live file's content, its file's
  stub target, or the version of an active recall.

Content, metadata and rename versions are pruned; tombstones are current
forever and stay.

A pruned version's copy in a migration source (an S3 object the catalog
adopted or followed) is never deleted: the source is the client's and
read-only to OmniFile (`catalog.source_readonly`). Its location stays
`committed`, and the audit lists the version as `retained_at_source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..catalog.source_readonly import on_source
from ..worker.queue import send
from .common import RetentionError, set_actor

RETENTION_QUEUE = "retention"
PRUNE_ACTOR = "system:retention_prune"


@dataclass(frozen=True)
class PruneResult:
    versions: list[int] = field(default_factory=list)
    #: Locations whose bytes go (queued on `retention`).
    locations: list[int] = field(default_factory=list)
    #: Locations in a migration source: kept, never deleted.
    retained_at_source: list[int] = field(default_factory=list)


async def set_rewind_depth(
    conn: psycopg.AsyncConnection, share_id: int, days: int, *, actor: str
) -> None:
    """An Owner sets the share's depth, within the ArchiveAdmin's maximum."""
    if days < 0:
        raise RetentionError("rewind depth can't be negative")
    async with conn.transaction():
        await set_actor(conn, actor)
        cur = await conn.execute(
            "SELECT rewind_depth_max_days FROM catalog.share WHERE share_id = %s FOR UPDATE",
            (share_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise RetentionError(f"no share {share_id}")
        if days > row[0]:
            raise RetentionError(
                f"share {share_id}: {days} days is above the ArchiveAdmin maximum of {row[0]}"
            )
        await conn.execute(
            "UPDATE catalog.share SET rewind_depth_days = %s WHERE share_id = %s",
            (days, share_id),
        )


async def set_rewind_max(
    conn: psycopg.AsyncConnection, share_id: int, max_days: int, *, actor: str
) -> None:
    """The ArchiveAdmin sets the maximum. Lowering it below the current depth
    lowers the depth with it."""
    if max_days < 0:
        raise RetentionError("rewind maximum can't be negative")
    async with conn.transaction():
        await set_actor(conn, actor)
        cur = await conn.execute(
            "UPDATE catalog.share SET rewind_depth_max_days = %s,"
            " rewind_depth_days = least(rewind_depth_days, %s) WHERE share_id = %s",
            (max_days, max_days, share_id),
        )
        if cur.rowcount == 0:
            raise RetentionError(f"no share {share_id}")


_CANDIDATES = """
WITH cand AS (
    SELECT v.version_id, v.file_id, v.kind, v.dataset_id, v.parent_id, v.superseded_at
      FROM catalog.file_version v
     WHERE v.superseded_at IS NOT NULL AND v.state = 'committed' AND NOT v.legal_hold
       AND v.kind <> 'tombstone'
       -- An annex's copies are kept until an ArchiveAdmin purges them
       -- (decision 51; annex/purge.py): never pruned by depth.
       AND NOT EXISTS (SELECT 1 FROM catalog.annex a WHERE a.dataset_id = v.dataset_id)
       AND (%(dataset_id)s::bigint IS NULL OR v.dataset_id = %(dataset_id)s::bigint)
       AND v.superseded_at < %(now)s::timestamptz - %(grace)s::interval
       AND (v.retention_until IS NULL
            OR (v.retention_until <= %(now)s::timestamptz AND v.retention_until <= now()))
     ORDER BY v.dataset_id, v.superseded_at
     LIMIT %(scan)s
),
deep AS (
    SELECT c.*
      FROM cand c
     WHERE c.superseded_at < %(now)s::timestamptz
                             - make_interval(days => catalog.rewind_depth_days(c.dataset_id,
                                                                                c.parent_id))
)
SELECT d.version_id, d.kind, d.dataset_id
  FROM deep d
  JOIN catalog.file f ON f.file_id = d.file_id
 WHERE NOT (f.deleted_at IS NULL AND f.content_version_id = d.version_id)
   AND f.stub_version_id IS DISTINCT FROM d.version_id
   AND NOT EXISTS (SELECT 1 FROM catalog.recall r
                    WHERE r.version_id = d.version_id
                      AND r.state IN ('requested', 'rehydrating', 'copying', 'downloading'))
   AND NOT EXISTS (SELECT 1 FROM catalog.file_version w
                    WHERE w.file_id = d.file_id AND w.content_version_id = d.version_id
                      AND w.state <> 'pruned'
                      AND w.version_id NOT IN (SELECT version_id FROM deep))
   AND NOT EXISTS (SELECT 1 FROM catalog.location l
                     JOIN catalog.restore s ON s.location_id = l.location_id
                    WHERE l.version_id = d.version_id
                      AND s.state IN ('requested', 'in_progress'))
"""


async def prune_versions(
    conn: psycopg.AsyncConnection,
    now: datetime,
    *,
    dataset_id: int | None = None,
    limit: int = 1000,
    grace: timedelta = timedelta(hours=24),
) -> PruneResult:
    """Prune one batch beyond the rewind depth. Returns what it pruned.

    Catalog rows are marked in one transaction with the queued backend
    deletes (decision 27), so neither happens without the other.
    """
    async with conn.transaction():
        await set_actor(conn, PRUNE_ACTOR)
        cur = await conn.execute(
            _CANDIDATES,
            {"dataset_id": dataset_id, "now": now, "grace": grace, "scan": limit},
        )
        rows = await cur.fetchall()
        if not rows:
            return PruneResult()
        ids = [r[0] for r in rows]
        # Lock in a stable order; skip anything another pruner holds.
        cur = await conn.execute(
            "SELECT version_id FROM catalog.file_version WHERE version_id = ANY(%s)"
            " AND state = 'committed' ORDER BY version_id FOR UPDATE SKIP LOCKED",
            (ids,),
        )
        locked = [r[0] for r in await cur.fetchall()]
        if not locked:
            return PruneResult()
        await conn.execute(
            "UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = ANY(%s)",
            (locked,),
        )
        # A copy in a migration source is the client's object: the version
        # is pruned from rewind, but its bytes stay where they are and the
        # location stays `committed` (source_readonly.py).
        cur = await conn.execute(
            "UPDATE catalog.location l SET state = 'deleting', verified = false"
            " WHERE l.version_id = ANY(%s) AND l.state IN ('pending', 'committed')"
            f" AND NOT {on_source('l')}"
            " RETURNING l.location_id",
            (locked,),
        )
        locs = [r[0] for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT l.version_id, l.location_id FROM catalog.location l"
            " WHERE l.version_id = ANY(%s) AND l.state IN ('pending', 'committed')"
            f" AND {on_source('l')}",
            (locked,),
        )
        kept_at_source = [(r[0], r[1]) for r in await cur.fetchall()]
        source_versions = {vid for vid, _ in kept_at_source}
        for lid in locs:
            await send(conn, RETENTION_QUEUE, {"op": "delete_blob", "location_id": str(lid)})
        per_ds: dict[int, list[int]] = {}
        for vid, _, ds in rows:
            if vid in locked:
                per_ds.setdefault(ds, []).append(vid)
        for ds, vids in per_ds.items():
            details: dict[str, Any] = {"versions": vids[:1000], "count": len(vids),
                                       "at": now.isoformat()}  # fmt: skip
            at_source = [v for v in vids if v in source_versions]
            if at_source:
                details["retained_at_source"] = at_source[:1000]
            await conn.execute(
                "SELECT catalog.audit('version.prune', %s, NULL, NULL, NULL, %s)",
                (ds, Jsonb(details)),
            )
        return PruneResult(sorted(locked), locs, sorted(lid for _, lid in kept_at_source))


def lifecycle_policy(*, data_container: str, pruned_after_days: int = 1) -> dict[str, Any]:
    """An Azure lifecycle management policy that backs up the worker.

    The worker tags a pruned blob ``omnifile_state=pruned`` before it deletes
    it; if the delete never happens (the worker is down, a throttle), the
    rule deletes tagged blobs after `pruned_after_days`. Immutability still
    applies: Azure won't delete a version under retention or hold. The rule
    is rendered here and applied by deployment (`deploy/azure`), not by the
    service. VERIFY on real Azure: that `blobIndexMatch` filters apply to the
    `version` action as well as `baseBlob`.
    """
    prefix = data_container.split("/", 1)[-1] + "/"
    return {
        "rules": [
            {
                "enabled": True,
                "name": "omnifile-pruned-versions",
                "type": "Lifecycle",
                "definition": {
                    "filters": {
                        "blobTypes": ["blockBlob"],
                        "prefixMatch": [prefix],
                        "blobIndexMatch": [
                            {"name": "omnifile_state", "op": "==", "value": "pruned"}
                        ],
                    },
                    "actions": {
                        "baseBlob": {
                            "delete": {"daysAfterModificationGreaterThan": pruned_after_days}
                        },
                        "version": {"delete": {"daysAfterCreationGreaterThan": pruned_after_days}},
                    },
                },
            }
        ]
    }
