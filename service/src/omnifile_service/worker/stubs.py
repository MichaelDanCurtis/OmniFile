"""M1's trivial policy, "stub after N minutes", and the `edge_instructions`
handler that turns a stub instruction into desired state.

Flow (control-api.md section 4.3):

1. `sweep_stub_candidates` (periodic) finds committed files whose content
   version is uploaded and verified, and older than N minutes, and enqueues
   ``{"kind": "stub", "file_id", "version_id"}`` on ``edge_instructions``.
2. `handle_edge_instruction` HEADs the blob through the dataset's backend
   (the bytes must really be in the cloud, with the size the catalog
   recorded) and then sets ``file.stub_version_id`` /
   ``file.stub_resident_bytes``. That change reaches the edge through the
   feed (``file.upsert`` carries ``stub``).
3. The edge runs the stub handshake (``stub/prepare`` -> punch ->
   ``stub/commit``) with the API; the API re-checks everything in one
   transaction, so this worker never decides alone that bytes may go.

Per-dataset overrides come from the active policy body: ``stub_after_minutes``
(M1), ``resident_mb``, and ``stub_at = never`` to turn stubbing off.

The sweep is M1's policy only. A dataset whose active policy is an M3
tiering policy (it has an ``archive_day``) is left to the tiering scheduler
(`policy.scheduler`), which stubs per ``stub_at`` (``archive`` or
``leaves_hot``) through the same ``edge_instructions`` handler.

Resident region: SEG-Y's 3,600 header bytes plus 1 MiB of readahead
(BUILD-PLAN M1: "resident region = headers + 1 MiB").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from ..backends.base import ObjectNotFound, ObjectRef
from .backends import BackendRegistry
from .queue import Message, Permanent, outside

log = logging.getLogger(__name__)

QUEUE = "edge_instructions"
SEGY_HEADER_BYTES = 3200 + 400
DEFAULT_RESIDENT_BYTES = SEGY_HEADER_BYTES + 1024 * 1024
MiB = 1024 * 1024


@dataclass(frozen=True)
class StubPolicy:
    after: timedelta = timedelta(minutes=60)
    resident_bytes: int = DEFAULT_RESIDENT_BYTES
    #: When the cloud copy doesn't match the catalog, look again after this.
    mismatch_backoff: timedelta = timedelta(hours=24)
    batch: int = 500


_RESIDENT_SQL = """
    coalesce(round((p.body->>'resident_mb')::numeric * %(mib)s)::bigint, %(resident)s::bigint)
"""

SWEEP_SQL = f"""
WITH due AS (
    SELECT f.file_id, f.content_version_id AS version_id
      FROM catalog.file f
      JOIN catalog.file_version v ON v.version_id = f.content_version_id
      JOIN catalog.location l ON l.location_id = f.primary_location_id
      LEFT JOIN catalog.policy p ON p.dataset_id = f.dataset_id AND p.state = 'active'
     WHERE f.kind = 'file'
       AND f.state = 'resident'
       AND f.deleted_at IS NULL
       AND f.stub_version_id IS NULL
       AND l.version_id = f.content_version_id
       AND l.state = 'committed' AND l.verified
       AND coalesce(p.body->>'stub_at', '') <> 'never'
       -- An M3 tiering policy (it has an archive day) stubs through the
       -- scheduler, per its stub_at (policy.engine); this sweep is M1's.
       AND NOT coalesce(p.body ? 'archive_day', false)
       AND f.size > {_RESIDENT_SQL}
       AND v.committed_at <= %(now)s::timestamptz - make_interval(
               mins => coalesce((p.body->>'stub_after_minutes')::int, %(after_min)s::int))
       AND (f.next_action IS DISTINCT FROM 'stub' OR f.next_action_at <= %(now)s::timestamptz)
       -- keep_online on the file or any folder above it
       AND NOT EXISTS (
           WITH RECURSIVE up (id) AS (
               SELECT f.file_id
               UNION ALL
               SELECT a.parent_id FROM catalog.file a JOIN up ON a.file_id = up.id
                WHERE a.parent_id IS NOT NULL)
           SELECT 1 FROM catalog.hold h JOIN up ON h.file_id = up.id
            WHERE h.kind = 'keep_online' AND h.released_at IS NULL
              AND (h.until IS NULL OR h.until > %(now)s::timestamptz))
       -- not already queued
       AND NOT EXISTS (
           SELECT 1 FROM pgmq.q_edge_instructions q
            WHERE q.message->>'kind' = 'stub'
              AND q.message->>'file_id' = f.file_id::text)
     ORDER BY v.committed_at, f.file_id
     LIMIT %(batch)s
)
SELECT due.file_id,
       pgmq.send('edge_instructions'::text,
                 jsonb_build_object('kind', 'stub', 'file_id', due.file_id::text,
                                    'version_id', due.version_id::text))
  FROM due
"""


async def sweep_stub_candidates(
    conn: psycopg.AsyncConnection, now: datetime, policy: StubPolicy | None = None
) -> list[int]:
    """Enqueue stub instructions for every due file. Returns their file ids."""
    policy = policy or StubPolicy()
    params = {
        "now": now,
        "after_min": max(0, int(policy.after.total_seconds() // 60)),
        "resident": policy.resident_bytes,
        "mib": MiB,
        "batch": policy.batch,
    }
    async with conn.transaction():
        cur = await conn.execute(SWEEP_SQL, params)
        rows = await cur.fetchall()
    return [r[0] for r in rows]


INFO_SQL = f"""
SELECT f.dataset_id, f.size, f.state, f.content_version_id, f.stub_version_id, f.deleted_at,
       l.backend_id, l.container, l.object_key, l.backend_version_id, l.verified, l.state,
       l.version_id, {_RESIDENT_SQL} AS resident
  FROM catalog.file f
  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
  LEFT JOIN catalog.policy p ON p.dataset_id = f.dataset_id AND p.state = 'active'
 WHERE f.file_id = %(file_id)s
"""


def _ids(msg: Message) -> tuple[int, int]:
    try:
        return int(msg.body["file_id"]), int(msg.body["version_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise Permanent(f"malformed stub instruction {dict(msg.body)!r}: {e}") from None


async def handle_edge_instruction(
    msg: Message,
    conn: psycopg.AsyncConnection,
    *,
    registry: BackendRegistry,
    now: datetime,
    policy: StubPolicy | None = None,
) -> None:
    policy = policy or StubPolicy()
    kind = msg.body.get("kind")
    if kind != "stub":
        raise Permanent(f"unknown edge instruction kind {kind!r}")
    file_id, version_id = _ids(msg)
    cur = await conn.execute(
        INFO_SQL, {"file_id": file_id, "resident": policy.resident_bytes, "mib": MiB}
    )
    row = await cur.fetchone()
    if row is None:
        log.info("stub %s: no such file", file_id)
        return
    (
        _dataset_id,
        size,
        state,
        content_version,
        stub_version,
        deleted_at,
        backend_id,
        container,
        key,
        backend_version,
        verified,
        loc_state,
        loc_version,
        resident,
    ) = row
    if (
        content_version != version_id
        or state != "resident"
        or deleted_at is not None
        or stub_version is not None
    ):
        log.info("stub %s@%s: no longer applicable (state %s)", file_id, version_id, state)
        return
    if backend_id is None or not verified or loc_state != "committed" or loc_version != version_id:
        log.info("stub %s@%s: primary location not verified yet", file_id, version_id)
        return
    backend = registry.get(backend_id)
    ref = ObjectRef(backend_id, container, key, backend_version)
    try:
        head = await outside(msg, "head", lambda: backend.head(ref))
    except ObjectNotFound:
        head = None
    if head is None or head.size != size:
        found = "missing" if head is None else f"{head.size} bytes"
        log.error(
            "stub %s@%s refused: cloud copy %s/%s is %s, catalog says %s bytes",
            file_id,
            version_id,
            container,
            key,
            found,
            size,
        )
        await conn.execute(
            "UPDATE catalog.file SET next_action = 'stub', next_action_at = %s"
            " WHERE file_id = %s AND (next_action IS NULL OR next_action = 'stub')",
            (now + policy.mismatch_backoff, file_id),
        )
        return
    resident_bytes = min(int(resident), max(0, size - 1))
    await conn.execute(
        """
        UPDATE catalog.file
           SET stub_version_id = %(version_id)s,
               stub_resident_bytes = %(resident)s,
               next_action = CASE WHEN next_action = 'stub' THEN NULL ELSE next_action END,
               next_action_at = CASE WHEN next_action = 'stub' THEN NULL ELSE next_action_at END
         WHERE file_id = %(file_id)s
           AND content_version_id = %(version_id)s
           AND state = 'resident'
           AND stub_version_id IS NULL
           AND deleted_at IS NULL
        """,
        {"file_id": file_id, "version_id": version_id, "resident": resident_bytes},
    )
