"""Activity: the recall and upload dashboards (portal-api.md section 5.13;
docs/design/observability.md section 3). M6.

`GET /v1/activity?days&dataset_id` sums the catalog over the last `days`
days (1 to 90, default 30), UTC, per day and in total:

- **uploads:** content versions an edge committed (files and bytes), the
  copies still waiting for their read-back verification, and the files
  still landing or uploading on an edge;
- **recalls:** started, available and failed per day; how long a recall
  took to become available (median and 90th percentile); the bytes made
  available; and the recalls open now.

Everything comes from the catalog, so the dashboard needs no metrics
pipeline. ArchiveAdmin, or an app with `Archive.Read`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..api.validate import rfc3339
from .context import Ctx, WebResult
from .problems import invalid
from .routes_sources import _reader

DAYS_MAX = 90

_DAILY = """
WITH days AS (
    SELECT generate_series((%(from)s AT TIME ZONE 'UTC')::date,
                           ((%(to)s - interval '1 second') AT TIME ZONE 'UTC')::date,
                           interval '1 day')::date AS day
), up AS (
    SELECT (v.committed_at AT TIME ZONE 'UTC')::date AS day, count(*) AS files,
           coalesce(sum(v.size), 0) AS bytes
      FROM catalog.file_version v
     WHERE v.kind = 'content' AND v.state = 'committed' AND v.edge_id IS NOT NULL
       AND v.committed_at >= %(from)s AND v.committed_at < %(to)s
       AND (%(ds)s::bigint IS NULL OR v.dataset_id = %(ds)s::bigint)
     GROUP BY 1
), started AS (
    SELECT (r.requested_at AT TIME ZONE 'UTC')::date AS day, count(*) AS n
      FROM catalog.recall r JOIN catalog.file f USING (file_id)
     WHERE r.requested_at >= %(from)s AND r.requested_at < %(to)s
       AND (%(ds)s::bigint IS NULL OR f.dataset_id = %(ds)s::bigint)
     GROUP BY 1
), ended AS (
    SELECT (coalesce(r.completed_at, r.updated_at) AT TIME ZONE 'UTC')::date AS day,
           count(*) FILTER (WHERE r.state = 'available') AS available,
           count(*) FILTER (WHERE r.state = 'failed') AS failed
      FROM catalog.recall r JOIN catalog.file f USING (file_id)
     WHERE r.state IN ('available', 'failed')
       AND coalesce(r.completed_at, r.updated_at) >= %(from)s
       AND coalesce(r.completed_at, r.updated_at) < %(to)s
       AND (%(ds)s::bigint IS NULL OR f.dataset_id = %(ds)s::bigint)
     GROUP BY 1
)
SELECT d.day, coalesce(up.files, 0) AS up_files, coalesce(up.bytes, 0) AS up_bytes,
       coalesce(started.n, 0) AS started, coalesce(ended.available, 0) AS available,
       coalesce(ended.failed, 0) AS failed
  FROM days d
  LEFT JOIN up USING (day) LEFT JOIN started USING (day) LEFT JOIN ended USING (day)
 ORDER BY d.day
"""

_RECALLS = """
SELECT count(*) FILTER (WHERE r.state = 'available') AS available,
       count(*) FILTER (WHERE r.state = 'failed') AS failed,
       coalesce(sum(v.size) FILTER (WHERE r.state = 'available'), 0) AS bytes,
       percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM r.completed_at - r.requested_at))
           FILTER (WHERE r.state = 'available') AS p50,
       percentile_cont(0.9) WITHIN GROUP (
           ORDER BY extract(epoch FROM r.completed_at - r.requested_at))
           FILTER (WHERE r.state = 'available') AS p90
  FROM catalog.recall r
  JOIN catalog.file f USING (file_id)
  JOIN catalog.file_version v ON v.version_id = r.version_id
 WHERE r.state IN ('available', 'failed')
   AND coalesce(r.completed_at, r.updated_at) >= %(from)s
   AND coalesce(r.completed_at, r.updated_at) < %(to)s
   AND (%(ds)s::bigint IS NULL OR f.dataset_id = %(ds)s::bigint)
"""

_NOW = """
SELECT (SELECT count(*) FROM catalog.recall r JOIN catalog.file f USING (file_id)
         WHERE r.state IN ('requested', 'rehydrating', 'copying', 'downloading')
           AND (%(ds)s::bigint IS NULL OR f.dataset_id = %(ds)s::bigint)) AS open,
       (SELECT count(*) FROM catalog.location l JOIN catalog.file_version v USING (version_id)
         WHERE l.state = 'committed' AND l.role = 'primary' AND NOT l.verified
           AND v.edge_id IS NOT NULL
           AND (%(ds)s::bigint IS NULL OR v.dataset_id = %(ds)s::bigint)) AS unverified,
       (SELECT count(*) FROM catalog.file f
         WHERE f.state IN ('landing', 'uploading') AND f.deleted_at IS NULL
           AND (%(ds)s::bigint IS NULL OR f.dataset_id = %(ds)s::bigint)) AS landing
"""


async def get_activity(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    raw = ctx.query("days")
    days = 30
    if raw is not None:
        if not raw.isdigit() or not 1 <= int(raw) <= DAYS_MAX:
            raise invalid("days", f"must be 1 to {DAYS_MAX}")
        days = int(raw)
    ds = ctx.query_id("dataset_id")
    now = ctx.now.astimezone(UTC)
    to = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    frm = to - timedelta(days=days)
    params: dict[str, Any] = {"from": frm, "to": to, "ds": ds}
    async with ctx.svc.catalog.read() as db:
        daily = await (await db.execute(_DAILY, params)).fetchall()
        rec = await (await db.execute(_RECALLS, params)).fetchone()
        cur = await (await db.execute(_NOW, params)).fetchone()
    assert rec is not None and cur is not None

    def secs(v: Any) -> int | None:
        return None if v is None else round(float(v))

    return WebResult(200, {
        "from": rfc3339(frm),
        "to": rfc3339(to),
        "dataset_id": None if ds is None else str(ds),
        "days": [{
            "date": d["day"].isoformat(),
            "uploads": {"files": int(d["up_files"]), "bytes": int(d["up_bytes"])},
            "recalls": {"started": int(d["started"]), "available": int(d["available"]),
                        "failed": int(d["failed"])},
        } for d in daily],
        "uploads": {
            "files": sum(int(d["up_files"]) for d in daily),
            "bytes": sum(int(d["up_bytes"]) for d in daily),
            "unverified": int(cur["unverified"]),
            "landing": int(cur["landing"]),
        },
        "recalls": {
            "open": int(cur["open"]),
            "available": int(rec["available"]),
            "failed": int(rec["failed"]),
            "bytes": int(rec["bytes"]),
            "time_to_available_s": {"p50": secs(rec["p50"]), "p90": secs(rec["p90"])},
        },
    })  # fmt: skip
