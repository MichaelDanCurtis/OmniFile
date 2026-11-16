"""Change-feed sync: provider change records into the catalog (M3).

The service never relies on push delivery (Event Grid can't reach a private
network [A]); it pulls `StorageBackend.read_changes` for every registered
`(backend, container)` and applies each record:

- TIER_CHANGED: the location's `storage_class`, `logical_tier` and
  `tier_changed_at`.
- RESTORE_STARTED: the restore `requested` -> `in_progress`.
- RESTORE_COMPLETED: the restore -> `completed` (with `expires_at`); a
  pending recall copy -> committed; active recalls on it are nudged on
  `restores`.
- RESTORE_EXPIRED / RESTORE_FAILED: the restore -> `expired` / `failed`.
- DELETED: a `deleting` location -> `deleted`; a live one is drift
  (`deleted_externally`), never silently dropped. A versioned tombstone
  keeps every version and is ignored.
- CREATED: nothing (uploads commit through the API).
- CREATED and DELETED for a key under a seeded source: the catalog follows
  the object (new file, new version, tombstone; `sources/follow.py`).

Location and restore updates fire the catalog triggers that refresh the file
summary and emit feed events, so the edge learns tier and restore state
from this stream.

**Idempotent.** The cursor is saved in the same transaction as the changes
of its page, so a crash replays at most that page. Each location remembers
the event time of the newest record applied (`feed_seen_at`) and ignores
older ones; restore transitions only move forward; drift rows are upserts.

**Held while seeding.** A container with a source that is not yet seeded
is not read at all (`last_error` says which source): its events wait on the
backend's queue until the seeded rows exist (catalog-seeding.md section 5).

**No transaction id across a provider call.** The edge feed's horizon is the
oldest open id, so an id held during `read_changes` keeps every edge's events
unread. Syncers of one container take turns on an advisory lock, which takes no id;
the page is read, and the HEADs its records need, before anything is written; and
only then is the cursor's row locked and looked at again.

**Lag** is measured per page as `now - record.at` (the worker's clock), kept
on the cursor row and sampled into `catalog.changefeed_lag`. On sim, the
lag is the backend's configurable `feed_lag` plus how long the sync waits
between polls; on real Azure, records appear "within a few minutes" (VERIFY
under load, real-azure-checks.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg

from ..backends.base import ChangeKind, ChangeRecord, StorageBackend
from ..reconcile.drift import record_drift
from ..retention.common import native_to_logical, set_actor
from ..sources.follow import Heads, follow, heads_to_follow
from .backends import BackendRegistry
from .queue import send

log = logging.getLogger(__name__)

SYNC_ACTOR = "system:changefeed_sync"
#: A page that fails this many times in a row has its failing records
#: skipped (each recorded as `feed_record_failed` drift).
MAX_RECORD_FAILURES = 3


class _PageFailed(Exception):  # noqa: N818 - internal control flow
    def __init__(self, rec: ChangeRecord, error: BaseException) -> None:
        super().__init__(f"{rec.kind} {rec.ref.container}/{rec.ref.key}: "
                         f"{type(error).__name__}: {error}")  # fmt: skip


LAG_SAMPLE_RETENTION = timedelta(days=30)


@dataclass
class SyncStats:
    pages: int = 0
    records: int = 0
    applied: int = 0
    unmatched: int = 0
    drift: int = 0
    max_lag: timedelta | None = None
    caught_up: dict[tuple[int, str], bool] = field(default_factory=dict)


async def register_container(
    conn: psycopg.AsyncConnection, backend_id: int, container: str
) -> None:
    """Start following a container's changes ("from now"). Idempotent."""
    await conn.execute(
        "INSERT INTO catalog.changefeed_cursor (backend_id, container) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING",
        (backend_id, container),
    )


async def _locations(
    conn: psycopg.AsyncConnection, backend_id: int, rec: ChangeRecord
) -> list[tuple]:
    cur = await conn.execute(
        """
        SELECT location_id, role, state, feed_seen_at, tier_changed_at
          FROM catalog.location
         WHERE object_key = %(key)s AND container = %(container)s AND backend_id = %(bid)s
           -- Exactly the version the record names. A record without a
           -- version id matches only locations without one: on a versioned
           -- container it can't say which version changed.
           AND backend_version_id IS NOT DISTINCT FROM %(ver)s::text
           AND state <> 'deleted'
         ORDER BY location_id
           FOR UPDATE
        """,
        {"key": rec.ref.key, "container": rec.ref.container, "bid": backend_id,
         "ver": rec.ref.version_id},
    )  # fmt: skip
    return list(await cur.fetchall())


async def _restores(
    conn: psycopg.AsyncConnection, backend_id: int, rec: ChangeRecord, loc_ids: list[int]
) -> list[tuple]:
    """Restores a restore record is about: by provider reference when the
    record carries one, else by the source or target location."""
    ref = rec.detail.get("provider_ref")
    if ref:
        cur = await conn.execute(
            "SELECT restore_id, state, target_location_id FROM catalog.restore"
            " WHERE backend_id = %s AND provider_ref = %s ORDER BY restore_id FOR UPDATE",
            (backend_id, ref),
        )
        rows = list(await cur.fetchall())
        if rows:
            return rows
    if not loc_ids:
        return []
    cur = await conn.execute(
        "SELECT restore_id, state, target_location_id FROM catalog.restore"
        " WHERE (location_id = ANY(%s) OR target_location_id = ANY(%s))"
        "   AND state NOT IN ('cancelled')"
        " ORDER BY requested_at DESC, restore_id DESC LIMIT 1 FOR UPDATE",
        (loc_ids, loc_ids),
    )
    return list(await cur.fetchall())


async def apply_record(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    rec: ChangeRecord,
    now: datetime,
    tiers: dict | None = None,
    heads: Heads | None = None,
) -> tuple[bool, int]:
    """Apply one record inside the caller's transaction. Returns (matched,
    drift rows recorded)."""
    bid = backend.backend_id
    locs = await _locations(conn, bid, rec)
    followed = await follow(conn, backend, rec, now, locs, heads)
    if followed is not None:
        return followed  # a key under a seeded source
    loc_ids = [r[0] for r in locs]
    kind = rec.kind
    drift = 0
    if kind is ChangeKind.TIER_CHANGED:
        tiers = tiers if tiers is not None else native_to_logical(backend.capabilities())
        if rec.native_class is None:
            return bool(locs), 0
        logical = tiers.get(rec.native_class.lower())
        for lid, _, state, seen, moved in locs:
            if state not in ("pending", "committed"):
                continue
            # Never let an older record undo a newer state: one already
            # applied from the feed, or a move the tiering worker made.
            if (seen is not None and rec.at < seen) or (moved is not None and rec.at < moved):
                continue
            await conn.execute(
                "UPDATE catalog.location SET storage_class = %s,"
                " logical_tier = coalesce(%s, logical_tier), tier_changed_at = %s,"
                " feed_seen_at = %s"
                " WHERE location_id = %s AND (storage_class, logical_tier, feed_seen_at)"
                " IS DISTINCT FROM (%s, coalesce(%s, logical_tier), %s)",
                (rec.native_class, logical, rec.at, rec.at, lid,
                 rec.native_class, logical, rec.at),
            )  # fmt: skip
        return bool(locs), 0
    if kind in (
        ChangeKind.RESTORE_STARTED,
        ChangeKind.RESTORE_COMPLETED,
        ChangeKind.RESTORE_EXPIRED,
        ChangeKind.RESTORE_FAILED,
    ):
        restores = await _restores(conn, bid, rec, loc_ids)
        for rid, state, target in restores:
            if kind is ChangeKind.RESTORE_STARTED and state == "requested":
                await conn.execute(
                    "UPDATE catalog.restore SET state = 'in_progress', updated_at = %s"
                    " WHERE restore_id = %s",
                    (now, rid),
                )
            elif kind is ChangeKind.RESTORE_COMPLETED and state in ("requested", "in_progress"):
                expires = rec.detail.get("expires_at")
                await conn.execute(
                    "UPDATE catalog.restore SET state = 'completed', completed_at = %s,"
                    " expires_at = coalesce(%s::timestamptz, expires_at), updated_at = %s"
                    " WHERE restore_id = %s",
                    (rec.at, expires or None, now, rid),
                )
                if target is not None:
                    await conn.execute(
                        "UPDATE catalog.location SET state = 'committed', committed_at = %s,"
                        " storage_class = 'Hot', logical_tier = 'hot', feed_seen_at = %s"
                        " WHERE location_id = %s AND state = 'pending'",
                        (rec.at, rec.at, target),
                    )
                cur = await conn.execute(
                    "SELECT recall_id FROM catalog.recall WHERE restore_id = %s"
                    " AND state IN ('requested', 'rehydrating', 'copying')",
                    (rid,),
                )
                for (recall_id,) in await cur.fetchall():
                    await send(conn, "restores", {"recall_id": str(recall_id)})
            elif kind is ChangeKind.RESTORE_EXPIRED and state == "completed":
                await conn.execute(
                    "UPDATE catalog.restore SET state = 'expired', updated_at = %s"
                    " WHERE restore_id = %s",
                    (now, rid),
                )
            elif kind is ChangeKind.RESTORE_FAILED and state in ("requested", "in_progress"):
                await conn.execute(
                    "UPDATE catalog.restore SET state = 'failed', failure = %s, completed_at = %s,"
                    " updated_at = %s WHERE restore_id = %s",
                    (rec.detail.get("reason") or "provider reported failure", rec.at, now, rid),
                )
        return bool(locs or restores), 0
    if kind is ChangeKind.DELETED:
        if rec.detail.get("tombstone") == "true":
            # A versioned delete keeps every version: nothing is lost.
            return bool(locs), 0
        for lid, role, state, seen, _moved in locs:
            if state == "deleting":
                await conn.execute(
                    "UPDATE catalog.location SET state = 'deleted', feed_seen_at = %s"
                    " WHERE location_id = %s",
                    (rec.at, lid),
                )
            elif state == "committed" and (seen is None or rec.at >= seen):
                drift += await record_drift(
                    conn,
                    source="changefeed",
                    backend_id=bid,
                    container=rec.ref.container,
                    key=rec.ref.key,
                    version=rec.ref.version_id,
                    kind="deleted_externally",
                    location_id=lid,
                    expected={"state": state, "role": role},
                    observed={"event": str(kind), "at": rec.at.isoformat()},
                    now=now,
                )
        return bool(locs), drift
    return bool(locs), 0  # CREATED: uploads commit through the API


async def sync_container(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    backend_id: int,
    container: str,
    now: datetime,
    *,
    max_pages: int = 20,
    stats: SyncStats | None = None,
) -> SyncStats:
    """Read and apply pages for one container until caught up (or
    `max_pages`). Each page is one transaction with its cursor."""
    stats = stats or SyncStats()
    backend = registry.get(backend_id)
    tiers = native_to_logical(backend.capabilities())
    lags: list[float] = []
    for _ in range(max_pages):
        try:
            done = await _sync_page(conn, backend, backend_id, container, now, tiers, stats, lags)
        except _PageFailed as e:
            # The page rolled back (cursor unchanged); count the failure.
            await conn.execute(
                "UPDATE catalog.changefeed_cursor SET last_error = %s, last_read_at = %s,"
                " consecutive_failures = consecutive_failures + 1"
                " WHERE backend_id = %s AND container = %s",
                (str(e)[:2000], now, backend_id, container),
            )
            log.warning("change feed %s/%s: %s", backend_id, container, e)
            stats.caught_up[(backend_id, container)] = False
            break
        if done:
            break
    await _record_lag(conn, backend_id, container, now, lags, stats)
    return stats


CURSOR_SQL = """
SELECT cursor, consecutive_failures FROM catalog.changefeed_cursor
 WHERE backend_id = %s AND container = %s
"""


async def _sync_page(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    backend_id: int,
    container: str,
    now: datetime,
    tiers: dict,
    stats: SyncStats,
    lags: list[float],
) -> bool:
    """Read and apply one page in one transaction. Returns True when there
    is nothing more to read now."""
    async with conn.transaction():
        await set_actor(conn, SYNC_ACTOR)
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
            (f"omnifile:changefeed:{backend_id}/{container}",),
        )
        (mine,) = await cur.fetchone()  # type: ignore[misc]
        if not mine:
            return True  # another worker has it
        cur = await conn.execute(CURSOR_SQL, (backend_id, container))
        row = await cur.fetchone()
        if row is None:
            return True  # not registered
        if await _held(conn, backend_id, container, now):
            stats.caught_up[(backend_id, container)] = False
            return True
        skip_failing = row[1] + 1 >= MAX_RECORD_FAILURES
        try:
            page = await backend.read_changes(container, row[0])
            heads = await heads_to_follow(conn, backend, page.records)
        except Exception as e:  # noqa: BLE001 - record and move on to the next container
            await conn.execute(
                "UPDATE catalog.changefeed_cursor SET last_error = %s, last_read_at = %s"
                " WHERE backend_id = %s AND container = %s",
                (f"{type(e).__name__}: {e}"[:2000], now, backend_id, container),
            )
            log.warning("change feed %s/%s: %s", backend_id, container, e)
            stats.caught_up[(backend_id, container)] = False
            return True
        # Only now take the row. A seed starting this container's feed holds it
        # through its commit, and then this page belongs to the wrong cursor.
        cur = await conn.execute(CURSOR_SQL + " FOR UPDATE", (backend_id, container))
        locked = await cur.fetchone()
        if locked is None or locked[0] != row[0]:
            return True
        applied = unmatched = skipped = 0
        newest = None
        page_lags: list[float] = []
        for rec in page.records:
            try:
                async with conn.transaction():  # a savepoint per record
                    matched, d = await apply_record(conn, backend, rec, now, tiers, heads)
            except Exception as e:  # noqa: BLE001 - counted, retried, then skipped
                if not skip_failing:
                    raise _PageFailed(rec, e) from e
                stats.drift += await _skip_record(conn, backend_id, rec, e, now)
                skipped += 1
                continue
            stats.drift += d
            if matched:
                applied += 1
            else:
                unmatched += 1
            page_lags.append(max(0.0, (now - rec.at).total_seconds()))
            newest = rec.at if newest is None or rec.at > newest else newest
        lag = (now - newest).total_seconds() if newest is not None else None
        await conn.execute(
            """
            UPDATE catalog.changefeed_cursor
               SET cursor = %s, caught_up = %s, last_read_at = %s, last_error = NULL,
                   consecutive_failures = 0, skipped_records = skipped_records + %s,
                   records_applied = records_applied + %s,
                   records_unmatched = records_unmatched + %s,
                   last_record_at = coalesce(%s, last_record_at),
                   last_lag_seconds = coalesce(%s, last_lag_seconds)
             WHERE backend_id = %s AND container = %s
            """,
            (page.cursor, page.caught_up, now, skipped, applied, unmatched, newest, lag,
             backend_id, container),
        )  # fmt: skip
        stats.pages += 1
        stats.records += len(page.records)
        stats.applied += applied
        stats.unmatched += unmatched
        stats.caught_up[(backend_id, container)] = page.caught_up
        lags.extend(page_lags)
        return page.caught_up


async def _held(
    conn: psycopg.AsyncConnection, backend_id: int, container: str, now: datetime
) -> bool:
    """A container with a source not yet seeded is held: its events stay on
    the backend's queue until every seeded row exists, so none is consumed
    for an object the catalog doesn't have yet (catalog-seeding.md
    section 5). Recalls poll their own restores, so they don't wait."""
    cur = await conn.execute(
        "SELECT source_id, state FROM catalog.source"
        " WHERE backend_id = %s AND container = %s AND state <> 'seeded'"
        " ORDER BY source_id LIMIT 1",
        (backend_id, container),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    await conn.execute(
        "UPDATE catalog.changefeed_cursor SET last_read_at = %s, caught_up = false,"
        " last_error = %s WHERE backend_id = %s AND container = %s",
        (now, f"held: source {row[0]} is {row[1]}", backend_id, container),
    )
    return True


async def _skip_record(
    conn: psycopg.AsyncConnection,
    backend_id: int,
    rec: ChangeRecord,
    error: BaseException,
    now: datetime,
) -> int:
    log.error("change feed: skipping %s %s/%s after %d failures: %s", rec.kind,
              rec.ref.container, rec.ref.key, MAX_RECORD_FAILURES, error)  # fmt: skip
    return await record_drift(
        conn,
        source="changefeed",
        backend_id=backend_id,
        container=rec.ref.container,
        key=rec.ref.key,
        version=rec.ref.version_id,
        kind="feed_record_failed",
        expected={"applied": True},
        observed={"kind": str(rec.kind), "at": rec.at.isoformat(),
                  "native_class": rec.native_class, "detail": dict(rec.detail),
                  "error": f"{type(error).__name__}: {error}"[:1000]},
        now=now,
    )  # fmt: skip


async def _record_lag(
    conn: psycopg.AsyncConnection,
    backend_id: int,
    container: str,
    now: datetime,
    lags: list[float],
    stats: SyncStats,
) -> None:
    if lags:
        worst = max(lags)
        stats.max_lag = max(stats.max_lag or timedelta(0), timedelta(seconds=worst))
        await conn.execute(
            """
            INSERT INTO catalog.changefeed_lag (backend_id, container, at, records,
                                                max_lag_seconds, mean_lag_seconds)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (backend_id, container, at) DO UPDATE
               SET records = catalog.changefeed_lag.records + EXCLUDED.records,
                   max_lag_seconds = greatest(catalog.changefeed_lag.max_lag_seconds,
                                              EXCLUDED.max_lag_seconds),
                   mean_lag_seconds = EXCLUDED.mean_lag_seconds
            """,
            (backend_id, container, now, len(lags), worst, sum(lags) / len(lags)),
        )
        log.info("change feed %s/%s: %d records, max lag %.0fs", backend_id, container,
                 len(lags), worst)  # fmt: skip


async def sync_all(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    now: datetime,
    *,
    max_pages: int = 20,
) -> SyncStats:
    """One pass over every registered container (the periodic sweep)."""
    cur = await conn.execute(
        "SELECT backend_id, container FROM catalog.changefeed_cursor ORDER BY 1, 2"
    )
    stats = SyncStats()
    for backend_id, container in await cur.fetchall():
        try:
            await sync_container(
                conn, registry, backend_id, container, now, max_pages=max_pages, stats=stats
            )
        except Exception as e:  # noqa: BLE001 - one container must not stop the others
            log.warning("change feed %s/%s: %s", backend_id, container, e)
            await conn.execute(
                "UPDATE catalog.changefeed_cursor SET last_error = %s, last_read_at = %s"
                " WHERE backend_id = %s AND container = %s",
                (f"{type(e).__name__}: {e}"[:2000], now, backend_id, container),
            )
    await conn.execute(
        "DELETE FROM catalog.changefeed_lag WHERE at < %s", (now - LAG_SAMPLE_RETENTION,)
    )
    return stats


async def lag_report(conn: psycopg.AsyncConnection, now: datetime | None = None) -> list[dict]:
    """The lag metric per container, for dashboards and alerts.

    `last_lag_seconds` is the lag of the newest record when it was applied;
    `last_record_age_seconds` is how old that record is now, so a feed that
    has stopped delivering shows up even though its last lag looked fine.
    """
    cur = await conn.execute(
        "SELECT backend_id, container, caught_up, last_record_at, last_lag_seconds,"
        " extract(epoch FROM coalesce(%s::timestamptz, now()) - last_record_at)::float8"
        "   AS last_record_age_seconds,"
        " last_read_at, records_applied, records_unmatched, consecutive_failures,"
        " skipped_records, last_error"
        " FROM catalog.changefeed_cursor ORDER BY 1, 2",
        (now,),
    )
    names = [d.name for d in cur.description or []]
    return [dict(zip(names, r, strict=True)) for r in await cur.fetchall()]


__all__ = [
    "SyncStats",
    "apply_record",
    "lag_report",
    "register_container",
    "sync_all",
    "sync_container",
]
