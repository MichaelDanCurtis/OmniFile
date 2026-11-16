"""Settings, Alerts: the rules of observability.md section 1, read-only.

The rules live in Azure Monitor (`deploy/azure/modules/app-metrics.bicep`) and
fire to the deployment's action group. This page lists them, and for the ones
the catalog can answer says how they stand now; the others say `unknown`
(see Azure Monitor).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..policy.overdue import ARCHIVE_OVERDUE_SQL, OWN_COPY_STUCK_SQL

WHERE = (
    "Log search alerts over AppMetrics in the deployment's Application Insights"
    " (deploy/azure/modules/app-metrics.bicep), sent to the action group in"
    " alertActionGroupId. Thresholds are its parameters."
)


def _rule(name: str, condition: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "condition": condition, "status": status, "detail": detail}


async def _n(db, q: str, params: tuple | dict = ()) -> int:
    row = await (await db.execute(q, params)).fetchone()
    return int((row or {}).get("n") or 0)


async def alert_settings(db, now: datetime) -> dict[str, Any]:
    from ..web.routes_edges import _EDGES, FREE_MIN, status_of

    edges = await (await db.execute(_EDGES)).fetchall()
    silent = sum(1 for e in edges if status_of(e, now) == "silent")
    live = [e for e in edges if status_of(e, now) not in ("silent", "pending")]
    unguarded = sum(1 for e in live if e["guard_state"] not in (None, "armed"))

    def low(e) -> bool:
        h = e["health"] or {}
        free, cap = h.get("data_free_bytes"), h.get("data_capacity_bytes")
        return free is not None and bool(cap) and free / cap < FREE_MIN

    low_disk = sum(1 for e in live if low(e))
    glass = sum(1 for e in live if (e["health"] or {}).get("recovery_used") == 1)
    failed = await _n(
        db, "SELECT count(*) AS n FROM catalog.migration_prefix WHERE state = 'failed'"
    )
    crl = await _n(
        db,
        "SELECT count(*) AS n FROM catalog.roles_anywhere_crl"
        " WHERE failing_since IS NOT NULL AND failing_since < %s",
        (now - timedelta(minutes=15),),
    )
    held = await _n(db, OWN_COPY_STUCK_SQL, {"since": now - timedelta(hours=25)})
    overdue = await _n(db, ARCHIVE_OVERDUE_SQL, {"now": now, "datasets": None})
    forced = await _n(
        db,
        "SELECT count(*) AS n FROM catalog.audit_log WHERE at > %s AND action = 'tier.move'"
        " AND details->>'forced' IS NOT NULL",
        (now - timedelta(days=1),),
    )
    late = await _n(
        db,
        "SELECT count(*) AS n FROM catalog.audit_log WHERE at > %s"
        " AND action = 'tier.archive.late'",
        (now - timedelta(days=1),),
    )
    unknown = "Read it in Azure Monitor; the catalog doesn't hold it."

    def by(n: int, what: str) -> tuple[str, str]:
        return ("firing", f"{n} {what} now.") if n else ("ok", f"No {what} now.")

    s_silent = by(silent, "edge(s) silent for 15 minutes")
    s_guard = by(unguarded, "edge(s) whose smb-guard isn't armed")
    s_disk = by(low_disk, "edge(s) with under 10% of the data volume free")
    s_glass = by(glass, "edge(s) whose break-glass account was used")
    s_failed = by(failed, "migration prefix(es) failed")
    s_crl = by(crl, "trust anchor(s) whose CRL upload keeps failing")
    s_held = (
        ("firing", f"{held} move(s) waiting for their own copy of a shared blob. Nothing has "
                   "moved. The audit's detail says why: most often the worker lacks write rights "
                   "on the storage account. Each file tries again daily, and this clears itself.")
        if held else ("ok", "No file is waiting for its own copy now.")
    )  # fmt: skip
    s_overdue = by(overdue, "file(s) past their archive date and not yet in Archive")
    s_forced = by(forced, "file(s) sent to Archive by another file's date in the last day "
                          "(see Activity): neither could get its own copy")  # fmt: skip
    s_late = by(late, "file(s) archived after their date in the last day (see Activity)")
    return {
        "where": WHERE,
        "alerts": [
            _rule(
                "queue-backlog",
                "A queue's oldest message is older than 15 minutes",
                "unknown",
                unknown,
            ),
            _rule("dead-letters", "A dead-letter queue is not empty", "unknown", unknown),
            _rule("change-feed-lag", "Change-feed lag over an hour", "unknown", unknown),
            _rule("migration-failed", "A migration prefix in failed", *s_failed),
            _rule(
                "s3-throttling", "S3 throttling in each of the last 10 minutes", "unknown", unknown
            ),
            _rule("edge-silent", "An edge silent for 15 minutes", *s_silent),
            _rule("edge-guard", "An edge whose smb-guard isn't armed", *s_guard),
            _rule("edge-disk", "An edge with less than 10% of its data volume free", *s_disk),
            _rule(
                "edge-break-glass",
                "An edge whose break-glass account was used and not sealed again",
                *s_glass,
            ),
            _rule(
                "default-partition",
                "Feed or audit rows in a DEFAULT time partition",
                "unknown",
                unknown,
            ),
            _rule(
                "host-trusted-restricted",
                "A sec=sys share over folders restricted to AD groups, with no host-trust override",
                "unknown",
                "See the Shares page: such a share is flagged there.",
            ),
            _rule("roles-anywhere-crl-failing", "The edge CA's CRL upload keeps failing", *s_crl),
            _rule(
                "tier-held-back",
                "A file that wants a different tier from others sharing its blob can't get "
                "its own copy, so it hasn't moved",
                *s_held,
            ),
            _rule("tier-archive-late", "A file reached Archive after its archive date", *s_late),
            _rule(
                "tier-archive-overdue",
                "A file is past its archive date, and not in Archive",
                *s_overdue,
            ),  # fmt: skip
            _rule(
                "tier-forced-archive",
                "A file went to Archive before its date, for another file's date",
                *s_forced,
            ),  # fmt: skip
        ],
    }
