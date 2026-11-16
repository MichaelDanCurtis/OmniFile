"""Dry runs' lifetime (annex.md section 4.5; control API section 4.8).

A run is `requested` until the edge takes it (`running`, `taken_at`), then
`done` or `failed`. A run the edge never takes, or never answers, fails by
itself, so nothing waits on it for ever and the throttle frees up:

- `requested` for more than `TAKE_WITHIN` (the edge looks every 2 seconds);
- `running` for more than `ANSWER_WITHIN` (the edge gives up on a preview
  after 2 minutes).

A newer draft from the same person replaces their older ones still waiting
or running: the builder asks again on every edit, and only the latest
counts are wanted.
"""

from __future__ import annotations

from datetime import timedelta

TAKE_WITHIN = timedelta(minutes=10)
ANSWER_WITHIN = timedelta(minutes=15)
#: Runs waiting for the edge, per annex, before a new one is refused.
MAX_WAITING = 20


async def expire(db, annex_id: int | None = None) -> int:
    """Fail the runs nobody took or answered in time. Returns how many."""
    cur = await db.execute(
        "UPDATE catalog.annex_dry_run SET state = 'failed', done_at = now(),"
        " error = CASE state WHEN 'requested' THEN 'the edge did not take it in time'"
        "                    ELSE 'the edge did not answer in time' END"
        " WHERE ((state = 'requested' AND requested_at < now() - %s)"
        "     OR (state = 'running' AND taken_at < now() - %s))"
        "   AND (%s::bigint IS NULL OR annex_id = %s::bigint)",
        (TAKE_WITHIN, ANSWER_WITHIN, annex_id, annex_id),
    )
    return cur.rowcount or 0


async def waiting(db, annex_id: int) -> int:
    row = await (
        await db.execute(
            "SELECT count(*) AS n FROM catalog.annex_dry_run WHERE annex_id = %s"
            " AND state IN ('requested', 'running')",
            (annex_id,),
        )
    ).fetchone()
    return int(row["n"])


async def supersede_drafts(db, annex_id: int, requested_by: str) -> int:
    """The person's older drafts, waiting or running, give way to a new one."""
    cur = await db.execute(
        "UPDATE catalog.annex_dry_run SET state = 'failed', done_at = now(),"
        " error = 'replaced by a newer draft'"
        " WHERE annex_id = %s AND requested_by = %s AND ruleset_version IS NULL"
        "   AND state IN ('requested', 'running')",
        (annex_id, requested_by),
    )
    return cur.rowcount or 0


__all__ = ["ANSWER_WITHIN", "MAX_WAITING", "TAKE_WITHIN", "expire", "supersede_drafts", "waiting"]
