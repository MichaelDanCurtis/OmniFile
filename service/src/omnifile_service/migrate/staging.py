"""Staging copies for urgent recalls (docs/design/migration-engine.md
section 4; decision 24).

A restore someone asked for (a user's recall, or the partner API's) of an
object under a migrating prefix can't wait for the prefix's next Additive
run, which may scan millions of objects. So:

- `stage`: the restored object is copied inside S3 (CopyObject: S3's copy,
  not ours, decision 20) to `omnifile-staging/<migration id>/<rest of key>`
  in the backend's staging bucket, as Standard;
- `staging_progress`: one Storage Mover run at a time per prefix copies the
  staging prefix onto the prefix's real target path. When it ends, the
  engine finds the copies by name (`discover_keys`) and verifies them like
  any other, against the original object's checksum (CopyObject keeps it);
- `unstage`: a staging object is deleted once its file has a verified copy
  (however it got one), or once it is given up.

A failed run puts its objects back for the next one, at most
`RUN_ATTEMPTS` times; after that they wait for the prefix's next run like
any other file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..backends.base import BackendError, ObjectNotFound, ObjectRef
from ..backends.s3.config import STAGING_PREFIX
from ..worker.queue import Message
from .calls import CALL_SECONDS, called, provider, start_run
from .mover import Mover, MoverJob

log = logging.getLogger("omnifile_service.migrate")

#: Restored objects copied into the staging bucket per step (each is a
#: server-side copy, up to UploadPartCopy for big files).
STAGE_PER_STEP = 10
#: A step's copies stop here. UploadPartCopy goes 512 MiB at a time, in sequence, so a
#: file of a few hundred GB takes hours. An object cut off is left to the prefix's next
#: run, and one not reached goes in the next step.
STAGE_SECONDS = 6 * 3600.0
#: Staging objects deleted per step, all within `DELETES_SECONDS`.
UNSTAGE_PER_STEP = 100
DELETES_SECONDS = CALL_SECONDS - 10
RUN_ATTEMPTS = 3
#: How many times a cutover tries to delete each staging object (`drain_staging` makes all of
#: them; before the prefix is verified, `unstage` tries too, and those don't count), and how
#: long it waits when
#: all that's left has just failed. Leftover bytes cost money and risk no data, so past this
#: the prefix cuts over and says so. `attempts` counts these after any runs' (`RUN_ATTEMPTS`).
DELETE_TRIES = 5
DELETE_RETRY = timedelta(minutes=5)
#: In the prefix's `last_error`, and how the console and the alert know it.
LEFT_BEHIND = "staging objects could not be deleted"
#: Restores that someone is waiting for: not a wave's (those wait for the run).
URGENT = ("user_recall", "api")


#: The calls whose answers must go on record at once (`engine.migrate_step`).
STAGE_TAG = "stage"
STAGING_START_TAG = "staging_start"


def staging_bucket(source: Any, m: Mapping[str, Any]) -> str | None:
    bucket = getattr(getattr(source, "config", None), "staging_bucket", None)
    if bucket is None or bucket == m["src_container"]:
        return None  # never stage into the dataset's own bucket
    return str(bucket)


def staging_subpath(m: Mapping[str, Any]) -> str:
    return f"{STAGING_PREFIX}{m['migration_id']}/"


async def _copy_all(
    source: Any, m: Mapping[str, Any], bucket: str, rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Copy `rows` into the staging bucket, in order, within `STAGE_SECONDS`. Returns
    what to record for each one reached: the catalog may have moved by the time this
    returns, and what was copied must be recorded, or it is never deleted."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STAGE_SECONDS
    done: list[dict[str, Any]] = []
    for r in rows:
        if loop.time() >= deadline:
            break  # no row: the next step picks it up
        key = staging_subpath(m) + r["object_key"][len(m["prefix"]) :]
        ref = ObjectRef(m["src_backend_id"], r["container"], r["object_key"],
                        r["backend_version_id"])  # fmt: skip
        state, error, version, gone = "staged", None, None, False
        try:
            # S3Backend aborts the multipart upload when this is cancelled.
            async with asyncio.timeout_at(deadline):
                new = await source.copy_object(ref, bucket, key)
            version = new.version_id
        except TimeoutError:
            # `failed`, not `removed`: the provider may have finished a single CopyObject
            # as it was cut off, so `unstage` deletes whatever is there.
            state, error = "failed", f"not copied in {STAGE_SECONDS:.0f} s"
        except (BackendError, ObjectNotFound) as e:
            # Gone or archived again since: nothing was staged, and the
            # prefix's next run deals with it.
            state, error, gone = "removed", f"{type(e).__name__}: {e}"[:500], True
        done.append({"location_id": r["location_id"], "restore_id": r["restore_id"],
                     "bucket": bucket, "object_key": key, "version": version, "state": state,
                     "error": error, "gone": gone})  # fmt: skip
    return done


async def stage(
    conn: psycopg.AsyncConnection,
    source: Any,
    m: Mapping[str, Any],
    like: str,
    now: datetime,
    msg: Message | None = None,
) -> int:
    """Copy restored objects someone is waiting for into the staging bucket.
    Returns how many it tried."""
    bucket = staging_bucket(source, m)
    if bucket is None:
        return 0
    cur = conn.cursor(row_factory=dict_row)
    # Never stage into a bucket any dataset reads: its follower would catalog
    # the staged objects as that dataset's files.
    await cur.execute(
        "SELECT 1 FROM catalog.source WHERE backend_id = %s AND container = %s LIMIT 1",
        (m["src_backend_id"], bucket),
    )
    if await cur.fetchone() is not None:
        await conn.execute(
            "UPDATE catalog.migration_prefix SET last_error = %s, updated_at = %s"
            " WHERE migration_id = %s",
            (f"staging bucket {bucket} is a source of a dataset: urgent recalls are not"
             " staged", now, m["migration_id"]),
        )  # fmt: skip
        return 0
    await cur.execute(
        """
        SELECT DISTINCT ON (l.location_id) l.location_id, l.container, l.object_key,
               l.backend_version_id, r.restore_id
          FROM catalog.location l
          JOIN catalog.file f ON f.content_version_id = l.version_id AND f.deleted_at IS NULL
          JOIN catalog.restore r ON r.location_id = l.location_id
         WHERE l.backend_id = %(sb)s AND l.container = %(sc)s
           AND l.object_key COLLATE "C" LIKE %(like)s AND l.state = 'committed'
           AND r.state = 'completed' AND r.triggered_by = ANY(%(urgent)s)
           AND (r.expires_at IS NULL OR r.expires_at > %(now)s)
           AND NOT EXISTS (SELECT 1 FROM catalog.location t
                            WHERE t.version_id = l.version_id AND t.backend_id = %(tb)s
                              AND t.state IN ('pending', 'committed'))
           AND NOT EXISTS (SELECT 1 FROM catalog.staging_object o
                            WHERE o.migration_id = %(mid)s AND o.location_id = l.location_id)
         ORDER BY l.location_id, r.completed_at DESC
         LIMIT %(n)s
        """,
        {"sb": m["src_backend_id"], "sc": m["src_container"], "like": like,
         "urgent": list(URGENT), "now": now, "tb": m["to_backend_id"],
         "mid": m["migration_id"], "n": STAGE_PER_STEP},
    )  # fmt: skip
    rows = await cur.fetchall()
    if not rows:
        return 0
    done = await provider(
        msg, STAGE_TAG, lambda: _copy_all(source, m, bucket, rows), seconds=STAGE_SECONDS + 60
    )
    await record_staged(conn, m, now, done)
    return len(done)


async def record_staged(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], now: datetime, done: Sequence[Any]
) -> None:
    """What `_copy_all` copied, or tried to."""
    for d in done:
        await conn.execute(
            "INSERT INTO catalog.staging_object (migration_id, location_id, restore_id, bucket,"
            " object_key, backend_version_id, state, error, staged_at, removed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (m["migration_id"], d["location_id"], d["restore_id"], d["bucket"], d["object_key"],
             d["version"], d["state"], d["error"], now, now if d["gone"] else None),
        )  # fmt: skip
    staged = sum(1 for d in done if d["state"] == "staged")
    if staged:
        log.info("staged urgent recalls", extra={"migration_id": m["migration_id"], "n": staged})


async def staging_progress(
    conn: psycopg.AsyncConnection,
    source: Any,
    mover: Mover | None,
    m: Mapping[str, Any],
    now: datetime,
    msg: Message | None = None,
) -> list[str]:
    """Look at the prefix's staging run and start one when objects wait.
    Returns the target keys a run that just ended copied, for the engine
    to find."""
    bucket = staging_bucket(source, m)
    if bucket is None or mover is None:
        return []
    mid = m["migration_id"]
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT * FROM catalog.staging_run WHERE migration_id = %s AND state = 'running'"
        " FOR UPDATE",
        (mid,),
    )
    run = await cur.fetchone()
    copied: list[str] = []
    if run is not None:
        status = await provider(
            msg,
            f"staging_status:{run['run_id']}",
            lambda: mover.run_status(run["job"], run["run_id"]),
        )
        if status.state == "running":
            return []
        ok = status.state == "succeeded"
        await conn.execute(
            "UPDATE catalog.staging_run SET state = %s, counters = %s, error = %s,"
            " finished_at = %s WHERE staging_run_id = %s",
            ("done" if ok else "failed", Jsonb(dict(status.counters)), status.error, now,
             run["staging_run_id"]),
        )  # fmt: skip
        if ok:
            await cur.execute(
                "UPDATE catalog.staging_object SET state = 'copied'"
                " WHERE staging_run_id = %s AND state = 'moving' RETURNING object_key",
                (run["staging_run_id"],),
            )
            sub = staging_subpath(m)
            target = m["target_root"] + m["prefix"]
            copied = [target + r["object_key"][len(sub) :] for r in await cur.fetchall()]
        else:
            await conn.execute(
                "UPDATE catalog.staging_object"
                "   SET state = CASE WHEN attempts >= %s THEN 'failed' ELSE 'staged' END,"
                "       error = %s"
                " WHERE staging_run_id = %s AND state = 'moving'",
                (RUN_ATTEMPTS, status.error or "staging run failed", run["staging_run_id"]),
            )
        # The next run waits for the next step: this one's end has to commit first, and
        # finding its copies is a call that can fail.
        return copied
    await cur.execute(
        "SELECT 1 FROM catalog.staging_object WHERE migration_id = %s AND state = 'staged' LIMIT 1",
        (mid,),
    )
    if await cur.fetchone() is None:
        return copied
    try:
        started = await start_run(
            msg,
            STAGING_START_TAG,
            mover,
            MoverJob(
                name=f"omnifile-staging-{mid}",
                source_backend_id=m["src_backend_id"],
                source_container=bucket,
                source_subpath=staging_subpath(m),
                target_backend_id=m["to_backend_id"],
                target_container=m["to_container"],
                target_subpath=m["target_root"] + m["prefix"],
            ),
            now,
        )
    except BackendError as e:
        # No Storage Mover endpoint for the staging bucket, say: name it,
        # and the files wait for the prefix's next run.
        await conn.execute(
            "UPDATE catalog.migration_prefix SET last_error = %s, updated_at = %s"
            " WHERE migration_id = %s",
            (f"staging run: {e}"[:500], now, mid),
        )
        return copied
    await record_staging_run(conn, m, now, started)
    return copied


async def record_staging_run(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], now: datetime, started: Any
) -> None:
    """A staging run that was started: `start_run`'s answer. What is staged by now goes in it."""
    job, run_id, _ = started
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "INSERT INTO catalog.staging_run (migration_id, job, run_id, started_at)"
        " VALUES (%s, %s, %s, %s) RETURNING staging_run_id",
        (m["migration_id"], job, run_id, now),
    )
    sr = await cur.fetchone()
    assert sr is not None
    await conn.execute(
        "UPDATE catalog.staging_object SET state = 'moving', staging_run_id = %s,"
        " attempts = attempts + 1 WHERE migration_id = %s AND state = 'staged'",
        (sr["staging_run_id"], m["migration_id"]),
    )


_REMOVABLE = """
  FROM catalog.staging_object o
  JOIN catalog.location s ON s.location_id = o.location_id
 WHERE o.migration_id = %(mid)s AND o.state <> 'removed'
   -- Only OmniFile's own staging keys are deleted, never an object
   -- of a bucket that is (or has since become) a migration source.
   AND o.object_key LIKE %(staged)s AND NOT catalog.on_source(%(sb)s, o.bucket)
   -- Past its tries, it is left for the record (`drain_staging`).
   AND o.attempts < %(give_up)s
"""


def _params(m: Mapping[str, Any]) -> dict[str, Any]:
    return {"mid": m["migration_id"], "staged": STAGING_PREFIX + "%", "sb": m["src_backend_id"],
            "tb": m["to_backend_id"], "give_up": RUN_ATTEMPTS + DELETE_TRIES}  # fmt: skip


async def unstage(
    conn: psycopg.AsyncConnection,
    source: Any,
    m: Mapping[str, Any],
    now: datetime,
    *,
    msg: Message | None = None,
) -> int:
    """Delete staging objects whose file has a verified copy, that were
    given up, or whose S3 object is no longer a live file's current content
    (overwritten or deleted since). Returns how many were removed, at most
    `UNSTAGE_PER_STEP`. What is left is looked at again in the next step."""
    rows: list[Any] = []
    if not called(msg, "unstage"):
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT o.location_id, o.bucket, o.object_key, o.backend_version_id"
            + _REMOVABLE
            + """
               AND (o.state = 'failed'
                    OR o.state IN ('staged', 'copied') AND (EXISTS (
                       SELECT 1 FROM catalog.location t
                        WHERE t.version_id = s.version_id AND t.backend_id = %(tb)s
                          AND t.state = 'committed' AND t.verified)
                       OR NOT EXISTS (
                       SELECT 1 FROM catalog.file f
                        WHERE f.content_version_id = s.version_id AND f.deleted_at IS NULL)))
             ORDER BY o.location_id
             LIMIT %(n)s
            """,
            {**_params(m), "n": UNSTAGE_PER_STEP},
        )
        rows = await cur.fetchall()
        if not rows:
            return 0
    tried = await provider(msg, "unstage", lambda: _delete_all(source, m, rows))
    removed = [lid for lid, why in tried if why is None]
    await _mark_removed(conn, m, now, removed)
    return len(removed)


async def drain_staging(
    conn: psycopg.AsyncConnection,
    source: Any,
    m: Mapping[str, Any],
    now: datetime,
    msg: Message | None = None,
) -> timedelta | None:
    """Before a prefix is cut over: delete every staging object left. Returns when to look
    again, or None once each has been deleted or tried `DELETE_TRIES` times. Those are then
    named on the prefix, for the alert, and stay on record as `failed`, and a run still open
    is closed. What is left decides this, not what was removed: the objects tried least go
    first, so one that keeps failing doesn't keep the others from being tried."""
    rows: list[Any] = []
    cur = conn.cursor(row_factory=dict_row)
    if not called(msg, "unstage_final"):
        await cur.execute(
            "SELECT o.location_id, o.bucket, o.object_key, o.backend_version_id"
            + _REMOVABLE
            + " ORDER BY o.attempts, o.location_id LIMIT %(n)s",
            {**_params(m), "n": UNSTAGE_PER_STEP},
        )
        rows = await cur.fetchall()
    failed = False
    if rows or called(msg, "unstage_final"):
        tried = await provider(msg, "unstage_final", lambda: _delete_all(source, m, rows))
        await _mark_removed(conn, m, now, [lid for lid, why in tried if why is None])
        for lid, why in tried:
            if why is not None:
                failed = True
                await conn.execute(
                    "UPDATE catalog.staging_object SET error = %s,"
                    " attempts = greatest(attempts, %s) + 1"
                    " WHERE migration_id = %s AND location_id = %s",
                    (why[:500], RUN_ATTEMPTS, m["migration_id"], lid),
                )
    await cur.execute(
        "SELECT count(*) AS n, min(o.attempts) AS least, max(o.attempts) AS most" + _REMOVABLE,
        _params(m),
    )
    left = await cur.fetchone()
    assert left is not None
    if left["n"]:
        # Failed, with nothing left that hasn't had its turn since: give it time.
        waits = failed and left["least"] == left["most"]
        return DELETE_RETRY if waits else timedelta(0)
    await cur.execute(
        "UPDATE catalog.staging_object o SET state = 'failed'"
        " WHERE o.migration_id = %(mid)s"
        "   AND o.state <> 'removed' AND o.object_key LIKE %(staged)s"
        "   AND NOT catalog.on_source(%(sb)s, o.bucket)"
        " RETURNING o.bucket",
        _params(m),
    )
    stuck = [r["bucket"] for r in await cur.fetchall()]
    if stuck:
        error = f"{len(stuck)} {LEFT_BEHIND} from {stuck[0]}: they stay there, and are billed"
        log.error(error, extra={"migration_id": m["migration_id"]})
        await conn.execute(
            "UPDATE catalog.migration_prefix SET last_error = %s, updated_at = %s"
            " WHERE migration_id = %s",
            (error, now, m["migration_id"]),
        )
    await conn.execute(
        "UPDATE catalog.staging_run SET state = 'failed', finished_at = %s,"
        " error = 'the prefix was cut over' WHERE migration_id = %s AND state = 'running'",
        (now, m["migration_id"]),
    )
    return None


async def _mark_removed(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], now: datetime, removed: Sequence[int]
) -> None:
    if removed:
        await conn.execute(
            "UPDATE catalog.staging_object SET state = 'removed', removed_at = %s"
            " WHERE migration_id = %s AND location_id = ANY(%s)",
            (now, m["migration_id"], list(removed)),
        )


async def _delete_all(
    source: Any, m: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> list[tuple[int, str | None]]:
    """For each row tried, its location id and why its object is still there, if it is. Out
    of time, the one being deleted counts as tried, so a hung delete can't hold a step on
    it, and the rest wait for the next step."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DELETES_SECONDS
    tried: list[tuple[int, str | None]] = []
    for r in rows:
        if loop.time() >= deadline:
            break
        ref = ObjectRef(m["src_backend_id"], r["bucket"], r["object_key"],
                        r["backend_version_id"])  # fmt: skip
        why = None
        try:
            async with asyncio.timeout_at(deadline):
                await source.delete(ref)
        except ObjectNotFound:
            pass
        except TimeoutError:
            why = f"no answer in {DELETES_SECONDS:.0f} s"
        except BackendError as e:
            why = str(e)
        if why is not None:
            log.warning("staging object not deleted", extra={"key": r["object_key"], "e": why})
        tried.append((r["location_id"], why))
    return tried


__all__ = [
    "LEFT_BEHIND",
    "STAGE_TAG",
    "STAGING_PREFIX",
    "STAGING_START_TAG",
    "drain_staging",
    "record_staged",
    "record_staging_run",
    "stage",
    "staging_progress",
    "unstage",
]
