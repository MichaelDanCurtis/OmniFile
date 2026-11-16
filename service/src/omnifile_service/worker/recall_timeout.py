"""Fail a `downloading` recall whose edge has stopped working on it.

A recall reaches `downloading` once its bytes are readable; the edge then
fills the file and reports `filled` (control-api.md section 4.4). The edge
itself resumes an interrupted fill and reports one that cannot succeed
(#52). If the edge is gone for good, nothing would ever end the recall, and
every later read would join it. So, once a minute, the worker fails a
`downloading` recall of a file's current content when its edge:

- **is silent:** no heartbeat (or, before its first, no contact) for
  ``silent``, counted only within the service's current *reachable window*:
  the time since heartbeats last started arriving from the edges with no gap
  over ``WINDOW_GAP`` (migration 0040's ``catalog.heartbeat_window``, kept by
  every heartbeat). An outage of the service, or of the edges' way to it
  (its ingress, allow-list or certificates, or the api losing the catalog),
  stops every heartbeat at once, ends the window, and fails nothing; after
  it, an edge has the whole limit again, from the window's new start. A site
  with a single edge cannot tell that edge being gone from such an outage,
  so its recalls are never failed for silence: only by the idle rule while
  the edge still heartbeats;
- **is alive but not filling:** it heartbeats now and reports ``fills_running``,
  but has reported none running for ``no_fill`` (migration 0039's
  ``catalog.edge.last_fill_at``), and it has caught up on its feed (its last
  heartbeat's ``feed_lag_events`` is 0: an edge still reading its feed may
  not have seen the recall reach `downloading` yet).

Both count from when the recall reached `downloading` as well, so a recall
is never failed for silence or idleness before it.

Only a recall of the file's current content: that is what an edge fills. A
recall of an older version (one a portal version restore made, closed by
the restore itself, or one the file has moved past since) or one an edge
fills into a rewind placeholder is not judged here; the service does not
track those fills.

Never on elapsed time: a fill of a very large file (a terabyte over a slow
link, or one rehydrated at the 10 GiB/h priority cap first) takes as long as
it takes. While the edge heartbeats with a fill running, its recalls stay
`downloading`. The idle rule is per edge, not per recall: an edge filling
one file keeps all its recalls alive. That is conservative on purpose; the
edge reports a fill of its own that fails (#52).

The recall becomes `failed`, with a reason that starts ``edge unreachable:``
(silent) or ``edge not filling:`` (idle). Users are told through the usual
card path (``notify``), and the card names the file server, not the storage
service (`notify/cards.py`). The feed carries `failed` to the edge, which
stops any fill of that recall it is still running (`omnifile-edge`
`core.rs`, a recall row that is `failed`). If the edge has the bytes after
all, its ``filled`` report still makes the recall `available`
(`api/recalls.recall_filled`). All times are the database's, as the
heartbeat's are.

``OMNIFILE_RECALL_EDGE_SILENT_MINUTES`` (default 60) and
``OMNIFILE_RECALL_NO_FILL_MINUTES`` (default 60) set the two limits; 0
turns that rule off.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from .jobs import Job, SweepJob

if TYPE_CHECKING:
    from .runner import Worker

log = logging.getLogger(__name__)

EVERY = timedelta(minutes=1)
NOTIFY = "notify"
#: The reasons this job records start with one of these (cards and `filled`
#: recognise them).
SILENT_PREFIX = "edge unreachable:"
NO_FILL_PREFIX = "edge not filling:"
REASON_PREFIX = SILENT_PREFIX  # the silent rule's, as before
#: Heartbeats arriving with no gap longer than this keep the service's
#: reachable window open (migration 0040); edges heartbeat every minute.
WINDOW_GAP = timedelta(minutes=3)


@dataclass(frozen=True)
class EdgeTimeoutConfig:
    #: No heartbeat for this long: the edge is gone. None: off.
    silent: timedelta | None = timedelta(minutes=60)
    #: Heartbeats, but no fill running for this long. None: off.
    no_fill: timedelta | None = timedelta(minutes=60)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> EdgeTimeoutConfig:
        def minutes(name: str, default: int) -> timedelta | None:
            raw = env.get(name)
            n = default if raw in (None, "") else int(raw)
            if n < 0:
                raise ValueError(f"{name} must be 0 (off) or a number of minutes")
            return timedelta(minutes=n) if n else None

        return cls(
            silent=minutes("OMNIFILE_RECALL_EDGE_SILENT_MINUTES", 60),
            no_fill=minutes("OMNIFILE_RECALL_NO_FILL_MINUTES", 60),
        )


# One row per `downloading` recall of a file's current content whose edge
# has stopped working on it, and which rule matched (`why`). `since`: when
# the recall reached `downloading` (its state log).
CANDIDATES = """
WITH r AS (
    SELECT r.recall_id, r.file_id, r.version_id, f.dataset_id, e.edge_id,
           coalesce(e.last_heartbeat_at, e.last_seen_at) AS heard_at,
           e.last_fill_at,
           e.health ? 'fills_running' AS reports_fills,
           coalesce((e.health->>'feed_lag_events')::bigint, 0) AS feed_lag,
           coalesce((SELECT max(l.at) FROM catalog.recall_state_log l
                      WHERE l.recall_id = r.recall_id AND l.state = 'downloading'),
                    r.updated_at) AS since
      FROM catalog.recall r
      JOIN catalog.file f ON f.file_id = r.file_id
      JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
      JOIN catalog.edge e ON e.edge_id = coalesce(r.edge_id, d.edge_id)
     WHERE r.state = 'downloading'
       AND r.version_id = f.content_version_id
),
-- The start of the service's current reachable window: heartbeats from the
-- edges with no gap over WINDOW_GAP since then (NULL: none arriving now).
win AS (
    SELECT max(started_at) AS start FROM catalog.heartbeat_window
     WHERE last_at >= now() - %(fresh)s::interval
),
m AS (
    SELECT r.*,
           (%(silent)s::interval IS NOT NULL
            AND win.start IS NOT NULL
            AND greatest(coalesce(heard_at, '-infinity'), since, win.start)
                < now() - %(silent)s::interval) AS silent,
           (%(no_fill)s::interval IS NOT NULL
            AND reports_fills
            AND feed_lag = 0
            -- Heartbeating now: an edge (or a service) gone quiet is the
            -- silent rule's to judge, not this one's.
            AND coalesce(heard_at, '-infinity') >= now() - %(fresh)s::interval
            AND win.start IS NOT NULL
            AND greatest(since, coalesce(last_fill_at, '-infinity'), win.start)
                < now() - %(no_fill)s::interval) AS idle
      FROM r, win
)
SELECT m.*, CASE WHEN silent THEN 'silent' ELSE 'no_fill' END AS why
  FROM m
 WHERE silent OR idle
 ORDER BY recall_id
"""


def _minutes(t: timedelta | None) -> int:
    return int(t.total_seconds() // 60) if t is not None else 0


async def candidates(
    conn: psycopg.AsyncConnection, config: EdgeTimeoutConfig
) -> list[dict[str, Any]]:
    """The recalls `fail_unreachable` would fail now, with `why`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            CANDIDATES,
            {"silent": config.silent, "no_fill": config.no_fill, "fresh": WINDOW_GAP},
        )
        return await cur.fetchall()


def reason(row: dict[str, Any], config: EdgeTimeoutConfig) -> str:
    if row["why"] == "silent":
        return (
            f"{SILENT_PREFIX} edge {row['edge_id']} has not been heard from for over"
            f" {_minutes(config.silent)} minutes"
        )
    return (
        f"{NO_FILL_PREFIX} edge {row['edge_id']} has reported no fill running for over"
        f" {_minutes(config.no_fill)} minutes"
    )


async def fail_one(
    conn: psycopg.AsyncConnection, row: dict[str, Any], config: EdgeTimeoutConfig
) -> bool:
    """Fail one candidate, if it is still `downloading` (a `filled`, a cancel
    or a restore may have come in since `candidates`). True if it was."""
    recall_id, why = row["recall_id"], reason(row, config)
    async with conn.transaction():
        done = await (
            await conn.execute(
                "UPDATE catalog.recall SET state = 'failed', failure = %s,"
                " completed_at = now(), updated_at = now()"
                " WHERE recall_id = %s AND state = 'downloading' RETURNING recall_id",
                (why, recall_id),
            )
        ).fetchone()
        if done is None:
            return False
        await conn.execute(
            "SELECT pgmq.send(%s, %s::jsonb)",
            (NOTIFY, json.dumps({"recall_id": str(recall_id)})),
        )
        await conn.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id,"
            " version_id, details)"
            " VALUES ('system:worker', 'recall.edge_timeout', 'info', %s, %s, %s, %s::jsonb)",
            (
                row["dataset_id"],
                row["file_id"],
                row["version_id"],
                json.dumps(
                    {
                        "recall_id": str(recall_id),
                        "edge_id": str(row["edge_id"]),
                        "why": row["why"],
                    }
                ),
            ),
        )
    log.warning("recall %s failed: %s", recall_id, why)
    return True


async def fail_unreachable(
    conn: psycopg.AsyncConnection, now: datetime, config: EdgeTimeoutConfig
) -> int:
    """Fail every `downloading` recall whose edge stopped working on it.
    Returns how many were failed. ``now`` is unused: the database's clock
    stamps heartbeats, so it judges them too."""
    del now
    if config.silent is None and config.no_fill is None:
        return 0
    failed = 0
    for row in await candidates(conn, config):
        failed += await fail_one(conn, row, config)
    return failed


def recall_timeout_jobs(worker: Worker, config: EdgeTimeoutConfig | None = None) -> list[Job]:
    cfg = config or EdgeTimeoutConfig.from_env()

    async def sweep(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await fail_unreachable(conn, now, cfg)

    return [SweepJob("recall_edge_timeout", EVERY, sweep)]
