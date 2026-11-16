"""Tiering work: the ``tiering`` consumer (moves) and the budgeted
``restores`` consumer (recalls).

**Moves** (``{"kind": "move", file_id, version_id, location_id, tier}``, from
`policy.scheduler`) go through `StorageBackend.set_class`:

- non-WORM (Azure two-account): hot/cool/cold are in-place Set Blob Tier on
  the live account; Archive is a server-side copy into the archive account,
  checked (length, Content-MD5 / CRC64) before it replaces the original,
  which is then marked ``deleting`` and deleted by the ``retention`` queue.
  See `handle_move` for the phases that make this crash-safe;
- WORM (one LRS/GRS account): every move is in place, Archive included
  (decision 31), so the location row is updated where it is.

A blob can have several references (`api.data.commit_dedupe`), and one blob can
be in one tier. **Decision 66:** before an in-place move, the other references are
asked what they want (`scheduler.wants_same_tier`, `engine.same_tier_wanted`):

- if they want the same tier, the blob moves, and every committed row of it says so;
- if not, the file that is moving gets its own blob, and only its row changes. The
  files that stay keep the original: its tier and its clock don't change, so a split
  never bills an early deletion. A read out of Cool or Cold bills one retrieval. See
  `_start_own_copy`, which also joins a copy already in the wanted tier.

The copy goes through the same phases as an archive copy: a server-side copy landing
in Hot, read back in full, then the wanted tier, then the record (`_commit_own_copy`).
A blob in Archive is never copied: that would be a rehydrate, which only a person's
recall starts.

**Only an archive date that has come may move a blob over a file that differs,** because a
date is always respected. A pin to Archive is a person's choice about their own file, so it
waits like any other move. If a file's copy can't be made, or a copy into Archive isn't done by
`OWN_COPY_DEADLINE` of reachable time, a move to Archive on its date falls back to moving the
blob for everyone, and says so to the other files' pin setters and to the alert.
Any other move changes nothing: it reports why and tries the copy again a day later
(`_wait_a_day`), since no date is at stake and a pin must stay true. So no disagreement can
loop and bill a fee: the only move over one is into Archive, which nothing leaves by policy.

Another move of a blob waits while a split of it runs, because the split decided on
what the blob was then and its copy is reading it. A move to Archive waits only up to
`ConsumerConfig.max_reads` looks, about 10 minutes, then takes its own copy from the
readable blob: an archive date is always respected. Any archive after its date is
audited and alerted on (`_archive_late`).

Nothing is moved in a migration source: an S3 class change is an in-place
CopyObject of the client's object (`catalog.source_readonly`). The
scheduler doesn't queue one, and a move queued before the bucket became a
source is refused and audited here, in every phase.

A copy-and-delete is never attempted on a retained or held version: the
handler checks first (the catalog's location_guard would refuse the delete
anyway, OF002, and the backend raises `Immutable`), and looks again when
retention ends. Smart is one-way: a move back to Smart is refused here too.

**Recalls**: before the M1 `restores` handler starts a High-priority
restore, `handle_restore_budgeted` applies the per-account 10 GiB/h budget
(`policy.recall.plan_recall`): admit, hold in the queue until there is room,
or downgrade to Standard when High would be no faster. It also makes sure
no recall above 10 GB carries an ETA.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

import psycopg
from psycopg.rows import dict_row

from .. import metrics
from ..backends.base import (
    BackendError,
    BackendKind,
    Immutable,
    LogicalTier,
    NotRestored,
    ObjectHead,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    Throttled,
    UnsupportedOperation,
)
from ..catalog.blob_lock import blob_lock_key
from ..catalog.source_readonly import location_on_source
from ..policy.engine import HELD_BACK, OWN_COPY_RETRY_AFTER
from ..policy.recall import PipelineEntry, account_of, no_promise, offers, plan_recall
from ..policy.scheduler import pin_in_force, wants_same_tier
from ..retention.worm_sync import stamp_worm
from .backends import BackendRegistry
from .queue import ConsumerConfig, Defer, Extend, Message, Permanent, Retry, outside
from .recall_timeout import WINDOW_GAP
from .recalls import RecallConfig, handle_restore

log = logging.getLogger(__name__)

QUEUE = "tiering"
ACTOR = "system:tiering"
LATER = timedelta(days=1)

RETENTION = "retention"
VERIFY = "verify"
#: How often a move waiting on the `verify` read-back looks again.
VERIFY_POLL_SECONDS = 30.0
#: The longest an ordinary provider call may run: Set Blob Tier takes moments.
MOVE_CALL_TIMEOUT = 240.0
#: The `tiering` consumer's visibility timeout: longer than that, so a live
#: holder's message isn't read again, and short, because a worker can die for
#: ordinary reasons (a deploy, a restart) and the sweep skips a file while its
#: message exists, a pin's recall included.
MOVE_VISIBILITY_SECONDS = 300
#: A copy can really run long, so its message asks for more time first
#: (`queue.Extend`), and only that file waits this long if its worker dies.
#: Azure's Copy Blob into the archive account has no time promise at any size, and
#: `_wait_copy` gives up after 6 hours. An S3 class change is a copy too, 368 part
#: copies for 184 GB, and one of this size or less is quick.
LONG_COPY_ABOVE = 1024**3
LONG_CALL_TIMEOUT = 6 * 3600.0 + 300
LONG_VISIBILITY_SECONDS = int(LONG_CALL_TIMEOUT) + 600
#: How soon a move looks again when another move of its blob is running.
BLOB_BUSY_SECONDS = 30.0
#: A move still blocked at `ConsumerConfig.max_reads` is handed back to the
#: scheduler, which looks at its file again this much later.
BLOB_BUSY_HANDED_BACK = timedelta(hours=1)
#: The message key that marks a split under way, naming the blob it is leaving.
OWN_COPY = "own_copy"
#: `tier.move.refused`'s reason when a file that differs couldn't get its own blob.
OWN_COPY_FAILED = "own_copy_failed"
#: How many times a copy that failed is tried before this try ends (`_give_up_own_copy`).
#: The next move of the file tries again.
OWN_COPY_TRIES = 3
OWN_COPY_RETRY_SECONDS = 60.0
#: An own copy into Archive ends by this long after it starts, in any phase, throttled or
#: stuck behind `verify` included, counting only time the service was reachable: then the
#: date wins, and the blob moves. Longer than three bounded copies (`LONG_CALL_TIMEOUT`).
#: A copy for any other move carries on past it, since no date is at stake, and says so.
OWN_COPY_DEADLINE = timedelta(hours=24)
#: `tier.move.waiting`'s reasons.
WAITING_BEHIND_SPLIT = "behind_another_files_copy"
WAITING_SLOW_COPY = "own_copy_slow"
#: The `notify` message kind for a pin a forced Archive overrode.
PIN_OVERRIDDEN = "pin_overridden"
#: An archive after the end of its date by more than this is late. A sweep a minute
#: past midnight isn't: `file.archive_at` is a date, the exact time can be 23:59.
ARCHIVE_LATE_GRACE = timedelta(hours=1)
#: A retained version's own blob must carry its retention and holds before it is the
#: record. Past this many looks this try ends, and the next one asks again.
STAMP_TRIES = 5
STAMP_RETRY_SECONDS = 60.0

MOVE_SQL = """
SELECT l.location_id, l.file_id, l.version_id, l.backend_id, l.container, l.object_key,
       l.backend_version_id, l.role, l.state, l.logical_tier, l.storage_class,
       l.explicit_tier_set, l.size, l.crc64nvme, l.md5, l.etag, l.verified, l.verified_at,
       l.verified_by, f.dataset_id, f.content_version_id, f.deleted_at, f.archive_at,
       v.state AS version_state, v.superseded_at, v.blake3, v.committed_at AS version_committed_at,
       v.legal_hold, v.retention_until, b.config AS backend_config, d.key_prefix
  FROM catalog.location l
  JOIN catalog.file f ON f.file_id = l.file_id
  JOIN catalog.file_version v ON v.version_id = l.version_id
  JOIN catalog.backend b ON b.backend_id = l.backend_id
  JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
 WHERE l.location_id = %s
"""

#: The committed rows of one blob. The match is the change feed's
#: (`changefeed_sync._locations`): this version of this object, so a later
#: version under the same name is another blob.
ON_BLOB = """
l.backend_id = %(b)s AND l.container = %(c)s AND l.object_key = %(k)s
   AND l.backend_version_id IS NOT DISTINCT FROM %(v)s
   AND l.state = 'committed'
"""

#: The blob's rows, lowest first, as the change feed, `delete_blob`, `_plan` and
#: `_commit_copy` take them: rows before files.
BLOB_ROWS_SQL = f"""
SELECT l.location_id, l.file_id, l.version_id, l.logical_tier, l.storage_class, f.dataset_id
  FROM catalog.location l
  JOIN catalog.file f ON f.file_id = l.file_id
 WHERE {ON_BLOB}
 ORDER BY l.location_id
   FOR UPDATE OF l
"""

#: Then their files, lowest first, before any update's trigger takes one. Two
#: movers of one blob take turns on the blob's lock. Two movers of different
#: blobs hold different rows and meet only on files, here, in one order, even
#: when two files share both blobs with their rows in opposite orders.
BLOB_FILES_SQL = f"""
SELECT file_id FROM catalog.file
 WHERE file_id IN (SELECT l.file_id FROM catalog.location l WHERE {ON_BLOB})
 ORDER BY file_id
   FOR UPDATE
"""

#: The files with a plan for this blob. A retained secondary, which is what a split
#: on a WORM share leaves behind, has none: it follows the blob.
REFERENCES_SQL = f"""
SELECT l.location_id, l.file_id, l.version_id, v.committed_at
  FROM catalog.location l
  JOIN catalog.file_version v ON v.version_id = l.version_id
 WHERE {ON_BLOB}
   AND l.role = 'primary' AND l.location_id <> %(me)s
 ORDER BY l.location_id
"""

#: Another file's split of this blob is under way: its message says so until it is
#: done, and it carries no row lock, because the copy can take hours.
OWN_COPY_RUNNING_SQL = """
SELECT 1 FROM pgmq.q_tiering WHERE message->>'own_copy' = %s AND msg_id <> %s LIMIT 1
"""

#: A copy an earlier attempt made for this version and left: a message can be lost to
#: the dead letters part-way. Not a copy that failed its read-back (a retained
#: version's stays until retention ends), and not a blob other rows use.
EARLIER_COPY_SQL = """
SELECT t.location_id
  FROM catalog.location t
 WHERE t.version_id = %(version)s AND t.role = 'secondary' AND t.state IN ('pending', 'committed')
   AND t.backend_id = %(b)s AND t.container = %(c)s AND starts_with(t.object_key, %(prefix)s)
   AND NOT EXISTS (SELECT 1 FROM catalog.audit_log a
                    WHERE a.file_id = t.file_id AND a.action = 'location.verify'
                      AND a.decision = 'deny' AND a.details->>'location_id' = t.location_id::text)
   AND NOT EXISTS (SELECT 1 FROM catalog.location o
                    WHERE o.backend_id = t.backend_id AND o.container = t.container
                      AND o.object_key = t.object_key AND o.location_id <> t.location_id
                      AND o.backend_version_id IS NOT DISTINCT FROM t.backend_version_id
                      AND o.state IN ('pending', 'committed'))
 ORDER BY t.location_id DESC
 LIMIT 1
"""

#: The same bytes already in the wanted tier, by `api.data.find_duplicate`'s test, in
#: the same container: a split joins it instead of copying.
SAME_BYTES_SQL = """
SELECT l.*
  FROM catalog.file f2
  JOIN catalog.location l ON l.version_id = f2.content_version_id
       AND l.role = 'primary' AND l.state = 'committed' AND l.verified
 WHERE f2.dataset_id = %(dataset)s AND f2.blake3 = %(blake3)s AND f2.size = %(size)s
   AND f2.deleted_at IS NULL
   AND l.backend_id = %(b)s AND l.container = %(c)s AND l.logical_tier = %(tier)s
   AND l.crc64nvme = %(crc)s AND l.object_key <> %(k)s
 ORDER BY l.location_id
 LIMIT 1
"""

#: Already reported today, for any reference of this blob.
BLOB_HOLD_REPORTED_SQL = f"""
SELECT 1
  FROM catalog.location l
  JOIN catalog.audit_log a
    ON a.file_id = l.file_id AND a.details->>'location_id' = l.location_id::text
 WHERE {ON_BLOB}
   AND a.action = 'tier.move.refused' AND a.details->>'why' = %(why)s
   AND (a.details->>'at')::timestamptz > %(since)s
 LIMIT 1
"""

#: An Owner's pin that held the file past its date, on the file or a folder above.
ARCHIVE_DATE_OVERRIDDEN_SQL = """
WITH RECURSIVE up (file_id, parent_id) AS (
    SELECT file_id, parent_id FROM catalog.file WHERE file_id = %(file)s
    UNION ALL
    SELECT a.file_id, a.parent_id FROM catalog.file a JOIN up ON a.file_id = up.parent_id
)
SELECT 1 FROM catalog.tier_pin p JOIN up ON up.file_id = p.file_id
 WHERE p.override_archive AND p.logical_tier <> 'archive'
   AND coalesce(p.released_at, p.until, 'infinity'::timestamptz) > %(date)s
 LIMIT 1
"""

TARGET_SQL = """
SELECT location_id, container, object_key, backend_version_id, role, state, size,
       crc64nvme, md5, verified, logical_tier, worm_sync_needed
  FROM catalog.location WHERE location_id = %s
"""


async def _wake(conn: psycopg.AsyncConnection, file_id: int, at: datetime) -> None:
    """Have the scheduler evaluate the file again at `at`."""
    await conn.execute(
        "UPDATE catalog.file SET next_action = coalesce(next_action, 'archive'),"
        " next_action_at = %s WHERE file_id = %s",
        (at, file_id),
    )


async def _audit(conn: psycopg.AsyncConnection, row: dict, action: str, details: dict) -> None:
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))
    await conn.execute(
        "SELECT catalog.audit(%s, %s, NULL, %s, %s, %s::jsonb)",
        (action, row["dataset_id"], row["file_id"], row["version_id"],
         psycopg.types.json.Jsonb(details)),
    )  # fmt: skip


async def _load(
    conn: psycopg.AsyncConnection, location_id: int, *, lock: bool = False
) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(MOVE_SQL + (" FOR UPDATE OF l, f" if lock else ""), (location_id,))
    return await cur.fetchone()


def _live(row: dict, version_id: int) -> bool:
    """The location is still the committed primary copy of `version_id`, and
    that version is the file's bytes now or a superseded, unpruned version."""
    if row["role"] != "primary" or row["state"] != "committed" or row["version_id"] != version_id:
        return False
    if row["version_state"] != "committed":
        return False
    current = row["content_version_id"] == version_id and row["deleted_at"] is None
    return current or row["superseded_at"] is not None or row["deleted_at"] is not None


def archive_container(config: dict | None, container: str) -> str | None:
    """Where a two-account archive move lands: the archive account's
    container of the same name (catalog.backend.config, as reconcile reads it)."""
    config = config or {}
    short = container.split("/", 1)[-1]
    if config.get("archive_container"):
        return str(config["archive_container"])
    if config.get("archive_account"):
        return f"{config['archive_account']}/{short}"
    if config.get("layout", "azure_two_account") == "azure_two_account":
        return f"archive/{short}"
    return None


async def handle_move(
    msg: Message, conn: psycopg.AsyncConnection, *, registry: BackendRegistry, now: datetime
) -> None:
    """One move, in phases so no catalog lock is held across a provider call
    and a crash anywhere leaves a state the next attempt finishes.

    In place (WORM, and hot/cool/cold): ``plan`` -> ``inplace`` (Set Blob
    Tier, then a guarded update of every row of that blob).

    Two-account archive (coordinator decision, M3 review): the copy is
    verified by an in-Azure CRC64 read-back before it becomes the record.

    1. ``plan`` (one transaction, no provider call): a ``pending`` secondary
       location in the archive account, landing in **Hot** (no minimum
       duration, so no early-deletion fee when it moves on);
    2. ``copy``: Copy Blob into the archive account as Hot (a copy already
       there with the right length is adopted), then the location is
       ``committed`` (unverified) and ``{"location_id"}`` goes on ``verify``,
       which reads the pinned version back and compares CRC64-NVME and length
       with the catalog;
    3. ``verifying``: wait for `verify`. Verified -> on; denied -> the copy is
       dropped, the source kept, audited, and the file looked at again later;
    4. ``archive``: Set Blob Tier -> Archive in place on the copy (the archive
       account is LRS/GRS);
    5. ``commit`` (one transaction): the copy becomes the primary, the
       original is marked ``deleting`` and its delete goes on ``retention``.

    A file's own copy (decision 66) goes ``inplace`` -> ``copy`` -> ``verifying`` ->
    ``archive`` (to the tier it wants) -> ``commit`` (`_commit_own_copy`), its message
    marked `OWN_COPY` throughout. Any trouble with it ends the try: a move to Archive goes
    back to ``inplace``, marked ``no_own_copy``, where it is for everyone. Any other waits.
    """
    body = msg.body
    if body.get("kind") != "move":
        raise Permanent(f"unknown tiering message kind {body.get('kind')!r}")
    try:
        location_id = int(body["location_id"])
        version_id = int(body["version_id"])
        target = LogicalTier(body["tier"])
        phase = str(body.get("phase", "plan"))
        target_location = int(body["target_location_id"]) if "target_location_id" in body else None
    except (KeyError, TypeError, ValueError) as e:
        raise Permanent(f"malformed tiering message {dict(body)!r}: {e}") from None
    for lid in (location_id, target_location):
        if lid is not None and await location_on_source(conn, lid):
            # Queued before the bucket became a source: a class change there
            # is an in-place copy of the client's object. Never; the file
            # waits for its cutover (catalog.source_readonly).
            row = await _load(conn, location_id)
            log.error("move: refused, location %s is in a migration source", lid)
            if row is not None:
                why = {"to": str(target), "why": "on_source"}
                await _audit(conn, row, "tier.move.refused", why)
            return
    if OWN_COPY in body and target_location is not None:
        await _own_copy_deadline(msg, conn, now, location_id, target_location, target)
    if phase == "plan":
        await _plan(msg, conn, registry, now, location_id, version_id, target)
    elif phase == "inplace":
        await _in_place(msg, conn, registry, now, location_id, version_id, target)
    elif target_location is None:
        raise Permanent(f"tiering phase {phase!r} needs target_location_id")
    elif phase == "copy":
        await _copy(msg, conn, registry, now, location_id, version_id, target_location, target)
    elif phase == "verifying":
        await _verifying(msg, conn, registry, now, location_id, target_location, target)
    elif phase == "archive":
        await _archive_copy(msg, conn, registry, now, location_id, target_location, target)
    elif phase == "commit" and OWN_COPY in msg.body:
        await _commit_own_copy(
            msg, conn, registry, now, location_id, version_id, target_location, target
        )
    elif phase == "commit":
        await _commit_copy(conn, registry, now, location_id, version_id, target_location)
    else:
        raise Permanent(f"unknown tiering phase {phase!r}")


def _next(msg: Message, delay: float = 0, **changes: object) -> Retry:
    """Commit this phase and run the next one (after `delay` seconds)."""
    body = {k: v for k, v in msg.body.items() if not k.startswith("_")}
    return Retry(delay, {**body, **changes})


async def _plan(
    msg: Message, conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime,
    location_id: int, version_id: int, target: LogicalTier,
) -> None:  # fmt: skip
    row = await _load(conn, location_id, lock=True)
    if row is None:
        log.info("move: location %s is gone", location_id)
        return
    file_id = row["file_id"]
    current = LogicalTier(row["logical_tier"] or "hot")
    if not _live(row, version_id):
        log.info("move %s@%s: no longer a live primary copy", file_id, version_id)
        await _wake(conn, file_id, now)
        return
    if current is target:
        await _wake(conn, file_id, now + LATER)  # a no-op: don't come straight back
        return
    backend = registry.get(row["backend_id"])
    caps = backend.capabilities()
    if target not in caps.tiers:
        log.warning("move %s: %s is not a tier of backend %s", file_id, target, row["backend_id"])
        await _audit(conn, row, "tier.move.refused", {"to": str(target), "why": "no_such_tier"})
        await _wake(conn, file_id, now + LATER)
        return
    if target is LogicalTier.SMART and (
        row["explicit_tier_set"] or current is not LogicalTier.SMART
    ):
        # Decision 29: a blob with an explicit tier never returns to Smart.
        log.warning("move %s: refused, an explicit tier can't return to Smart", file_id)
        await _audit(conn, row, "tier.move.refused", {"to": str(target), "why": "smart_one_way"})
        await _wake(conn, file_id, now + LATER)
        return
    copy_delete = target is LogicalTier.ARCHIVE and caps.archive_needs_separate_account
    if not copy_delete:
        raise _next(msg, phase="inplace")
    if _retained(row, now):
        # WORM data can't be copied and then deleted [W]; try when it's free.
        until = row["retention_until"] if not row["legal_hold"] else None
        log.warning("move %s: version %s is retained; no copy-and-delete", file_id, version_id)
        await _audit(conn, row, "tier.move.refused", {"to": str(target), "why": "retained"})
        await _wake(conn, file_id, max(until or now, now) + LATER)
        return
    if row["crc64nvme"] is None or row["size"] is None:
        # Nothing to verify the copy against, so the original must stay.
        await _audit(conn, row, "tier.move.refused", {"to": str(target), "why": "no_crc64"})
        await _wake(conn, file_id, now + LATER)
        return
    dst = archive_container(row["backend_config"], row["container"])
    if dst is None:
        raise Permanent(f"move {file_id}: backend {row['backend_id']} has no archive account")
    # The intent, reused if an earlier attempt made one.
    cur = await conn.execute(
        "SELECT location_id FROM catalog.location WHERE version_id = %s AND role = 'secondary'"
        " AND state IN ('pending', 'committed') AND container = %s AND object_key = %s",
        (version_id, dst, row["object_key"]),
    )
    got = await cur.fetchone()
    if got is None:
        hot = caps.tiers[LogicalTier.HOT][0].native_class
        cur = await conn.execute(
            """
            INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,
                role, state, logical_tier, storage_class, explicit_tier_set, size, crc64nvme,
                md5, etag)
            VALUES (%s, %s, %s, %s, %s, 'secondary', 'pending', 'hot', %s, true, %s, %s, %s, %s)
            RETURNING location_id
            """,
            (file_id, version_id, row["backend_id"], dst, row["object_key"], hot,
             row["size"], row["crc64nvme"], row["md5"], row["etag"]),
        )  # fmt: skip
        got = await cur.fetchone()
    assert got is not None
    raise _next(msg, phase="copy", target_location_id=str(got[0]))


def _on_blob(ref: ObjectRef) -> dict:
    return {"b": ref.backend_id, "c": ref.container, "k": ref.key, "v": ref.version_id}


def _retained(row: dict, now: datetime) -> bool:
    return bool(row["legal_hold"]) or (
        row["retention_until"] is not None and row["retention_until"] > now
    )


async def _references_differing(
    conn: psycopg.AsyncConnection, references: list[tuple], current: LogicalTier,
    target: LogicalTier, now: datetime, *, all_of_them: bool = False,
) -> list[int]:  # fmt: skip
    """The other rows on this blob that don't want it in `target` (decision 66): the first,
    or all of them. Read only: a provider call may follow."""
    differ = []
    for location, file_id, version_id, committed_at in references:
        if not await wants_same_tier(conn, file_id, version_id, committed_at, current, target, now):
            differ.append(location)
            if not all_of_them:
                break
    return differ


async def _start_own_copy(
    msg: Message, conn: psycopg.AsyncConnection, backend, row: dict, current: LogicalTier,
    target: LogicalTier, now: datetime,
) -> None:  # fmt: skip
    """This file wants another tier than the others on its blob, so it leaves the
    blob. The blob's lock is held, so no other split of it can start.

    **Which side copies:** the file that is moving, and the others keep the original.
    Leaving costs one read of the blob's tier: nothing out of Hot, a retrieval out of
    Cool or Cold. Staying costs nothing, because the blob doesn't change. So a split
    bills no early deletion while another file stays on the blob, whichever way the two
    wishes point. When nobody stays (a Cool blob, one file wanting Hot and the other
    Cold), whoever moves first copies, and the other then has the blob to itself and
    moves in place. If they all leave during a copy, the original's delete bills what
    moving it in place would have.

    Returns when it has nothing to copy. Otherwise raises, committing what it
    wrote: the copy runs in later phases of this message, or this try ends."""
    # A copy costs money and can take hours, and this message may be old: a pin may
    # have gone since it was queued. The scheduler decides afresh.
    if not await wants_same_tier(
        conn, row["file_id"], row["version_id"], row["version_committed_at"], current, target, now
    ):
        await _wake(conn, row["file_id"], now)
        return
    if row["crc64nvme"] is None or row["size"] is None:
        # Nothing to check a copy against, so it could never be the record.
        await _audit(conn, row, "tier.move.refused",
                     {"to": str(target), "why": OWN_COPY_FAILED, "detail": "no_crc64"})  # fmt: skip
        metrics.tier_own_copy(row["dataset_id"], row["file_id"], "failed")
        raise _fall_back(msg)
    # Not a retained version: nothing of it may be retired, and a blob whose retention
    # differs couldn't be stamped true for it.
    if not _retained(row, now) and await _join_same_bytes(conn, row, current, target, now):
        return
    own = blob_lock_key(row["backend_id"], row["container"], row["object_key"])
    prefix = f"{row['key_prefix'] or ''}{row['file_id']}/{row['version_id']}.own-"
    cur = await conn.execute(
        EARLIER_COPY_SQL,
        {"version": row["version_id"], "b": row["backend_id"], "c": row["container"],
         "prefix": prefix},
    )  # fmt: skip
    got = await cur.fetchone()
    if got is None:
        # A blob of its own, in the same container: the way this file's own upload
        # would be named, and never a name another version or a retry could be using.
        # It lands in Hot (no minimum duration, so no fee when it moves on).
        hot = backend.capabilities().tiers[LogicalTier.HOT][0].native_class
        cur = await conn.execute(
            """
            INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,
                role, state, logical_tier, storage_class, explicit_tier_set, tier_changed_at,
                size, crc64nvme, md5, etag)
            VALUES (%s, %s, %s, %s, %s, 'secondary', 'pending', 'hot', %s, true, %s, %s, %s, %s,
                    %s)
            RETURNING location_id
            """,
            (row["file_id"], row["version_id"], row["backend_id"], row["container"],
             f"{prefix}{now:%Y%m%dT%H%M%S%f}", hot, now, row["size"], row["crc64nvme"],
             row["md5"], row["etag"]),
        )  # fmt: skip
        got = await cur.fetchone()
    assert got is not None
    raise _next(msg, phase="copy", target_location_id=str(got[0]), own_copy=own,
                own_copy_since=now.isoformat())  # fmt: skip


async def _join_same_bytes(
    conn: psycopg.AsyncConnection, row: dict, current: LogicalTier, target: LogicalTier,
    now: datetime,
) -> bool:  # fmt: skip
    """The same bytes are already in the wanted tier, because another file that wants
    what this one wants left the blob first: this file becomes one more reference
    there, and nothing is copied or billed. So there is one blob per wish, not per file."""
    if row["blake3"] is None:
        return False
    cur = conn.cursor(row_factory=dict_row)
    params = {"dataset": row["dataset_id"], "blake3": row["blake3"], "size": row["size"],
              "b": row["backend_id"], "c": row["container"], "tier": str(target),
              "crc": row["crc64nvme"], "k": row["object_key"]}  # fmt: skip
    await cur.execute(SAME_BYTES_SQL, params)
    found = await cur.fetchone()
    if found is None:
        return False
    # As a dedupe does: that blob's lock shared, and the row read again under it. A
    # move of that blob under way means this file copies instead.
    got = await conn.execute(
        "SELECT pg_try_advisory_xact_lock_shared(hashtext(%s))",
        (blob_lock_key(found["backend_id"], found["container"], found["object_key"]),),
    )
    (mine,) = await got.fetchone()  # type: ignore[misc]
    if not mine:
        return False
    await cur.execute(SAME_BYTES_SQL, params)
    found = await cur.fetchone()
    if found is None:
        return False
    await _load(conn, row["location_id"], lock=True)  # rows, then files
    await conn.execute(
        "UPDATE catalog.location SET state = 'deleting', verified = false WHERE location_id = %s",
        (row["location_id"],),
    )
    cur = await conn.execute(
        """
        INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,
            backend_version_id, role, state, logical_tier, storage_class, tier_changed_at,
            explicit_tier_set, size, crc64nvme, md5, etag, verified, verified_at, verified_by,
            committed_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s, %s, %s, %s, %s,
                true, %s, %s, %s)
        RETURNING location_id
        """,
        (row["file_id"], row["version_id"], found["backend_id"], found["container"],
         found["object_key"], found["backend_version_id"], found["logical_tier"],
         found["storage_class"], found["tier_changed_at"], found["explicit_tier_set"],
         found["size"], found["crc64nvme"], found["md5"], found["etag"], found["verified_at"],
         found["verified_by"], now),
    )  # fmt: skip
    (joined,) = await cur.fetchone()  # type: ignore[misc]
    await conn.execute(
        "SELECT pgmq.send(%s::text, %s::jsonb)",
        (RETENTION, psycopg.types.json.Jsonb(
            {"op": "delete_blob", "location_id": str(row["location_id"])})),
    )  # fmt: skip
    await _audit(
        conn, row, "tier.move",
        {"from": str(current), "to": str(target), "how": "own_copy", "joined": True,
         "target_location_id": joined},
    )  # fmt: skip
    metrics.tier_own_copy(row["dataset_id"], row["file_id"], "joined")
    if target is LogicalTier.ARCHIVE:
        await _archive_late(conn, row, now)
    await _wake(conn, row["file_id"], now)
    log.info("move %s@%s: to %s by joining a copy already there", row["file_id"],
             row["version_id"], target)  # fmt: skip
    return True


async def _reachable_since(
    conn: psycopg.AsyncConnection, since: datetime, now: datetime
) -> timedelta | None:
    """How long of `since` until now the service has been reachable without a break: the
    same window a recall's silence counts in (migration 0040, `recall_timeout`). An outage
    starts it again from nothing. None while heartbeats aren't arriving at all."""
    cur = await conn.execute("SELECT started_at, last_at FROM catalog.heartbeat_window")
    got = await cur.fetchone()
    if got is None or now - got[1] > WINDOW_GAP:
        return None
    return now - max(since, got[0])


async def _own_copy_deadline(
    msg: Message, conn: psycopg.AsyncConnection, now: datetime, location_id: int,
    target_location: int, target: LogicalTier,
) -> None:  # fmt: skip
    """Past `OWN_COPY_DEADLINE` of reachable time, a move to Archive falls back. Any other
    move carries on and says so once. Not when only the record is left: the copy is checked
    and in its tier, and dropping it could bill an early deletion, for nothing."""
    since = msg.body.get("own_copy_since")
    if not since:
        return
    took = await _reachable_since(conn, datetime.fromisoformat(since), now)
    if took is None or took <= OWN_COPY_DEADLINE:
        return
    row = await _load(conn, location_id)
    tgt = await _target(conn, target_location)
    if row is None or tgt is None or tgt["role"] == "primary":
        return
    if tgt["verified"] and tgt["logical_tier"] == str(target) and not tgt["worm_sync_needed"]:
        return
    if target is not LogicalTier.ARCHIVE:
        await _report_waiting(conn, row, target, WAITING_SLOW_COPY, now)
        return
    log.warning("move %s to %s: its own copy passed its deadline", row["file_id"], target)
    await _give_up_own_copy(msg, conn, row, target_location, target, now, "deadline")


async def _give_up_own_copy(
    msg: Message, conn: psycopg.AsyncConnection, row: dict, target_location: int,
    target: LogicalTier, now: datetime, detail: str,
) -> None:  # fmt: skip
    """This try at a copy is over, and its row with it. Never returns. The move runs
    again without a copy, where `_in_place` decides: a move to Archive moves the blob for
    everyone, because its date is always respected, and any other waits a day."""
    await _drop_copy(conn, row, target_location, now, OWN_COPY_FAILED, to=target, detail=detail)
    metrics.tier_own_copy(row["dataset_id"], row["file_id"], "failed")
    raise _fall_back(msg)


async def _wait_a_day(
    conn: psycopg.AsyncConnection, row: dict, target: LogicalTier, now: datetime
) -> None:
    """A file that differs and has no copy of its own stays where it is, and tries again
    in `OWN_COPY_RETRY_AFTER`. Each try reports again, which keeps the alert firing until
    the copy can be made (`policy.overdue`, `tier-held-back`). The scheduler stays away
    until then, and at that time decides afresh, so a pin released meanwhile moves nothing."""
    until = now + OWN_COPY_RETRY_AFTER
    ref = ObjectRef(row["backend_id"], row["container"], row["object_key"],
                    row["backend_version_id"])  # fmt: skip
    await _report_hold(conn, row, ref, target, until, now)
    await _wake(conn, row["file_id"], _hold_wake(row, until, now))


def _fall_back(msg: Message) -> Retry:
    """Commit what this attempt wrote, then run the move again without a copy, in
    `_in_place`, which decides what that means. Its own attempt, because a provider call
    may follow and no id may be held across one."""
    drop = {OWN_COPY, "own_copy_since", "target_location_id", "copy_tries", "stamp_tries"}
    body = {k: v for k, v in msg.body.items() if not k.startswith("_") and k not in drop}
    return Retry(0, {**body, "phase": "inplace", "no_own_copy": True})


async def _report_hold(
    conn: psycopg.AsyncConnection, row: dict, ref: ObjectRef, target: LogicalTier,
    until: datetime, now: datetime,
) -> None:  # fmt: skip
    """One audit row and one alert a day per blob, under this file's own dataset
    and naming no other file. The time compared is the worker's `now`, written in
    the row."""
    cur = await conn.execute(
        BLOB_HOLD_REPORTED_SQL,
        {**_on_blob(ref), "why": HELD_BACK, "since": now - timedelta(days=1)},
    )
    if await cur.fetchone() is not None:
        return
    details = {"to": str(target), "why": HELD_BACK, "until": until.isoformat(),
               "at": now.isoformat(), "location_id": str(row["location_id"])}  # fmt: skip
    await _audit(conn, row, "tier.move.refused", details)
    metrics.tier_held_back(row["dataset_id"], row["file_id"])
    log.warning(
        "move %s to %s waits until %s: the blob has other references, and it couldn't get "
        "its own copy", row["file_id"], target, until.isoformat(),
    )  # fmt: skip


async def _report_waiting(
    conn: psycopg.AsyncConnection, row: dict, target: LogicalTier, why: str, now: datetime
) -> None:
    """One audit line for a move that is waiting, not one per look: at most one a day for
    one reason, on the file's own row, naming no other file."""
    cur = await conn.execute(
        "SELECT 1 FROM catalog.audit_log WHERE file_id = %s AND action = 'tier.move.waiting'"
        " AND details->>'why' = %s AND (details->>'at')::timestamptz > %s LIMIT 1",
        (row["file_id"], why, now - timedelta(days=1)),
    )
    if await cur.fetchone() is None:
        await _audit(conn, row, "tier.move.waiting",
                     {"to": str(target), "why": why, "at": now.isoformat()})  # fmt: skip


def _hold_wake(row: dict, until: datetime, now: datetime) -> datetime:
    """When to evaluate a waiting file again: when the wait ends, or on its archive
    date if that comes first, because a move to Archive doesn't wait."""
    if row["archive_at"] is not None:
        archive = datetime.combine(row["archive_at"], time(), tzinfo=UTC)
        if now < archive < until:
            return archive
    return until


async def _archive_late(conn: psycopg.AsyncConnection, row: dict, now: datetime) -> None:
    """An archive date is always respected, so one that wasn't is said out loud, whatever
    the cause: an audit row and an alert. Not when an Owner's pin held the file past it,
    and not for an old version, since `file.archive_at` is about the current one."""
    if (
        row["archive_at"] is None
        or row["version_id"] != row["content_version_id"]
        or row["deleted_at"] is not None
    ):
        return
    date = datetime.combine(row["archive_at"], time(), tzinfo=UTC)
    end = date + timedelta(days=1)
    if now <= end + ARCHIVE_LATE_GRACE:
        return
    cur = await conn.execute(ARCHIVE_DATE_OVERRIDDEN_SQL, {"file": row["file_id"], "date": date})
    if await cur.fetchone() is not None:
        return
    late_by = int((now - end).total_seconds() // 3600)
    await _audit(
        conn, row, "tier.archive.late",
        {"archive_at": row["archive_at"].isoformat(), "at": now.isoformat(),
         "late_by_hours": late_by, "to": "archive"},
    )  # fmt: skip
    metrics.tier_archive_late(row["dataset_id"], row["file_id"])
    log.warning("file %s archived %s h after its date %s", row["file_id"], late_by,
                row["archive_at"].isoformat())  # fmt: skip


async def _blob_busy(
    msg: Message, conn: psycopg.AsyncConnection, row: dict, target: LogicalTier, now: datetime
) -> None:
    """Another move of this blob is running: look again soon. Past the bound it is
    handed back, which isn't a failure, so not a dead letter: the holder is alive, and
    the move is derived from the file's plan, so nothing is lost."""
    if msg.read_ct < ConsumerConfig.max_reads:
        raise Defer(BLOB_BUSY_SECONDS)
    log.warning("move %s to %s: the blob stayed busy; handed back", row["file_id"], target)
    await _wake(conn, row["file_id"], now + BLOB_BUSY_HANDED_BACK)


async def _in_place(
    msg: Message, conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime,
    location_id: int, version_id: int, target: LogicalTier,
) -> None:  # fmt: skip
    row = await _load(conn, location_id)  # no row locks across the provider call
    if row is None or not _live(row, version_id):
        return
    # Movers of one blob take turns, so the order they reach the provider is
    # the order they commit and the rows end on the blob's real tier. A busy
    # blob defers this one: an S3 class change can take many minutes, and the
    # consumer's batch must not wait behind it.
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
        (blob_lock_key(row["backend_id"], row["container"], row["object_key"]),),
    )
    (mine,) = await cur.fetchone()  # type: ignore[misc]
    if not mine:
        await _blob_busy(msg, conn, row, target, now)
        return
    row = await _load(conn, location_id)  # what the last holder left
    if row is None or not _live(row, version_id):
        return
    backend = registry.get(row["backend_id"])
    ref = ObjectRef(row["backend_id"], row["container"], row["object_key"],
                    row["backend_version_id"])  # fmt: skip
    current = LogicalTier(row["logical_tier"] or "hot")
    if current is target:
        # Another reference's move got there first; the tier clock stays its.
        await _wake(conn, row["file_id"], now)
        return
    if current is LogicalTier.ARCHIVE:
        # Queued before another reference archived the blob. Leaving Archive
        # is a rehydrate, never a policy step (`engine.evaluate`).
        log.info("move %s to %s: refused, the blob is archived", row["file_id"], target)
        await _audit(conn, row, "tier.move.refused", {"to": str(target), "why": "archived"})
        await _wake(conn, row["file_id"], now + LATER)
        return
    cur = await conn.execute(REFERENCES_SQL, {**_on_blob(ref), "me": location_id})
    references = await cur.fetchall()
    if references:
        # Another file's split of this blob is under way, and it decided on what the
        # blob was then. Wait for it: its copy is reading this blob. Its deadline bounds
        # that. A move to Archive has a date that is always respected, so it waits only
        # a short bound. Then it goes on below: to its own copy from the readable blob,
        # beside the other, or, if it has already failed to make one, in place.
        running = await conn.execute(
            OWN_COPY_RUNNING_SQL,
            (blob_lock_key(row["backend_id"], row["container"], row["object_key"]), msg.msg_id),
        )
        if await running.fetchone() is not None and not (
            target is LogicalTier.ARCHIVE
            and (msg.body.get("no_own_copy") or msg.read_ct >= ConsumerConfig.max_reads)
        ):
            if target is not LogicalTier.ARCHIVE:
                await _report_waiting(conn, row, target, WAITING_BEHIND_SPLIT, now)
            await _blob_busy(msg, conn, row, target, now)
            return
    # S3's class change makes a new version, so it is a copy already.
    forced: list[dict] = []
    if backend.kind is not BackendKind.S3 and await _references_differing(
        conn, references, current, target, now
    ):
        if not msg.body.get("no_own_copy"):
            await _start_own_copy(msg, conn, backend, row, current, target, now)
            return
        if target is not LogicalTier.ARCHIVE or not await _archive_date_arrived(conn, row, now):
            await _wait_a_day(conn, row, target, now)
            return
        forced = await _forced_references(conn, references, current, target, now)
    # An S3 class change is a copy; Set Blob Tier is not.
    long_call = backend.kind is BackendKind.S3 and (row["size"] or 0) > LONG_COPY_ABOVE
    if long_call and not msg.extended:
        raise Extend(LONG_VISIBILITY_SECONDS)
    # Nothing so far has written or locked a row: this transaction has no id, so
    # it can't hold back the edge feed's horizon however long the provider takes.
    # (An advisory lock takes no id.) Keep it that way up to here.
    try:
        # A call that never returns would hold the blob's lock for ever. Past
        # this it is a failure like any other: backed off, then dead-lettered.
        new_ref = await asyncio.wait_for(
            backend.set_class(ref, target), LONG_CALL_TIMEOUT if long_call else MOVE_CALL_TIMEOUT
        )
    except Immutable as e:
        log.warning("move %s: immutable: %s", row["file_id"], e)
        await _wake(conn, row["file_id"], now + LATER)
        return
    except UnsupportedOperation as e:
        log.warning("move %s to %s refused by the backend: %s", row["file_id"], target, e)
        await _audit(conn, row, "tier.move.refused", {"to": str(target), "why": str(e)})
        await _wake(conn, row["file_id"], now + LATER)
        return
    except Throttled as e:
        raise Retry((e.retry_after or timedelta(seconds=30)).total_seconds()) from None
    except NotRestored as e:
        raise Retry(3600) from e
    except ObjectNotFound as e:
        raise Permanent(f"move {row['file_id']}: the cloud copy is missing: {e}") from None
    if (new_ref.container, new_ref.key) != (ref.container, ref.key):
        raise Permanent(f"move {row['file_id']}: an in-place tier change moved the blob")
    native = backend.capabilities().tiers[target][0].native_class
    blob = conn.cursor(row_factory=dict_row)
    await blob.execute(BLOB_ROWS_SQL, _on_blob(ref))
    others = [r for r in await blob.fetchall() if r["location_id"] != location_id]
    await conn.execute(BLOB_FILES_SQL, _on_blob(ref))
    cur = await conn.execute(
        """
        UPDATE catalog.location
           SET logical_tier = %s, storage_class = %s, tier_changed_at = %s,
               explicit_tier_set = explicit_tier_set OR %s,
               backend_version_id = coalesce(%s, backend_version_id)
         WHERE location_id = %s AND state = 'committed' AND role = 'primary'
        """,
        (str(target), native, now, target is not LogicalTier.SMART, new_ref.version_id,
         location_id),
    )  # fmt: skip
    if cur.rowcount:
        await _audit(
            conn, row, "tier.move", {"from": str(current), "to": str(target), "how": "in_place"}
        )
    # S3's class change is a CopyObject: it mints a version, and the one the
    # other rows name keeps its class, so their rows are already true.
    minted = ref.version_id is not None and new_ref.version_id not in (None, ref.version_id)
    if not minted:
        await _other_references(conn, others, target, native, now, forced)
    if target is LogicalTier.ARCHIVE:
        await _archive_late(conn, row, now)
    await _wake(conn, row["file_id"], now)
    log.info("move %s@%s: %s -> %s (in place)", row["file_id"], version_id, current, target)


async def _archive_date_arrived(conn: psycopg.AsyncConnection, row: dict, now: datetime) -> bool:
    """Whether this move is an archive date's, which is always respected, and so may move a
    blob over a file that differs. A person's pin to Archive is a choice about their own
    file, so it waits like any other move. A version that is no longer the file's current
    one (overwritten, restored over, or its file deleted) has its own date, from its commit,
    as `engine.old_version_wants_same_tier` reads it: a message queued for it before then,
    by a pin, is no date's."""
    if row["content_version_id"] == row["version_id"] and row["deleted_at"] is None:
        return row["archive_at"] is not None and row["archive_at"] <= now.astimezone(UTC).date()
    committed = row["version_committed_at"]
    if committed is None:
        return False
    cur = await conn.execute(
        "SELECT (body->>'archive_day')::int FROM catalog.policy"
        " WHERE dataset_id = %s AND state = 'active' AND body ? 'archive_day'",
        (row["dataset_id"],),
    )
    got = await cur.fetchone()
    return got is not None and committed + timedelta(days=got[0]) <= now


async def _forced_references(
    conn: psycopg.AsyncConnection,
    references: list[tuple],
    current: LogicalTier,
    target: LogicalTier,
    now: datetime,
) -> list[dict]:
    """Who a forced Archive overrides: each row that doesn't want it, and who set its pin."""
    differ = set(await _references_differing(conn, references, current, target, now,
                                             all_of_them=True))  # fmt: skip
    out = []
    for location, file_id, version_id, _committed in references:
        if location in differ:
            pin = await pin_in_force(conn, file_id, version_id, now)
            out.append({"location_id": location, "file_id": file_id, "pin": pin})
    return out


async def _tell_pin_setter(conn: psycopg.AsyncConnection, forced: dict, now: datetime) -> None:
    """A Teams card, or email, to whoever set the pin that wasn't kept (`notify`)."""
    pin = forced["pin"]
    if not pin:
        return  # a forced row never wanted Archive, so a pin here is one it overrode
    await conn.execute(
        "SELECT pgmq.send('notify', %s::jsonb)",
        (psycopg.types.json.Jsonb({
            "kind": PIN_OVERRIDDEN, "file_id": str(forced["file_id"]),
            "identity_id": str(pin["set_by_identity_id"]), "tier": pin["logical_tier"],
            "at": now.isoformat(),
        }),),
    )  # fmt: skip


async def _other_references(
    conn: psycopg.AsyncConnection, others: list[dict], target: LogicalTier, native: str,
    now: datetime, forced: list[dict] | None = None,
) -> None:  # fmt: skip
    """The blob moved, so these rows say so, each with its own audit under its
    own dataset (naming no other file). The triggers refresh each file's tier
    and tell its edge. Its tier clock moves too, so an older change-feed record
    can't undo this. They are not woken: a file's own schedule and pins come
    round when they do, and waking it would turn one disagreement between two
    files' plans into a loop."""
    stale = [r for r in others if (r["logical_tier"], r["storage_class"]) != (str(target), native)]
    if not stale:
        return
    await conn.execute(
        """
        UPDATE catalog.location
           SET logical_tier = %s, storage_class = %s, tier_changed_at = %s,
               explicit_tier_set = explicit_tier_set OR %s
         WHERE location_id = ANY(%s)
        """,
        (str(target), native, now, target is not LogicalTier.SMART,
         [r["location_id"] for r in stale]),
    )  # fmt: skip
    by_location = {f["location_id"]: f for f in forced or []}
    for r in stale:
        details = {"from": str(r["logical_tier"] or "hot"), "to": str(target),
                   "how": "shared_blob"}  # fmt: skip
        overridden = by_location.get(r["location_id"])
        if overridden is not None:
            # Offline before its own date, and maybe against a pin. Why, and that it's
            # known. It names no other file.
            details["forced"] = "another_files_archive_date"
            details["why"] = "neither_file_could_get_its_own_copy"
            metrics.tier_forced_archive(r["dataset_id"], r["file_id"])
            await _tell_pin_setter(conn, overridden, now)
            log.warning("file %s forced to Archive by another file's date", r["file_id"])
        await _audit(conn, r, "tier.move", details)


async def _target(conn: psycopg.AsyncConnection, target_location: int, *, lock: bool = False):
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(TARGET_SQL + (" FOR UPDATE" if lock else ""), (target_location,))
    return await cur.fetchone()


async def _send_verify(conn: psycopg.AsyncConnection, target_location: int) -> None:
    await conn.execute(
        "SELECT pgmq.send(%s::text, %s::jsonb)",
        (VERIFY, psycopg.types.json.Jsonb({"location_id": str(target_location)})),
    )


async def _copy(
    msg: Message, conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime,
    location_id: int, version_id: int, target_location: int, target: LogicalTier,
) -> None:  # fmt: skip
    row = await _load(conn, location_id)  # no catalog locks across provider calls
    tgt = await _target(conn, target_location)
    if row is None or tgt is None:
        return
    if tgt["state"] == "committed":
        raise _next(msg, VERIFY_POLL_SECONDS, phase="verifying")  # copied before a crash
    if tgt["state"] != "pending":
        return
    backend = registry.get(row["backend_id"])
    src = ObjectRef(row["backend_id"], row["container"], row["object_key"],
                    row["backend_version_id"])  # fmt: skip
    dst = ObjectRef(row["backend_id"], tgt["container"], tgt["object_key"])
    if OWN_COPY in msg.body and not msg.called:
        # The others may have left while this waited its turn. Then nothing needs copying.
        cur = await conn.execute(REFERENCES_SQL, {**_on_blob(src), "me": location_id})
        if not await cur.fetchall():
            await _drop_copy(conn, row, target_location, now, "nobody_left", to=target, quiet=True)
            raise _fall_back(msg)

    async def existing() -> ObjectHead | None:
        """A copy an earlier attempt left, of the right length. Its bytes are
        checked by the read-back like any other."""
        try:
            head = await backend.head(dst)
        except ObjectNotFound:
            return None
        return head if head.size == row["size"] else None

    head = await outside(msg, "copy_head", existing)
    if head is None or head.logical_tier is not LogicalTier.HOT:
        if not msg.extended:
            raise Extend(LONG_VISIBILITY_SECONDS)

        async def copied() -> ObjectHead | None:
            await asyncio.wait_for(
                backend.copy_object(src, tgt["container"], tgt["object_key"], LogicalTier.HOT),
                LONG_CALL_TIMEOUT,
            )
            return await existing()

        try:
            # Nothing is open during the copy, which can run for hours.
            head = await outside(msg, "copy", copied)
        except ObjectNotFound:
            # The source went first (a crash under an older code path, or
            # someone else). A copy of the right length is all there is.
            if head is None or OWN_COPY in msg.body:
                raise Permanent(
                    f"move {row['file_id']}@{version_id}: the source is gone and no archive "
                    f"copy exists at {dst.container}/{dst.key}"
                ) from None
            # Already in Archive: it can't be read back; adopt it unverified
            # (the edge keeps its bytes) rather than lose the only copy.
            await conn.execute(
                "UPDATE catalog.location SET backend_version_id = %s, logical_tier = 'archive'"
                " WHERE location_id = %s AND state = 'pending'",
                (head.ref.version_id, target_location),
            )
            raise _next(msg, phase="commit", adopted="unverified") from None
        except Throttled as e:
            raise Retry((e.retry_after or timedelta(seconds=30)).total_seconds()) from None
        except NotRestored:
            if OWN_COPY not in msg.body:
                raise
            # The catalog is behind: the blob is in Archive. A copy now would be a
            # rehydrate, which only a person's recall starts. The feed puts the row
            # right, and then this file's pin asks for a recall.
            await _drop_copy(conn, row, target_location, now, "source_archived", to=target)
            return
        except (BackendError, TimeoutError) as e:
            if OWN_COPY not in msg.body:
                raise
            tries = int(msg.body.get("copy_tries", 0)) + 1
            log.warning("move %s: its own copy failed (%s of %s): %s", row["file_id"], tries,
                        OWN_COPY_TRIES, e)  # fmt: skip
            if tries < OWN_COPY_TRIES:
                raise _next(msg, OWN_COPY_RETRY_SECONDS, copy_tries=tries) from None
            await _give_up_own_copy(msg, conn, row, target_location, target, now, type(e).__name__)
    if head is None:
        if OWN_COPY in msg.body:
            await _give_up_own_copy(
                msg, conn, row, target_location, target, now, "copy_length_mismatch"
            )
        await _drop_copy(conn, row, target_location, now, "copy_length_mismatch")
        return
    await conn.execute(
        "UPDATE catalog.location SET backend_version_id = %s, etag = coalesce(%s, etag),"
        " state = 'committed', committed_at = %s, tier_changed_at = %s"
        " WHERE location_id = %s AND state = 'pending'",
        (head.ref.version_id, head.etag, now, now, target_location),
    )
    await _send_verify(conn, target_location)
    raise _next(msg, VERIFY_POLL_SECONDS, phase="verifying")


async def _verify_denied(conn: psycopg.AsyncConnection, file_id: int, target_location: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM catalog.audit_log WHERE file_id = %s AND action = 'location.verify'"
        " AND decision = 'deny' AND details->>'location_id' = %s LIMIT 1",
        (file_id, str(target_location)),
    )
    return await cur.fetchone() is not None


async def _verifying(
    msg: Message, conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime,
    location_id: int, target_location: int, target: LogicalTier,
) -> None:  # fmt: skip
    row = await _load(conn, location_id)
    tgt = await _target(conn, target_location)
    if row is None or tgt is None or tgt["state"] != "committed":
        return
    if tgt["verified"]:
        raise _next(msg, phase="archive")
    if await _verify_denied(conn, row["file_id"], target_location):
        src = ObjectRef(row["backend_id"], row["container"], row["object_key"],
                        row["backend_version_id"])  # fmt: skip
        try:
            await registry.get(row["backend_id"]).head(src)
        except ObjectNotFound:
            # Never drop the only copy, even a bad one: a person decides.
            raise Permanent(
                f"move {row['file_id']}: the archive copy failed its read-back and the "
                "original is gone"
            ) from None
        if OWN_COPY in msg.body:
            await _give_up_own_copy(msg, conn, row, target_location, target, now, "verify_failed")
        await _drop_copy(conn, row, target_location, now, "verify_failed")
        return
    # Not read back yet. If its verify message was lost, ask again. One that
    # dead-lettered isn't lost: `redrive.py` sends it again, and in the end denies it,
    # which this phase then sees.
    cur = await conn.execute(
        "SELECT 1 FROM pgmq.q_verify WHERE message->>'location_id' = %(l)s"
        " UNION ALL"
        " SELECT 1 FROM pgmq.q_verify_dlq WHERE message->'message'->>'location_id' = %(l)s"
        " LIMIT 1",
        {"l": str(target_location)},
    )
    if await cur.fetchone() is None:
        await _send_verify(conn, target_location)
    raise _next(msg, VERIFY_POLL_SECONDS)


async def _archive_copy(
    msg: Message, conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime,
    location_id: int, target_location: int, target: LogicalTier,
) -> None:  # fmt: skip
    """The checked copy, in Hot, goes to its tier: Archive for an archive copy, and
    whatever the file wanted for its own copy. It leaves Hot, so no fee."""
    tgt = await _target(conn, target_location)
    if tgt is None or tgt["state"] != "committed" or not tgt["verified"]:
        return
    if tgt["logical_tier"] != str(target):
        cur = await conn.execute(
            "SELECT backend_id FROM catalog.location WHERE location_id = %s", (target_location,)
        )
        (backend_id,) = await cur.fetchone()  # type: ignore[misc]
        backend = registry.get(backend_id)
        ref = ObjectRef(backend_id, tgt["container"], tgt["object_key"], tgt["backend_version_id"])
        try:
            # In place: the archive account is LRS/GRS, so Archive is allowed.
            new_ref = await outside(
                msg,
                "archive",
                lambda: asyncio.wait_for(backend.set_class(ref, target), MOVE_CALL_TIMEOUT),
            )
        except Throttled as e:
            raise Retry((e.retry_after or timedelta(seconds=30)).total_seconds()) from None
        except (BackendError, TimeoutError) as e:
            row = await _load(conn, location_id)
            if OWN_COPY not in msg.body or row is None:
                raise
            await _give_up_own_copy(msg, conn, row, target_location, target, now, type(e).__name__)
        native = backend.capabilities().tiers[target][0].native_class
        await conn.execute(
            "UPDATE catalog.location SET logical_tier = %s, storage_class = %s,"
            " tier_changed_at = %s, backend_version_id = coalesce(%s, backend_version_id)"
            " WHERE location_id = %s",
            (str(target), native, now, new_ref.version_id, target_location),
        )
    raise _next(msg, phase="commit")


async def _drop_copy(
    conn: psycopg.AsyncConnection, row: dict, target_location: int, now: datetime, why: str,
    *, to: LogicalTier = LogicalTier.ARCHIVE, detail: str | None = None, quiet: bool = False,
) -> None:  # fmt: skip
    """A copy that failed its check: delete it, keep the original, try later. A copy of
    a retained version stays where it is, as an unused secondary, and goes with the
    version when its retention ends: nothing of a retained version is deleted, and
    the catalog's guard would refuse it (OF002). Only the primary is ever read."""
    details = {"to": str(to), "why": why}
    if detail is not None:
        details["detail"] = detail
    if _retained(row, now):
        if not quiet:
            await _audit(conn, row, "tier.move.refused", details)
        await _wake(conn, row["file_id"], now + LATER)
        log.warning("move %s: its copy of a retained version kept unused (%s)", row["file_id"], why)
        return
    await conn.execute(
        "UPDATE catalog.location SET state = 'deleting', verified = false,"
        " committed_at = coalesce(committed_at, %s) WHERE location_id = %s"
        " AND state IN ('pending', 'committed')",
        (now, target_location),
    )
    await conn.execute(
        "SELECT pgmq.send(%s::text, %s::jsonb)",
        (RETENTION, psycopg.types.json.Jsonb(
            {"op": "delete_blob", "location_id": str(target_location)})),
    )  # fmt: skip
    if not quiet:
        await _audit(conn, row, "tier.move.refused", details)
    await _wake(conn, row["file_id"], now + LATER)
    log.warning("move %s: copy dropped (%s); the original is kept", row["file_id"], why)


async def _commit_own_copy(
    msg: Message, conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime,
    location_id: int, version_id: int, target_location: int, target: LogicalTier,
) -> None:  # fmt: skip
    """The file's own blob, checked and in its tier, becomes the record, and only this
    file's row changes. The other files keep the original: its delete goes on
    `retention` as any original's does, and `delete_blob` keeps the bytes while another
    row uses them.

    A retained or held version keeps its old copy too: nothing of it is deleted before
    retention ends, so the row becomes a secondary (the swap a migration's cutover
    makes, 0027), after the new blob carries the version's retention and holds."""
    row = await _load(conn, location_id)
    tgt = await _target(conn, target_location)
    if row is None or tgt is None or tgt["role"] == "primary":
        return  # already done (a replay)
    if not _live(row, version_id) or tgt["state"] != "committed":
        if tgt["state"] in ("committed", "pending"):
            await _drop_copy(conn, row, target_location, now, "superseded_during_copy", to=target)
        return
    if not tgt["verified"] or tgt["logical_tier"] != str(target):
        return  # not through the read-back and the tier change: never the record
    if tgt["worm_sync_needed"]:
        try:
            await outside(
                msg,
                "stamp",
                lambda: asyncio.wait_for(
                    stamp_worm(conn, registry, now, only=[target_location]), MOVE_CALL_TIMEOUT
                ),
            )
        except TimeoutError:
            log.warning("move %s: stamping its own copy timed out", row["file_id"])
        tgt = await _target(conn, target_location)
        assert tgt is not None
        if tgt["worm_sync_needed"]:
            tries = int(msg.body.get("stamp_tries", 0)) + 1
            if tries >= STAMP_TRIES:
                await _give_up_own_copy(
                    msg, conn, row, target_location, target, now, "no_retention"
                )
            raise _next(msg, STAMP_RETRY_SECONDS, stamp_tries=tries)
    # A dedupe in flight holds this shared, and a mover holds it across its call.
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
        (blob_lock_key(row["backend_id"], row["container"], row["object_key"]),),
    )
    (mine,) = await cur.fetchone()  # type: ignore[misc]
    if not mine:
        raise _next(msg, BLOB_BUSY_SECONDS)
    # Rows before files, lowest first, as the change feed takes them.
    await conn.execute(
        "SELECT 1 FROM catalog.location WHERE location_id = ANY(%s) ORDER BY location_id"
        " FOR UPDATE",
        ([location_id, target_location],),
    )
    row = await _load(conn, location_id, lock=True)
    tgt = await _target(conn, target_location)
    if row is None or tgt is None or not _live(row, version_id) or tgt["state"] != "committed":
        raise _next(msg)  # moved while this waited: the checks above decide
    if tgt["worm_sync_needed"]:
        raise _next(msg)  # a hold set meanwhile: stamp it first
    current = str(row["logical_tier"] or "hot")
    if _retained(row, now):
        await conn.execute(
            "UPDATE catalog.location SET role = 'secondary' WHERE location_id = %s", (location_id,)
        )
    else:
        await conn.execute(
            "UPDATE catalog.location SET state = 'deleting', verified = false"
            " WHERE location_id = %s",
            (location_id,),
        )
        await conn.execute(
            "SELECT pgmq.send(%s::text, %s::jsonb)",
            (RETENTION, psycopg.types.json.Jsonb(
                {"op": "delete_blob", "location_id": str(location_id)})),
        )  # fmt: skip
    await conn.execute(
        "UPDATE catalog.location SET role = 'primary' WHERE location_id = %s", (target_location,)
    )
    await _audit(
        conn, row, "tier.move",
        {"from": current, "to": str(target), "how": "own_copy", "verified": True,
         "target_location_id": target_location},
    )  # fmt: skip
    metrics.tier_own_copy(row["dataset_id"], row["file_id"], "copied")
    if target is LogicalTier.ARCHIVE:
        await _archive_late(conn, row, now)
    await _wake(conn, row["file_id"], now)
    log.info("move %s@%s: to %s on a blob of its own", row["file_id"], version_id, target)


async def _commit_copy(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime, location_id: int,
    version_id: int, target_location: int,
) -> None:  # fmt: skip
    row = await _load(conn, location_id, lock=True)
    tgt = await _target(conn, target_location, lock=True)
    if row is None or tgt is None:
        return
    file_id = row["file_id"]
    if tgt["role"] == "primary":
        return  # already done (a replay)
    adopted = tgt["state"] == "pending"  # the source vanished; the copy was already Archive
    if not _live(row, version_id) or tgt["state"] not in ("committed", "pending"):
        if tgt["state"] in ("committed", "pending"):
            await _drop_copy(conn, row, target_location, now, "superseded_during_copy")
        return
    if not adopted and (not tgt["verified"] or tgt["logical_tier"] != "archive"):
        return  # not through the read-back and the tier change: never the record
    # Old primary out first (one primary per version), then the copy in.
    await conn.execute(
        "UPDATE catalog.location SET state = 'deleting', verified = false WHERE location_id = %s",
        (location_id,),
    )
    await conn.execute(
        """
        UPDATE catalog.location
           SET role = 'primary', state = 'committed', committed_at = coalesce(committed_at, %s),
               logical_tier = 'archive', storage_class = 'Archive'
         WHERE location_id = %s
        """,
        (now, target_location),
    )
    await conn.execute(
        "SELECT pgmq.send(%s::text, %s::jsonb)",
        (RETENTION, psycopg.types.json.Jsonb(
            {"op": "delete_blob", "location_id": str(location_id)})),
    )  # fmt: skip
    await _audit(
        conn, row, "tier.move",
        {"from": str(row["logical_tier"] or "hot"), "to": "archive", "how": "copy_verify_delete",
         "verified": not adopted, "target_location_id": target_location},
    )  # fmt: skip
    await _archive_late(conn, row, now)
    await _wake(conn, file_id, now)
    log.info("move %s@%s: archived by verified copy", file_id, version_id)


# ---------------------------------------------------------------------------
# Recalls: the per-account High budget in front of the M1 restores handler
# ---------------------------------------------------------------------------

RECALL_SQL = """
SELECT r.recall_id, r.state, r.priority, r.restore_id, r.requested_at, r.eta,
       v.size, l.backend_id, l.container
  FROM catalog.recall r
  JOIN catalog.file_version v ON v.version_id = r.version_id
  LEFT JOIN catalog.location l
         ON l.version_id = r.version_id AND l.role = 'primary' AND l.state = 'committed'
 WHERE r.recall_id = %s
"""

#: One statement, under the account's advisory lock: the admitted High
#: restores (the pipeline) and the High recalls queued ahead of this one.
BUDGET_SQL = """
SELECT 'admitted' AS kind, s.requested_at AS at, coalesce(s.bytes, l.size, 0) AS bytes
  FROM catalog.restore s
  JOIN catalog.location l ON l.location_id = s.location_id
 WHERE s.backend_id = %(backend)s AND s.speed = 'high'
   AND s.state IN ('requested', 'in_progress')
   AND split_part(l.container, '/', 1) = %(account)s
UNION ALL
SELECT 'ahead', NULL, coalesce(sum(v.size), 0)
  FROM catalog.recall r
  JOIN catalog.file_version v ON v.version_id = r.version_id
  JOIN catalog.location l
    ON l.version_id = r.version_id AND l.role = 'primary' AND l.state = 'committed'
 WHERE r.priority = 'high' AND r.state = 'requested' AND r.restore_id IS NULL
   AND l.backend_id = %(backend)s AND split_part(l.container, '/', 1) = %(account)s
   AND (r.requested_at, r.recall_id) < (%(at)s, %(id)s)
"""


def budget_lock_key(backend_id: int, account: str) -> str:
    return f"omnifile:high-budget:{backend_id}/{account}"


async def handle_restore_budgeted(
    msg: Message,
    conn: psycopg.AsyncConnection,
    *,
    registry: BackendRegistry,
    now: datetime,
    config: RecallConfig | None = None,
) -> None:
    try:
        recall_id = int(msg.body["recall_id"])
    except (KeyError, TypeError, ValueError):
        recall_id = None
    row = None
    if recall_id is not None:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(RECALL_SQL, (recall_id,))
        row = await cur.fetchone()
    caps = None
    admitted = False
    if row is not None and row["backend_id"] is not None:
        caps = registry.get(row["backend_id"]).capabilities()
        if (
            row["state"] == "requested"
            and row["restore_id"] is None
            and row["priority"] == "high"
            and offers(caps, RestoreSpeed.HIGH)
        ):
            admitted = await _admit_high(conn, row, caps, now, config or RecallConfig())
    try:
        # An admitted High recall keeps the budget's lock until its restore is on
        # record, so its call stays in this transaction, which holds no id. Others
        # can let everything go first.
        await handle_restore(msg, conn, registry=registry, now=now, config=config,
                             in_place=admitted)  # fmt: skip
    except Retry:
        # Retry commits this transaction's writes: clear a promise first.
        await _no_promise_above_limit(conn, row, caps)
        raise
    # (Any other exception aborts the transaction; nothing to add to it.)
    await _no_promise_above_limit(conn, row, caps)


async def _no_promise_above_limit(conn: psycopg.AsyncConnection, row: dict | None, caps) -> None:
    """Never a promised time above 10 GB, whatever the provider reported."""
    if row is None or caps is None or row["size"] is None:
        return
    size = row["size"]
    if no_promise(caps, RestoreSpeed.STANDARD, size) or no_promise(caps, RestoreSpeed.HIGH, size):
        await conn.execute(
            "UPDATE catalog.recall SET eta = NULL WHERE recall_id = %s AND eta IS NOT NULL",
            (row["recall_id"],),
        )


async def _admit_high(
    conn: psycopg.AsyncConnection, row: dict, caps, now: datetime, config: RecallConfig
) -> bool:
    """True: this High recall goes ahead now, under the budget's lock."""
    account = account_of(row["container"])
    # Serialise admission per (backend, account) across workers. The lock is
    # transaction-scoped, so it is held through handle_restore's restore
    # insert and released at commit, when the next worker can see it.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))", (budget_lock_key(row["backend_id"], account),)
    )
    cur = await conn.execute(
        BUDGET_SQL,
        {"backend": row["backend_id"], "account": account, "at": row["requested_at"],
         "id": row["recall_id"]},
    )  # fmt: skip
    backlog: list[PipelineEntry] = []
    ahead = 0
    for kind, at, size in await cur.fetchall():
        if kind == "admitted":
            backlog.append(PipelineEntry(at, int(size)))
        else:
            ahead = int(size)
    plan = plan_recall(
        caps, int(row["size"] or 0), "high", now, high_backlog=backlog, waiting_ahead_bytes=ahead
    )
    if plan.downgraded:
        log.info("recall %s: High downgraded to Standard (%s)", row["recall_id"], plan.reason)
        await conn.execute(
            "UPDATE catalog.recall SET priority = 'standard' WHERE recall_id = %s",
            (row["recall_id"],),
        )
        return False
    if plan.queued:
        await conn.execute(
            "UPDATE catalog.recall SET eta = %s WHERE recall_id = %s AND eta IS DISTINCT FROM %s",
            (plan.eta, row["recall_id"], plan.eta),
        )
        wait = plan.submit_at - now
        if config.hold_recheck_max is not None:
            wait = min(wait, config.hold_recheck_max)
        raise Retry(max(wait.total_seconds(), 0.0))
    return True
