"""The `restores` consumer: start (or join) a provider restore for a recall,
then poll it until the bytes are readable.

The API inserts ``catalog.recall`` and sends ``{"recall_id"}`` on
``restores`` in the same transaction (decision 27). Here, against the
content version's primary location and its backend:

- **online** (not archived, or a live restored copy): the recall goes
  straight to ``downloading``; its feed event carries a read credential and
  the edge fills the file (control-api.md section 4.4);
- **archived:** start-or-join one ``catalog.restore`` per location
  (``restore_one_open``). Azure's restore is a Copy Blob rehydrate whose
  target is recorded as a ``recall_copy`` location; S3's is RestoreObject.
  The recall moves to ``rehydrating`` with the provider's ETA (none above
  10 GB on Azure);
- **polling:** `restore_status` until ``completed`` (recall ->
  ``downloading``) or ``failed`` (recall -> ``failed``). The message is
  re-queued with a delay between polls (`Retry`).

Each recall state change also sends ``{"recall_id"}`` on ``notify`` so the
Teams card follows. The Blob change feed is the bulk status stream; this
poll is for the one file a user is waiting on [A].

**No transaction id across a provider call.** One would stop the edge feed for
every edge until the call returned, so the recall's row is read without a lock,
the provider is called, and only then is the row locked and looked at again. A
second holder of the message, or a cancel, may have got there during the call:
the restore a call started is still recorded, because it is running, and a
later recall joins it.

**A provider's error is not the end of a recall.** It backs off and, after
`ConsumerConfig.max_failures`, dead-letters. `redrive.py` sends it again, with
longer waits, and in the end fails the recall so the user is told. A successful
answer starts that count again, so a long restore isn't failed for old errors.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from ..backends.base import (
    BackendKind,
    ObjectNotFound,
    ObjectRef,
    RestoreHandle,
    RestoreSpeed,
    RestoreState,
    RestoreStatus,
    StorageBackend,
    Throttled,
)
from .backends import BackendRegistry
from .queue import Message, Permanent, Retry, outside, send, throttled

log = logging.getLogger(__name__)

QUEUE = "restores"
NOTIFY = "notify"
ACTIVE = ("requested", "rehydrating", "copying")
#: A provider call that never returns must not hold its message for ever: past
#: this it is a failure like any other. A restore start and a status are moments.
CALL_TIMEOUT = 240.0
#: The `restores` consumer's visibility timeout: longer than that, so a live holder
#: isn't joined by a second one that would make every call again.
VISIBILITY_SECONDS = 300


@dataclass(frozen=True)
class RecallConfig:
    poll_min: timedelta = timedelta(minutes=1)
    poll_max: timedelta = timedelta(minutes=15)
    poll_no_eta: timedelta = timedelta(minutes=10)
    #: S3 restored-copy lifetime (`Days`) for user recalls.
    s3_days: int = 7
    #: A High recall held over the per-account budget is looked at again when
    #: there should be room; with this set, at least this often (dev and e2e,
    #: where virtual time moves faster than the queue's real delays).
    hold_recheck_max: timedelta | None = None


def _is_azure(backend: StorageBackend) -> bool:
    if backend.kind is BackendKind.AZURE_BLOB:
        return True
    if backend.kind is BackendKind.S3:
        return False
    speeds = {o.speed for o in backend.capabilities().restore_options}
    return RestoreSpeed.HIGH in speeds  # sim: Azure flavour offers High


def _speed(backend: StorageBackend, priority: str) -> RestoreSpeed:
    offered = {o.speed for o in backend.capabilities().restore_options}
    if priority == "high" and RestoreSpeed.HIGH in offered:
        return RestoreSpeed.HIGH
    if RestoreSpeed.STANDARD in offered:
        return RestoreSpeed.STANDARD
    return sorted(offered)[0]


def _poll_delay(cfg: RecallConfig, eta: datetime | None, now: datetime) -> float:
    if eta is None:
        return cfg.poll_no_eta.total_seconds()
    left = (eta - now) / 4
    return max(cfg.poll_min, min(cfg.poll_max, left)).total_seconds()


#: No lock: a row lock takes a transaction id, and a provider call follows.
RECALL_SQL = """
SELECT r.recall_id, r.state, r.priority, r.restore_id, r.file_id, r.version_id,
       l.location_id, l.backend_id, l.container, l.object_key, l.backend_version_id, l.size
  FROM catalog.recall r
  LEFT JOIN catalog.location l
         ON l.version_id = r.version_id AND l.role = 'primary' AND l.state = 'committed'
 WHERE r.recall_id = %s
"""

LOCK_RECALL_SQL = """
SELECT state, restore_id FROM catalog.recall WHERE recall_id = %s FOR UPDATE
"""

RESTORE_SQL = """
SELECT s.restore_id, s.state, s.speed, s.provider_ref, s.requested_at, s.days, s.eta,
       t.container, t.object_key, t.backend_version_id, s.target_location_id
  FROM catalog.restore s
  LEFT JOIN catalog.location t ON t.location_id = s.target_location_id
 WHERE s.restore_id = %s
"""


async def _set_recall(
    conn: psycopg.AsyncConnection,
    recall_id: int,
    state: str,
    *,
    restore_id: int | None = None,
    eta: datetime | None = None,
    failure: str | None = None,
    now: datetime,
) -> None:
    await conn.execute(
        """
        UPDATE catalog.recall
           SET state = %(state)s,
               restore_id = coalesce(%(restore_id)s, restore_id),
               eta = %(eta)s,
               failure = %(failure)s,
               completed_at = CASE WHEN %(state)s IN ('failed', 'available')
                                   THEN %(now)s ELSE completed_at END,
               updated_at = %(now)s
         WHERE recall_id = %(recall_id)s
           AND (state, restore_id, eta, failure)
               IS DISTINCT FROM (%(state)s, coalesce(%(restore_id)s, restore_id), %(eta)s,
                                 %(failure)s)
        """,
        {
            "state": state,
            "restore_id": restore_id,
            "eta": eta,
            "failure": failure,
            "now": now,
            "recall_id": recall_id,
        },
    )
    # A placement of an annexed file (annex.md section 4.6) waits on its
    # recall: ready for its writer once the bytes are readable, failed with it.
    if state == "downloading":
        cur = await conn.execute(
            "UPDATE catalog.annex_placement SET state = 'ready', updated_at = %s"
            " WHERE recall_id = %s AND state = 'rehydrating'"
            " RETURNING placement_id, target->>'kind' AS kind",
            (now, recall_id),
        )
        for pid, kind in await cur.fetchall():
            if kind == "share":
                # Into a share: the worker places it (share_placements.py, A3.2).
                await send(conn, "portal", {"kind": "annex_placement", "placement_id": str(pid)})
    elif state == "failed":
        await conn.execute(
            "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = 'recall_failed',"
            " fail_detail = left(%s, 500), done_at = %s, updated_at = %s"
            " WHERE recall_id = %s AND state IN ('requested', 'rehydrating')",
            (failure or "the recall failed", now, now, recall_id),
        )


async def _notify(conn: psycopg.AsyncConnection, recall_id: int) -> None:
    await send(conn, NOTIFY, {"recall_id": str(recall_id)})


async def _lock_recall(conn: psycopg.AsyncConnection, recall_id: int) -> tuple[str, int | None]:
    """The recall's state and restore, once its row is ours: what a provider call's
    result is written against."""
    cur = await conn.execute(LOCK_RECALL_SQL, (recall_id,))
    row = await cur.fetchone()
    if row is None:
        raise Permanent(f"no recall {recall_id}")
    return row[0], row[1]


async def _gone(conn: psycopg.AsyncConnection, recall_id: int, now: datetime) -> None:
    """The cloud copy isn't there. No retry can help, so the user hears now."""
    state, _ = await _lock_recall(conn, recall_id)
    if state in ACTIVE:
        await _set_recall(conn, recall_id, "failed", failure="no committed cloud copy", now=now)
        await _notify(conn, recall_id)


def _keep_polling(recall_id: int, delay: float) -> Retry:
    """Look again later. The provider answered, so the message carries no count of
    earlier errors (`redrive.py`)."""
    return Retry(delay, {"recall_id": str(recall_id)})


async def handle_restore(
    msg: Message,
    conn: psycopg.AsyncConnection,
    *,
    registry: BackendRegistry,
    now: datetime,
    config: RecallConfig | None = None,
    in_place: bool = False,
) -> None:
    """`in_place`: make the provider calls in this transaction instead of `outside`
    it. For a High recall holding the budget's lock, which must last until its
    restore is recorded. The transaction holds no id until then."""
    cfg = config or RecallConfig()
    try:
        recall_id = int(msg.body["recall_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise Permanent(f"malformed restores message {dict(msg.body)!r}: {e}") from None
    cur = await conn.execute(RECALL_SQL, (recall_id,))
    row = await cur.fetchone()
    if row is None:
        raise Permanent(f"no recall {recall_id}")
    (_, state, priority, restore_id, file_id, version_id, location_id, backend_id, container,
     key, backend_version, size) = row  # fmt: skip
    # Unless a restore was started for this message and the recall was cancelled
    # during the call: `_start` puts that on record, since the provider is running it.
    started = msg.called.get("start")
    unrecorded = started is not None and started.error is None and restore_id is None
    if state not in ACTIVE and not unrecorded:
        return  # downloading, available, failed or cancelled: nothing to do here
    if location_id is None:
        await _gone(conn, recall_id, now)
        return
    backend = registry.get(backend_id)
    ref = ObjectRef(backend_id, container, key, backend_version)
    via = None if in_place else msg
    try:
        if restore_id is None:
            await _start(conn, backend, ref, cfg, now, recall_id, priority, location_id,
                         file_id, version_id, size, via)  # fmt: skip
            return
        await _poll(conn, backend, ref, cfg, now, recall_id, restore_id, via)
    except Throttled as e:
        # The provider asked for time (S3's 1,000 restores a second is shared by users
        # and waves), so a burst uses up no attempt. A gateway's 503 looks the same, so
        # there is a limit.
        raise throttled(msg, e, e.retry_after) from None
    except ObjectNotFound:
        await _gone(conn, recall_id, now)


async def _start(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    ref: ObjectRef,
    cfg: RecallConfig,
    now: datetime,
    recall_id: int,
    priority: str,
    location_id: int,
    file_id: int,
    version_id: int,
    size: int | None,
    via: Message | None,
) -> None:
    # Join a restore already running for this location (start-or-join).
    cur = await conn.execute(OPEN_RESTORE_SQL, (location_id,))
    if await _join(conn, cfg, now, recall_id, await cur.fetchone()):
        return
    speed = _speed(backend, priority)
    azure = _is_azure(backend)
    days = None if azure else cfg.s3_days

    async def call() -> tuple[RestoreHandle, RestoreStatus] | None:
        head = await backend.head(ref)
        if head.restore_state in (RestoreState.NOT_NEEDED, RestoreState.COMPLETED):
            return None
        handle = await backend.restore(ref, speed, days)
        return handle, await backend.restore_status(handle)

    started = await outside(via, "start", lambda: asyncio.wait_for(call(), CALL_TIMEOUT))
    state, restore_now = await _lock_recall(conn, recall_id)
    if started is None:
        if state in ACTIVE:
            await _set_recall(conn, recall_id, "downloading", now=now)
            await _notify(conn, recall_id)
        return
    handle, status = started
    if restore_now is not None:
        if state in ACTIVE:
            raise _keep_polling(recall_id, 0)  # a second holder recorded it first
        return
    # A wave may have opened one during the call, and it stands.
    cur = await conn.execute(OPEN_RESTORE_SQL, (location_id,))
    if await _join(conn, cfg, now, recall_id, await cur.fetchone(), state=state):
        return
    target_location = None
    if handle.target is not None:
        cur = await conn.execute(
            """
            INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,
                                          backend_version_id, role, state, logical_tier,
                                          storage_class, size)
            VALUES (%s, %s, %s, %s, %s, %s, 'recall_copy', 'pending', 'hot', 'Hot', %s)
            RETURNING location_id
            """,
            (file_id, version_id, ref.backend_id, handle.target.container, handle.target.key,
             handle.target.version_id, size),
        )  # fmt: skip
        target_location = (await cur.fetchone())[0]  # type: ignore[index]
    cur = await conn.execute(
        """
        INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, provider_ref,
                                     target_location_id, days, state, triggered_by, bytes,
                                     requested_at, eta, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'in_progress', 'user_recall', %s, %s, %s, %s)
        RETURNING restore_id
        """,
        (location_id, ref.backend_id, "azure_copy_rehydrate" if azure else "s3_restore_object",
         str(speed), handle.provider_ref, target_location, handle.days, size,
         handle.requested_at, status.eta, now),
    )  # fmt: skip
    restore_id = (await cur.fetchone())[0]  # type: ignore[index]
    if not azure:
        await _mark_migrating(conn, restore_id, ref)
    log.info("recall %s: restore %s started (%s, %s)", recall_id, restore_id, speed, ref.key)
    if state not in ACTIVE:
        # Cancelled during the call. The restore is running, so it is on record,
        # and a later recall of this file joins it.
        return
    await _set_recall(
        conn, recall_id, "rehydrating", restore_id=restore_id, eta=status.eta, now=now
    )
    await _notify(conn, recall_id)
    if status.state is RestoreState.COMPLETED:
        await _poll(conn, backend, ref, cfg, now, recall_id, restore_id, None, status=status)
        return
    raise _keep_polling(recall_id, _poll_delay(cfg, status.eta, now))


OPEN_RESTORE_SQL = """
SELECT restore_id, eta FROM catalog.restore
 WHERE location_id = %s AND state IN ('requested', 'in_progress')
"""


async def _join(
    conn: psycopg.AsyncConnection,
    cfg: RecallConfig,
    now: datetime,
    recall_id: int,
    open_row: tuple | None,
    *,
    state: str | None = None,
) -> bool:
    """Join an open restore, if there is one. `state`: the recall's, if its row is
    already locked. Raises `Retry` to poll it."""
    if open_row is None:
        return False
    if state is None:
        state, _ = await _lock_recall(conn, recall_id)
    if state not in ACTIVE:
        return True
    await _set_recall(
        conn, recall_id, "rehydrating", restore_id=open_row[0], eta=open_row[1], now=now
    )
    await _notify(conn, recall_id)
    raise _keep_polling(recall_id, _poll_delay(cfg, open_row[1], now))


async def _mark_migrating(conn: psycopg.AsyncConnection, restore_id: int, ref: ObjectRef) -> None:
    """A restore of an object under a prefix being migrated serves the move
    too (decision 15): the engine stages it rather than waiting for the
    prefix's next run (migrate/staging.py)."""
    await conn.execute(
        """
        UPDATE catalog.restore SET migrate_on_complete = true
         WHERE restore_id = %s AND EXISTS (
               SELECT 1 FROM catalog.migration_prefix mp
                 JOIN catalog.source s USING (source_id)
                WHERE s.backend_id = %s AND s.container = %s
                  AND starts_with(%s, mp.prefix) AND mp.copy_by = 'omnifile'
                  AND mp.state NOT IN ('cut_over', 'failed'))
        """,
        (restore_id, ref.backend_id, ref.container, ref.key),
    )


async def _poll(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    ref: ObjectRef,
    cfg: RecallConfig,
    now: datetime,
    recall_id: int,
    restore_id: int,
    via: Message | None,
    *,
    status: RestoreStatus | None = None,
) -> None:
    """`status`: what the provider has just said, if the caller has asked already."""
    cur = await conn.execute(RESTORE_SQL, (restore_id,))
    row = await cur.fetchone()
    if row is None:
        raise Permanent(f"recall {recall_id}: restore {restore_id} is gone")
    (_, rstate, speed, provider_ref, requested_at, days, eta, t_container, t_key, t_version,
     target_location) = row  # fmt: skip
    if rstate in ("failed", "cancelled", "expired"):
        state, _ = await _lock_recall(conn, recall_id)
        if state in ACTIVE:
            await _set_recall(conn, recall_id, "failed", failure=f"restore {rstate}", now=now)
            await _notify(conn, recall_id)
        return
    if rstate != "completed":
        target = ObjectRef(ref.backend_id, t_container, t_key, t_version) if t_container else None
        handle = RestoreHandle(
            ref=ref,
            speed=RestoreSpeed(speed),
            provider_ref=provider_ref or "",
            requested_at=requested_at,
            days=days,
            target=target,
        )
        if status is None:
            status = await outside(
                via,
                "status",
                lambda: asyncio.wait_for(backend.restore_status(handle), CALL_TIMEOUT),
            )
        state, _ = await _lock_recall(conn, recall_id)
        if state not in ACTIVE:
            return  # finished or cancelled during the call
        if status.state is RestoreState.IN_PROGRESS:
            await conn.execute(
                "UPDATE catalog.restore SET eta = %s, updated_at = %s"
                " WHERE restore_id = %s AND eta IS DISTINCT FROM %s",
                (status.eta, now, restore_id, status.eta),
            )
            await _set_recall(conn, recall_id, "rehydrating", eta=status.eta, now=now)
            raise _keep_polling(recall_id, _poll_delay(cfg, status.eta, now))
        if status.state is not RestoreState.COMPLETED:
            detail = status.detail or str(status.state)
            await conn.execute(
                "UPDATE catalog.restore SET state = 'failed', failure = %s, updated_at = %s"
                " WHERE restore_id = %s",
                (detail, now, restore_id),
            )
            await _set_recall(conn, recall_id, "failed", failure=detail, now=now)
            await _notify(conn, recall_id)
            return
        await conn.execute(
            "UPDATE catalog.restore SET state = 'completed', completed_at = %s,"
            " expires_at = %s, updated_at = %s WHERE restore_id = %s",
            (now, status.expires_at, now, restore_id),
        )
        if target_location is not None:
            await conn.execute(
                "UPDATE catalog.location SET state = 'committed', committed_at = %s"
                " WHERE location_id = %s AND state = 'pending'",
                (now, target_location),
            )
    else:
        state, _ = await _lock_recall(conn, recall_id)
        if state not in ACTIVE:
            return
    await _set_recall(conn, recall_id, "downloading", now=now)
    await _notify(conn, recall_id)
