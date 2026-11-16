"""Point-in-time listings for rewind trees (control-api.md section 4.5;
decision 30; docs/design/fsal-namespace.md section 7).

`GET /v0/edges/{id}/asof?dir_id&t&after&limit` returns a directory's
entries as they were at `t`, in the FileRow shape, each with the content
version valid then and that version's primary location. The edge
materialises them as read-only placeholders under `.omni/snap/`; it keeps
no version history itself.

- `t` in the future is a 400; older than the share's rewind depth
  (`catalog.rewind_depth_days`) a 410 `beyond_rewind_depth`.
- Rows are always `state: remote` with nothing resident: the edge decides
  whether a placeholder can be a reflink of the live file (the same
  content version, resident).
- `entity_version` is the version row valid at `t` (rewind rows never
  enter the edge's live replica).
- `acl_id` is the ACL at `t`; the edge intersects it with the live folder's.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .context import Ctx
from .idempotency import Result
from .problems import ApiError
from .rows import file_row
from .validate import opt_id, query_int, timestamp

ASOF_SELECT = """
SELECT v.file_id, v.dataset_id, v.parent_id, v.name, f.kind, f.symlink_target,
       coalesce(cv.size, v.size) AS size, v.mtime_ns, v.uid, v.gid, v.mode,
       NULL::text AS owner_sid, v.acl_id, 'remote' AS state, 0::bigint AS resident_bytes,
       cv.version_id AS content_version_id, cv.crc64nvme,
       r.state AS restore_state, r.expires_at AS restore_expires_at,
       NULL::bigint AS stub_version_id, NULL::bigint AS stub_resident_bytes,
       NULL::timestamptz AS deleted_at, v.version_id AS entity_version,
       d.storage_layout,
       l.location_id AS loc_id, l.backend_id AS loc_backend_id, l.container AS loc_container,
       l.object_key AS loc_key, l.backend_version_id AS loc_bvid,
       l.logical_tier AS loc_tier, l.storage_class AS loc_class,
       b.kind AS backend_kind, b.config AS backend_config
  FROM catalog.folder_as_of(%(dir)s, %(t)s) v
  JOIN catalog.file f ON f.file_id = v.file_id
  JOIN catalog.dataset d ON d.dataset_id = v.dataset_id
  LEFT JOIN catalog.file_version cv
         ON cv.version_id = CASE WHEN v.kind = 'content' THEN v.version_id
                                 ELSE v.content_version_id END
  LEFT JOIN LATERAL (
        SELECT * FROM catalog.location
         WHERE version_id = cv.version_id AND role = 'primary' AND state = 'committed'
         ORDER BY location_id LIMIT 1) l ON true
  LEFT JOIN catalog.backend b ON b.backend_id = l.backend_id
  LEFT JOIN LATERAL (
        SELECT state, expires_at FROM catalog.restore
         WHERE location_id = l.location_id AND state <> 'cancelled'
         ORDER BY requested_at DESC, restore_id DESC LIMIT 1) r ON true
 WHERE (%(after)s::text IS NULL OR v.name COLLATE "C" > %(after)s::text COLLATE "C")
 ORDER BY v.name COLLATE "C"
 LIMIT %(limit)s
"""


async def get_asof(ctx: Ctx) -> Result:
    q = dict(ctx.request.query_params)
    dir_id = opt_id(q, "dir_id")
    if dir_id is None:
        raise ApiError("invalid_request", "dir_id is required")
    if "t" not in q:
        raise ApiError("invalid_request", "t is required")
    t = timestamp(q["t"], "t")
    after = q.get("after")
    limit = query_int(ctx.request, "limit", 5000, 1, 10000)
    async with ctx.svc.catalog.read() as db:
        d = await (
            await db.execute(
                "SELECT f.dataset_id, f.kind, now() AS now FROM catalog.file f"
                "  JOIN catalog.dataset d ON d.dataset_id = f.dataset_id"
                " WHERE f.file_id = %s AND d.edge_id = %s",
                (dir_id, ctx.edge_id),
            )
        ).fetchone()
        if d is None or d["kind"] != "dir":
            raise ApiError("not_found", "no such directory on this edge")
        if t > d["now"]:
            raise ApiError("invalid_request", "t is in the future")
        depth = await (
            await db.execute(
                "SELECT catalog.rewind_depth_days(%s, %s) AS days", (d["dataset_id"], dir_id)
            )
        ).fetchone()
        days = int((depth or {}).get("days") or 0)
        if t < d["now"] - timedelta(days=days):
            raise ApiError("beyond_rewind_depth", f"this share keeps {days} days of versions")
        rows = await (
            await db.execute(
                ASOF_SELECT, {"dir": dir_id, "t": t, "after": after, "limit": limit + 1}
            )
        ).fetchall()
    entries: list[dict[str, Any]] = [file_row(r) for r in rows[:limit]]
    return Result(200, {"entries": entries, "more": len(rows) > limit})
