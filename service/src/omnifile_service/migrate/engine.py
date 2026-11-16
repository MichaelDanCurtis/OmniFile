"""The migration engine: copy, verify, cut over, one prefix at a time
(docs/design/migration-engine.md).

`request_location` (the catalog half, run by `web` or the CLI) records a
migration prefix and queues it. The worker's `migrate` queue then steps it,
one transaction per step, re-queueing itself:

    pending -> copying -> verifying -> verified -> cut_over

- **pending:** start a Storage Mover run (`copy_by = omnifile`), or just
  start watching the target (`external`: Prizmanyx copies).
- **copying:** poll the run; when it ends, discover the copies.
- **verifying:** every copy is checked on the `migrate_verify` queue (a full
  CRC64-NVME read in Azure, `verify_copy`). The prefix is verified when
  every live file under it has a verified copy of its current content and
  the bucket's change feed has been applied past the run's start. Files
  still without a copy (archived and not restored yet, or changed in S3
  since) wait for the next Additive run.
- **verified:** flip files in batches: the copy becomes the primary
  location, the S3 object a secondary one. Retained and held files flip
  like any other, on WORM datasets too (migration 0027 lets the catalog's
  guard swap primary and secondary towards a verified copy), once their
  copy carries the version's retention and hold. The S3 object stays a
  retained secondary.

Discovery, verification and the flip only ever touch files of this
prefix's source, and nothing is deleted in S3.

No provider call is made inside a step's transaction (`migrate/calls.py`): the
step runs again from the top after each call, and only its last pass writes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..backends.azure.backend import posix_metadata
from ..backends.base import BackendError, ObjectNotFound, ObjectRef, StorageBackend
from ..backends.sim.crc64 import to_bytes
from ..catalog.blob_lock import blob_lock_key
from ..reconcile.drift import record_drift
from ..retention.common import data_container
from ..worker.queue import Defer, Message, send
from ..worker.verify import read_crc
from .calls import called, provider, start_run
from .checksums import CHECKSUM_TAG, checksum_progress, plan_checksums, record_checksum_job
from .mover import Mover, MoverJob
from .staging import (
    STAGE_TAG,
    STAGING_START_TAG,
    drain_staging,
    record_staged,
    record_staging_run,
    stage,
    staging_progress,
    unstage,
)
from .waves import WAVE_TAG, expiring, plan_wave, record_wave, restored_since, wave_progress
from .windows import PLAN_ACTOR, wave_gate

log = logging.getLogger("omnifile_service.migrate")

QUEUE = "migrate"
VERIFY_QUEUE = "migrate_verify"
ACTOR = "system:migrate"
#: How often a step looks again while a run or verification is under way.
POLL = timedelta(minutes=1)
#: A prefix with files still uncopied runs its job again this often, or
#: sooner (but not more than hourly) once objects under it were restored.
RERUN_EVERY = timedelta(hours=6)
RERUN_MIN = timedelta(hours=1)
#: Files flipped per transaction.
FLIP_BATCH = 10_000
#: How often a verified prefix held for a manual cutover is counted again.
HELD_POLL = timedelta(minutes=10)
#: The prefix's `last_error` while held copies wait for their WORM stamp.
UNSTAMPED = "copies still wait for their retention or legal hold"
#: A step's message: where the listing of the prefix's target has got to.
DISCOVER = "discover"
#: The slowest read of a copy we wait for, and the least time a read gets.
READ_BYTES_PER_S = 25 * 1024 * 1024
READ_SECONDS = 3600.0
#: A copy's blob is being removed by another message: look again this soon.
BLOB_BUSY_S = 30.0


class MigrationError(Exception):
    """A request the catalog refuses; `code` is the API's problem code."""

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def _like(prefix: str) -> str:
    """A LIKE pattern matching keys that start with `prefix`."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def request_location(
    conn: psycopg.AsyncConnection,
    *,
    dataset_id: int,
    prefix: str,
    to_backend_id: int,
    copy_by: str = "omnifile",
    mode: str = "serve",
    requested_by: str = "system",
    cutover: str = "auto",
) -> int:
    """Record a migration prefix and queue its first step, on the caller's
    transaction. `prefix` is an S3 key prefix inside one of the dataset's
    seeded sources. `cutover`: `auto` flips once verified; `manual` waits at
    `verified` until `cutover_requested_at` is set (the partner API's
    cutover). Returns the migration id."""
    if mode != "serve":
        raise MigrationError(f"mode {mode!r} is not offered yet; v1 serves (decision 35)")
    if copy_by not in ("omnifile", "external"):
        raise MigrationError("copy must be omnifile or external")
    if cutover not in ("auto", "manual"):
        raise MigrationError("cutover must be auto or manual")
    if prefix and not prefix.endswith("/"):
        raise MigrationError(f"prefix {prefix!r} must be empty or end in '/'")
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT d.backend_id, d.migration_state, d.source_backend_id, b.config"
        "  FROM catalog.dataset d JOIN catalog.backend b ON b.backend_id = d.backend_id"
        " WHERE d.dataset_id = %s FOR UPDATE OF d",
        (dataset_id,),
    )
    ds = await cur.fetchone()
    if ds is None:
        raise MigrationError(f"no dataset {dataset_id}")
    if to_backend_id != ds["backend_id"]:
        raise MigrationError(
            f"v1 moves data to the dataset's own backend ({ds['backend_id']}), decision 23"
        )
    if ds["migration_state"] != "migrating":
        state = ds["migration_state"]
        raise MigrationError(f"dataset {dataset_id} is {state}", "migration_conflict")
    await cur.execute(
        "SELECT * FROM catalog.source WHERE dataset_id = %s AND kind = 'source'"
        "   AND backend_id = %s AND starts_with(%s, prefix)"
        " ORDER BY length(prefix) DESC LIMIT 1",
        (dataset_id, ds["source_backend_id"], prefix),
    )
    src = await cur.fetchone()
    if src is None:
        raise MigrationError(f"{prefix!r} is not inside a source of dataset {dataset_id}")
    if src["state"] != "seeded":
        raise MigrationError(f"source {src['source_id']} is {src['state']}", "migration_conflict")
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"omnifile.migration:{src['source_id']}",),
    )
    await cur.execute(
        "SELECT migration_id, prefix FROM catalog.migration_prefix WHERE source_id = %s"
        "   AND state <> 'cancelled'"
        "   AND (starts_with(prefix, %s) OR starts_with(%s, prefix))",
        (src["source_id"], prefix, prefix),
    )
    clash = await cur.fetchone()
    if clash is not None:
        raise MigrationError(
            f"{prefix!r} overlaps migration {clash['migration_id']} ({clash['prefix']!r})",
            "migration_conflict",
        )
    await cur.execute(
        """
        INSERT INTO catalog.migration_prefix
            (dataset_id, prefix, source_id, to_backend_id, to_container, target_root, copy_by,
             mode, requested_by, cutover)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING migration_id
        """,
        (dataset_id, prefix, src["source_id"], to_backend_id,
         data_container(ds["config"] or {}), f"migrated/{src['container']}/", copy_by, mode,
         requested_by, cutover),
    )  # fmt: skip
    row = await cur.fetchone()
    assert row is not None
    await send(conn, QUEUE, {"migration_id": int(row["migration_id"])})
    return int(row["migration_id"])


_ON_RECORD = {
    CHECKSUM_TAG: (record_checksum_job, timedelta(0)),
    WAVE_TAG: (record_wave, timedelta(0)),
    STAGE_TAG: (record_staged, timedelta(0)),
    STAGING_START_TAG: (record_staging_run, POLL),
}


@dataclass(frozen=True)
class Step:
    state: str
    #: Look again after this long; None: nothing more to do.
    again: timedelta | None
    #: More for the next step's message.
    body: Mapping[str, Any] = field(default_factory=dict)


async def migrate_step(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    mover: Mover | None,
    migration_id: int,
    now: datetime,
    msg: Message | None = None,
) -> Step:
    """Advance one migration prefix by one step, on the caller's transaction,
    and queue the next step. With a message a consumer read, each provider call is
    made `outside` the transaction (`migrate/calls.py`); with none, in place."""
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT m.*, s.backend_id AS src_backend_id, s.container AS src_container"
        "  FROM catalog.migration_prefix m JOIN catalog.source s USING (source_id)"
        " WHERE m.migration_id = %s FOR UPDATE OF m",
        (migration_id,),
    )
    m = await cur.fetchone()
    if m is None:
        return Step("gone", None)
    again = await _put_on_record(conn, m, now, msg)
    if again is not None:
        step = Step(m["state"], again)
    else:
        step = await _advance(conn, backend_for, mover, m, now, msg)
    if step.again is not None:
        body = {"migration_id": migration_id, **step.body}
        await send(conn, QUEUE, body, step.again.total_seconds())
    return step


async def _put_on_record(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], now: datetime, msg: Message | None
) -> timedelta | None:
    """The answer to a call a retry would make again, or pay for again, goes on record in the
    pass after it, and commits. Nothing else happens in that pass, so no later call's error
    can roll it back, and no cancel or closing window drops it: the job or the copy exists
    either way, and a later wave or migration would ask for it again. Returns when the next
    step should run, or None if there was nothing to record. A call that failed has nothing
    to record: the pass goes on, and the call's own place raises the error.

    The pass that asked was rolled back, so what it wrote before the call isn't there. So
    nothing a record depends on may be written in the same pass before such a call: a wave's
    and a checksum job's are planned only with none running, and a staging run ending
    waits for the next step to start another."""
    if msg is None:
        return None
    for tag, (record, again) in _ON_RECORD.items():
        done = msg.called.get(tag)
        if done is not None and done.error is None:
            await record(conn, m, now, done.value)
            return again
    return None


async def _set(conn: psycopg.AsyncConnection, mid: int, now: datetime, **cols: Any) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in cols)
    await conn.execute(
        f"UPDATE catalog.migration_prefix SET {sets}, updated_at = %(now)s"
        " WHERE migration_id = %(mid)s",
        {
            **{k: (Jsonb(v) if isinstance(v, dict) else v) for k, v in cols.items()},
            "now": now,
            "mid": mid,
        },  # fmt: skip
    )


def _drop_plan_cutover(m: Mapping[str, Any]) -> dict[str, Any]:
    """Columns that drop a plan sweep's cutover request when the migration
    leaves `verified`, so the next cutover is asked inside the window."""
    if m["cutover_requested_by"] == PLAN_ACTOR:
        return {"cutover_requested_at": None, "cutover_requested_by": None}
    return {}


async def _start_run(
    conn: psycopg.AsyncConnection,
    mover: Mover,
    m: Mapping[str, Any],
    now: datetime,
    msg: Message | None,
) -> Step:
    job, run_id, asked_at = await start_run(
        msg,
        "start_run",
        mover,
        MoverJob(
            name=f"omnifile-migration-{m['migration_id']}",
            source_backend_id=m["src_backend_id"],
            source_container=m["src_container"],
            source_subpath=m["prefix"],
            target_backend_id=m["to_backend_id"],
            target_container=m["to_container"],
            target_subpath=m["target_root"] + m["prefix"],
        ),
        now,
    )
    # When it was asked for, not this pass's clock: the journal gate and `_record` take
    # this as the moment the copy began.
    await _set(conn, m["migration_id"], now, state="copying", storage_mover_job=job,
               last_run_id=run_id, run_state="running", last_copy_started_at=asked_at,
               last_error=None, rerun_requested_at=None)  # fmt: skip
    return Step("copying", POLL)


def _resuming(msg: Message | None, m: Mapping[str, Any]) -> str | None:
    """Where the last step's listing of the target stopped, if it was this listing."""
    found = msg.body.get(DISCOVER) if msg is not None else None
    if not isinstance(found, Mapping):
        return None
    since = str(m["last_copy_started_at"])
    same = found.get("state") == m["state"] and found.get("since") == since
    return str(found["after"]) if same and found.get("after") else None


def _more(m: Mapping[str, Any], after: str) -> Step:
    """The next page of the target, in the next message: a listing of 10^8 blobs takes
    hours, and each page's records commit as they go."""
    since = str(m["last_copy_started_at"])
    body = {DISCOVER: {"state": m["state"], "since": since, "after": after}}
    return Step(m["state"], timedelta(0), body)


async def _advance(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    mover: Mover | None,
    m: Mapping[str, Any],
    now: datetime,
    msg: Message | None = None,
) -> Step:
    mid, state = m["migration_id"], m["state"]
    external = m["copy_by"] == "external"
    if state == "pending":
        if external:
            await _set(conn, mid, now, state="copying", last_copy_started_at=now)
            return Step("copying", POLL)
        if mover is None:
            await _set(conn, mid, now, last_error="no Storage Mover is configured")
            return Step("pending", POLL * 10)
        return await _start_run(conn, mover, m, now, msg)
    if state == "copying":
        after = _resuming(msg, m)
        if not external and after is None:
            assert mover is not None
            await _urgent(conn, backend_for, mover, m, now, msg)
            run = await provider(
                msg,
                f"run_status:{m['last_run_id']}",
                lambda: mover.run_status(m["storage_mover_job"], m["last_run_id"]),
            )
            await _set(conn, mid, now, run_state=run.state, run_counters=dict(run.counters),
                       last_error=run.error)  # fmt: skip
            if run.state == "running":
                return Step("copying", POLL)
        more = await _discover(conn, backend_for, m, msg, after)
        if more is not None:
            return _more(m, more)
        await _set(conn, mid, now, state="verifying")
        return Step("verifying", POLL)
    if state == "verifying":
        if external:
            more = await _discover(conn, backend_for, m, msg, _resuming(msg, m))
            if more is not None:
                return _more(m, more)
        source = backend_for(m["src_backend_id"])
        tlike = _like(m["target_root"] + m["prefix"])
        if await checksum_progress(conn, source, m, now, msg) is None:
            await plan_checksums(conn, source, m, tlike, now, msg)
        if not external:
            await _urgent(conn, backend_for, mover, m, now, msg)
        total, verified, _waiting = await _count(conn, m)
        through = await _journal(conn, m)
        await _set(conn, mid, now, files_total=total, files_verified=verified,
                   journal_checked_through=through)  # fmt: skip
        last = m["last_copy_started_at"]
        if total == verified and (through is not None and last is not None and through >= last):
            await _set(conn, mid, now, state="verified", verified_at=now)
            return Step("verified", timedelta(0))
        # Copies still waiting to be checked hold nothing up: one that can
        # never be checked (no checksum S3 can give) must not stop the
        # prefix's waves and runs. A wave skips objects whose copy waits.
        if external:
            return Step("verifying", POLL)  # Prizmanyx copies, and plans its own restores
        like = _like(m["prefix"])
        soon = await expiring(conn, m, like, now)
        if soon:
            await _set(conn, mid, now, last_error=f"{soon} restored objects expire within"
                                                  " 24 h with no verified copy")  # fmt: skip
        # A plan wave's window and blackouts gate new restore waves and runs
        # (migrate/windows.py); one already under way goes on.
        started = called(msg, "start_run")
        gated = await wave_gate(conn, mid, now)
        if gated is not None and not started:
            return Step("verifying", POLL)
        if await wave_progress(conn, source, mid, now, msg) is None:
            # The console's Restore again renews copies about to expire; it
            # waits while a wave is running, then goes into the next one.
            renew = m["restore_requested_at"] is not None
            await plan_wave(conn, source, m, like, now, renew_expiring=renew, msg=msg)
            if renew:
                await _set(conn, mid, now, restore_requested_at=None)
        if mover is None or last is None:
            return Step("verifying", POLL)
        if started:
            # Whatever a window or the schedule says by now, it was started. (A cancel
            # stands: that run finishes, and its copies are found if the prefix starts again.)
            return await _start_run(conn, mover, m, now, msg)
        # The console's Copy again: a new Additive run now, which also copies
        # again what failed its check. It moves the journal gate forward like
        # any run, so nothing flips before the bucket's events pass it.
        asked = m["rerun_requested_at"]
        if asked is not None and asked > last:
            return await _start_run(conn, mover, m, now, msg)
        early = now - last >= RERUN_MIN and await restored_since(conn, m, like, now)
        if now - last >= RERUN_EVERY or early:
            return await _start_run(conn, mover, m, now, msg)
        return Step("verifying", POLL)
    if state == "verified":
        if m["cutover"] == "manual" and m["cutover_requested_at"] is None:
            # Held for the partner API's cutover, which queues the next
            # step. Until then, a file changed in S3 sends the prefix back
            # to verifying, as after a flip batch.
            total, verified, _ = await _count(conn, m)
            if total != verified:
                await _set(conn, mid, now, state="verifying", verified_at=None,
                           files_total=total, files_verified=verified)  # fmt: skip
                return Step("verifying", POLL)
            return Step("verified", HELD_POLL)
        waiting = await _unstamped(conn, m)
        if waiting:
            # A retained or held version's copy becomes its primary only once
            # the WORM stamp sweep (retention/worm_sync.py) has given it the
            # version's retention and hold (decision 31).
            await _set(conn, mid, now, last_error=f"{waiting} {UNSTAMPED}")
            return Step("verified", POLL)
        if m["last_error"] is not None and m["last_error"].endswith(UNSTAMPED):
            await _set(conn, mid, now, last_error=None)
        flipped = await _flip(conn, m)
        if flipped >= FLIP_BATCH:
            return Step("verified", timedelta(0))
        total, verified, _ = await _count(conn, m)
        if total != verified:
            # A file was written or overwritten in S3 since the prefix was
            # verified: copy and check it before cutting over. A plan's
            # cutover request goes: the sweep asks again, in the window.
            await _set(conn, mid, now, state="verifying", verified_at=None,
                       files_total=total, files_verified=verified,
                       **_drop_plan_cutover(m))  # fmt: skip
            return Step("verifying", POLL)
        if not external:
            source = backend_for(m["src_backend_id"])
            wait = await drain_staging(conn, source, m, now, msg)
            if wait is not None:
                return Step("verified", wait)
        await _set(conn, mid, now, state="cut_over", cut_over_at=now)
        await _maybe_cut_over_dataset(conn, m["dataset_id"])
        return Step("cut_over", None)
    return Step(state, None)


async def _urgent(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    mover: Mover | None,
    m: Mapping[str, Any],
    now: datetime,
    msg: Message | None = None,
) -> None:
    """Restores someone is waiting for go through the staging bucket
    (migrate/staging.py) instead of waiting for the prefix's next run."""
    source = backend_for(m["src_backend_id"])
    await stage(conn, source, m, _like(m["prefix"]), now, msg)
    copied = await staging_progress(conn, source, mover, m, now, msg)
    if copied:
        await _discover_keys(conn, backend_for, m, copied, msg)
    await unstage(conn, source, m, now, msg=msg)


# --- discovery -------------------------------------------------------------------


async def _discover(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    m: Mapping[str, Any],
    msg: Message | None = None,
    after: str | None = None,
) -> str | None:
    """Find the copies under the prefix's target and record each one whose
    S3 object is a live file's current content as a pending secondary
    location, queued for verification. With a message a consumer read, this
    is one page, and it returns where the next one starts. Otherwise it reads
    them all, and at the last page it returns None."""
    target: StorageBackend = backend_for(m["to_backend_id"])
    cur = conn.cursor(row_factory=dict_row)
    new = 0
    cursor = after
    while True:
        page = await _page(msg, target, m, cursor)
        for head in page.items:
            new += await _record(cur, conn, m, head)
        cursor = page.cursor
        if cursor is None or (msg is not None and msg.consumed):
            break
    log.info("discovered copies", extra={"migration_id": m["migration_id"], "new": new})
    return cursor


async def _page(
    msg: Message | None, target: StorageBackend, m: Mapping[str, Any], cursor: str | None
) -> Any:
    under = m["target_root"] + m["prefix"]
    return await provider(
        msg, f"list:{cursor}", lambda: target.list(m["to_container"], under, cursor)
    )


async def _discover_keys(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    m: Mapping[str, Any],
    keys: list[str],
    msg: Message | None = None,
) -> int:
    """`_discover` for named target keys (a staging run's copies)."""
    target: StorageBackend = backend_for(m["to_backend_id"])

    async def heads() -> list[Any]:
        found = []
        for key in keys:
            try:
                ref = ObjectRef(m["to_backend_id"], m["to_container"], key)
                found.append(await target.head(ref))
            except ObjectNotFound:
                pass  # the run failed it; the prefix's next run copies the file
        return found

    cur = conn.cursor(row_factory=dict_row)
    new = 0
    for head in await provider(msg, "copied_heads", heads):
        new += await _record(cur, conn, m, head)
    return new


async def _record(cur: Any, conn: psycopg.AsyncConnection, m: Mapping[str, Any], head: Any) -> bool:
    """Record one copy as a pending secondary location of its S3 object's
    version, queued for verification, if it is a live file's current
    content and not recorded already."""
    root = m["target_root"]
    key = head.ref.key[len(root) :]
    await cur.execute(
        """
        SELECT l.location_id, l.version_id, l.file_id, l.size, l.committed_at
          FROM catalog.location l
          JOIN catalog.file f ON f.content_version_id = l.version_id
                             AND f.deleted_at IS NULL
         WHERE l.backend_id = %s AND l.container = %s AND l.object_key = %s
           AND l.state = 'committed'
         LIMIT 1
        """,
        (m["src_backend_id"], m["src_container"], key),
    )
    s3 = await cur.fetchone()
    if s3 is None or s3["size"] not in (None, head.size):
        return False  # not a live file's current object, or another version
    started = m["last_copy_started_at"]
    if (
        m["copy_by"] != "external"
        and started is not None
        and s3["committed_at"] is not None
        and s3["committed_at"] > started
    ):
        # Written in S3 after the copy started: the next run copies it. An
        # external copier runs on its own schedule, so for its copies the
        # checksum is the only guard.
        return False
    await cur.execute(
        "SELECT 1 FROM catalog.location WHERE version_id = %s AND backend_id = %s"
        "   AND container = %s AND object_key = %s AND state IN ('pending', 'committed')",
        (s3["version_id"], m["to_backend_id"], m["to_container"], head.ref.key),
    )
    if await cur.fetchone() is not None:
        return False
    await cur.execute(
        """
        INSERT INTO catalog.location
            (file_id, version_id, backend_id, container, object_key, backend_version_id,
             role, state, logical_tier, storage_class, size)
        VALUES (%s, %s, %s, %s, %s, %s, 'secondary', 'pending', %s, %s, %s)
        RETURNING location_id
        """,
        (s3["file_id"], s3["version_id"], m["to_backend_id"], m["to_container"],
         head.ref.key, head.ref.version_id, str(head.logical_tier), head.native_class,
         head.size),
    )  # fmt: skip
    row = await cur.fetchone()
    assert row is not None
    await send(conn, VERIFY_QUEUE, {"location_id": int(row["location_id"]),
                                    "migration_id": int(m["migration_id"])})  # fmt: skip
    return True


# --- counting and the journal ------------------------------------------------------

_FILES = """
SELECT f.file_id, f.content_version_id
  FROM catalog.location l
  JOIN catalog.file f ON f.content_version_id = l.version_id AND f.deleted_at IS NULL
 WHERE l.backend_id = %(sb)s AND l.container = %(sc)s
   AND l.object_key COLLATE "C" LIKE %(like)s
   AND l.state = 'committed'
   AND NOT EXISTS (SELECT 1 FROM catalog.migration_exclusion x
                    WHERE x.migration_id = %(mid)s AND x.object_key = l.object_key)
"""


async def _count(conn: psycopg.AsyncConnection, m: Mapping[str, Any]) -> tuple[int, int, int]:
    """(live files under the prefix, those with a verified copy of their
    current content, copies still waiting to be checked)."""
    cur = conn.cursor()
    await cur.execute(
        f"""
        WITH files AS ({_FILES})
        SELECT count(DISTINCT files.file_id),
               count(DISTINCT files.file_id) FILTER (WHERE EXISTS (
                   SELECT 1 FROM catalog.location t
                    WHERE t.version_id = files.content_version_id AND t.backend_id = %(tb)s
                      AND t.state = 'committed' AND t.verified)),
               (SELECT count(*) FROM catalog.location t
                 WHERE t.backend_id = %(tb)s AND t.container = %(tc)s
                   AND t.object_key COLLATE "C" LIKE %(tlike)s AND t.state = 'pending')
          FROM files
        """,
        {
            "mid": m["migration_id"],
            "sb": m["src_backend_id"],
            "sc": m["src_container"],
            "like": _like(m["prefix"]),
            "tb": m["to_backend_id"],
            "tc": m["to_container"],
            "tlike": _like(m["target_root"] + m["prefix"]),
        },  # fmt: skip
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


async def _journal(conn: psycopg.AsyncConnection, m: Mapping[str, Any]) -> datetime | None:
    """Every event of the bucket up to this time has been applied: when the
    feed was last read and caught up (None: never, or not caught up)."""
    cur = await conn.execute(
        "SELECT last_read_at, caught_up FROM catalog.changefeed_cursor"
        " WHERE backend_id = %s AND container = %s",
        (m["src_backend_id"], m["src_container"]),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    last_read, caught_up = row[0], row[1]
    return last_read if caught_up else None


# --- verifying one copy -------------------------------------------------------------


_COPY = """
SELECT t.*, m.migration_id, m.src_backend_id, m.src_container
  FROM catalog.location t
  JOIN LATERAL (SELECT mp.migration_id, s.backend_id AS src_backend_id,
                       s.container AS src_container
                  FROM catalog.migration_prefix mp JOIN catalog.source s USING (source_id)
                 WHERE mp.to_backend_id = t.backend_id AND mp.to_container = t.container
                   AND starts_with(t.object_key, mp.target_root || mp.prefix)
                 ORDER BY (mp.state = 'cancelled'), mp.migration_id DESC
                 LIMIT 1) m ON true
 WHERE t.location_id = %s
"""

_SOURCE = """
SELECT * FROM catalog.location WHERE version_id = %s AND backend_id = %s
   AND container = %s AND state = 'committed' ORDER BY location_id LIMIT 1
"""


async def verify_copy(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    location_id: int,
    now: datetime,
    msg: Message | None = None,
) -> str:
    """Read one copy back in Azure and compare its CRC64-NVME with the S3
    object's (decision 24). Returns 'verified', 'mismatch', 'no_checksum',
    'stale' (the S3 object changed under an unversioned key) or 'gone'.

    The read (possibly hundreds of GB) runs before any row is locked; the
    rows are then locked and checked again before anything is written. With `msg`,
    every provider call here runs `outside` the transaction, so a bad copy's delete
    holds no row: the blob's lock keeps a copy from being verified under it."""
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(_COPY, (location_id,))
    t = await cur.fetchone()
    if t is None or t["state"] != "pending":
        return "gone"
    await cur.execute(_SOURCE, (t["version_id"], t["src_backend_id"], t["src_container"]))
    s3 = await cur.fetchone()
    if s3 is None:
        return "gone"
    expected = s3["crc64nvme"]
    if expected is None:
        src: StorageBackend = backend_for(s3["backend_id"])
        ref = ObjectRef(s3["backend_id"], s3["container"], s3["object_key"],
                        s3["backend_version_id"])  # fmt: skip
        try:
            head = await provider(msg, "source_head", lambda: src.head(ref))
        except ObjectNotFound:
            return "gone"
        if ref.version_id is None and not _same_object(head, s3):
            # No version id to pin the read to, and the key now holds other
            # bytes: this checksum isn't the catalog version's. The copy is
            # dropped from the catalog (not deleted: it may be the new
            # bytes, which the next discovery records for their version).
            await cur.execute(
                "UPDATE catalog.location SET state = 'deleted',"
                " committed_at = coalesce(committed_at, %s)"
                " WHERE location_id = %s AND state = 'pending'",
                (now, location_id),
            )
            return "stale"
        expected = head.crc64nvme
        if expected is None:
            # The prefix's next step asks S3 Batch Operations to compute it
            # (migrate/checksums.py), then queues this copy again.
            return "no_checksum"
    target: StorageBackend = backend_for(t["backend_id"])
    tref = ObjectRef(t["backend_id"], t["container"], t["object_key"], t["backend_version_id"])
    size = int(s3["size"] if s3["size"] is not None else t["size"])
    bound = max(READ_SECONDS, size / READ_BYTES_PER_S)
    try:
        n, crc = await provider(
            msg, "read_back", lambda: read_crc(target, tref, size), seconds=bound
        )
    except ObjectNotFound:
        n, crc = -1, 0

    # Lock, and check that nothing moved during the read.
    await cur.execute(_COPY + " FOR UPDATE OF t", (location_id,))
    t = await cur.fetchone()
    if t is None or t["state"] != "pending":
        return "gone"
    await cur.execute(_SOURCE + " FOR UPDATE", (t["version_id"], t["src_backend_id"],
                                               t["src_container"]))  # fmt: skip
    locked = await cur.fetchone()
    if locked is None or locked["location_id"] != s3["location_id"]:
        return "gone"
    if locked["crc64nvme"] is not None:
        expected = locked["crc64nvme"]
    elif s3["crc64nvme"] is None:
        await cur.execute(
            "UPDATE catalog.location SET crc64nvme = %s WHERE location_id = %s",
            (expected, s3["location_id"]),
        )
    if n < 0:
        await _blob_not_being_removed(conn, t)
        await cur.execute(
            "UPDATE catalog.location SET state = 'deleted',"
            " committed_at = coalesce(committed_at, %s) WHERE location_id = %s",
            (now, location_id),
        )
        return "gone"
    if n == size and to_bytes(crc) == bytes(expected):
        await _blob_not_being_removed(conn, t)
        await _stamp(conn, target, tref, t, msg)
        await cur.execute(
            "UPDATE catalog.location SET state = 'committed', committed_at = %s,"
            " verified = true, verified_at = %s, verified_by = 'azure_read_crc',"
            " crc64nvme = %s WHERE location_id = %s",
            (now, now, bytes(expected), location_id),
        )
        # A seeded file has no CRC: every fill is checked against one, so
        # the edge could not read the copy without it (section 5).
        await cur.execute(
            "UPDATE catalog.file SET crc64nvme = %s"
            " WHERE content_version_id = %s AND crc64nvme IS NULL",
            (bytes(expected), t["version_id"]),
        )
        return "verified"
    # A bad copy: report it, and remove it so the next Additive run copies
    # the object again (Additive skips a target that exists).
    kind = "size_mismatch" if n != size else "checksum_mismatch"
    await record_drift(
        conn, source="migrate", backend_id=t["backend_id"], container=t["container"],
        key=t["object_key"], version=t["backend_version_id"], kind=kind,
        location_id=location_id, now=now,
        expected={"size": size, "crc64nvme": bytes(expected).hex()},
        observed={"size": n, "crc64nvme": to_bytes(crc).hex()},
    )  # fmt: skip
    await _hold_blob(conn, t, msg)
    if await _shared(cur, t):
        # With no version id, the key may now hold another version's
        # verified copy (a file's primary, even): never delete that one.
        log.warning("bad copy left in place: its key is in use", extra={"key": t["object_key"]})
    else:
        try:
            await provider(msg, "drop_bad_copy", lambda: target.delete(tref))
        except (ObjectNotFound, BackendError) as e:
            log.warning("could not remove a bad copy",
                        extra={"key": t["object_key"], "err": str(e)})  # fmt: skip
    await cur.execute(
        "UPDATE catalog.location SET state = 'deleted', committed_at = coalesce(committed_at, %s)"
        " WHERE location_id = %s",
        (now, location_id),
    )
    await conn.execute(
        "UPDATE catalog.migration_prefix SET files_failed = files_failed + 1, updated_at = %s"
        " WHERE migration_id = %s",
        (now, t["migration_id"]),
    )
    return "mismatch"


def _same_object(head: Any, s3: Mapping[str, Any]) -> bool:
    """Whether an unversioned key still holds the catalog's object: its
    ETag, when the catalog has one, and its size."""
    if s3["etag"] is not None and head.etag is not None:
        if head.etag.strip('"') != str(s3["etag"]).strip('"'):
            return False
    return s3["size"] is None or head.size == s3["size"]


def _blob_key(t: Mapping[str, Any]) -> str:
    return blob_lock_key(t["backend_id"], t["container"], t["object_key"])


async def _hold_blob(
    conn: psycopg.AsyncConnection, t: Mapping[str, Any], msg: Message | None
) -> None:
    """Hold the blob's lock while its copy is deleted, with no row locked. A session
    lock, since the delete is `outside` the transaction: the consumer lets it go when
    the message ends. With no consumer, this is a test or a CLI in its own transaction,
    where a lock for that transaction does the same."""
    fn = "pg_try_advisory_lock" if msg is not None and msg.consumed else "pg_try_advisory_xact_lock"
    cur = await conn.execute(f"SELECT {fn}(hashtext(%s))", (_blob_key(t),))
    row = await cur.fetchone()
    if not (row and row[0]):
        raise Defer(BLOB_BUSY_S)


async def _blob_not_being_removed(conn: psycopg.AsyncConnection, t: Mapping[str, Any]) -> None:
    """Another message is deleting what this key held: what this one read may be gone,
    so it waits. Then it finds the row changed, or reads the blob again."""
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock_shared(hashtext(%s))", (_blob_key(t),)
    )
    row = await cur.fetchone()
    if not (row and row[0]):
        raise Defer(BLOB_BUSY_S)


async def _shared(cur: Any, t: Mapping[str, Any]) -> bool:
    """Another committed location names the same unversioned target key."""
    if t["backend_version_id"] is not None:
        return False
    await cur.execute(
        "SELECT 1 FROM catalog.location WHERE backend_id = %s AND container = %s"
        "   AND object_key = %s AND location_id <> %s AND state = 'committed' LIMIT 1",
        (t["backend_id"], t["container"], t["object_key"], t["location_id"]),
    )
    return await cur.fetchone() is not None


async def _stamp(
    conn: psycopg.AsyncConnection,
    target: StorageBackend,
    ref: ObjectRef,
    t: Mapping[str, Any],
    msg: Message | None = None,
) -> None:
    """POSIX metadata on the copy, in the backend's spelling (design note
    section 6). The tier is the tiering worker's, from the file's dates."""
    cur = await conn.execute(
        "SELECT uid, gid, mode FROM catalog.file WHERE file_id = %s", (t["file_id"],)
    )
    row = await cur.fetchone()
    if row is None:
        return
    meta = posix_metadata(row[0] or 0, row[1] or 0, row[2] or 0o660)
    new = await provider(msg, "stamp", lambda: target.set_metadata(ref, meta))
    if new.version_id != ref.version_id:
        await conn.execute(
            "UPDATE catalog.location SET backend_version_id = %s WHERE location_id = %s",
            (new.version_id, t["location_id"]),
        )


# --- cutting over ---------------------------------------------------------------------


async def _unstamped(conn: psycopg.AsyncConnection, m: Mapping[str, Any]) -> int:
    """Verified copies of the prefix's files whose version is retained or
    held and whose blob doesn't carry that yet (`worm_sync_needed`)."""
    cur = await conn.execute(
        f"""
        WITH files AS ({_FILES})
        SELECT count(DISTINCT t.location_id)
          FROM files
          JOIN catalog.location t ON t.version_id = files.content_version_id
                                 AND t.backend_id = %(tb)s AND t.state = 'committed'
                                 AND t.verified AND t.worm_sync_needed
        """,
        {
            "mid": m["migration_id"],
            "sb": m["src_backend_id"],
            "sc": m["src_container"],
            "like": _like(m["prefix"]),
            "tb": m["to_backend_id"],
        },  # fmt: skip
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _flip(conn: psycopg.AsyncConnection, m: Mapping[str, Any]) -> int:
    """Make verified copies the primary location of up to FLIP_BATCH files;
    their S3 objects become secondary. Returns how many were flipped."""
    cur = await conn.execute(
        f"""
        WITH files AS ({_FILES}),
        pick AS (
            SELECT s.location_id AS s3_id, t.location_id AS copy_id
              FROM files
              JOIN catalog.location s ON s.version_id = files.content_version_id
                                     AND s.role = 'primary' AND s.state = 'committed'
                                     AND s.backend_id = %(sb)s
              JOIN catalog.location t ON t.version_id = files.content_version_id
                                     AND t.backend_id = %(tb)s AND t.state = 'committed'
                                     AND t.verified AND t.role = 'secondary'
             LIMIT %(n)s
             FOR UPDATE OF s, t
        ),
        demote AS (
            UPDATE catalog.location l SET role = 'secondary'
              FROM pick WHERE l.location_id = pick.s3_id
            RETURNING pick.copy_id
        )
        SELECT copy_id FROM demote
        """,
        {
            "mid": m["migration_id"],
            "sb": m["src_backend_id"],
            "sc": m["src_container"],
            "like": _like(m["prefix"]),
            "tb": m["to_backend_id"],
            "n": FLIP_BATCH,
        },  # fmt: skip
    )
    ids = [r[0] for r in await cur.fetchall()]
    if ids:
        # location_sync_file repoints each file at its new primary.
        await conn.execute(
            "UPDATE catalog.location SET role = 'primary' WHERE location_id = ANY(%s)", (ids,)
        )
    return len(ids)


async def _maybe_cut_over_dataset(conn: psycopg.AsyncConnection, dataset_id: int) -> None:
    """The dataset is cut over once no live file's bytes come from its
    source backend any more and all its sources are seeded."""
    await conn.execute(
        """
        UPDATE catalog.dataset d SET migration_state = 'cut_over'
         WHERE d.dataset_id = %(ds)s AND d.migration_state = 'migrating'
           AND NOT EXISTS (SELECT 1 FROM catalog.source s
                            WHERE s.dataset_id = d.dataset_id AND s.kind = 'source'
                              AND s.state <> 'seeded')
           AND NOT EXISTS (SELECT 1 FROM catalog.file f
                             JOIN catalog.location l ON l.location_id = f.primary_location_id
                            WHERE f.dataset_id = d.dataset_id AND f.deleted_at IS NULL
                              AND l.backend_id = d.source_backend_id)
        """,
        {"ds": dataset_id},
    )


__all__ = [
    "QUEUE",
    "VERIFY_QUEUE",
    "MigrationError",
    "Step",
    "migrate_step",
    "request_location",
    "verify_copy",
]
