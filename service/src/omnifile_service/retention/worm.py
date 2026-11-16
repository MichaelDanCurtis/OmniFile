"""WORM per share, catalog side (decision 31).

- `enable_worm`: an ArchiveAdmin turns WORM on, typing the share's name to
  confirm. The share enters `unlocked_test` with a lock deadline at most 24 h
  away; the backend container policy is created unlocked.
- `cancel_worm_test`: inside the window only. Versions already committed
  keep the retention they were given (the catalog never shortens one).
- `lock_due_shares`: the auto-lock. When the window ends the share locks,
  versions already in it are given retention, and every retained copy is
  re-stamped as locked (compliance mode).
- `extend_worm_retention`: once locked, retention only grows.
- `set_legal_hold` / `clear_legal_hold`: per file or folder; versions under
  an active hold are held, including ones committed later (0006 trigger).

The database enforces the rules too (0003): a locked share can't be
unlocked, shortened, moved or deleted; a retained or held version can't be
pruned or deleted, nor can its primary or secondary copies. Every change is
audited with the actor.

Backend effects are queued on `retention` in the same transaction: the
container policy (`{"op": "worm_policy"}`); per-version retention, holds and
state tags are raised as `location.worm_sync_needed` by triggers and stamped
by the `worm_stamp` sweep (`retention/worm_sync.py`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ..worker.queue import send
from .common import RetentionError, set_actor
from .depth import RETENTION_QUEUE

log = logging.getLogger(__name__)

MAX_TEST_WINDOW = timedelta(hours=24)
DEFAULT_TEST_WINDOW = timedelta(hours=4)
AUTOLOCK_ACTOR = "system:worm_autolock"


async def _share(conn: psycopg.AsyncConnection, share_id: int) -> dict:
    cur = await conn.execute(
        "SELECT share_id, dataset_id, name, root_dir_id, worm_enabled, worm_mode,"
        " worm_retention_days, worm_lock_deadline, quiet_s FROM catalog.share"
        " WHERE share_id = %s FOR UPDATE",
        (share_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RetentionError(f"no share {share_id}")
    keys = ("share_id", "dataset_id", "name", "root_dir_id", "worm_enabled", "worm_mode",
            "worm_retention_days", "worm_lock_deadline", "quiet_s")  # fmt: skip
    return dict(zip(keys, row, strict=True))


#: Decision 67: a WORM share with no quiet period of its own waits this long, and
#: one set below it is asked for in so many words (#80 review).
QUIET_WORM_DEFAULT_S = 60


async def enable_worm(
    conn: psycopg.AsyncConnection,
    share_id: int,
    *,
    retention_days: int,
    confirmation: str,
    actor: str,
    now: datetime,
    test_window: timedelta = DEFAULT_TEST_WINDOW,
    quiet_confirmed: bool = False,
) -> datetime:
    """Turn WORM on. Returns the lock deadline.

    `confirmation` is what the admin typed; it must be the share's name. A share
    whose own quiet period is below the WORM default needs `quiet_confirmed`, as
    setting it on a WORM share does (decision 67, #80 review): otherwise setting
    it first and turning WORM on after would skip that question.
    """
    if retention_days <= 0:
        raise RetentionError("retention must be at least one day")
    if not timedelta(0) < test_window <= MAX_TEST_WINDOW:
        raise RetentionError("the validation window is more than 0 and at most 24 hours")
    async with conn.transaction():
        await set_actor(conn, actor)
        s = await _share(conn, share_id)
        if s["worm_enabled"]:
            raise RetentionError(f"share {s['name']}: WORM is already on ({s['worm_mode']})")
        if confirmation != s["name"]:
            raise RetentionError(
                f"type the share name {s['name']!r} to confirm; WORM can't be turned off once"
                " it locks"
            )
        short_quiet = s["quiet_s"] is not None and s["quiet_s"] < QUIET_WORM_DEFAULT_S
        if short_quiet and not quiet_confirmed:
            raise RetentionError(
                f"share {s['name']} waits only {s['quiet_s']} s before a file counts as finished,"
                f" under the WORM default of {QUIET_WORM_DEFAULT_S} s: a copy that pauses can leave"
                " a half-written version kept until retention ends. Confirm it, or set a longer"
                " period first"
            )
        deadline = now + test_window
        await conn.execute(
            "UPDATE catalog.share SET worm_enabled = true, worm_mode = 'unlocked_test',"
            " worm_retention_days = %s, worm_lock_deadline = %s, worm_enabled_at = %s,"
            " worm_confirmation = %s WHERE share_id = %s",
            (retention_days, deadline, now, confirmation, share_id),
        )
        await conn.execute(
            "SELECT catalog.audit('share.worm.confirm', %s, %s, NULL, NULL, %s)",
            (s["dataset_id"], share_id,
             Jsonb({"typed": confirmation, "lock_deadline": deadline.isoformat(),
                    "retention_days": retention_days, "one_way": True,
                    **({"quiet_s": s["quiet_s"], "quiet_confirmed_below_worm_default": True}
                       if short_quiet else {})})),
        )  # fmt: skip
        await _alert_cycling(conn, s, now)
        await send(conn, RETENTION_QUEUE, {"op": "worm_policy", "share_id": str(share_id)})
    return deadline


#: Enabling WORM again after it was cancelled this recently is flagged: a
#: share that keeps entering and leaving its validation window never locks.
CYCLE_WINDOW = timedelta(days=30)


async def _alert_cycling(conn: psycopg.AsyncConnection, s: dict, now: datetime) -> None:
    cur = await conn.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE share_id = %s"
        " AND action = 'share.worm.disable' AND at >= %s",
        (s["share_id"], now - CYCLE_WINDOW),
    )
    cancels = (await cur.fetchone())[0]  # type: ignore[index]
    if cancels:
        log.warning("share %s: WORM enabled again after %d cancellation(s) in %d days",
                    s["name"], cancels, CYCLE_WINDOW.days)  # fmt: skip
        await conn.execute(
            "SELECT catalog.audit('share.worm.cycling', %s, %s, NULL, NULL, %s)",
            (s["dataset_id"], s["share_id"],
             Jsonb({"cancellations": cancels, "window_days": CYCLE_WINDOW.days})),
        )  # fmt: skip


async def cancel_worm_test(
    conn: psycopg.AsyncConnection, share_id: int, *, actor: str, now: datetime
) -> None:
    """Turn WORM off during the validation window. Refused once locked."""
    async with conn.transaction():
        await set_actor(conn, actor)
        s = await _share(conn, share_id)
        if s["worm_mode"] != "unlocked_test":
            raise RetentionError(f"share {s['name']}: WORM is {s['worm_mode'] or 'off'}")
        await conn.execute(
            "UPDATE catalog.share SET worm_enabled = false, worm_mode = NULL,"
            " worm_lock_deadline = NULL WHERE share_id = %s",
            (share_id,),
        )
        await send(conn, RETENTION_QUEUE, {"op": "worm_policy", "share_id": str(share_id)})


async def extend_worm_retention(
    conn: psycopg.AsyncConnection, share_id: int, days: int, *, actor: str
) -> None:
    """Change retention days. Once locked, only upwards (also enforced by
    catalog.share_worm_guard)."""
    async with conn.transaction():
        await set_actor(conn, actor)
        s = await _share(conn, share_id)
        if not s["worm_enabled"]:
            raise RetentionError(f"share {s['name']}: WORM is off")
        if s["worm_mode"] == "locked" and days < s["worm_retention_days"]:
            raise RetentionError(
                f"share {s['name']}: locked retention can be extended, never shortened"
            )
        await conn.execute(
            "UPDATE catalog.share SET worm_retention_days = %s WHERE share_id = %s",
            (days, share_id),
        )
        # Decision (coordinator, M3.md): an extension reaches the versions
        # already in the share; the triggers re-stamp their backend copies.
        await conn.execute(
            _IN_SCOPE
            + """
            UPDATE catalog.file_version v
               SET retention_until = greatest(v.retention_until,
                                              v.committed_at + make_interval(days => %(days)s))
             WHERE v.dataset_id = %(ds)s AND v.state = 'committed'
               AND v.retention_until IS NOT NULL
               AND v.parent_id IN (SELECT file_id FROM tree)
               AND v.retention_until < v.committed_at + make_interval(days => %(days)s)
            """,
            {"root": s["root_dir_id"], "ds": s["dataset_id"], "days": days},
        )
        await send(conn, RETENTION_QUEUE, {"op": "worm_policy", "share_id": str(share_id)})


# Versions in a share's scope: the share root (or the whole dataset) and
# everything below it, by the folder each version was committed in.
_IN_SCOPE = """
WITH RECURSIVE tree(file_id) AS (
    SELECT coalesce(%(root)s::bigint,
                    (SELECT file_id FROM catalog.file
                      WHERE dataset_id = %(ds)s AND parent_id IS NULL))
    UNION
    SELECT f.file_id FROM catalog.file f JOIN tree t ON f.parent_id = t.file_id
     WHERE f.kind = 'dir'
)
"""


async def lock_due_shares(conn: psycopg.AsyncConnection, now: datetime) -> list[int]:
    """Lock every share whose validation window has ended. Returns their ids.

    A share with no recorded deadline locks 24 h after it was enabled (or
    created).
    """
    locked: list[int] = []
    async with conn.transaction():
        await set_actor(conn, AUTOLOCK_ACTOR)
        cur = await conn.execute(
            "SELECT share_id FROM catalog.share WHERE worm_mode = 'unlocked_test'"
            " AND coalesce(worm_lock_deadline, worm_enabled_at + interval '24 hours',"
            "              created_at + interval '24 hours') <= %s"
            " ORDER BY share_id FOR UPDATE SKIP LOCKED",
            (now,),
        )
        for (share_id,) in await cur.fetchall():
            s = await _share(conn, share_id)
            await conn.execute(
                "UPDATE catalog.share SET worm_mode = 'locked', worm_locked_at = %s"
                " WHERE share_id = %s",
                (now, share_id),
            )
            params = {"root": s["root_dir_id"], "ds": s["dataset_id"], "now": now,
                      "days": s["worm_retention_days"]}  # fmt: skip
            # Decision (M3.md): live versions, including those committed in
            # the validation window, are retained from the lock time;
            # superseded ones from their commit, if that still reaches the
            # future. Retention only ever grows (greatest).
            await conn.execute(
                _IN_SCOPE
                + """
                UPDATE catalog.file_version v
                   SET retention_until = greatest(v.retention_until, CASE
                         WHEN v.superseded_at IS NULL
                         THEN greatest(v.committed_at, %(now)s::timestamptz)
                              + make_interval(days => %(days)s)
                         ELSE v.committed_at + make_interval(days => %(days)s) END)
                 WHERE v.dataset_id = %(ds)s AND v.state = 'committed'
                   AND v.parent_id IN (SELECT file_id FROM tree)
                   AND (v.superseded_at IS NULL
                        OR v.committed_at + make_interval(days => %(days)s)
                           > %(now)s::timestamptz)
                   AND v.retention_until IS DISTINCT FROM greatest(v.retention_until, CASE
                         WHEN v.superseded_at IS NULL
                         THEN greatest(v.committed_at, %(now)s::timestamptz)
                              + make_interval(days => %(days)s)
                         ELSE v.committed_at + make_interval(days => %(days)s) END)
                """,
                params,
            )
            # Everything retained in scope is re-stamped locked.
            await conn.execute(
                _IN_SCOPE
                + """
                UPDATE catalog.location l SET worm_sync_needed = true
                  FROM catalog.file_version v
                 WHERE l.version_id = v.version_id AND v.dataset_id = %(ds)s
                   AND v.parent_id IN (SELECT file_id FROM tree)
                   AND v.retention_until IS NOT NULL
                   AND l.role IN ('primary', 'secondary') AND l.state = 'committed'
                   AND NOT l.worm_stamped_locked
                """,
                params,
            )
            await send(conn, RETENTION_QUEUE, {"op": "worm_policy", "share_id": str(share_id)})
            locked.append(share_id)
    return locked


_SUBTREE = """
WITH RECURSIVE tree(file_id) AS (
    SELECT %(file_id)s::bigint
    UNION
    SELECT f.file_id FROM catalog.file f JOIN tree t ON f.parent_id = t.file_id
)
"""


async def set_legal_hold(
    conn: psycopg.AsyncConnection,
    file_id: int,
    *,
    reason: str,
    actor: str,
    identity_id: int | None = None,
    app: str | None = None,
) -> int:
    """Put a file, or a folder and everything below it, on legal hold.
    Returns the hold id. Audited (hold.set, and version.legal_hold per
    version)."""
    if (identity_id is None) == (app is None):
        raise RetentionError("a legal hold is set by exactly one identity or application")
    async with conn.transaction():
        await set_actor(conn, actor)
        cur = await conn.execute(
            "SELECT dataset_id FROM catalog.file WHERE file_id = %s", (file_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise RetentionError(f"no file {file_id}")
        cur = await conn.execute(
            "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id,"
            " set_by_app) VALUES (%s, %s, 'legal', %s, %s, %s) RETURNING hold_id",
            (row[0], file_id, reason, identity_id, app),
        )
        hold_id = (await cur.fetchone())[0]  # type: ignore[index]
        await conn.execute(
            _SUBTREE + "UPDATE catalog.file_version SET legal_hold = true"
            " WHERE file_id IN (SELECT file_id FROM tree)"
            " AND state IN ('pending', 'committed') AND NOT legal_hold",
            {"file_id": file_id},
        )
    return hold_id


async def clear_legal_hold(
    conn: psycopg.AsyncConnection,
    hold_id: int,
    *,
    actor: str,
    identity_id: int,
    now: datetime,
) -> int:
    """Release a legal hold. Versions stay held while another active legal
    hold still covers them. Returns how many versions were released."""
    async with conn.transaction():
        await set_actor(conn, actor)
        cur = await conn.execute(
            "UPDATE catalog.hold SET released_at = %s, released_by_identity_id = %s"
            " WHERE hold_id = %s AND kind = 'legal' AND released_at IS NULL RETURNING file_id",
            (now, identity_id, hold_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise RetentionError(f"no active legal hold {hold_id}")
        cur = await conn.execute(
            _SUBTREE + "UPDATE catalog.file_version SET legal_hold = false"
            " WHERE file_id IN (SELECT file_id FROM tree) AND legal_hold"
            " AND NOT catalog.legal_hold_covers(file_id)",
            {"file_id": row[0]},
        )
        return cur.rowcount
