"""Purging the copy of a file deleted on the NAS (decisions 40 and 51).

In Copy mode a NAS delete is reported, never mirrored: the catalog file is
tombstoned and its versions and blobs are kept. Only an ArchiveAdmin purges
them (`POST /v1/annexes/{annex_id}/files/{file_id}/purge`), or the annex's
optional deleted-file retention, off by default. The portal records the
request and queues ``{"op": "annex_purge", "annex_id", "file_id"}`` on the
`retention` queue; this module does the rest, as the worker:

- every content, metadata and rename version of the file is marked `pruned`
  (tombstones stay: the file's history says it was deleted), and its
  locations `deleting`, with a `delete_blob` message each, in the same
  transaction (decision 27);
- the catalog's own guards still apply: a version under WORM retention or a
  legal hold refuses the change (OF002), and the purge is recorded as
  refused, never forced;
- `annex_file.purged_at` and an annex event `purged` (or `purge_refused`)
  record it, and the audit log has `annex.purge`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ..retention.common import set_actor
from ..worker.queue import send
from .placement_states import open_sql

log = logging.getLogger(__name__)

PURGE_ACTOR = "system:annex_purge"
RETENTION_QUEUE = "retention"
#: How long after a refusal a retention asks again.
PURGE_RETRY = timedelta(days=7)
#: A file on its way back isn't due, from the moment it is asked for: its recall can take
#: many hours, and a purge then deletes the blob it is recalling, which can't be undone.
_NO_OPEN_PLACEMENT = (
    "   AND NOT EXISTS (SELECT 1 FROM catalog.annex_placement p WHERE p.file_id = af.file_id"
    f"                    AND {open_sql('p')})"
)


async def purge_file(conn: psycopg.AsyncConnection, annex_id: int, file_id: int,
                     now: datetime) -> str:  # fmt: skip
    """`purged`, `refused`, or `noop` (already purged, or never requested)."""
    try:
        async with conn.transaction():
            await set_actor(conn, PURGE_ACTOR)
            cur = await conn.execute(
                "SELECT af.path, af.purge_requested_by, af.purged_at, af.gone_since,"
                "       f.dataset_id"
                "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
                " WHERE af.annex_id = %s AND af.file_id = %s FOR UPDATE OF af",
                (annex_id, file_id),
            )
            row = await cur.fetchone()
            if row is None:
                return "noop"
            path, by, purged_at, gone, dataset_id = row
            if purged_at is not None or by is None or gone is None:
                return "noop"
            cur = await conn.execute(
                "UPDATE catalog.file_version SET state = 'pruned'"
                " WHERE file_id = %s AND state = 'committed' AND kind <> 'tombstone'"
                " RETURNING version_id",
                (file_id,),
            )
            versions = [r[0] for r in await cur.fetchall()]
            cur = await conn.execute(
                "UPDATE catalog.location SET state = 'deleting', verified = false"
                " WHERE file_id = %s AND version_id = ANY(%s) AND state IN ('pending', 'committed')"
                " RETURNING location_id",
                (file_id, versions),
            )
            locations = [r[0] for r in await cur.fetchall()]
            for lid in locations:
                await send(conn, RETENTION_QUEUE, {"op": "delete_blob", "location_id": str(lid)})
            await conn.execute(
                "UPDATE catalog.annex_file SET purged_at = %s, purged_by = %s,"
                " purge_refused_at = NULL, updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (now, by, annex_id, file_id),
            )
            details = {"versions": versions, "locations": locations, "requested_by": by}
            await conn.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, details)"
                " VALUES (%s, 'purged', %s, %s, %s)",
                (annex_id, path, file_id, Jsonb(details)),
            )
            await conn.execute(
                "SELECT catalog.audit('annex.purge', %s, NULL, %s, NULL, %s)",
                (dataset_id, file_id, Jsonb({"annex_id": str(annex_id), **details})),
            )
        return "purged"
    except psycopg.Error as e:
        # WORM retention or a legal hold (OF002), or the catalog refusing
        # another way: record it, keep everything. The request ends here, or the
        # file would stay spoken for after the hold is gone: it can be asked for
        # again, and a retention asks again after `PURGE_RETRY`.
        log.warning("annex %s file %s: purge refused: %s", annex_id, file_id, e)
        async with conn.transaction():
            await set_actor(conn, PURGE_ACTOR)
            await conn.execute(
                "UPDATE catalog.annex_file SET purge_requested_at = NULL,"
                " purge_requested_by = NULL, purge_refused_at = %s, updated_at = now()"
                " WHERE annex_id = %s AND file_id = %s",
                (now, annex_id, file_id),
            )
            await conn.execute(
                "INSERT INTO catalog.annex_event (annex_id, kind, file_id, details)"
                " VALUES (%s, 'purge_refused', %s, %s)",
                (annex_id, file_id, Jsonb({"error": str(e).splitlines()[0][:500]})),
            )
        return "refused"


async def purge_due(conn: psycopg.AsyncConnection, now: datetime, limit: int = 500) -> int:
    """The optional per-annex deleted-file retention: request the purge of
    copies of files gone from the NAS longer than `deleted_retention_days`.
    Returns how many were purged."""
    cur = await conn.execute(
        "SELECT af.annex_id, af.file_id, a.deleted_retention_days"
        "  FROM catalog.annex_file af JOIN catalog.annex a ON a.annex_id = af.annex_id"
        " WHERE a.deleted_retention_days IS NOT NULL AND af.gone_since IS NOT NULL"
        "   AND af.purged_at IS NULL AND af.purge_requested_at IS NULL"
        # Refused lately (a legal hold, say): not every pass.
        "   AND (af.purge_refused_at IS NULL OR af.purge_refused_at < %s)"
        # Removed by the annex: the copy is the file now, never purged.
        "   AND af.nas_state NOT IN ('removed', 'removing')" + _NO_OPEN_PLACEMENT +
        # Only those due: rows not due yet never fill the batch.
        "   AND af.gone_since < %s::timestamptz - make_interval(days => a.deleted_retention_days)"
        " ORDER BY af.gone_since LIMIT %s",
        (now - PURGE_RETRY, now, limit),
    )
    done = 0
    for annex_id, file_id, days in await cur.fetchall():
        async with conn.transaction():
            moved = await conn.execute(
                "UPDATE catalog.annex_file af SET purge_requested_at = %s,"
                " purge_requested_by = 'system:annex_retention'"
                " WHERE af.annex_id = %s AND af.file_id = %s AND af.purge_requested_at IS NULL"
                "   AND af.gone_since < %s" + _NO_OPEN_PLACEMENT,
                (now, annex_id, file_id, now - timedelta(days=days)),
            )
            # In the same transaction (decision 27): a crash before the purge below
            # leaves a request the worker still picks up, not one nothing ever does.
            if moved.rowcount:
                await send(conn, RETENTION_QUEUE, {"op": "annex_purge", "annex_id": str(annex_id),
                                                   "file_id": str(file_id)})  # fmt: skip
        if moved.rowcount and await purge_file(conn, annex_id, file_id, now) == "purged":
            done += 1
    return done
