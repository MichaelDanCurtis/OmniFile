"""Removals from a NAS (phase A2.2; wp14-a2.md, "What writes to the filer").

The edge never decides a removal alone: for every file it asks for a ticket,
and before each of its two writes (hiding the file, then deleting it) it asks
again. Every answer comes from here.

**Live** (`LIVE_SQL`), all of:
- the annex is active, and its running rule set removes, approved by an
  ArchiveAdmin (`decided_as_archive_admin`);
- its dry run has ended (`dry_run_until`, decision 5);
- an ArchiveAdmin confirmed the dry run for the running version
  (decision 59: removals never start by themselves);
- neither the annex nor Settings pauses removals.

**A file** (`file_gate`), all of:
- the rule the edge names is one of the running set's removing rules
  (`arrived` or `age`): the edge decides by `RuleSet::removal`, and the
  service checks the answer names a rule that can remove;
- it is in the annex, present on the NAS, and its current version is the one
  that was copied, with the NAS attributes it was copied with;
- no legal hold stands on it or a folder above it (WORM is a share's, and an
  annex's dataset has no share, so there is no WORM gate here);
- that copy's location is committed and verified, and was **read back in
  Azure** within `AZURE_READ_FRESH` (otherwise a read is queued and the
  answer is "not yet": decision 24, annex.md section 3.3);
- the rate and the window allow another removal now.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb

#: A copy must have been read back in Azure this recently to be removed.
AZURE_READ_FRESH = timedelta(hours=24)
#: A read asked for is asked again only after this.
AZURE_READ_RETRY = timedelta(hours=1)
#: A ticket's hide must be asked for within this.
TICKET_TTL = timedelta(minutes=10)
#: A file placed back more often than this in 30 days is pinned where it is
#: (the loop breaker, annex.md section 4.6).
LOOP_PLACEMENTS = 3
#: Why removals may be stopped for now without cancelling one in its grace
#: period: the hidden file waits, and its grace starts again.
HOLD_REASONS = frozenset({"paused", "outside_window", "grace"})

#: Whether the annex's removals are live, with the running rule set.
LIVE_SQL = """
SELECT a.annex_id, a.state, a.removals_paused, a.dry_run_until, a.removals_confirmed_version,
       a.removal_rate_per_hour, a.removal_window, a.grace_minutes, a.pointer_files,
       r.version AS rs_version, r.removes AS rs_removes, r.body AS rs_body,
       coalesce(r.decided_as_archive_admin, false) AS rs_by_admin,
       d.removals_paused AS all_paused,
       (a.state = 'active' AND coalesce(r.removes, false) AND r.decided_as_archive_admin
        AND NOT a.removals_paused
        AND NOT d.removals_paused AND a.dry_run_until IS NOT NULL
        AND a.dry_run_until <= %(now)s::timestamptz
        AND a.removals_confirmed_version = r.version) AS live
  FROM catalog.annex a
  CROSS JOIN catalog.annex_defaults d
  LEFT JOIN catalog.annex_ruleset r ON r.annex_id = a.annex_id AND r.state = 'active'
 WHERE a.annex_id = %(annex_id)s
"""


def why_not_live(row: dict[str, Any], now: datetime) -> str | None:
    """None if live, else the first reason it isn't (for the edge and the portal)."""
    if row["live"]:
        return None
    if row["state"] != "active":
        return "annex_not_active"
    if not row["rs_removes"]:
        return "no_removing_rules"
    if not row["rs_by_admin"]:
        return "not_archive_admin_approved"
    if row["removals_paused"] or row["all_paused"]:
        return "paused"
    if row["dry_run_until"] is None or row["dry_run_until"] > now:
        return "dry_run"
    return "not_confirmed"


def in_window(window: dict[str, Any] | None, now: datetime) -> bool:
    """`{start, end, tz?}` in local time; an end before the start wraps midnight."""
    if not window:
        return True
    try:
        tz = ZoneInfo(window.get("tz") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return False  # fail closed: a window we can't read allows nothing
    t = now.astimezone(tz).strftime("%H:%M")
    start, end = window["start"], window["end"]
    return start <= t < end if start <= end else (t >= start or t < end)


def removing_rules(body: dict[str, Any] | None) -> set[str]:
    """The ids of the rules that can remove a file: `arrived` with
    `remove_from_source` or `remove_after_days`, and `age` with
    `remove_from_source` (`changed` rules never remove)."""
    out: set[str] = set()
    for r in (body or {}).get("rules", []):
        acts = r.get("actions", [])
        if r.get("trigger") == "arrived" and any(
            "remove_from_source" in a or "remove_after_days" in a for a in acts
        ):
            out.add(r.get("id", ""))
        elif r.get("trigger") == "age" and any("remove_from_source" in a for a in acts):
            out.add(r.get("id", ""))
    return out


async def live(db, annex_id: int, now: datetime) -> dict[str, Any]:
    return await (await db.execute(LIVE_SQL, {"annex_id": annex_id, "now": now})).fetchone()


async def file_gate(
    db, a: dict[str, Any], c: dict[str, Any], now: datetime, *, recheck: bool = False
) -> tuple[str, str | None, dict[str, Any] | None]:
    """(status, reason, row): `ticket` with the file's catalog row, or
    `not_yet` (try later) / `refused` (don't) with why. `recheck`: the same
    gates again before a write on an issued ticket (its own file is
    `removing`, and it already counts toward the rate)."""
    row = await (
        await db.execute(
            """
            SELECT af.file_id, af.path, af.nas_file_id, af.size, af.mtime_ns, af.ctime_ns,
                   af.copied_version_id, af.copied_size, af.copied_mtime_ns, af.copied_ctime_ns,
                   af.gone_since, af.nas_state, af.rule, f.content_version_id, f.deleted_at,
                   af.hold_until, af.pinned_by_breaker, af.breaker_cleared_at,
                   l.location_id, l.verified, l.azure_read_at, l.azure_read_requested_at,
                   l.crc64nvme
              FROM catalog.annex_file af
              JOIN catalog.file f ON f.file_id = af.file_id
              LEFT JOIN catalog.location l ON l.version_id = af.copied_version_id
                   AND l.role = 'primary' AND l.state = 'committed'
             WHERE af.annex_id = %s AND af.file_id = %s
             FOR UPDATE OF af
            """,
            (a["annex_id"], c["file_id"]),
        )
    ).fetchone()
    if row is None:
        return "refused", "not_in_annex", None
    if c.get("rule") not in removing_rules(a.get("rs_body")):
        return "refused", "rule_not_removing", row
    if row["gone_since"] is not None or row["deleted_at"] is not None:
        return "refused", "gone", row
    if row["nas_state"] not in (("present", "removing") if recheck else ("present",)):
        return "refused", f"nas_{row['nas_state']}", row
    if row["copied_version_id"] != c["version_id"] or row["content_version_id"] != c["version_id"]:
        return "refused", "not_the_current_copy", row
    # A placement replacing this file may have it hidden (A3.4; #98's S4).
    from . import replace as RP  # replace imports this module

    replacing = await (
        await db.execute(
            f"SELECT 1 FROM catalog.annex_placement p WHERE p.file_id = %s AND {RP.OPEN_SQL}"
            " LIMIT 1",
            (c["file_id"],),
        )
    ).fetchone()
    if replacing is not None:
        return "refused", "replace_open", row
    # A file placed back (A3.3) stays until its hold ends; one placed and
    # removed again and again is pinned until an Owner looks (section 4.6).
    if row["pinned_by_breaker"]:
        return "refused", "loop_breaker", row
    if row["hold_until"] is not None and row["hold_until"] > now:
        return "not_yet", "held", row
    # A loop is the file coming back where it came from, and then leaving
    # again. A copy into a share or under another name isn't one, and neither
    # is anything before an Owner last looked.
    since = now - timedelta(days=30)
    if row["breaker_cleared_at"] is not None and row["breaker_cleared_at"] > since:
        since = row["breaker_cleared_at"]
    placed = await (
        await db.execute(
            "SELECT count(*) AS n FROM catalog.annex_placement WHERE file_id = %s"
            # A replace at the origin (A3.4) is a return there too.
            " AND state = 'placed' AND outcome IN ('placed', 'replaced', 'replaced_old_kept')"
            " AND done_at > %s"
            " AND target->>'kind' = 'nas' AND (target->>'origin')::boolean"
            " AND final_name = target->>'name'",
            (c["file_id"], since),
        )
    ).fetchone()
    if placed["n"] > LOOP_PLACEMENTS:
        await db.execute(
            "UPDATE catalog.annex_file SET pinned_by_breaker = true WHERE annex_id = %s"
            " AND file_id = %s",
            (a["annex_id"], c["file_id"]),
        )
        await db.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, details)"
            " VALUES (%s, 'loop_breaker', %s, %s, %s)",
            (a["annex_id"], row["path"], c["file_id"],
             Jsonb({"placements_30d": placed["n"]})),
        )  # fmt: skip
        await db.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, file_id, details)"
            " VALUES ('omnifile', 'annex.breaker.pinned', 'deny', %s, %s)",
            (c["file_id"], Jsonb({"annex_id": str(a["annex_id"]), "path": row["path"],
                                  "returns_30d": placed["n"]})),
        )  # fmt: skip
        return "refused", "loop_breaker", row
    copied = (row["copied_size"], row["copied_mtime_ns"], row["copied_ctime_ns"])
    if (c["size"], c["mtime_ns"], c["ctime_ns"]) != copied or c["nas_file_id"] != row[
        "nas_file_id"
    ]:
        return "refused", "changed_since_copied", row
    held = await (
        await db.execute(
            "SELECT 1 FROM catalog.hold h WHERE h.kind = 'legal' AND h.released_at IS NULL"
            "   AND (h.until IS NULL OR h.until > %s)"
            "   AND h.file_id IN (SELECT file_id FROM catalog.file_ancestors(%s))"
            " LIMIT 1",
            (now, c["file_id"]),
        )
    ).fetchone()
    if held is not None:
        return "refused", "legal_hold", row
    if row["location_id"] is None or not row["verified"]:
        return "not_yet", "copy_not_verified", row
    fresh = row["azure_read_at"] is not None and row["azure_read_at"] > now - AZURE_READ_FRESH
    if not fresh:
        asked = row["azure_read_requested_at"]
        if asked is None or asked < now - AZURE_READ_RETRY:
            await db.execute(
                "UPDATE catalog.location SET azure_read_requested_at = %s WHERE location_id = %s",
                (now, row["location_id"]),
            )
            await db.execute(
                "SELECT pgmq.send('verify', %s::jsonb)",
                (json.dumps({"location_id": str(row["location_id"]), "purpose": "removal"}),),
            )
        return "not_yet", "needs_azure_read", row
    if not in_window(a["removal_window"], now):
        return "not_yet", "outside_window", row
    if recheck:
        return "ticket", None, row
    issued = await (
        await db.execute(
            "SELECT count(*) AS n FROM catalog.annex_removal WHERE annex_id = %s"
            " AND issued_at > %s",
            (a["annex_id"], now - timedelta(hours=1)),
        )
    ).fetchone()
    if issued["n"] >= a["removal_rate_per_hour"]:
        return "not_yet", "rate", row
    return "ticket", None, row
