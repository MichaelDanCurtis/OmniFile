"""Restore waves: archived objects restored in bulk before Storage Mover can
copy them (docs/design/migration-engine.md section 8; decision 15).

- `plan_wave`: one S3 Batch Operations restore job over a prefix's archived
  objects that have no verified copy and no live or pending restore, with a
  `catalog.restore` row per object (`s3_batch_restore`, `migration_wave`,
  `migrate_on_complete`), matched to Restore Completed events like any
  restore.
- `wave_progress`: a wave is done when none of its restores is open; one
  open past its window (`WAVE_WINDOW`) is failed, so a task the job never
  ran (its failure report) can't hold the prefix forever.
- `restored_since`: a restore under the prefix completed since the last
  copy started, a wave's or a user's recall: the engine copies again
  without waiting for the next scheduled run (not for one being staged,
  migrate/staging.py: that one is on its way already).
- `expiring`: restored copies about to expire with no verified copy yet,
  for the location's `last_error` (an alert); expired ones go into the
  next wave.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..backends.base import ObjectRef, RestoreSpeed
from ..backends.s3.events import restore_ref
from ..worker.queue import Message
from .calls import provider
from .checksums import job_name, one_kind
from .opsbuckets import batch_writes_source

log = logging.getLogger("omnifile_service.migrate")

WAVE_MAX = 100_000
WAVE_DAYS = 14
WAVE_SPEED = RestoreSpeed.BULK
#: Deep Archive Bulk takes up to 48 h [V]; a wave's restores get a day more.
WAVE_WINDOW = timedelta(hours=72)
EXPIRY_WARNING = timedelta(hours=24)
#: The call whose answer must go on record at once (`engine.migrate_step`).
WAVE_TAG = "wave_job"
ABANDONED_PER_SWEEP = 100
#: Classes a Batch restore with `Days` covers (not Intelligent-Tiering).
WAVE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")

_LIVE = """
SELECT l.location_id, l.object_key, l.backend_version_id, l.size, f.content_version_id
  FROM catalog.location l
  JOIN catalog.file f ON f.content_version_id = l.version_id AND f.deleted_at IS NULL
 WHERE l.backend_id = %(sb)s AND l.container = %(sc)s
   AND l.object_key COLLATE "C" LIKE %(like)s
   AND l.state = 'committed'
"""

_UNCOPIED = """
   AND NOT EXISTS (SELECT 1 FROM catalog.location t
                    WHERE t.version_id = f.content_version_id AND t.backend_id = %(tb)s
                      AND t.state = 'committed' AND t.verified)
"""


def _params(m: Mapping[str, Any], like: str, now: datetime) -> dict[str, Any]:
    return {"sb": m["src_backend_id"], "sc": m["src_container"], "like": like,
            "tb": m["to_backend_id"], "now": now}  # fmt: skip


async def plan_wave(
    conn: psycopg.AsyncConnection,
    backend: Any,
    m: Mapping[str, Any],
    like: str,
    now: datetime,
    *,
    renew_expiring: bool = False,
    msg: Message | None = None,
) -> int | None:
    """Start a wave for the prefix's archived, uncopied objects, if the
    source backend runs Batch Operations and any need one. Returns the wave
    id. `renew_expiring` (the console's Restore again): restored copies that
    expire within EXPIRY_WARNING, still unverified, are restored again too
    (S3 extends a restored copy's life)."""
    if getattr(getattr(backend, "config", None), "batch", None) is None:
        return None
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        _LIVE
        + _UNCOPIED
        + """
           AND l.storage_class = ANY(%(classes)s)
           AND NOT EXISTS (SELECT 1 FROM catalog.location p
                            WHERE p.version_id = f.content_version_id
                              AND p.backend_id = %(tb)s AND p.state = 'pending')
           AND NOT EXISTS (SELECT 1 FROM catalog.restore r
                            WHERE r.location_id = l.location_id
                              AND (r.state IN ('requested', 'in_progress')
                                   OR (r.state = 'completed'
                                       AND (r.expires_at IS NULL OR r.expires_at > %(keep)s))))
         ORDER BY l.location_id LIMIT %(max)s
        """,
        {
            **_params(m, like, now),
            "classes": list(WAVE_CLASSES),
            "max": WAVE_MAX,
            "keep": now + EXPIRY_WARNING if renew_expiring else now,
        },  # fmt: skip
    )
    rows = one_kind(await cur.fetchall())
    if not rows:
        return None
    if await batch_writes_source(conn, backend, m, now):
        return None
    await cur.execute(
        "SELECT count(*) AS n FROM catalog.restore_wave WHERE migration_id = %s",
        (m["migration_id"],),
    )
    n = int((await cur.fetchone())["n"])  # type: ignore[index]
    refs = [ObjectRef(m["src_backend_id"], m["src_container"], r["object_key"],
                      r["backend_version_id"]) for r in rows]  # fmt: skip
    name = job_name("wave", m["migration_id"], n + 1, refs)

    # The job first, named the same on a retry over the same objects (the
    # request token): a lost CreateJob response finds the same job, and a
    # failed call leaves no row.
    async def create() -> tuple[str, list[Any], list[ObjectRef], bool]:
        job = await backend.batch_restore(refs, WAVE_SPEED, WAVE_DAYS, name=name)
        return job, rows, refs, renew_expiring

    made = await provider(msg, WAVE_TAG, create)
    return await record_wave(conn, m, now, made)


async def record_wave(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], now: datetime, made: Any
) -> int:
    """A job that was created: the objects it was asked to restore, whatever the catalog says
    now. S3 is restoring them, so a later wave or migration mustn't ask for them again."""
    job, rows, refs, renewed = made
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "INSERT INTO catalog.restore_wave (migration_id, backend_id, job_id, objects, speed,"
        " days, requested_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING wave_id",
        (m["migration_id"], m["src_backend_id"], job, len(rows), str(WAVE_SPEED), WAVE_DAYS,
         now),
    )  # fmt: skip
    wave = await cur.fetchone()
    assert wave is not None
    wave_id = int(wave["wave_id"])
    await cur.execute(
        """
        INSERT INTO catalog.restore
            (location_id, backend_id, mechanism, speed, provider_ref, days, state, triggered_by,
             migrate_on_complete, bytes, requested_at, wave_id)
        SELECT t.l, %s, 's3_batch_restore', %s, t.ref, %s, 'requested', 'migration_wave', true,
               t.b, %s, %s
          FROM unnest(%s::bigint[], %s::text[], %s::bigint[]) AS t(l, ref, b)
        ON CONFLICT (location_id) WHERE state IN ('requested', 'in_progress') DO NOTHING
        """,
        (m["src_backend_id"], str(WAVE_SPEED), WAVE_DAYS, now, wave_id,
         [r["location_id"] for r in rows], [restore_ref(ref) for ref in refs],
         [r["size"] for r in rows]),
    )  # fmt: skip
    if renewed:
        await conn.execute(
            "UPDATE catalog.migration_prefix SET restore_requested_at = NULL"
            " WHERE migration_id = %s",
            (m["migration_id"],),
        )
    log.info("restore wave", extra={"wave_id": wave_id, "objects": len(rows), "job": job})
    return wave_id


async def wave_progress(
    conn: psycopg.AsyncConnection,
    backend: Any,
    migration_id: int,
    now: datetime,
    msg: Message | None = None,
) -> str | None:
    """The running wave's state after this look: None (no running wave),
    'running' or 'done'."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT * FROM catalog.restore_wave WHERE migration_id = %s AND state = 'running'"
        " FOR UPDATE",
        (migration_id,),
    )
    w = await cur.fetchone()
    if w is None:
        return None
    status = None
    if w["job_id"] and hasattr(backend, "batch_job"):
        try:
            status = dict(
                await provider(msg, f"job:{w['job_id']}", lambda: backend.batch_job(w["job_id"]))
            )
        except Exception as e:  # noqa: BLE001 - the restores' own events still count
            log.warning("batch job status", extra={"job": w["job_id"], "err": str(e)})
    if now - w["requested_at"] > WAVE_WINDOW:
        await cur.execute(
            "UPDATE catalog.restore SET state = 'failed', failure = %s, completed_at = %s,"
            " updated_at = %s WHERE wave_id = %s AND state IN ('requested', 'in_progress')",
            ("no completion within the wave's window", now, now, w["wave_id"]),
        )
    await cur.execute(
        "SELECT count(*) AS n FROM catalog.restore WHERE wave_id = %s"
        "   AND state IN ('requested', 'in_progress')",
        (w["wave_id"],),
    )
    row = await cur.fetchone()
    if row is not None and row["n"] > 0:
        if status is not None:
            await cur.execute(
                "UPDATE catalog.restore_wave SET last_status = %s WHERE wave_id = %s",
                (Jsonb(status), w["wave_id"]),
            )
        return "running"
    await cur.execute(
        "UPDATE catalog.restore_wave SET state = 'done', finished_at = %s, last_status = %s"
        " WHERE wave_id = %s",
        (now, Jsonb(status) if status is not None else None, w["wave_id"]),
    )
    return "done"


async def close_abandoned(conn: psycopg.AsyncConnection, now: datetime) -> int:
    """Close waves no step will look at again: running, past `WAVE_WINDOW`, under a prefix that
    is cancelled or cut over. `wave_progress` does this for a live prefix. Restores still open
    are failed, so a task that never ran doesn't block its object in any later wave, or hold
    a recall that joined it. Returns how many were closed."""
    cur = conn.cursor(row_factory=dict_row)
    closed = 0
    # One wave per transaction: a wave can have 100,000 restores, and an id held across many
    # of those would hold back every edge's feed.
    while closed < ABANDONED_PER_SWEEP:
        async with conn.transaction():
            await cur.execute("SELECT set_config('omnifile.actor', 'system:migrate', true)")
            await cur.execute(
                "SELECT w.wave_id, p.state FROM catalog.restore_wave w"
                "  JOIN catalog.migration_prefix p USING (migration_id)"
                " WHERE w.state = 'running' AND p.state IN ('cancelled', 'cut_over')"
                "   AND w.requested_at < %s"
                " ORDER BY w.wave_id LIMIT 1 FOR UPDATE OF w SKIP LOCKED",
                (now - WAVE_WINDOW,),
            )
            w = await cur.fetchone()
            if w is None:
                break
            why = f"its prefix is {w['state']}, so no step will close it"
            await conn.execute(
                "UPDATE catalog.restore SET state = 'failed', failure = %s, completed_at = %s,"
                " updated_at = %s WHERE wave_id = %s AND state IN ('requested', 'in_progress')",
                (why, now, now, w["wave_id"]),
            )
            await conn.execute(
                "UPDATE catalog.restore_wave SET state = 'done', finished_at = %s, failure = %s"
                " WHERE wave_id = %s",
                (now, why, w["wave_id"]),
            )
        closed += 1
    if closed:
        log.warning("closed abandoned restore waves", extra={"n": closed})
    return closed


async def restored_since(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], like: str, now: datetime
) -> bool:
    """A live restored copy under the prefix, of a file not verified yet,
    completed after the last copy started."""
    if m["last_copy_started_at"] is None:
        return False
    cur = await conn.execute(
        "SELECT EXISTS ("
        + _LIVE
        + _UNCOPIED
        + """
           AND EXISTS (SELECT 1 FROM catalog.restore r
                        WHERE r.location_id = l.location_id AND r.state = 'completed'
                          AND r.completed_at > %(since)s
                          AND (r.expires_at IS NULL OR r.expires_at > %(now)s))
           AND NOT EXISTS (SELECT 1 FROM catalog.staging_object o
                            WHERE o.migration_id = %(mid)s AND o.location_id = l.location_id
                              AND o.state IN ('staged', 'moving', 'copied')))
        """,
        {
            **_params(m, like, now),
            "since": m["last_copy_started_at"],
            "mid": m["migration_id"],
        },  # fmt: skip
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def expiring(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], like: str, now: datetime
) -> int:
    """Restored copies under the prefix that expire within EXPIRY_WARNING
    while their file has no verified copy."""
    cur = await conn.execute(
        "SELECT count(*) FROM ("
        + _LIVE
        + _UNCOPIED
        + """
           AND EXISTS (SELECT 1 FROM catalog.restore r
                        WHERE r.location_id = l.location_id AND r.state = 'completed'
                          AND r.expires_at > %(now)s AND r.expires_at <= %(soon)s)) x
        """,
        {**_params(m, like, now), "soon": now + EXPIRY_WARNING},
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


__all__ = ["WAVE_DAYS", "WAVE_MAX", "WAVE_TAG", "WAVE_WINDOW", "close_abandoned", "expiring",
           "plan_wave",
           "record_wave", "restored_since", "wave_progress"]  # fmt: skip
