"""The portal's visual views: a file's journey along the tiers, and a share's
map (portal-api.md section 5.22). Read-only, computed from the catalog.

- `GET /v1/files/{file_id}/journey` (read on the file): the tier ladder of
  the backend that holds the file's primary copy, from its `capabilities()`
  (native class, minimum days, whether a read needs a restore), which account
  holds it, and the pin and holds that apply, with who set them and when.
  `set_by` and `reason` follow `list_pins` and `list_holds`: the item the pin
  or hold is on must be readable to the caller, and the caller an
  ArchiveAdmin, a DataSteward or the share's Owner (`set_by` also goes to
  whoever set it). Otherwise they are `null`.
- `GET /v1/shares/{share_id}/map` (anyone who can see the share): files and
  bytes under the share's root, on the edge (whole, stub, arriving, remote)
  and per backend, account and tier; recalls in flight per step; and, for an
  ArchiveAdmin, the dataset's open migrations.
  - Counts come from the worker's roll-up (`usage/share_map.py`, migration
    0056): groups of each folder's files, summed over the folders the map
    walks, as of the oldest count among them (`basis: rollup`). A share with a
    folder not counted yet is `basis: counting`, never a total of zero.
  - ArchiveAdmin and DataSteward (owner, 2026-11-04) get whole-share totals
    without names (`scope: all`).
  - Everyone else gets the counts of only what they can read (`scope:
    readable`): groups whose files they can read, in folders they can read,
    walked only through readable folders, each group through the same
    predicate its files go through. It stops after LIVE_TIMEOUT_MS
    (`basis: none`), and the route is on the preview limiter.
- `GET /v1/shares/{share_id}/map/files` (anyone who can see the share): the
  files behind one node of the map. It names files, so it lists only files
  the caller can read, in folders the caller can read, whatever the role.
"""

from __future__ import annotations

from typing import Any

import psycopg

from ..api.validate import rfc3339
from ..backends.base import LogicalTier
from ..catalog.read_set import PASS_CTE, readable_group_sql, readable_sql
from ..worker.backends import capabilities_for
from . import rows
from .access import acl_params, load_scope
from .context import Ctx, Principal, WebResult, page
from .problems import WebError, invalid, not_found
from .routes_shares import _load as load_share
from .routes_shares import visible

#: The ladder, warmest first. Smart sits beside Hot, Cool and Cold.
LADDER = (LogicalTier.HOT, LogicalTier.WARM, LogicalTier.COLD, LogicalTier.ARCHIVE,
          LogicalTier.SMART)  # fmt: skip
OPEN_MIGRATION = ("pending", "copying", "verifying", "verified")
#: Edge buckets of the map, from `file.state`.
EDGE_BUCKETS = {
    "resident": ("resident",),
    "stub": ("stub", "recalling"),
    "arriving": ("landing", "uploading"),
    "remote": ("remote",),
}
#: Recall states -> the four steps (`copying` is shown as step 2, section 6).
RECALL_STEP = {"requested": "requested", "rehydrating": "rehydrating",
               "copying": "rehydrating", "downloading": "downloading"}  # fmt: skip
MAP_FILES_MAX = 200


def account_of(kind: str, layout: str | None, tier: str | None) -> str | None:
    """Which account holds a copy: two-account Azure layouts keep Archive in
    the archive account and everything else in the live one. S3 has no
    accounts in this sense."""
    if kind == "s3" or tier is None:
        return None
    if layout == "two_account":
        return "archive" if tier == "archive" else "live"
    if layout == "single_account":
        return "single"
    return None


def _ladder(kind: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        caps = capabilities_for(kind, config or {})
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for tier in LADDER:
        infos = caps.tiers.get(tier) or []
        if not infos:
            continue
        out.append({
            "logical": tier.value,
            "classes": [{"native_class": i.native_class, "minimum_days": i.minimum_days,
                         "needs_restore": i.needs_restore} for i in infos],
        })  # fmt: skip
    return out


def _role(backend_id: int | None, ds: dict[str, Any]) -> str:
    """The backend's part in the dataset: its home, its migration source, or other."""
    if backend_id == ds["backend_id"]:
        return "home"
    return "source" if backend_id == ds["source_backend_id"] else "other"


def _who_set(
    r: dict[str, Any], privileged: bool, who: Principal, *, app: str | None = None
) -> dict[str, Any] | None:
    mine = who.identity_id is not None and r.get("set_by_identity_id") == who.identity_id
    if not (privileged or mine):
        return None
    return rows.person(
        identity_id=r.get("set_by_identity_id"), display_name=r.get("setter_name"), app=app
    )


async def get_journey(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    async with ctx.svc.catalog.read() as db:
        scope = (await load_scope(db, who, ctx.path_id("file_id"))).require_read()
        f = scope.file
        ds = await (
            await db.execute(
                "SELECT d.backend_id, d.source_backend_id, d.storage_layout, d.migration_state,"
                " b.kind, b.config FROM catalog.dataset d"
                " JOIN catalog.backend b ON b.backend_id = d.backend_id WHERE d.dataset_id = %s",
                (scope.dataset_id,),
            )
        ).fetchone()
        loc = None
        if f["primary_location_id"]:
            loc = await (
                await db.execute(
                    "SELECT l.backend_id, l.logical_tier::text AS logical_tier, l.storage_class,"
                    " b.kind, b.config FROM catalog.location l"
                    " JOIN catalog.backend b ON b.backend_id = l.backend_id"
                    " WHERE l.location_id = %s AND l.state = 'committed'",
                    (f["primary_location_id"],),
                )
            ).fetchone()
        held = loc or ds
        chain = scope.ancestor_ids  # self first, nearest folder next
        pins = await (
            await db.execute(
                "SELECT p.*, i.display_name AS setter_name FROM catalog.tier_pin p"
                " LEFT JOIN catalog.identity i ON i.identity_id = p.set_by_identity_id"
                " WHERE p.released_at IS NULL AND p.file_id = ANY(%s)"
                " AND (p.until IS NULL OR p.until > %s)",
                (chain, now),
            )
        ).fetchall()
        holds = await (
            await db.execute(
                "SELECT h.*, i.display_name AS setter_name FROM catalog.hold h"
                " LEFT JOIN catalog.identity i ON i.identity_id = h.set_by_identity_id"
                " WHERE h.released_at IS NULL AND h.file_id = ANY(%s)"
                " AND (h.until IS NULL OR h.until > %s) ORDER BY h.hold_id",
                (chain, now),
            )
        ).fetchall()
    role_ok = who.has("ArchiveAdmin", "DataSteward") or scope.roles.owner
    readable = {c["file_id"] for c in scope.chain if c["readable"]}

    def privileged(item: dict[str, Any]) -> bool:
        """As list_pins and list_holds: the role, and read on the item it is on."""
        return role_ok and item["file_id"] in readable

    depth = {fid: i for i, fid in enumerate(chain)}
    pin = min(pins, key=lambda p: depth[p["file_id"]], default=None)
    role = _role(held["backend_id"], ds)
    tier = loc["logical_tier"] if loc else None
    return WebResult(200, {
        "file_id": str(f["file_id"]),
        "backend": {
            "kind": held["kind"],
            "role": role,
            "storage_layout": ds["storage_layout"],
            "account": account_of(held["kind"], ds["storage_layout"], tier),
            "migration_state": ds["migration_state"],
        },
        "tiers": _ladder(held["kind"], held["config"]),
        "pin": None if pin is None else {
            "pin_id": str(pin["pin_id"]),
            "tier": pin["logical_tier"],
            "until": rfc3339(pin["until"]),
            "override_archive": bool(pin["override_archive"]),
            "set_at": rfc3339(pin["set_at"]),
            "set_by": _who_set(pin, privileged(pin), who),
            "reason": pin.get("reason") if privileged(pin) else None,
            "inherited_from": None if pin["file_id"] == f["file_id"] else str(pin["file_id"]),
        },
        "holds": [{
            "hold_id": str(h["hold_id"]),
            "kind": h["kind"],
            "until": rfc3339(h["until"]),
            "set_at": rfc3339(h["set_at"]),
            "set_by": _who_set(h, privileged(h), who, app=h.get("set_by_app")),
            "reason": h["reason"] if privileged(h) else None,
            "inherited_from": None if h["file_id"] == f["file_id"] else str(h["file_id"]),
        } for h in holds],
    })  # fmt: skip


# --- the share map ---------------------------------------------------------------


#: The caller's live count (everyone but an ArchiveAdmin) stops after this,
#: and the map then says the counts are unavailable (section 5.22).
LIVE_TIMEOUT_MS = 3000


def readable_dirs() -> str:
    """`t`: the share's root and the folders under it the caller can read,
    reached only through folders the caller can read (section 3.2: a folder
    must be readable to list it). Parameters %(root)s and `acl_params`. The
    NAS folders the caller passes (decision 63) are walked once for the whole
    query, `pass`."""
    return f"""
WITH RECURSIVE {PASS_CTE}, t(file_id) AS (
    SELECT r.file_id FROM catalog.file r JOIN catalog.acl acl ON acl.acl_id = r.acl_id
     WHERE r.file_id = %(root)s::bigint AND {readable_sql("r.file_id", walked=True)}
    UNION ALL
    SELECT f.file_id FROM catalog.file f JOIN t ON f.parent_id = t.file_id
      JOIN catalog.acl acl ON acl.acl_id = f.acl_id
     WHERE f.kind = 'dir' AND f.deleted_at IS NULL AND {readable_sql("f.file_id", walked=True)}
)"""


#: A reader's map counts the folders with a change waiting live, from their files,
#: so a file's own ACL change or move counts at once (the review of #91). Past this
#: many files in them, at their last count, it says "counting" rather than a stale
#: number: at 10^7 files, 2 x 10^4 such files make the map 1.12 s (1.05 s with
#: none) and 2 x 10^5 make it 9.1 s (results/wp14-share-map.md). A folder that
#: grew since its count is still bounded by the 3 s limit, past which the map has
#: no totals at all.
QUEUED_LIVE_FILES = 20_000

#: The map's parts under the walked folders `t` (usage/share_map.py, migration 0056),
#: and one `counted` row (usage/share_map.py, "as of"): how many folders summed make
#: it "counting", and the time its counts are true as of.
#: - `queued`: the folders under `t` with a change waiting.
#: - A reader's map (`{live}`): their files counted live, each through the read
#:   predicate, and the roll-up only for the other folders; true now.
#: - The whole share's: the roll-up for every folder; a folder with a change waiting
#:   is as of its count.
_GROUP_PARTS = """
, since AS MATERIALIZED (
    SELECT value::timestamptz AS at FROM catalog.share_map_state WHERE key = 'since'
), queued AS MATERIALIZED (
    SELECT t.file_id FROM t
     WHERE EXISTS (SELECT 1 FROM catalog.share_map_dirty q WHERE q.folder_id = t.file_id)
), too_many AS MATERIALIZED (
    SELECT {live} AND coalesce((SELECT sum(g.files) FROM catalog.share_map_group g
                                 WHERE g.folder_id IN (SELECT file_id FROM queued)), 0)
                      > {queued_max} AS yes
), each AS MATERIALIZED (
    SELECT c.folder_id IS NOT NULL AS counted,
           d.created_at < (SELECT at FROM since) AS before,
           q.file_id IS NOT NULL AS waiting,
           CASE WHEN q.file_id IS NOT NULL AND {live} THEN statement_timestamp()
                WHEN c.folder_id IS NULL THEN d.created_at
                WHEN q.file_id IS NOT NULL THEN c.counted_at
                ELSE statement_timestamp() END AS true_at
      FROM t JOIN catalog.file d ON d.file_id = t.file_id
      LEFT JOIN catalog.share_map_folder c ON c.folder_id = t.file_id
      LEFT JOIN queued q ON q.file_id = t.file_id
), counted AS MATERIALIZED (
    SELECT count(*) FILTER (WHERE NOT e.counted AND e.before AND NOT (e.waiting AND {live}))
           + CASE WHEN bool_or(e.counted OR (e.waiting AND {live})) THEN 0 ELSE 1 END
           + CASE WHEN (SELECT yes FROM too_many) THEN 1 ELSE 0 END AS missing,
           min(e.true_at) AS as_of
      FROM each e
), g AS MATERIALIZED (
    SELECT g.edge_state, g.logical_tier, g.storage_class, g.backend_id, g.files, g.bytes,
           g.local_bytes
      FROM catalog.share_map_group g
      LEFT JOIN catalog.acl acl ON acl.acl_id = g.acl_id
     WHERE g.folder_id IN (SELECT file_id FROM t) AND {readable}
       AND NOT ({live} AND g.folder_id IN (SELECT file_id FROM queued))
    UNION ALL
    SELECT f.state, l.logical_tier::text, l.storage_class, l.backend_id, 1,
           coalesce(f.size, 0), coalesce(f.resident_bytes, 0)
      FROM catalog.file f
      JOIN catalog.acl acl ON acl.acl_id = f.acl_id
      LEFT JOIN catalog.location l
        ON l.location_id = f.primary_location_id AND l.state = 'committed'
     WHERE {live} AND NOT (SELECT yes FROM too_many)
       AND f.parent_id IN (SELECT file_id FROM queued) AND f.kind = 'file'
       AND f.deleted_at IS NULL AND {readable_file}
)
SELECT 'edge' AS part, g.edge_state AS k1, NULL::text AS k2, NULL::bigint AS backend_id,
       sum(g.files) AS files, sum(g.bytes) AS bytes, sum(g.local_bytes) AS local_bytes,
       NULL::timestamptz AS as_of
  FROM g GROUP BY g.edge_state
UNION ALL
SELECT 'store', g.logical_tier, g.storage_class, g.backend_id, sum(g.files), sum(g.bytes), 0,
       NULL
  FROM g WHERE g.backend_id IS NOT NULL
 GROUP BY g.logical_tier, g.storage_class, g.backend_id
UNION ALL
SELECT 'counted', NULL, NULL, NULL, c.missing, 0, 0, c.as_of FROM counted c
UNION ALL
SELECT 'recall', r.state, NULL, NULL, count(*), coalesce(sum(f.size), 0), 0, NULL
  FROM catalog.recall r
  JOIN catalog.file f ON f.file_id = r.file_id AND r.version_id = f.content_version_id
  JOIN catalog.acl acl ON acl.acl_id = f.acl_id
 WHERE r.state = ANY(%(active)s) AND f.kind = 'file' AND f.deleted_at IS NULL
   AND f.parent_id IN (SELECT file_id FROM t) AND {readable_file}
 GROUP BY r.state
"""

#: Every folder under a share's root, readable or not: an ArchiveAdmin's and a
#: DataSteward's whole-share counts (decision 56).
_ALL_DIRS = """
WITH RECURSIVE t(file_id) AS (
    SELECT %(root)s::bigint
    UNION ALL
    SELECT f.file_id FROM catalog.file f JOIN t ON f.parent_id = t.file_id
     WHERE f.kind = 'dir' AND f.deleted_at IS NULL
)"""


def _map_whole() -> str:
    """The whole share's counts, names never shown (decision 56)."""
    return _ALL_DIRS + _GROUP_PARTS.format(
        readable="true", readable_file="true", live="false", queued_max=QUEUED_LIVE_FILES
    )


def _map_readable() -> str:
    """The counts of what the caller can read, in folders it can read: the groups
    of folders with nothing waiting, each through the same predicate as its files
    (catalog/read_set.py), and the files of folders with a change waiting, counted
    live through that predicate, so a file's ACL, folder or annex folder changing
    counts at once, worker or no worker."""
    return readable_dirs() + _GROUP_PARTS.format(
        readable=readable_group_sql("g.annexed", "g.dir_id"),
        readable_file=readable_sql("f.file_id", walked=True),
        live="true",
        queued_max=QUEUED_LIVE_FILES,
    )


async def _with_timeout(db, query: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """`query` under LIVE_TIMEOUT_MS, in a savepoint; None if it ran out of time.
    The timeout in force before is put back either way."""
    before = await (await db.execute("SELECT current_setting('statement_timeout') AS t")).fetchone()
    try:
        async with db.transaction():
            await db.execute(
                "SELECT set_config('statement_timeout', %s, true)", (f"{LIVE_TIMEOUT_MS}ms",)
            )
            found = list(await (await db.execute(query, params)).fetchall())
            await db.execute("SELECT set_config('statement_timeout', %s, true)", (before["t"],))
            return found
    except psycopg.errors.QueryCanceled:
        return None


async def _share_scope(ctx: Ctx, db) -> tuple[dict[str, Any], dict[str, Any], int]:
    """The share (404 if the caller can't see it), its dataset and root."""
    s = await load_share(db, ctx.path_id("share_id"))
    ok, _roles, _can_root, root = await visible(db, ctx.who, s)
    if not ok:
        raise not_found("share")
    ds = await (
        await db.execute(
            "SELECT d.dataset_id, d.backend_id, d.source_backend_id, d.storage_layout,"
            " d.migration_state FROM catalog.dataset d WHERE d.dataset_id = %s",
            (s["dataset_id"],),
        )
    ).fetchone()
    return s, ds, root


async def get_map(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin = who.has("ArchiveAdmin")
    # Whole-share totals, never names: ArchiveAdmin and DataSteward (owner, 2026-11-04).
    whole = who.has("ArchiveAdmin", "DataSteward")
    computed_at = None
    async with ctx.svc.catalog.read() as db:
        s, ds, root = await _share_scope(ctx, db)
        found: list[dict[str, Any]] | None
        params = {**acl_params(who), "root": root, "active": list(RECALL_STEP)}
        if whole:
            found = list(await (await db.execute(_map_whole(), params)).fetchall())
        else:
            found = await _with_timeout(db, _map_readable(), params)
        counted = next((r for r in found or [] if r["part"] == "counted"), None)
        recalls = [r for r in found or [] if r["part"] == "recall"]
        if found is None:
            basis = "none"  # the 3 s ran out
        elif counted is None or counted["files"] or counted["as_of"] is None:
            # A folder it would sum has no count yet: never a total of zero.
            basis, found = "counting", None
        else:
            # As of the oldest count summed.
            basis, computed_at = "rollup", counted["as_of"]
            found = [r for r in found if r["part"] in ("edge", "store")]
        found = None if found is None else found + recalls
        rows_ = found if found is not None else list(recalls)
        ids = [i for i in (ds["backend_id"], ds["source_backend_id"]) if i is not None]
        ids += [r["backend_id"] for r in rows_ if r["backend_id"] is not None]
        kinds = {
            r["backend_id"]: r["kind"]
            for r in await (
                await db.execute(
                    "SELECT backend_id, kind FROM catalog.backend WHERE backend_id = ANY(%s)",
                    (sorted(set(ids)),),
                )
            ).fetchall()
        }
        migrations = None
        if admin:
            migrations = [{
                "migration_id": str(m["migration_id"]),
                "state": m["state"],
                "prefix": m["prefix"],
                "files": {"total": m["files_total"], "verified": int(m["files_verified"])},
            } for m in await (await db.execute(
                "SELECT migration_id, state, prefix, files_total, files_verified"
                "  FROM catalog.migration_prefix WHERE dataset_id = %s AND state = ANY(%s)"
                " ORDER BY migration_id",
                (ds["dataset_id"], list(OPEN_MIGRATION)),
            )).fetchall()]  # fmt: skip

    edge = {b: {"files": 0, "bytes": 0, "local_bytes": 0} for b in EDGE_BUCKETS}
    steps = {"requested": 0, "rehydrating": 0, "downloading": 0}
    stores: dict[int, dict[str, Any]] = {}
    for r in rows_:
        if r["part"] == "edge":
            bucket = next((b for b, st in EDGE_BUCKETS.items() if r["k1"] in st), r["k1"])
            e = edge.setdefault(bucket, {"files": 0, "bytes": 0, "local_bytes": 0})
            e["files"] += int(r["files"])
            e["bytes"] += int(r["bytes"])
            e["local_bytes"] += int(r["local_bytes"])
        elif r["part"] == "recall":
            steps[RECALL_STEP[r["k1"]]] += int(r["files"])
        else:
            bid = r["backend_id"]
            kind = kinds.get(bid, "unknown")
            store = stores.setdefault(bid, {"backend_kind": kind, "role": _role(bid, ds),
                                            "tiers": []})  # fmt: skip
            store["tiers"].append({
                "logical_tier": r["k1"],
                "native_class": r["k2"],
                "account": account_of(kind, ds["storage_layout"], r["k1"]),
                "files": int(r["files"]),
                "bytes": int(r["bytes"]),
            })  # fmt: skip
    order = {t.value: i for i, t in enumerate(LADDER)}
    for store in stores.values():
        store["tiers"].sort(
            key=lambda t: (order.get(t["logical_tier"], 99), t["native_class"] or "")
        )
    role_order = {"home": 0, "source": 1, "other": 2}
    recalls_known = basis != "none"
    counted = found is not None
    return WebResult(200, {
        "share_id": str(s["share_id"]),
        "dataset_id": str(ds["dataset_id"]),
        "scope": "all" if whole else "readable",
        "basis": basis,
        "computed_at": rfc3339(computed_at),
        "home": {"backend_kind": kinds.get(ds["backend_id"], "unknown"),
                 "storage_layout": ds["storage_layout"]},
        "source": None if ds["source_backend_id"] is None
        else {"backend_kind": kinds.get(ds["source_backend_id"], "unknown")},
        "migration_state": ds["migration_state"],
        "edge": edge if counted else None,
        "stores": sorted(stores.values(), key=lambda x: role_order.get(x["role"], 9)),
        "recalls": {"open": sum(steps.values()), "by_step": steps} if recalls_known else None,
        "migrations": migrations,
    })  # fmt: skip


_NODE_FILES = """
SELECT f.file_id, f.name, f.parent_id, f.size, f.state, f.resident_bytes,
       f.restore_state, f.restore_expires_at,
       l.logical_tier::text AS logical_tier, l.storage_class, l.explicit_tier_set,
       l.tier_changed_at
  FROM catalog.file f
  JOIN catalog.acl acl ON acl.acl_id = f.acl_id
  LEFT JOIN catalog.location l
    ON l.location_id = f.primary_location_id AND l.state = 'committed'
 WHERE f.parent_id IN (SELECT file_id FROM t) AND f.kind = 'file' AND f.deleted_at IS NULL
   AND {readable}
   AND (%(states)s::text[] IS NULL OR f.state = ANY(%(states)s::text[]))
   AND (%(tier)s::text IS NULL OR l.logical_tier::text = %(tier)s::text)
   AND (%(backend)s::bigint IS NULL OR l.backend_id = %(backend)s::bigint)
   AND (NOT %(recalling)s OR EXISTS (
        SELECT 1 FROM catalog.recall r
         WHERE r.file_id = f.file_id AND r.version_id = f.content_version_id
           AND r.state = ANY(%(active)s)))
   AND (%(after)s::bigint IS NULL OR f.file_id > %(after)s::bigint)
 ORDER BY f.file_id LIMIT %(limit)s
"""


async def list_map_files(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    edge = ctx.query("edge")
    tier = ctx.query("tier")
    role = ctx.query("role")
    recalling = ctx.query("recalling")
    if edge is not None and edge not in EDGE_BUCKETS:
        raise invalid("edge", f"must be one of {', '.join(EDGE_BUCKETS)}")
    if tier is not None and tier not in {t.value for t in LADDER}:
        raise invalid("tier", "must be hot, warm, cold, archive or smart")
    if role is not None and role not in ("home", "source"):
        raise invalid("role", "must be home or source")
    if recalling not in (None, "true"):
        raise invalid("recalling", "must be true")
    if role is not None and tier is None:
        raise invalid("tier", "is required with role")
    if edge is None and tier is None and recalling is None:
        raise invalid("edge", "name a node: edge, tier (and role) or recalling")
    limit = ctx.limit(50, MAP_FILES_MAX)
    after = ctx.cursor("map_files")
    async with ctx.svc.catalog.read() as db:
        _s, ds, root = await _share_scope(ctx, db)
        backend = None
        if role is not None:
            backend = ds["backend_id"] if role == "home" else ds["source_backend_id"]
            if backend is None:
                return WebResult(200, page([], None))
        params = {
            **acl_params(who),
            "root": root,
            "states": list(EDGE_BUCKETS[edge]) if edge else None,
            "tier": tier,
            "backend": backend,
            "recalling": recalling == "true",
            "active": list(RECALL_STEP),
            "after": int(after[0]) if after else None,
            "limit": limit + 1,
        }
        got = await _with_timeout(
            db,
            readable_dirs() + _NODE_FILES.format(readable=readable_sql("f.file_id", walked=True)),
            params,
        )
        if got is None:
            raise WebError("unavailable", "too many files to list here now", retry_after_s=5)
        found = got
    more = len(found) > limit
    found = list(found[:limit])
    items = [{
        "file_id": str(f["file_id"]),
        "name": f["name"],
        "parent_id": rows.sid_(f["parent_id"]),
        "size": int(f["size"]),
        "state": f["state"],
        "resident_bytes": int(f["resident_bytes"]),
        "tier": rows.tier_state(f if f["logical_tier"] or f["storage_class"] else None, now),
    } for f in found]  # fmt: skip
    nxt = ctx.next_cursor("map_files", [found[-1]["file_id"]]) if more else None
    return WebResult(200, page(items, nxt))
