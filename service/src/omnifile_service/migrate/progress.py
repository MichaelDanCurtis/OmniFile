"""The console's rolled-up progress (migration 0037; portal-api.md 5.22).

Reading the console never scans a migration's objects: the worker's
`progress_sweep` (every 5 minutes) counts each migration under way once,
stores the totals on its row, and keeps a sample of its verified bytes, so
the console's throughput is the difference between two samples an hour
apart. A migration that ended is counted one last time, in the state it ended
in (`counted_state`), and left. The counts live in their own table, so a count
never changes the migration's row or its version.

It also counts copies whose check won't end by itself, so `web` reads a number and never a
queue. A count takes no transaction id until its writes, so however long it reads, it doesn't
hold back the edge feed. `STUCK` reads both queues whole, so it runs when there is a reason to
look, and a count is kept until the next one:
- the prefix made no progress since its last count, so a stuck copy shows within a sweep or two
  of a prefix stalling;
- `STUCK_EVERY` has passed: a source that keeps making progress must not hide one for ever;
- an ArchiveAdmin excluded an object since the last look, so the number falls by the next sweep.

`STUCK` also keeps which objects these are, up to `KEEP_KEYS` per kind, in
`catalog.migration_stuck_copy`: the list the Exclude dialog takes from (portal-api.md 5.22).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("omnifile_service.migrate")

EXPIRY_WARNING = timedelta(hours=24)
ARCHIVE_CLASSES = ["GLACIER", "DEEP_ARCHIVE"]
KEEP_SAMPLES = timedelta(days=2)
PER_SWEEP = 200
#: How long a prefix making progress goes without a look. A sweep short of the hour, so that
#: with a sweep every 5 minutes it looks at least hourly.
STUCK_EVERY = timedelta(minutes=55)
#: The objects listed per kind, the first by key. Excluding them lets the next ones show.
KEEP_KEYS = 10_000

#: One migration's bytes, progress, current failures, archived and expiring objects.
COUNT = """
SELECT count(*) AS files, coalesce(sum(l.size), 0) AS bytes,
       count(t.location_id) AS files_verified, coalesce(sum(t.size), 0) AS bytes_verified,
       count(*) FILTER (WHERE t.location_id IS NULL AND EXISTS (
           SELECT 1 FROM catalog.location d WHERE d.version_id = l.version_id
              AND d.backend_id = %(tb)s AND d.state = 'deleted')) AS failing_now,
       count(*) FILTER (WHERE t.location_id IS NULL AND l.storage_class = ANY(%(archive)s)
                          AND NOT EXISTS (SELECT 1 FROM catalog.restore r
                                           WHERE r.location_id = l.location_id
                                             AND r.state = 'completed'
                                             AND (r.expires_at IS NULL OR r.expires_at > %(now)s)))
           AS archived_waiting,
       count(*) FILTER (WHERE t.location_id IS NULL AND EXISTS (
           SELECT 1 FROM catalog.restore r WHERE r.location_id = l.location_id
              AND r.state = 'completed' AND r.expires_at > %(now)s AND r.expires_at <= %(soon)s))
           AS expiring
  FROM catalog.location l
  JOIN catalog.file f ON f.content_version_id = l.version_id AND f.deleted_at IS NULL
  LEFT JOIN LATERAL (SELECT c.location_id, c.size FROM catalog.location c
                      WHERE c.version_id = l.version_id AND c.backend_id = %(tb)s
                        AND c.state = 'committed' AND c.verified LIMIT 1) t ON true
 WHERE l.backend_id = %(sb)s AND l.container = %(sc)s AND l.state = 'committed'
   AND l.object_key COLLATE "C" LIKE %(like)s
"""


#: A pending copy that holds up the cutover: its file's current content, with an S3 object to
#: check it against, and not excluded. Any other pending copy holds nothing up (`_count`).
_HOLDS_UP = """
  JOIN catalog.file f ON f.content_version_id = t.version_id AND f.deleted_at IS NULL
  JOIN catalog.location s ON s.version_id = t.version_id AND s.backend_id = %(sb)s
                         AND s.container = %(sc)s AND s.state = 'committed'
 WHERE t.backend_id = %(tb)s AND t.container = %(tc)s AND t.state = 'pending'
   AND NOT EXISTS (SELECT 1 FROM catalog.migration_exclusion x
                    WHERE x.migration_id = %(mid)s AND x.object_key = s.object_key)
"""

#: A location id from a queue's message, or NULL: a letter can be malformed.
_ID = """CASE WHEN {v} ~ '^[0-9]{{1,18}}$' THEN ({v})::bigint END"""
_QUEUED = _ID.format(v="q.message->>'location_id'")
_LETTER = _ID.format(v="q.message->'message'->>'location_id'")

#: Copies with a dead letter. Starts from the letters, which are few: it runs each sweep.
GIVEN_UP = f"""
SELECT count(*) AS n FROM (SELECT DISTINCT {_LETTER} AS id FROM pgmq.q_migrate_verify_dlq q) q
  JOIN catalog.location t ON t.location_id = q.id
{_HOLDS_UP}
   AND t.object_key COLLATE "C" >= %(tp)s AND t.object_key COLLATE "C" < %(tp)s || chr(1114111)
"""

#: Copies nothing will check: no message, no letter.
#: - The S3 object's checksum is known: stranded. Nothing is on its way.
#: - Not known: `plan_checksums` asks a job, if the backend runs Batch, within a minute, and the
#:   job queues the check again. So this waits, unless the job for it has been and gone with no
#:   value (SSE-C, or its attempts ran out) or there is no Batch to ask. A job running for the
#:   prefix may be a week yet, and later objects wait for it.
#: The keys come from the same read as the counts, so the list and the number agree.
STUCK = f"""
WITH queued AS (SELECT DISTINCT {_QUEUED} AS id FROM pgmq.q_migrate_verify q),
     lettered AS (SELECT DISTINCT {_LETTER} AS id FROM pgmq.q_migrate_verify_dlq q),
     copies AS (
SELECT t.location_id, s.location_id AS source_id, s.object_key, s.crc64nvme IS NULL AS no_crc
  FROM catalog.location t
{_HOLDS_UP}
   AND t.object_key COLLATE "C" >= %(tp)s AND t.object_key COLLATE "C" < %(tp)s || chr(1114111)),
     stuck AS MATERIALIZED (
SELECT c.object_key, NOT c.no_crc AS stranded,
       c.no_crc AND NOT %(job_running)s AND (NOT %(batch)s OR EXISTS (
           SELECT 1 FROM catalog.checksum_job_object o
             JOIN catalog.checksum_job j USING (checksum_job_id)
             JOIN catalog.migration_prefix p ON p.migration_id = j.migration_id
            WHERE o.location_id = c.source_id
              AND NOT (j.state = 'running' AND p.state = 'cancelled'))) AS no_checksum
  FROM copies c
  LEFT JOIN queued ON queued.id = c.location_id
  LEFT JOIN lettered ON lettered.id = c.location_id
 WHERE queued.id IS NULL AND lettered.id IS NULL)
SELECT count(*) FILTER (WHERE stranded) AS stranded,
       count(*) FILTER (WHERE no_checksum) AS no_checksum,
       ARRAY(SELECT DISTINCT object_key COLLATE "C" AS k FROM stuck WHERE stranded
              ORDER BY k LIMIT %(keep)s) AS stranded_keys,
       ARRAY(SELECT DISTINCT object_key COLLATE "C" AS k FROM stuck WHERE no_checksum
              ORDER BY k LIMIT %(keep)s) AS no_checksum_keys
  FROM stuck
"""

#: Whether a source backend runs Batch Operations: its binding says, not the catalog.
HasBatch = Callable[[int], bool]

#: Only these states wait on a copy's check.
CHECKING = ("copying", "verifying")

#: What a sweep needs of a migration.
TO_COUNT = """
SELECT m.migration_id, m.state, m.prefix, m.to_backend_id, m.to_container, m.target_root,
       s.backend_id AS src_backend_id, s.container AS src_container,
       c.copies_stranded, c.copies_no_checksum, c.stuck_counted_at,
       EXISTS (SELECT 1 FROM catalog.migration_exclusion x
                WHERE x.migration_id = m.migration_id AND x.excluded_at > c.stuck_counted_at)
           AS excluded_since,
       (SELECT p.files_verified FROM catalog.migration_progress p
         WHERE p.migration_id = m.migration_id ORDER BY p.at DESC LIMIT 1) AS last_files_verified,
       EXISTS (SELECT 1 FROM catalog.checksum_job j
                WHERE j.migration_id = m.migration_id AND j.state = 'running') AS job_running
  FROM catalog.migration_prefix m JOIN catalog.source s USING (source_id)
  LEFT JOIN catalog.migration_count c USING (migration_id)
"""


@dataclass(frozen=True)
class Checks:
    given_up: int
    stranded: int
    no_checksum: int
    #: When `STUCK` last looked.
    looked_at: datetime | None
    #: The objects by kind, when this count looked or the prefix stopped waiting on a check.
    keys: dict[str, list[str]] | None


def _time_to_look(m: dict, files_verified: int, now: datetime) -> bool:
    """Whether `STUCK` should read the queues for this prefix now. A clock set back is a reason
    to look too: otherwise it would be no reason for as long as it was set back."""
    last = m["stuck_counted_at"]
    return (
        last is None
        or m["last_files_verified"] == files_verified
        or m["excluded_since"]
        or not timedelta(0) <= now - last < STUCK_EVERY
    )


async def _checks(
    cur: psycopg.AsyncCursor, m: dict, files_verified: int, now: datetime, has_batch: HasBatch
) -> Checks:
    """The copies of this prefix whose check won't end by itself."""
    if m["state"] not in CHECKING:
        return Checks(0, 0, 0, None, {"stranded": [], "no_checksum": []})
    p = {"mid": m["migration_id"], "sb": m["src_backend_id"], "sc": m["src_container"],
         "tb": m["to_backend_id"], "tc": m["to_container"],
         "tp": m["target_root"] + m["prefix"], "batch": has_batch(m["src_backend_id"]),
         "job_running": m["job_running"], "keep": KEEP_KEYS}  # fmt: skip
    await cur.execute(GIVEN_UP, p)
    row = await cur.fetchone()
    assert row is not None
    given_up = int(row["n"])
    if not _time_to_look(m, files_verified, now):
        return Checks(given_up, int(m["copies_stranded"] or 0), int(m["copies_no_checksum"] or 0),
                      m["stuck_counted_at"], None)  # fmt: skip
    await cur.execute(STUCK, p)
    row = await cur.fetchone()
    assert row is not None
    keys = {"stranded": row["stranded_keys"], "no_checksum": row["no_checksum_keys"]}
    return Checks(given_up, int(row["stranded"]), int(row["no_checksum"]), now, keys)


async def _keep_keys(cur: psycopg.AsyncCursor, mid: int, keys: dict[str, list[str]]) -> None:
    """Make the prefix's list these keys, writing only what changed: a stalled prefix is looked
    at every sweep, and its list rarely changes. Two replicas can sweep at once, each with its
    own keys, so one replaces the list at a time. The other leaves it, rather than wait with an
    id held, and the next look puts it right."""
    await cur.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok",
        (f"omnifile:migration-stuck-list:{mid}",),
    )
    row = await cur.fetchone()
    assert row is not None
    if not row["ok"]:
        return
    for kind, wanted in keys.items():
        await cur.execute(
            "SELECT object_key FROM catalog.migration_stuck_copy"
            " WHERE migration_id = %s AND kind = %s",
            (mid, kind),
        )
        listed = {r["object_key"] for r in await cur.fetchall()}
        if gone := list(listed.difference(wanted)):
            await cur.execute(
                "DELETE FROM catalog.migration_stuck_copy"
                " WHERE migration_id = %s AND kind = %s AND object_key = ANY(%s)",
                (mid, kind, gone),
            )
        if new := list(set(wanted).difference(listed)):
            await cur.execute(
                "INSERT INTO catalog.migration_stuck_copy (migration_id, kind, object_key)"
                " SELECT %s, %s, k FROM unnest(%s::text[]) AS k ON CONFLICT DO NOTHING",
                (mid, kind, new),
            )


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def count_one(
    conn: psycopg.AsyncConnection, m: dict, now: datetime, has_batch: HasBatch
) -> None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        COUNT,
        {
            "tb": m["to_backend_id"],
            "sb": m["src_backend_id"],
            "sc": m["src_container"],
            "like": _like(m["prefix"]),
            "now": now,
            "soon": now + EXPIRY_WARNING,
            "archive": ARCHIVE_CLASSES,
        },  # fmt: skip
    )
    b = await cur.fetchone()
    assert b is not None
    checks = await _checks(cur, m, int(b["files_verified"]), now, has_batch)
    # Its own table: a count never touches the migration's row, so it neither
    # bumps its version nor looks like a change worth counting again.
    await conn.execute(
        "INSERT INTO catalog.migration_count (migration_id, counted_state, bytes_total,"
        " bytes_verified, failing_now, archived_waiting, restores_expiring, counted_at,"
        " copies_given_up, copies_stranded, copies_no_checksum, stuck_counted_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (migration_id) DO UPDATE SET"
        " counted_state = EXCLUDED.counted_state, bytes_total = EXCLUDED.bytes_total,"
        " bytes_verified = EXCLUDED.bytes_verified, failing_now = EXCLUDED.failing_now,"
        " archived_waiting = EXCLUDED.archived_waiting,"
        " restores_expiring = EXCLUDED.restores_expiring, counted_at = EXCLUDED.counted_at,"
        " copies_given_up = EXCLUDED.copies_given_up,"
        " copies_stranded = EXCLUDED.copies_stranded,"
        " copies_no_checksum = EXCLUDED.copies_no_checksum,"
        " stuck_counted_at = EXCLUDED.stuck_counted_at",
        (m["migration_id"], m["state"], int(b["bytes"]), int(b["bytes_verified"]),
         int(b["failing_now"]), int(b["archived_waiting"]), int(b["expiring"]), now,
         checks.given_up, checks.stranded, checks.no_checksum, checks.looked_at),
    )  # fmt: skip
    await conn.execute(
        "INSERT INTO catalog.migration_progress (migration_id, at, bytes_verified,"
        " files_verified) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (m["migration_id"], now, int(b["bytes_verified"]), int(b["files_verified"])),
    )
    if checks.keys is not None:
        await _keep_keys(cur, m["migration_id"], checks.keys)


async def progress_sweep(
    conn: psycopg.AsyncConnection, now: datetime, has_batch: HasBatch = lambda _: False
) -> int:
    """Count the migrations under way, and the ones that ended since their last count. `conn`
    is in autocommit: each migration is its own transaction. One that fails is logged and left
    for the next sweep: the longest uncounted go first, so it would otherwise stop the same ones
    behind it every time. A connection that died ends the sweep. Returns how many were counted."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        TO_COUNT
        # Under way: every sweep. Ended: once more, after the state it ended in.
        + " WHERE m.state NOT IN ('cut_over', 'cancelled')"
        "    OR c.counted_state IS DISTINCT FROM m.state"
        " ORDER BY c.counted_at NULLS FIRST, m.migration_id LIMIT %s",
        (PER_SWEEP,),
    )
    ms = await cur.fetchall()
    counted = 0
    for m in ms:
        try:
            async with conn.transaction():
                await count_one(conn, m, now, has_batch)
            counted += 1
        except Exception:  # noqa: BLE001 - one prefix must not stop the others' counts
            if conn.closed:
                raise
            log.exception("a prefix's count failed", extra={"migration_id": m["migration_id"]})
    await conn.execute("DELETE FROM catalog.migration_progress WHERE at < %s",
                       (now - KEEP_SAMPLES,))  # fmt: skip
    return counted


__all__ = ["TO_COUNT", "count_one", "progress_sweep"]
