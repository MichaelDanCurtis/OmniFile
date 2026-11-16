"""Live `Entry` rows: a file's tier, effective pin, holds and active recall,
fetched in a few batched queries for a whole listing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..api.validate import rfc3339
from ..backends.base import LogicalTier
from ..policy.engine import HELD_BACK, OWN_COPY_RETRY_AFTER, FileFacts, anchor
from ..policy.model import Policy, PolicyError
from . import rows


def _wants_a_move(
    f: dict[str, Any], loc: dict[str, Any] | None, pin: dict | None, now: datetime
) -> bool:
    """The file's pin asks for another tier than it is in, as a move a shared blob can make
    wait (from Archive it would be a recall). A pin to Archive waits too, except once the
    file's archive date has come, which may move the blob for everyone."""
    if loc is None or pin is None or f["kind"] != "file":
        return False
    tier = LogicalTier(loc["logical_tier"] or "hot")
    want = LogicalTier(pin["logical_tier"])
    if tier is LogicalTier.ARCHIVE or want is tier:
        return False
    date = f.get("archive_at")
    today = now.astimezone(UTC).date()  # as `tiering._archive_date_arrived` compares it
    return not (want is LogicalTier.ARCHIVE and date is not None and date <= today)


def _archive_date_won(f: dict[str, Any], pin: dict, body: dict, now: datetime) -> bool:
    """Past the file's exact archive time, a pin without `override_archive` no
    longer asks for a warmer tier (`engine.evaluate`). The stored date alone can't
    say: on that date the time may be still to come."""
    if pin["override_archive"]:
        return False
    written = f.get("written_at")
    try:
        if written is not None:
            policy = Policy.from_body(body)
            start = anchor(
                policy, FileFacts(written_at=written, last_read_at=f.get("last_read_at"))
            )
            return now >= start + timedelta(days=policy.archive_day)
    except PolicyError:
        pass
    date = f.get("archive_at")
    return date is not None and date < now.date()


async def _read_backs_failed(
    db, files: list[dict[str, Any]], locs: dict[int, dict[str, Any]]
) -> dict[int, datetime]:
    """Of these files' primary copies, the unverified ones whose read-back was denied:
    a CRC that didn't match, or one the worker gave up on (`worker/redrive.py`). Nothing
    will stub such a file, and nothing tries again without a person. Only unverified rows
    are looked up, so a page of verified files costs nothing, and verifying the copy
    later clears this. By the audit log's file index."""
    bad = {
        f["file_id"]: f["primary_location_id"]
        for f in files
        if f.get("primary_location_id") in locs
        and not locs[f["primary_location_id"]].get("verified")
    }
    if not bad:
        return {}
    rows_ = await (
        await db.execute(
            "SELECT a.file_id, max(a.at) AS at, a.details->>'location_id' AS location_id"
            "  FROM catalog.audit_log a"
            " WHERE a.file_id = ANY(%s) AND a.action = 'location.verify' AND a.decision = 'deny'"
            " GROUP BY a.file_id, a.details->>'location_id'",
            (list(bad),),
        )
    ).fetchall()
    return {bad[r["file_id"]]: r["at"] for r in rows_ if r["location_id"] == str(bad[r["file_id"]])}


async def _move_holds(
    db, wanted: dict[int, tuple[dict[str, Any], dict]], now: datetime
) -> dict[int, datetime]:
    """Of these locations (with their file and pin), the ones whose move is waiting for
    its own copy of a shared blob, and until when: the worker's report (`tiering._wait_a_day`)
    says so, and the wait ends a day after it. It says nothing about the other rows but
    that there are some. Only under an active tiering policy: elsewhere nothing would move."""
    if not wanted:
        return {}
    found = await (
        await db.execute(
            """
            SELECT l.location_id, max((a.details->>'at')::timestamptz) AS reported_at, p.body
              FROM catalog.location l
              JOIN catalog.file f ON f.file_id = l.file_id
              JOIN catalog.policy p ON p.dataset_id = f.dataset_id AND p.state = 'active'
                   AND p.body ? 'archive_day'
              JOIN catalog.location h
                ON h.backend_id = l.backend_id AND h.container = l.container
               AND h.object_key = l.object_key
               AND h.backend_version_id IS NOT DISTINCT FROM l.backend_version_id
               AND h.state = 'committed'
              JOIN catalog.audit_log a ON a.file_id = h.file_id
               AND a.details->>'location_id' = h.location_id::text
               AND a.action = 'tier.move.refused' AND a.details->>'why' = %s
             WHERE l.location_id = ANY(%s)
               AND EXISTS (SELECT 1 FROM catalog.location o
                            WHERE o.backend_id = l.backend_id AND o.container = l.container
                              AND o.object_key = l.object_key
                              AND o.backend_version_id IS NOT DISTINCT FROM l.backend_version_id
                              AND o.state = 'committed' AND o.role = 'primary'
                              AND o.location_id <> l.location_id)
             GROUP BY l.location_id, p.body
            """,
            (HELD_BACK, list(wanted)),
        )
    ).fetchall()
    out: dict[int, datetime] = {}
    for r in found:
        until = r["reported_at"] + OWN_COPY_RETRY_AFTER
        f, pin = wanted[r["location_id"]]
        if now < until and not _archive_date_won(f, pin, r["body"], now):
            out[r["location_id"]] = until
    return out


async def decorate(
    db, files: list[dict[str, Any]], inherit_ids: list[int], now: datetime
) -> list[dict[str, Any]]:
    """`Entry` JSON for `files`. `inherit_ids`: the folders above them,
    nearest first, whose pins and holds apply to them too."""
    if not files:
        return []
    ids = [f["file_id"] for f in files]
    scope = ids + [i for i in inherit_ids if i not in ids]
    loc_ids = [f["primary_location_id"] for f in files if f.get("primary_location_id")]
    locs = {
        r["location_id"]: r
        for r in await (
            await db.execute(
                "SELECT location_id, logical_tier, storage_class, explicit_tier_set,"
                " tier_changed_at, verified FROM catalog.location WHERE location_id = ANY(%s)",
                (loc_ids,),
            )
        ).fetchall()
    }
    pins = {
        r["file_id"]: r
        for r in await (
            await db.execute(
                "SELECT * FROM catalog.tier_pin WHERE released_at IS NULL AND file_id = ANY(%s)"
                " AND (until IS NULL OR until > %s)",
                (scope, now),
            )
        ).fetchall()
    }
    holds: dict[int, list[dict[str, Any]]] = {}
    for r in await (
        await db.execute(
            "SELECT * FROM catalog.hold WHERE released_at IS NULL AND file_id = ANY(%s)"
            " AND (until IS NULL OR until > %s)",
            (scope, now),
        )
    ).fetchall():
        holds.setdefault(r["file_id"], []).append(r)
    recalls = {
        r["file_id"]: r
        for r in await (
            await db.execute(
                "SELECT r.recall_id, r.file_id, r.state, r.eta FROM catalog.recall r"
                " JOIN catalog.file f ON f.file_id = r.file_id"
                " AND r.version_id = f.content_version_id"
                " WHERE r.file_id = ANY(%s) AND r.state = ANY(%s)",
                (ids, list(rows.ACTIVE_RECALL)),
            )
        ).fetchall()
    }
    inherited_pin = next((pins[i] for i in inherit_ids if i in pins), None)
    inherited_holds = [h for i in inherit_ids for h in holds.get(i, [])]
    wanted = {}
    for f in files:
        lid, pin = f.get("primary_location_id"), pins.get(f["file_id"], inherited_pin)
        if _wants_a_move(f, locs.get(lid), pin, now):
            wanted[lid] = (f, pin)
    held = await _move_holds(db, wanted, now)
    denied = await _read_backs_failed(db, files, locs)
    out = []
    for f in files:
        loc = locs.get(f.get("primary_location_id"))
        pin = pins.get(f["file_id"], inherited_pin)
        until = held.get(f.get("primary_location_id"))
        if loc is not None:
            loc = {
                **loc,
                "restore_state": f.get("restore_state"),
                "restore_expires_at": f.get("restore_expires_at"),
            }
        out.append(
            rows.entry(
                f,
                loc=loc,
                pin=pin,
                holds=holds.get(f["file_id"], []) + inherited_holds,
                recall=recalls.get(f["file_id"]),
                now=now,
                move_hold=(
                    {"to": pin["logical_tier"], "until": rfc3339(until)}
                    if until is not None and pin is not None
                    else None
                ),
                read_back_failed_at=denied.get(f.get("primary_location_id")),
            )
        )
    return out
