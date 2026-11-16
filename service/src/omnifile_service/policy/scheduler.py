"""The tiering scheduler: evaluate due files against their policy, then
enqueue the work in the same transaction as the catalog change (decision 27).

One sweep:

1. **Candidates** (one indexed query, `file_next_action`):
   - files whose `next_action_at` has come;
   - files never evaluated (`next_action IS NULL`) that still have work left;
   - files under an active tier pin whose tier differs from the pin.

   Only datasets whose active policy is an M3 tiering policy (it has an
   `archive_day`) take part. Files with a move already queued on
   ``tiering`` are skipped.
2. **Evaluate** each with `engine.evaluate` (policy, effective pin, holds,
   location tier).
3. **Apply**, per file, in one transaction:
   - a move goes on ``tiering`` as ``{"kind": "move", file_id, version_id,
     location_id, tier}``, and `worker.tiering.handle_move` runs `set_class`;
   - a recall for a pin (a warmer pin on an archived stub) inserts
     ``catalog.recall`` plus its requester (the pin's setter, via the
     portal), and sends ``{"recall_id"}`` on ``restores``;
   - a stub goes on ``edge_instructions`` as M1's ``{"kind": "stub",
     file_id, version_id}``, so the existing handler HEADs the blob before
     setting the file's desired stub state;
   - ``file.next_action``, ``next_action_at`` and ``archive_at`` record when
     to look again.

Superseded content versions (and a deleted file's last one) are tiered too
(`sweep_versions`): colder only, from their commit, with no pins or stubs.

A file whose copy is in a migration source is not moved, by schedule or pin:
an S3 class change rewrites the client's object (`catalog.source_readonly`).
It waits for its prefix's cutover; then the verified Azure copy is the
primary, and the schedule and pins act on it.

All times come from the caller's `now`; the SQL never reads the wall clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..backends.base import LogicalTier
from ..catalog.source_readonly import on_source
from .engine import (
    HELD_BACK,
    OWN_COPY_RETRY_AFTER,
    FileFacts,
    Pin,
    Plan,
    evaluate,
    old_version_wants_same_tier,
    same_tier_wanted,
)
from .model import Policy, PolicyError

log = logging.getLogger(__name__)

TIERING = "tiering"
RESTORES = "restores"
EDGE_INSTRUCTIONS = "edge_instructions"
ACTOR = "system:tiering"

CANDIDATES_SQL = f"""
WITH pol AS (
    SELECT dataset_id, body FROM catalog.policy
     WHERE state = 'active' AND body ? 'archive_day'
       AND (%(datasets)s::bigint[] IS NULL OR dataset_id = ANY (%(datasets)s::bigint[]))
),
pins AS (
    SELECT p.pin_id, p.file_id, p.logical_tier, p.override_archive
      FROM catalog.tier_pin p JOIN pol USING (dataset_id)
     WHERE p.released_at IS NULL AND (p.until IS NULL OR p.until > %(now)s)
),
pinned AS (
    WITH RECURSIVE down (file_id, pin_tier, override_archive) AS (
        SELECT file_id, logical_tier::text, override_archive FROM pins
        UNION
        SELECT c.file_id, down.pin_tier, down.override_archive
          FROM catalog.file c JOIN down ON c.parent_id = down.file_id
         WHERE c.deleted_at IS NULL
    )
    SELECT f.file_id
      FROM down
      JOIN catalog.file f ON f.file_id = down.file_id
      JOIN catalog.location l ON l.location_id = f.primary_location_id
     WHERE f.kind = 'file'
       AND coalesce(l.logical_tier, 'hot') <> down.pin_tier
       AND (f.next_action_at IS NULL OR f.next_action_at > %(now)s)
       -- settled cases that would otherwise come back every sweep:
       AND NOT (down.pin_tier = 'smart' AND l.explicit_tier_set)
       AND NOT (l.logical_tier = 'archive' AND f.state <> 'stub')
       AND NOT EXISTS (SELECT 1 FROM catalog.recall r
                        WHERE r.file_id = f.file_id
                          AND r.state IN ('requested', 'rehydrating', 'copying', 'downloading'))
       AND (down.override_archive OR f.archive_at IS NULL
            OR f.archive_at > (%(now)s AT TIME ZONE 'UTC')::date)
       -- A file waiting a day to try for its own copy of a shared blob
       -- (`tiering._wait_a_day`) comes back through next_action_at, not on every
       -- sweep. A recall isn't a move. An archive date inside the wait isn't missed: the
       -- wait wakes the file on it (`tiering._hold_wake`), so it comes back as due.
       AND NOT coalesce(
            coalesce(l.logical_tier::text, 'hot') <> 'archive'
            AND EXISTS (SELECT 1 FROM catalog.location o
                         WHERE o.backend_id = l.backend_id AND o.container = l.container
                           AND o.object_key = l.object_key
                           AND o.backend_version_id IS NOT DISTINCT FROM l.backend_version_id
                           AND o.state = 'committed' AND o.role = 'primary'
                           AND o.location_id <> l.location_id)
            AND EXISTS (SELECT 1 FROM catalog.location o
                          JOIN catalog.audit_log a ON a.file_id = o.file_id
                               AND a.details->>'location_id' = o.location_id::text
                         WHERE o.backend_id = l.backend_id AND o.container = l.container
                           AND o.object_key = l.object_key
                           AND o.backend_version_id IS NOT DISTINCT FROM l.backend_version_id
                           AND o.state = 'committed'
                           AND a.action = 'tier.move.refused'
                           AND a.details->>'why' = %(held_back)s
                           AND (a.details->>'at')::timestamptz > %(now)s - %(retry_after)s),
            false)
)
SELECT f.file_id
  FROM catalog.file f
  JOIN pol ON pol.dataset_id = f.dataset_id
  JOIN catalog.location l ON l.location_id = f.primary_location_id
 WHERE f.kind = 'file'
   AND f.deleted_at IS NULL
   AND l.state = 'committed'
   AND l.version_id = f.content_version_id
   -- A copy in a migration source is never re-tiered; the file waits for
   -- its cutover, when the verified Azure copy is the primary.
   AND NOT {on_source("l")}
   AND (f.next_action_at <= %(now)s
        OR (f.next_action IS NULL
            AND NOT (coalesce(l.logical_tier, 'hot') = 'archive'
                     AND (f.state <> 'resident' OR f.stub_version_id IS NOT NULL
                          OR pol.body->>'stub_at' = 'never')))
        OR f.file_id IN (SELECT file_id FROM pinned))
   AND NOT EXISTS (SELECT 1 FROM pgmq.q_tiering q
                    WHERE q.message->>'file_id' = f.file_id::text)
 ORDER BY f.next_action_at NULLS FIRST, f.file_id
 LIMIT %(batch)s
"""

FACTS_READ_SQL = f"""
WITH RECURSIVE up (file_id, parent_id, depth) AS (
    SELECT file_id, parent_id, 0 FROM catalog.file WHERE file_id = %(file_id)s
    UNION ALL
    SELECT a.file_id, a.parent_id, up.depth + 1
      FROM catalog.file a JOIN up ON a.file_id = up.parent_id
)
SELECT f.file_id, f.dataset_id, f.state, f.content_version_id, f.stub_version_id,
       f.written_at, f.last_read_at, f.next_action, f.next_action_at, f.archive_at,
       f.deleted_at, f.kind, v.committed_at,
       l.location_id, l.logical_tier, l.explicit_tier_set, l.verified, l.state AS loc_state,
       l.version_id AS loc_version_id, {on_source("l")} AS loc_on_source,
       d.storage_layout, d.edge_id, pol.body,
       (SELECT to_jsonb(t) FROM catalog.tier_pin t JOIN up ON up.file_id = t.file_id
         WHERE t.released_at IS NULL AND (t.until IS NULL OR t.until > %(now)s)
         ORDER BY up.depth LIMIT 1) AS pin,
       (SELECT jsonb_build_object('n', count(*), 'forever', bool_or(h.until IS NULL),
                                  'until', max(h.until))
          FROM catalog.hold h JOIN up ON up.file_id = h.file_id
         WHERE h.kind = 'keep_online' AND h.released_at IS NULL
           AND (h.until IS NULL OR h.until > %(now)s)) AS keep_online
  FROM catalog.file f
  JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
  JOIN catalog.policy pol ON pol.dataset_id = f.dataset_id AND pol.state = 'active'
  LEFT JOIN catalog.file_version v ON v.version_id = f.content_version_id
  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
 WHERE f.file_id = %(file_id)s
"""
FACTS_SQL = FACTS_READ_SQL + " FOR UPDATE OF f"


@dataclass
class SweepResult:
    evaluated: int = 0
    moves: list[tuple[int, str]] = field(default_factory=list)
    #: (version_id, tier) for superseded versions.
    version_moves: list[tuple[int, str]] = field(default_factory=list)
    stubs: list[int] = field(default_factory=list)
    recalls: list[int] = field(default_factory=list)
    flags: dict[int, tuple[str, ...]] = field(default_factory=dict)
    skipped: dict[int, str] = field(default_factory=dict)

    def __int__(self) -> int:
        return len(self.moves) + len(self.version_moves) + len(self.stubs) + len(self.recalls)


def _ts(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def facts_from_row(row: dict[str, Any]) -> tuple[FileFacts, Pin | None]:
    written = row["written_at"] or row["committed_at"]
    hold = row["keep_online"] or {}
    held = bool(hold.get("n"))
    pin_j = row["pin"]
    pin = None
    if pin_j:
        pin = Pin(
            tier=LogicalTier(pin_j["logical_tier"]),
            until=_ts(pin_j.get("until")),
            override_archive=bool(pin_j.get("override_archive", False)),
            pin_id=pin_j.get("pin_id"),
            set_by_identity_id=pin_j.get("set_by_identity_id"),
        )
    facts = FileFacts(
        written_at=written,
        last_read_at=row["last_read_at"],
        tier=LogicalTier(row["logical_tier"]) if row["logical_tier"] else None,
        explicit_tier_set=bool(row["explicit_tier_set"]),
        smart_available=row["storage_layout"] == "two_account",
        state=row["state"],
        stub_requested=row["stub_version_id"] is not None,
        verified=bool(row["verified"]),
        keep_online=held,
        keep_online_until=None if hold.get("forever") else _ts(hold.get("until")),
    )
    return facts, pin


async def _set_actor(conn: psycopg.AsyncConnection) -> None:
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))


async def _send(conn: psycopg.AsyncConnection, queue: str, body: dict[str, Any]) -> None:
    await conn.execute("SELECT pgmq.send(%s::text, %s::jsonb)", (queue, Jsonb(body)))


async def _queued(conn: psycopg.AsyncConnection, queue: str, kind: str, file_id: int) -> bool:
    cur = await conn.execute(
        f"SELECT 1 FROM pgmq.q_{queue} WHERE message->>'kind' = %s"
        " AND message->>'file_id' = %s LIMIT 1",
        (kind, str(file_id)),
    )
    return await cur.fetchone() is not None


async def _start_recall(
    conn: psycopg.AsyncConnection, row: dict[str, Any], pin: Pin | None, now: datetime
) -> int | None:
    cur = await conn.execute(
        """
        INSERT INTO catalog.recall (file_id, version_id, edge_id, priority, requested_at,
                                    updated_at)
        VALUES (%s, %s, %s, 'standard', %s, %s)
        ON CONFLICT (file_id, version_id)
            WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')
            DO NOTHING
        RETURNING recall_id
        """,
        (row["file_id"], row["content_version_id"], row["edge_id"], now, now),
    )
    got = await cur.fetchone()
    if got is None:
        return None  # joined: a recall for this version is already running
    recall_id = int(got[0])
    if pin is not None and pin.set_by_identity_id is not None:
        await conn.execute(
            "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof,"
            " requested_at) VALUES (%s, %s, 'portal', 'proven', %s)",
            (recall_id, pin.set_by_identity_id, now),
        )
    await _send(conn, RESTORES, {"recall_id": str(recall_id)})
    return recall_id


async def apply_plan(
    conn: psycopg.AsyncConnection, row: dict[str, Any], plan: Plan, pin: Pin | None,
    now: datetime, result: SweepResult,
) -> None:  # fmt: skip
    file_id = row["file_id"]
    if plan.move_to is not None and await _queued(conn, TIERING, "move", file_id):
        # Another sweep queued a move after our candidate query; the file row
        # is locked now, so this check is final.
        result.skipped[file_id] = "move already queued"
    elif plan.move_to is not None:
        await _send(
            conn,
            TIERING,
            {
                "kind": "move",
                "file_id": str(file_id),
                "version_id": str(row["content_version_id"]),
                "location_id": str(row["location_id"]),
                "tier": str(plan.move_to),
            },
        )
        result.moves.append((file_id, str(plan.move_to)))
    if plan.recall:
        rid = await _start_recall(conn, row, pin, now)
        if rid is not None:
            result.recalls.append(rid)
    if plan.stub and not await _queued(conn, EDGE_INSTRUCTIONS, "stub", file_id):
        await _send(
            conn,
            EDGE_INSTRUCTIONS,
            {
                "kind": "stub",
                "file_id": str(file_id),
                "version_id": str(row["content_version_id"]),
            },
        )
        result.stubs.append(file_id)
    archive_date = plan.archive_at.date() if plan.archive_at else None
    await conn.execute(
        """
        UPDATE catalog.file
           SET next_action = %(action)s, next_action_at = %(at)s, archive_at = %(arch)s
         WHERE file_id = %(file_id)s
           AND (next_action, next_action_at, archive_at)
               IS DISTINCT FROM (%(action)s, %(at)s::timestamptz, %(arch)s::date)
        """,
        {
            "action": plan.next_action if plan.next_at is not None else None,
            "at": plan.next_at,
            "arch": archive_date,
            "file_id": file_id,
        },
    )
    if plan.flags:
        result.flags[file_id] = plan.flags


async def evaluate_file(
    conn: psycopg.AsyncConnection, file_id: int, now: datetime, result: SweepResult
) -> Plan | None:
    """Evaluate one file and apply its plan, in the caller's transaction."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(FACTS_SQL, {"file_id": file_id, "now": now})
    row = await cur.fetchone()
    if row is None:
        result.skipped[file_id] = "no active policy"
        return None
    if (
        row["deleted_at"] is not None
        or row["kind"] != "file"
        or row["location_id"] is None
        or row["loc_state"] != "committed"
        or row["loc_version_id"] != row["content_version_id"]
    ):
        result.skipped[file_id] = "no committed cloud copy of the content version"
        return None
    if row["loc_on_source"]:
        result.skipped[file_id] = "in a migration source: waits for its cutover"
        return None
    try:
        policy = Policy.from_body(row["body"])
    except PolicyError as e:
        result.skipped[file_id] = f"policy: {e}"
        return None
    facts, pin = facts_from_row(row)
    if facts.written_at is None:
        result.skipped[file_id] = "not committed"
        return None
    plan = evaluate(policy, facts, pin, now)
    await apply_plan(conn, row, plan, pin, now, result)
    result.evaluated += 1
    return plan


async def wants_same_tier(
    conn: psycopg.AsyncConnection,
    file_id: int,
    version_id: int,
    committed_at: datetime | None,
    current: LogicalTier,
    target: LogicalTier,
    now: datetime,
) -> bool:
    """Decision 66, for one reference of a blob the worker is about to move: see
    `engine.same_tier_wanted`. A file with no tiering policy has no plan of its own,
    so it never needs anyone to copy. Read only: the caller may go on to a provider
    call, so nothing here takes a lock or a transaction id."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(FACTS_READ_SQL, {"file_id": file_id, "now": now})
    row = await cur.fetchone()
    if row is None:
        return True
    try:
        policy = Policy.from_body(row["body"])
    except PolicyError:
        return True
    if row["deleted_at"] is not None or row["content_version_id"] != version_id:
        if committed_at is None:
            return True
        return old_version_wants_same_tier(policy, committed_at, current, now, target)
    facts, pin = facts_from_row(row)
    if facts.written_at is None:
        return True
    return same_tier_wanted(policy, replace(facts, tier=current), pin, now, target)


async def pin_in_force(
    conn: psycopg.AsyncConnection, file_id: int, version_id: int, now: datetime
) -> dict[str, Any] | None:
    """The pin a file's current version is under now: the row, as JSON. Read only."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(FACTS_READ_SQL, {"file_id": file_id, "now": now})
    row = await cur.fetchone()
    if row is None or row["deleted_at"] is not None or row["content_version_id"] != version_id:
        return None
    return row["pin"]


#: Superseded (and deleted files') content versions that are still stored:
#: they follow the schedule from their commit, colder only, with no pins,
#: stubs or recalls (they aren't on the edge). Archive on a two-account
#: layout is copy-and-delete, so retained or held versions wait there; on a
#: WORM layout they tier in place.
VERSIONS_SQL = f"""
WITH pol AS (
    SELECT dataset_id, body,
           coalesce(body->>'mode', 'scheduled') AS mode,
           (body->>'warm_day')::int AS warm_day,
           (body->>'cold_day')::int AS cold_day,
           (body->>'archive_day')::int AS archive_day
      FROM catalog.policy
     WHERE state = 'active' AND body ? 'archive_day'
       AND (%(datasets)s::bigint[] IS NULL OR dataset_id = ANY (%(datasets)s::bigint[]))
),
v AS (
    SELECT v.version_id, v.file_id, l.location_id, coalesce(l.logical_tier, 'hot') AS tier,
           d.storage_layout, v.legal_hold, v.retention_until,
           l.backend_id, l.container, l.object_key, l.backend_version_id,
           CASE WHEN v.committed_at <= %(now)s - make_interval(days => pol.archive_day)
                    THEN 'archive'
                WHEN pol.mode = 'scheduled' AND pol.cold_day IS NOT NULL
                     AND v.committed_at <= %(now)s - make_interval(days => pol.cold_day)
                    THEN 'cold'
                WHEN pol.mode = 'scheduled' AND pol.warm_day IS NOT NULL
                     AND v.committed_at <= %(now)s - make_interval(days => pol.warm_day)
                    THEN 'warm'
           END AS desired
      FROM catalog.file_version v
      JOIN pol ON pol.dataset_id = v.dataset_id
      JOIN catalog.dataset d ON d.dataset_id = v.dataset_id
      JOIN catalog.file f ON f.file_id = v.file_id
      JOIN catalog.location l
        ON l.version_id = v.version_id AND l.role = 'primary' AND l.state = 'committed'
     WHERE v.kind = 'content' AND v.state = 'committed'
       AND (v.version_id IS DISTINCT FROM f.content_version_id OR f.deleted_at IS NOT NULL)
       AND coalesce(l.logical_tier, 'hot') <> 'archive'
       AND NOT {on_source("l")}
)
SELECT version_id, file_id, location_id, desired
  FROM v
 WHERE desired IS NOT NULL
   AND (CASE desired WHEN 'warm' THEN 1 WHEN 'cold' THEN 2 ELSE 3 END)
       > (CASE tier WHEN 'warm' THEN 1 WHEN 'cold' THEN 2 WHEN 'archive' THEN 3 ELSE 0 END)
   AND NOT (desired = 'archive' AND storage_layout = 'two_account'
            AND (legal_hold OR coalesce(retention_until > %(now)s, false)))
   AND NOT EXISTS (SELECT 1 FROM pgmq.q_tiering q
                    WHERE q.message->>'location_id' = v.location_id::text)
   -- Waiting a day to try for its own copy of a shared blob (`tiering._wait_a_day`).
   -- Archive doesn't wait.
   AND NOT (desired <> 'archive' AND EXISTS (
        SELECT 1 FROM catalog.location o
          JOIN catalog.audit_log a ON a.file_id = o.file_id
               AND a.details->>'location_id' = o.location_id::text
         WHERE o.backend_id = v.backend_id AND o.container = v.container
           AND o.object_key = v.object_key
           AND o.backend_version_id IS NOT DISTINCT FROM v.backend_version_id
           AND o.state = 'committed'
           AND a.action = 'tier.move.refused'
           AND a.details->>'why' = %(held_back)s
           AND (a.details->>'at')::timestamptz > %(now)s - %(retry_after)s))
 ORDER BY version_id
 LIMIT %(batch)s
"""


async def sweep_versions(
    conn: psycopg.AsyncConnection, now: datetime, result: SweepResult, *,
    batch: int = 500, dataset_ids: list[int] | None = None,
) -> None:  # fmt: skip
    async with conn.transaction():
        cur = await conn.execute(
            VERSIONS_SQL,
            {
                "now": now,
                "batch": batch,
                "datasets": dataset_ids,
                "held_back": HELD_BACK,
                "retry_after": OWN_COPY_RETRY_AFTER,
            },
        )
        for version_id, file_id, location_id, desired in await cur.fetchall():
            await _send(
                conn,
                TIERING,
                {
                    "kind": "move",
                    "file_id": str(file_id),
                    "version_id": str(version_id),
                    "location_id": str(location_id),
                    "tier": desired,
                    "superseded": True,
                },
            )
            result.version_moves.append((version_id, desired))


async def sweep(
    conn: psycopg.AsyncConnection,
    now: datetime,
    *,
    batch: int = 500,
    dataset_ids: list[int] | None = None,
) -> SweepResult:
    """Evaluate every due file (up to `batch`), optionally only in some
    datasets. Each file is its own transaction, so one bad row doesn't hold
    back the rest."""
    result = SweepResult()
    async with conn.transaction():
        cur = await conn.execute(
            CANDIDATES_SQL,
            {
                "now": now,
                "batch": batch,
                "datasets": dataset_ids,
                "retry_after": OWN_COPY_RETRY_AFTER,
                "held_back": HELD_BACK,
            },
        )
        ids = [r[0] for r in await cur.fetchall()]
    for file_id in ids:
        try:
            async with conn.transaction():
                await _set_actor(conn)
                await evaluate_file(conn, file_id, now, result)
        except psycopg.Error as e:
            log.warning("tiering: file %s not evaluated: %s", file_id, e)
            result.skipped[file_id] = f"{type(e).__name__}: {e}"
    try:
        await sweep_versions(conn, now, result, batch=batch, dataset_ids=dataset_ids)
    except psycopg.Error as e:
        log.warning("tiering: superseded versions not swept: %s", e)
    return result


async def reschedule_dataset(conn: psycopg.AsyncConnection, dataset_id: int, now: datetime) -> int:
    """A new policy version: every live file is evaluated on the next sweep. A policy with
    no tiering in it takes nothing, so the tiering steps the last one left are cleared
    rather than left due for ever (`policy.overdue` reads them). A stub step stays."""
    cur = await conn.execute(
        "SELECT 1 FROM catalog.policy WHERE dataset_id = %s AND state = 'active'"
        " AND body ? 'archive_day'",
        (dataset_id,),
    )
    if await cur.fetchone() is None:
        cur = await conn.execute(
            "UPDATE catalog.file SET next_action = NULL, next_action_at = NULL"
            " WHERE dataset_id = %s AND kind = 'file'"
            " AND next_action IN ('archive', 'tier_warm', 'tier_cold')",
            (dataset_id,),
        )
        return cur.rowcount
    cur = await conn.execute(
        "UPDATE catalog.file SET next_action = coalesce(next_action, 'archive'),"
        " next_action_at = %s WHERE dataset_id = %s AND kind = 'file' AND deleted_at IS NULL",
        (now, dataset_id),
    )
    return cur.rowcount
