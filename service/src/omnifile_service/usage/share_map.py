"""The share map's roll-up (portal-api.md section 5.22; migration 0056).

The worker keeps, per catalog folder, how many live files it holds and their
bytes, in groups of one captured ACL, one annex folder, one state and one
primary copy's backend, tier and class (`catalog.share_map_group`). The map sums
the groups of the folders it walks, putting each group through the same read
predicate a file goes through, so nothing about who may read is stored here.

`refresh` recounts the folders in `catalog.share_map_dirty`, which triggers fill
when a folder's files change (migration 0056 says which changes):

- A batch of at most `BATCH` folders is recounted in one short transaction, at
  REPEATABLE READ: its groups are deleted and counted again, its folders stamped
  `counted_at`, and only the queue entries that transaction's snapshot saw are
  taken off. A change committed while it ran stays queued and is counted next.
- At most `MAX_BATCHES` batches, or `BUDGET_S` seconds, per run; the queue is
  the cursor, so a run that stops is resumed by the next. Each batch reads at
  most `SCAN` of the oldest entries, by `seq`; folders whose recount met a
  concurrent change wait for the next run, and the run goes on to others.
- The folders made before migration 0056 are counted first, `BACKFILL` at a
  time, through a cursor in `catalog.share_map_state`, so the first count also
  resumes. Until a folder is counted, a map that would sum it says "counting".
- Nothing is held between transactions, and an edge's commit only inserts into
  the queue, so it never waits on a refresh.

"As of": a folder with nothing queued is true now; one with something queued, as
of its `counted_at`; one made since the roll-up began and not counted yet, as of
when it was made (it was empty then, and its files are queued). The map is true
as of the oldest of these among the folders it sums, so a worker that falls behind
makes it older, never a wrong total current. A share with a folder from before
the roll-up and no count yet, or with none counted, is "counting". `age_s` is how old
the oldest pending change is (`omnifile.share_map.age`, with an alert).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import psycopg
from psycopg.rows import tuple_row

log = logging.getLogger(__name__)

#: Folders recounted in one transaction.
BATCH = 500
#: Batches per run, and seconds per run, at most.
MAX_BATCHES = 40
BUDGET_S = 20.0
#: Folders made before migration 0056 queued per run, at most.
BACKFILL = 20_000

#: Queue entries a batch reads, oldest first by `seq`, at most: a folder changed
#: many times takes several, so a batch may hold fewer than `BATCH` folders.
SCAN = 4 * BATCH

#: The next batch: the folders of the oldest entries, read by the queue's primary
#: key, so a big backlog costs a batch no more than `SCAN` entries; without the
#: folders a recount in this run already failed on.
_NEXT = """
SELECT DISTINCT q.folder_id
  FROM (SELECT folder_id FROM catalog.share_map_dirty
         WHERE folder_id <> ALL(%(skip)s::bigint[]) ORDER BY seq LIMIT %(scan)s) q
 LIMIT %(batch)s
"""

_COUNT = """
INSERT INTO catalog.share_map_group
    (folder_id, acl_id, annexed, dir_id, edge_state, backend_id, logical_tier, storage_class,
     files, bytes, local_bytes)
SELECT f.parent_id, f.acl_id, x.file_id IS NOT NULL, x.dir_id, f.state, l.backend_id,
       l.logical_tier::text, l.storage_class, count(*), coalesce(sum(f.size), 0),
       coalesce(sum(f.resident_bytes), 0)
  FROM catalog.file f
  LEFT JOIN catalog.annex_file x ON x.file_id = f.file_id
  LEFT JOIN catalog.location l
    ON l.location_id = f.primary_location_id AND l.state = 'committed'
 WHERE f.parent_id = ANY(%(folders)s) AND f.kind = 'file' AND f.deleted_at IS NULL
 GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""

#: Folders made before migration 0056 with no count yet, by id from a cursor (those
#: made since are queued as they are made).
_BACKFILL = """
SELECT d.file_id FROM catalog.file d
 WHERE d.kind = 'dir' AND d.file_id > %(after)s
   AND d.created_at < (SELECT value::timestamptz FROM catalog.share_map_state
                        WHERE key = 'since')
   AND NOT EXISTS (SELECT 1 FROM catalog.share_map_folder c WHERE c.folder_id = d.file_id)
 ORDER BY d.file_id LIMIT %(n)s
"""


async def _backfill(conn: psycopg.AsyncConnection) -> int:
    """Queue the next folders made before migration 0056; 0 once all are."""
    cur = conn.cursor(row_factory=tuple_row)
    async with conn.transaction():
        await cur.execute(
            "SELECT value FROM catalog.share_map_state WHERE key = 'backfill' FOR UPDATE"
        )
        row = await cur.fetchone()
        if row is not None and row[0] == "done":
            return 0
        await cur.execute(
            "INSERT INTO catalog.share_map_state (key, value) VALUES ('backfill_since', %s)"
            " ON CONFLICT (key) DO NOTHING",
            (datetime.now(UTC).isoformat(),),
        )
        after = int(row[0]) if row is not None else 0
        await cur.execute(_BACKFILL, {"after": after, "n": BACKFILL})
        ids = [r[0] for r in await cur.fetchall()]
        if ids:
            await cur.execute(
                "INSERT INTO catalog.share_map_dirty (folder_id) SELECT unnest(%s::bigint[])",
                (ids,),
            )
        value = str(ids[-1]) if len(ids) == BACKFILL else "done"
        await cur.execute(
            "INSERT INTO catalog.share_map_state (key, value) VALUES ('backfill', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (value,),
        )
        return len(ids)


async def recount(
    conn: psycopg.AsyncConnection,
    folders: list[int],
    *,
    before_take: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Count `folders` again, in one transaction at REPEATABLE READ: every
    statement sees one snapshot, so the queue entries taken off are those the
    count saw. `before_take` runs before they are (a test's hook)."""
    async with conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        await conn.execute(
            "DELETE FROM catalog.share_map_group WHERE folder_id = ANY(%s)", (folders,)
        )
        await conn.execute(_COUNT, {"folders": folders})
        # Folders that are still there; a folder hard-deleted since is gone.
        await conn.execute(
            "INSERT INTO catalog.share_map_folder (folder_id, counted_at)"
            " SELECT d.file_id, now() FROM catalog.file d"
            "  WHERE d.file_id = ANY(%s) AND d.kind = 'dir'"
            " ON CONFLICT (folder_id) DO UPDATE SET counted_at = EXCLUDED.counted_at",
            (folders,),
        )
        if before_take is not None:
            await before_take()
        await conn.execute(
            "DELETE FROM catalog.share_map_dirty WHERE folder_id = ANY(%s)", (folders,)
        )


async def refresh(conn: psycopg.AsyncConnection, now: datetime) -> int:
    """One run on an autocommit connection: the backfill's next step, then the
    queue, oldest first, bounded. Returns the folders recounted."""
    del now
    await _backfill(conn)
    done, start = 0, time.monotonic()
    cur = conn.cursor(row_factory=tuple_row)
    skip: list[int] = []
    for _ in range(MAX_BATCHES):
        if time.monotonic() - start > BUDGET_S:
            break
        await cur.execute(_NEXT, {"skip": skip, "scan": SCAN, "batch": BATCH})
        folders = [r[0] for r in await cur.fetchall()]
        if not folders:
            break
        try:
            await recount(conn, folders)
        except psycopg.errors.SerializationFailure:
            # A folder made while it ran: its row is the edge's. These folders wait
            # for the next run, and this one goes on to others.
            log.info("share map recount of %d folders met a concurrent change", len(folders))
            skip.extend(folders)
            continue
        done += len(folders)
    return done


async def age_s(conn: psycopg.AsyncConnection, now: datetime) -> float:
    """How old the oldest pending change is, in seconds (0: none); with the
    first count not done, how long since it started."""
    row = await (
        await conn.execute(
            "SELECT extract(epoch FROM %(now)s - min(at)) FROM catalog.share_map_dirty",
            {"now": now},
        )
    ).fetchone()
    queued = float(row[0]) if row and row[0] is not None else 0.0
    state = dict(
        await (
            await conn.execute(
                "SELECT key, value FROM catalog.share_map_state"
                " WHERE key IN ('backfill', 'backfill_since')"
            )
        ).fetchall()
    )
    first = 0.0
    if state.get("backfill") != "done":
        since = state.get("backfill_since")
        first = (now - datetime.fromisoformat(since)).total_seconds() if since else 0.0
    return max(0.0, queued, first)
