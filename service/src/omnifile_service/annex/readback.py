"""A read-back that refused an annexed file's copy (worker/verify.py, worker/redrive.py).

The location stays unverified, so nothing leaves the NAS for it (the removal gate answers
`copy_not_verified`). That alone was all: no word on the annex, and the edge copied it again
only at its next reconciliation, up to `reconcile_days` away. Now:

- the annex shows a `copy_refused` problem, once per version;
- the file's refusals are counted, one per version, back to 0 when a version verifies
  (the trigger `catalog.annex_location_verified`, migration 0051);
- below `MAX_REFUSALS`, its edge is asked to reconcile, after `ASK_BACKOFF` for that
  refusal. An asked reconciliation copies again only the files whose records say
  `refused` (`api/annex.py`, `get_records`), never a copy whose read-back is only
  pending;
- at `MAX_REFUSALS` it stops asking: an ALERT is logged once, and the gauge
  `omnifile.annex.copies_refused` keeps an alert up while any file is there. The edge's
  periodic reconciliation still copies it again, every `reconcile_days`, as before;
- an ArchiveAdmin may stop that, per file ("stop copying again until I say"), and may
  acknowledge the refusals, which takes the file out of the gauge (migration 0058,
  web/routes_annex.py). A stopped file isn't asked for, and its records say
  `copy_stopped`, so no reconciliation copies the same bytes again. Both end when a
  version verifies.
  - A stopped file is in the gauge whatever its count, until acknowledged
    (`ALERTING_SQL`): stopped at its first refusal, it would never reach the bound.
  - A new refusal of a file that isn't stopped ends its acknowledgement.

Locks: the annex row first, as an edge's event batch takes it, then the location, then
the annex file, as the `copied` event and the verified trigger do.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

#: Refusals in a row after which the edge isn't asked again.
MAX_REFUSALS = 3
#: How long after the n-th refusal (n = 1, 2) the edge is asked: at once, then an hour.
ASK_BACKOFF = (timedelta(0), timedelta(hours=1))
assert len(ASK_BACKOFF) == MAX_REFUSALS - 1

#: What the read-back found, by cause.
CAUSES = {
    "mismatch": "Azure's read-back of this copy didn't match what was sent",
    "missing": "Azure doesn't have this copy, or can't read it",
    "gave_up": "Azure's read-back of this copy couldn't be done: it kept failing",
}


async def copy_refused(
    conn: psycopg.AsyncConnection,
    location_id: int,
    file_id: int,
    version_id: int,
    why: str,
    cause: str,
) -> int:
    """Say so on the annex whose copy this is, and ask its edge to reconcile. How many.

    The location's row before the annex file, as the `copied` event takes it: either
    that event has committed, and its copy is found here, or it waits, and then finds
    this read-back's refusal itself (`api/annex.py`)."""
    annexes = [
        r[0]
        for r in await (
            await conn.execute(
                "SELECT annex_id FROM catalog.annex_file WHERE file_id = %s ORDER BY annex_id",
                (file_id,),
            )
        ).fetchall()
    ]
    if annexes:
        await conn.execute(
            "SELECT 1 FROM catalog.annex WHERE annex_id = ANY(%s) ORDER BY annex_id FOR UPDATE",
            (annexes,),
        )
    await conn.execute(
        "SELECT 1 FROM catalog.location WHERE location_id = %s FOR UPDATE", (location_id,)
    )
    rows = await (
        await conn.execute(
            "SELECT annex_id, path FROM catalog.annex_file"
            " WHERE file_id = %s AND copied_version_id = %s AND gone_since IS NULL"
            "   AND annex_id = ANY(%s)",
            (file_id, version_id, annexes),
        )
    ).fetchall()
    for annex_id, path in rows:
        await refused_on(conn, annex_id, path, file_id, version_id, why, cause)
    return len(rows)


async def refused_on(
    conn, annex_id: int, path: str, file_id: int, version_id: int, why: str, cause: str
) -> bool:
    """Count the refusal of this version, say it, and ask for a reconciliation below
    the bound. Whether it was new (a version already counted isn't again)."""
    row = await (
        await conn.execute(
            "SELECT copy_refusals, copy_refused_version_id, copy_stopped_at IS NOT NULL"
            " FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s FOR UPDATE",
            (annex_id, file_id),
        )
    ).fetchone()
    if row is None:
        return False
    # Tuples on the worker's connection, dicts on the api's.
    refusals, last, stopped = tuple(row.values()) if isinstance(row, dict) else tuple(row)
    if last == version_id:
        return False
    n = int(refusals) + 1
    await conn.execute(
        "UPDATE catalog.annex_file SET copy_refusals = %s, copy_refused_version_id = %s,"
        " copy_refused_at = now(), updated_at = now(),"
        # A new refusal ends an acknowledgement, unless copying is stopped: a file that is
        # still copied again can be refused again, and the alert must not stay quiet for
        # it. A stopped file isn't copied again, so its acknowledgement stays.
        " copy_refused_ack_at = CASE WHEN copy_stopped_at IS NULL THEN NULL"
        "   ELSE copy_refused_ack_at END,"
        " copy_refused_ack_by = CASE WHEN copy_stopped_at IS NULL THEN NULL"
        "   ELSE copy_refused_ack_by END"
        " WHERE annex_id = %s AND file_id = %s",
        (n, version_id, annex_id, file_id),
    )
    what = CAUSES.get(cause, CAUSES["mismatch"])
    base = f"{what} ({why[:300]}). It doesn't count as copied, and nothing leaves the NAS for it."
    if stopped:
        detail = (
            f"{base} Copying it again is stopped by an ArchiveAdmin, so the edge isn't asked"
            f" to (refusal {n}). Check the storage backend, then resume copying it."
        )
    elif n < MAX_REFUSALS:
        delay = ASK_BACKOFF[n - 1]
        when = "now" if not delay else f"in {int(delay.total_seconds() // 60)} minutes"
        detail = (
            f"{base} The edge is asked to copy it again {when} (refusal {n} of {MAX_REFUSALS})."
        )
        await conn.execute(
            "UPDATE catalog.annex SET reconcile_asked_at = CASE"
            "  WHEN reconcile_asked_at > now() THEN least(reconcile_asked_at, now() + %s)"
            "  ELSE now() + %s END WHERE annex_id = %s",
            (delay, delay, annex_id),
        )
    else:
        detail = (
            f"{base} It has been refused {n} times in a row, so the edge isn't asked to copy"
            " it again; its periodic reconciliation still will. Check the storage backend."
        )
        if n == MAX_REFUSALS:
            log.error(
                "ALERT annex %s: the copy of file %s was refused %d times in a row (%s): %s",
                annex_id, file_id, n, cause, why[:300],
            )  # fmt: skip
    await conn.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
        " VALUES (%s, 'problem', %s, %s, %s, %s)",
        (annex_id, path, file_id, version_id,
         Jsonb({"reason": "copy_refused", "cause": cause, "refusals": n, "detail": detail})),
    )  # fmt: skip
    return True


#: Whether a file's refused copy keeps the alert up: at the bound, or stopped by an
#: ArchiveAdmin whatever its count (a file stopped at its first refusal is never asked
#: for again, so it would never reach the bound), and not acknowledged. So a file with
#: no verified copy is silent only after two deliberate, audited acts: a stop and an
#: acknowledgement, or an acknowledgement at the bound. `af` is the annex file.
ALERTING_SQL = (
    "((af.copy_refusals >= %s OR af.copy_stopped_at IS NOT NULL)"
    " AND af.copy_refused_ack_at IS NULL)"
)

#: The gauge's rows: annexes with such files, still on the NAS (migration 0058).
AT_BOUND_SQL = (
    "SELECT a.name, count(*) FROM catalog.annex_file af"
    "  JOIN catalog.annex a ON a.annex_id = af.annex_id"
    f" WHERE {ALERTING_SQL} AND af.gone_since IS NULL AND a.state <> 'retired'"
    " GROUP BY a.name"
)


def alerting(refusals: int, stopped: bool, acknowledged: bool) -> bool:
    """`ALERTING_SQL`, for a row in hand."""
    return (refusals >= MAX_REFUSALS or stopped) and not acknowledged
