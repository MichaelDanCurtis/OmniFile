"""The live Activity feed: `GET /v1/activity/live` (portal-api.md section 5.13).

One answer holds what happened since the caller's `since`, newest first, from
six sources, plus the counters and per-minute series the page draws and a strip
of the edges:

| kind        | from                                                   | id            |
|-------------|--------------------------------------------------------|---------------|
| `upload`    | content versions an edge committed                     | `up:<version>`|
| `recall`    | each state a recall reached                            | `rc:<log id>` |
| `tier`      | the tiering worker's audited moves (`tier.move*`)      | `au:<audit>`  |
| `heartbeat` | each edge's latest heartbeat                           | `hb:<edge>:<s>`|
| `migration` | migrations' typed events (started, verified, cut over) | `mg:<event>`  |
| `annex`     | annex events (arrived, copied, verified, left, ...)    | `an:<event>`  |

Every source is read by time through an index (migration 0043), and only for a
short window: `since` minus `OVERLAP` (rows of transactions that committed late
still show; the page drops ids it has), never more than `MAX_LOOKBACK`, and
never past now (0031's commit floor dates some versions ahead). The filters
(edge, share, person, and an app's datasets) apply inside each source, before
its limit, so a busy edge can't push a quiet share's events out of an answer.

**Access.** The route's gate is the one of `GET /v1/activity`: ArchiveAdmin, or
an app with `Archive.Read`. Roles never grant read (decision 28): a file's name
and path are shown only when the caller's AD groups may read the file
(`catalog.read_set.readable_sql`); otherwise the event says only that a file of the share
changed, with no name, no path and no file id. An app has no SID, so it sees no
names, and no people. An app also sees only the datasets its API client's owner
may see (`apiclients.access`, as for the annex reads): events, series and the
edge strip's counts alike. People are named by their catalog display name, as
in the audit log.

**Long-poll.** `wait` (0 to 25 s) holds the request until something newer than
`since` (and not after now) exists, probing every `live_probe_s` with one cheap
query (an EXISTS per source, each a short index range). A replica holds at most
`live_max_waiting` polls, and no more than twice its catalog pool, so a small
pool isn't swamped by probes; beyond that it answers at once and says to come
back in `next_poll_s` (10 s). `next_poll_s` is never under 2 s. No connection
is held while waiting.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..api.validate import rfc3339, timestamp
from ..apiclients.access import datasets_owner_can_see
from ..catalog.read_set import readable_page_sql
from .access import acl_params, covering_share
from .context import Ctx, WebResult
from .problems import WebError, invalid, not_found
from .routes_edges import status_of
from .routes_sources import _reader

KINDS = ("upload", "recall", "tier", "heartbeat", "migration", "annex")
OVERLAP = timedelta(seconds=30)
MAX_LOOKBACK = timedelta(minutes=15)
WAIT_MAX_S = 25
LIMIT_MAX = 200
SERIES_MINUTES = 30
#: The shortest pause a client is told to make between polls.
MIN_POLL_S = 2
CAPPED_POLL_S = 10
ACTIVE = ("requested", "rehydrating", "copying", "downloading")
TIER_ACTIONS = ("tier.move", "tier.move.refused", "tier.move.waiting", "tier.archive.late")


@dataclass(frozen=True)
class Source:
    """One kind's SELECT (its FROM and base WHERE, no ORDER or LIMIT) and the
    expressions the filters apply to; None where the kind has no such thing."""

    select: str
    at: str
    file: str | None
    ds: str | None
    edge: str
    person: str | None = None
    actor: str | None = None


_SOURCES: dict[str, Source] = {
    "upload": Source(
        """
        SELECT 'upload' AS kind, 'up:' || v.version_id AS id, v.committed_at AS at,
               v.edge_id, v.dataset_id, v.file_id, v.size AS bytes,
               v.actor_identity_id AS identity_id, NULL::text AS actor, NULL::text AS state,
               jsonb_build_object('via', v.actor_via) AS detail
          FROM catalog.file_version v
         WHERE v.kind = 'content' AND v.edge_id IS NOT NULL AND v.committed_at IS NOT NULL""",
        at="v.committed_at", file="v.file_id", ds="v.dataset_id", edge="v.edge_id",
        person="v.actor_identity_id",
    ),
    "recall": Source(
        """
        SELECT 'recall', 'rc:' || l.log_id, l.at, coalesce(r.edge_id, d.edge_id), f.dataset_id,
               r.file_id, v.size, q.identity_id, NULL::text, l.state,
               jsonb_build_object('recall_id', r.recall_id::text, 'priority', r.priority,
                                  'via', q.via)
          FROM catalog.recall_state_log l
          JOIN catalog.recall r ON r.recall_id = l.recall_id
          JOIN catalog.file f ON f.file_id = r.file_id
          JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
          JOIN catalog.file_version v ON v.version_id = r.version_id
          LEFT JOIN LATERAL (SELECT rq.identity_id, rq.via FROM catalog.recall_requester rq
                              WHERE rq.recall_id = r.recall_id
                              ORDER BY rq.requester_id LIMIT 1) q ON true
         WHERE true""",
        at="l.at", file="r.file_id", ds="f.dataset_id", edge="coalesce(r.edge_id, d.edge_id)",
        person="q.identity_id",
    ),
    "tier": Source(
        """
        SELECT 'tier', 'au:' || a.audit_id, a.at, d.edge_id, d.dataset_id, a.file_id, v.size,
               NULL::bigint, a.actor, CASE a.action WHEN 'tier.move' THEN 'moved'
                                                     WHEN 'tier.archive.late' THEN 'late'
                                                     WHEN 'tier.move.waiting' THEN 'waiting'
                                                     ELSE 'refused' END,
               a.details - 'why'
          FROM catalog.audit_log a
          LEFT JOIN catalog.file f ON f.file_id = a.file_id
          LEFT JOIN catalog.dataset d ON d.dataset_id = coalesce(a.dataset_id, f.dataset_id)
          LEFT JOIN catalog.file_version v ON v.version_id = a.version_id
         WHERE a.action = ANY(%(tier_actions)s)""",
        at="a.at", file="a.file_id", ds="d.dataset_id", edge="d.edge_id", actor="a.actor",
    ),
    "heartbeat": Source(
        """
        SELECT 'heartbeat', 'hb:' || e.edge_id || ':'
                            || floor(extract(epoch FROM e.last_heartbeat_at))::bigint,
               e.last_heartbeat_at, e.edge_id, NULL::bigint, NULL::bigint, NULL::bigint,
               NULL::bigint, NULL::text, e.guard_state,
               jsonb_build_object('software_version', e.software_version,
                                  'outbox_depth', e.health -> 'outbox_depth')
          FROM catalog.edge e
         WHERE e.state = 'active'""",
        at="e.last_heartbeat_at", file=None, ds=None, edge="e.edge_id",
    ),
    # The prefix is a source path: left out, like any name the caller may not read.
    "migration": Source(
        """
        SELECT 'migration', 'mg:' || p.event_id, p.created_at, d.edge_id, p.dataset_id,
               NULL::bigint, NULL::bigint, NULL::bigint, NULL::text,
               substr(p.type, length('migration.') + 1),
               jsonb_build_object('migration_id', p.entity_id::text, 'mode', p.data -> 'mode')
          FROM catalog.partner_event p
          JOIN catalog.dataset d ON d.dataset_id = p.dataset_id
         WHERE p.entity = 'migration'""",
        at="p.created_at", file=None, ds="p.dataset_id", edge="d.edge_id",
    ),
    "annex": Source(
        """
        SELECT 'annex', 'an:' || e.event_id, e.at, a.edge_id, a.dataset_id, e.file_id, e.size,
               NULL::bigint, NULL::text, e.kind,
               jsonb_build_object('annex_id', a.annex_id::text, 'annex_name', a.name)
          FROM catalog.annex_event e
          JOIN catalog.annex a ON a.annex_id = e.annex_id
         WHERE true""",
        at="e.at", file="e.file_id", ds="a.dataset_id", edge="a.edge_id",
    ),
}  # fmt: skip

#: Anything newer than `since`, and not after now? One EXISTS per kind, each a
#: short index range. Unbounded above, a row dated ahead (0031's commit floor)
#: would end every wait at once while the answer, which stops at now, stays empty.
_PROBES: dict[str, str] = {
    "upload": "EXISTS (SELECT 1 FROM catalog.file_version v WHERE v.kind = 'content'"
    " AND v.edge_id IS NOT NULL AND v.committed_at IS NOT NULL"
    " AND v.committed_at > %(since)s AND v.committed_at <= %(until)s)",
    "recall": "EXISTS (SELECT 1 FROM catalog.recall_state_log l"
    " WHERE l.at > %(since)s AND l.at <= %(until)s)",
    "tier": "EXISTS (SELECT 1 FROM catalog.audit_log a WHERE a.at > %(since)s"
    " AND a.at <= %(until)s AND a.action = ANY(%(tier_actions)s))",
    "heartbeat": "EXISTS (SELECT 1 FROM catalog.edge e WHERE e.state = 'active'"
    " AND e.last_heartbeat_at > %(since)s AND e.last_heartbeat_at <= %(until)s)",
    "migration": "EXISTS (SELECT 1 FROM catalog.partner_event p WHERE p.entity = 'migration'"
    " AND p.created_at > %(since)s AND p.created_at <= %(until)s)",
    "annex": "EXISTS (SELECT 1 FROM catalog.annex_event e"
    " WHERE e.at > %(since)s AND e.at <= %(until)s)",
}

#: An app sees only its client owner's datasets (`ds_ok`; NULL: no such limit).
_DS_OK = "(%(ds_ok)s::bigint[] IS NULL OR {ds} = ANY(%(ds_ok)s::bigint[]))"

_SERIES = f"""
SELECT date_trunc('minute', v.committed_at) AS m, v.edge_id, count(*) AS files,
       coalesce(sum(v.size), 0) AS bytes
  FROM catalog.file_version v
 WHERE v.kind = 'content' AND v.edge_id IS NOT NULL AND v.committed_at IS NOT NULL
   AND v.committed_at > %(from)s AND v.committed_at <= %(until)s
   AND {_DS_OK.format(ds="v.dataset_id")}
 GROUP BY 1, 2
"""

_RECALLED = f"""
SELECT date_trunc('minute', l.at) AS m, count(*) AS n, coalesce(sum(v.size), 0) AS bytes
  FROM catalog.recall_state_log l
  JOIN catalog.recall r ON r.recall_id = l.recall_id
  JOIN catalog.file_version v ON v.version_id = r.version_id
 WHERE l.state = 'available' AND l.at > %(from)s AND l.at <= %(until)s
   AND {_DS_OK.format(ds="v.dataset_id")}
 GROUP BY 1
"""

_OPEN = f"""
SELECT coalesce(r.edge_id, d.edge_id) AS edge_id, count(*) AS n
  FROM catalog.recall r
  JOIN catalog.file f ON f.file_id = r.file_id
  JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
 WHERE r.state = ANY(%(active)s) AND {_DS_OK.format(ds="f.dataset_id")}
 GROUP BY 1
"""

_EDGES = """
SELECT edge_id, name, state, guard_state, health, last_heartbeat_at
  FROM catalog.edge WHERE state <> 'revoked' ORDER BY name
"""

#: The share filter: a file in the share's dataset, under its root, and not under
#: a share nested inside it (a file belongs to the deepest share that holds it).
_IN_SHARE = """(%(share_ds)s::bigint IS NULL OR ({ds} = %(share_ds)s::bigint
     AND (%(share_root)s::bigint IS NULL OR EXISTS (
          SELECT 1 FROM catalog.file_ancestors({file}) sa
           WHERE sa.file_id = %(share_root)s::bigint))
     AND NOT EXISTS (
          SELECT 1 FROM catalog.file_ancestors({file}) sn
           WHERE sn.file_id = ANY(%(share_nested)s::bigint[]))))"""


def _source_sql(kind: str) -> str:
    s = _SOURCES[kind]
    where = [
        f"{s.at} > %(since)s",
        f"{s.at} <= %(until)s",
        f"(%(edge)s::bigint IS NULL OR {s.edge} = %(edge)s::bigint)",
    ]
    who = []
    if s.person:
        who.append(f"{s.person} = %(person)s::bigint")
    if s.actor:
        who.append(f"{s.actor} = 'user:' || %(person)s::text")
    where.append(f"(%(person)s::bigint IS NULL OR {' OR '.join(who) or 'false'})")
    # A kind with no dataset (heartbeats) is about an edge, which apps may list.
    if s.ds:
        where.append(_DS_OK.format(ds=s.ds))
    if s.file and s.ds:
        where.append(_IN_SHARE.format(ds=s.ds, file=s.file))
    else:
        where.append("%(share_ds)s::bigint IS NULL")
    return (
        f"{s.select}\n           AND "
        + "\n           AND ".join(where)
        + (f"\n         ORDER BY {s.at} DESC LIMIT %(n)s")
    )


class LiveGate:
    """How many long-polls this replica holds at once."""

    def __init__(self) -> None:
        self.waiting = 0


def _gate(ctx: Ctx) -> LiveGate:
    app = ctx.request.app
    gate = getattr(app.state, "live_gate", None)
    if gate is None:
        gate = app.state.live_gate = LiveGate()
    return gate


def _int_param(ctx: Ctx, name: str, lo: int, hi: int, default: int) -> int:
    raw = ctx.query(name)
    if raw is None:
        return default
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise invalid(name, f"must be an integer from {lo} to {hi}")
    return int(raw)


def _kinds(ctx: Ctx) -> list[str]:
    raw = ctx.query("kind")
    if raw is None or raw == "":
        return list(KINDS)
    out = []
    for k in raw.split(","):
        if k not in KINDS:
            raise invalid("kind", "a comma-separated list of " + ", ".join(KINDS))
        if k not in out:
            out.append(k)
    return out


async def _names(db, ctx: Ctx, rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per file: its covering share, and its name and path only if readable."""
    fids = sorted({r["file_id"] for r in rows if r["file_id"] is not None})
    if not fids:
        return {}
    who = ctx.who
    chains: dict[int, list[dict[str, Any]]] = {f: [] for f in fids}
    found = await (
        await db.execute(
            f"""
            SELECT x.fid, a.file_id, a.parent_id, a.name, a.depth,
                   (a.acl_id IS NOT NULL AND {readable_page_sql("a.file_id")}) AS readable
              FROM unnest(%(ids)s::bigint[]) AS x (fid)
             CROSS JOIN LATERAL catalog.file_ancestors(x.fid) a
              LEFT JOIN catalog.acl acl ON acl.acl_id = a.acl_id
             ORDER BY x.fid, a.depth
            """,
            {**acl_params(who), "ids": fids},
        )
    ).fetchall()
    for r in found:
        chains[r["fid"]].append(dict(r))
    shares = await (
        await db.execute("SELECT share_id, name, dataset_id, root_dir_id FROM catalog.share")
    ).fetchall()
    by_ds: dict[int, list[dict[str, Any]]] = {}
    for s in shares:
        by_ds.setdefault(s["dataset_id"], []).append(dict(s))
    ds_of = {r["file_id"]: r["dataset_id"] for r in rows if r["file_id"] is not None}
    out: dict[int, dict[str, Any]] = {}
    blind = who.is_app or who.sid is None
    for fid, chain in chains.items():
        share = covering_share(by_ds.get(ds_of.get(fid), []), [c["file_id"] for c in chain])
        # Read access alone decides (an annex's dataset has no share: its files carry
        # the NAS's captured ACLs).
        readable = bool(chain) and chain[0]["readable"] and not blind
        path = None
        if readable:
            segs = []
            root = share["root_dir_id"] if share else None
            for c in chain:
                if c["file_id"] == root or (root is None and c["parent_id"] is None):
                    break
                # An unreadable folder above a readable file is nameless (access.FileScope.path).
                segs.append(c["name"] if c["readable"] else "…")
            path = "/".join(reversed(segs))
        out[fid] = {
            "share": share,
            "readable": readable,
            "name": chain[0]["name"] if readable else None,
            "path": path,
        }
    return out


async def _people(db, rows: list[dict[str, Any]]) -> dict[int, str | None]:
    ids = {r["identity_id"] for r in rows if r["identity_id"] is not None}
    for r in rows:
        a = r["actor"] or ""
        if a.startswith("user:") and a[5:].isdigit():
            ids.add(int(a[5:]))
    if not ids:
        return {}
    found = await (
        await db.execute(
            "SELECT identity_id, coalesce(display_name, upn, sam_account_name) AS name"
            " FROM catalog.identity WHERE identity_id = ANY(%s)",
            (sorted(ids),),
        )
    ).fetchall()
    return {r["identity_id"]: r["name"] for r in found}


def _person_of(r: dict[str, Any], people: dict[int, str | None]) -> dict[str, Any] | None:
    iid = r["identity_id"]
    a = r["actor"] or ""
    if iid is None and a.startswith("user:") and a[5:].isdigit():
        iid = int(a[5:])
    if iid is not None:
        return {"identity_id": str(iid), "display_name": people.get(iid)}
    return None


def _events_sql(kinds: list[str]) -> str:
    union = "\nUNION ALL\n".join(f"({_source_sql(k)})" for k in kinds)
    return f"""
        SELECT * FROM ({union}) ev (kind, id, at, edge_id, dataset_id, file_id, bytes,
                                    identity_id, actor, state, detail)
         ORDER BY ev.at DESC, ev.id DESC
         LIMIT %(n)s"""


async def _share_filter(db, share_id: int | None) -> dict[str, Any]:
    """The share's dataset, root and the roots of shares nested in it."""
    if share_id is None:
        return {"share_ds": None, "share_root": None, "share_nested": []}
    s = await (
        await db.execute(
            "SELECT dataset_id, root_dir_id FROM catalog.share WHERE share_id = %s", (share_id,)
        )
    ).fetchone()
    if s is None:
        raise not_found("share")
    nested = await (
        await db.execute(
            "SELECT o.root_dir_id FROM catalog.share o WHERE o.dataset_id = %(ds)s"
            " AND o.share_id <> %(share)s AND o.root_dir_id IS NOT NULL"
            " AND (%(root)s::bigint IS NULL OR EXISTS (SELECT 1 FROM"
            " catalog.file_ancestors(o.root_dir_id) a WHERE a.file_id = %(root)s::bigint"
            " AND a.depth > 0))",
            {"ds": s["dataset_id"], "share": share_id, "root": s["root_dir_id"]},
        )
    ).fetchall()
    return {
        "share_ds": s["dataset_id"],
        "share_root": s["root_dir_id"],
        "share_nested": [n["root_dir_id"] for n in nested],
    }


async def _app_datasets(db, ctx: Ctx) -> list[int] | None:
    """For an app: the datasets its API client's owner may see. None for a user."""
    who = ctx.who
    if not who.is_app:
        return None
    if who.api_client is None:
        # Every admitted app has a client (apiclients.registry); fail closed.
        raise WebError("client_not_registered", "this application is not registered")
    return await datasets_owner_can_see(db, who.api_client["owner_identity_id"], ctx.now)


async def _probe(ctx: Ctx, kinds: list[str], since) -> bool:
    sql = "SELECT " + " OR ".join(_PROBES[k] for k in kinds) + " AS hit"
    params = {"since": since, "until": ctx.now, "tier_actions": list(TIER_ACTIONS)}
    async with ctx.svc.catalog.read() as db:
        row = await (await db.execute(sql, params)).fetchone()
    return bool(row and row["hit"])


def _max_waiting(ctx: Ctx) -> int:
    """The settings' cap, and at most twice the catalog pool: each held poll
    probes every couple of seconds, and the small profile's pool is 4."""
    pool = getattr(getattr(ctx.svc.catalog, "pool", None), "max_size", None)
    cap = ctx.svc.settings.live_max_waiting
    return min(cap, 2 * pool) if isinstance(pool, int) and pool > 0 else cap


async def get_live(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    kinds = _kinds(ctx)
    limit = _int_param(ctx, "limit", 1, LIMIT_MAX, 100)
    wait = _int_param(ctx, "wait", 0, WAIT_MAX_S, 0)
    edge = ctx.query_id("edge_id")
    share_id = ctx.query_id("share_id")
    person = ctx.query_id("identity_id")
    if person is not None and ctx.who.is_app:
        # An app is never told who did what; a filter by person would tell it (#63).
        raise invalid("identity_id", "an application may not filter by person")
    now = ctx.now
    raw_since = ctx.query("since")
    since = now - MAX_LOOKBACK
    if raw_since is not None:
        since = max(timestamp(raw_since, "since"), since)
    async with ctx.svc.catalog.read() as db:
        # Before any wait: a caller refused here is refused at once.
        ds_ok = await _app_datasets(db, ctx)
        share = await _share_filter(db, share_id)
    s = ctx.svc.settings
    gate = _gate(ctx)
    capped = False
    waited = False
    if wait and raw_since is not None:
        if gate.waiting >= _max_waiting(ctx):
            capped = True
        else:
            gate.waiting += 1
            try:
                deadline = time.monotonic() + wait
                while not await _probe(ctx, kinds, since):
                    if time.monotonic() >= deadline or await ctx.request.is_disconnected():
                        break
                    await asyncio.sleep(min(s.live_probe_s, max(0.0, deadline - time.monotonic())))
                waited = True
            finally:
                gate.waiting -= 1
            now = ctx.now
    params: dict[str, Any] = {
        "since": since - OVERLAP if raw_since is not None else since,
        "until": now,
        "n": limit,
        "edge": edge,
        "person": person,
        "tier_actions": list(TIER_ACTIONS),
        "from": now - timedelta(minutes=SERIES_MINUTES),
        "active": list(ACTIVE),
        "ds_ok": ds_ok,
        **share,
    }
    async with ctx.svc.catalog.read() as db:
        rows = [dict(r) for r in await (await db.execute(_events_sql(kinds), params)).fetchall()]
        names = await _names(db, ctx, rows)
        # An app sees nobody: who did what is for people signed in (#63 review).
        people = {} if ctx.who.is_app else await _people(db, rows)
        series = await (await db.execute(_SERIES, params)).fetchall()
        recalled = await (await db.execute(_RECALLED, params)).fetchall()
        open_by_edge = await (await db.execute(_OPEN, params)).fetchall()
        edges = await (await db.execute(_EDGES)).fetchall()
    edge_names = {e["edge_id"]: e["name"] for e in edges}
    events = []
    for r in rows:
        n = names.get(r["file_id"]) if r["file_id"] is not None else None
        shared = n["share"] if n else None
        readable = bool(n and n["readable"])
        events.append({
            "id": r["id"],
            "at": rfc3339(r["at"]),
            "kind": r["kind"],
            "state": r["state"],
            "edge_id": None if r["edge_id"] is None else str(r["edge_id"]),
            "edge_name": edge_names.get(r["edge_id"]),
            "dataset_id": None if r["dataset_id"] is None else str(r["dataset_id"]),
            "share_id": None if shared is None else str(shared["share_id"]),
            "share_name": None if shared is None else shared["name"],
            "file_id": str(r["file_id"]) if readable else None,
            "name": n["name"] if readable else None,
            "path": n["path"] if readable else None,
            "hidden": r["file_id"] is not None and not readable,
            "bytes": r["bytes"],
            "person": None if ctx.who.is_app else _person_of(r, people),
            "detail": {k: v for k, v in (r["detail"] or {}).items() if v is not None},
        })  # fmt: skip
    minute = now.replace(second=0, microsecond=0)
    start = minute - timedelta(minutes=SERIES_MINUTES - 1)
    idx = {start + timedelta(minutes=i): i for i in range(SERIES_MINUTES)}
    up_files = [0] * SERIES_MINUTES
    moved = [0] * SERIES_MINUTES
    per_edge: dict[int, dict[str, int]] = {}
    recent = minute - timedelta(minutes=14)
    for p in series:
        i = idx.get(p["m"])
        if i is not None:
            up_files[i] += int(p["files"])
            moved[i] += int(p["bytes"])
        if p["m"] >= recent:
            e = per_edge.setdefault(p["edge_id"], {"files": 0, "bytes": 0})
            e["files"] += int(p["files"])
            e["bytes"] += int(p["bytes"])
    for p in recalled:
        i = idx.get(p["m"])
        if i is not None:
            moved[i] += int(p["bytes"])
    open_n = {o["edge_id"]: int(o["n"]) for o in open_by_edge}
    strip = []
    for e in edges:
        st = status_of(e, now)
        pe = per_edge.get(e["edge_id"], {"files": 0, "bytes": 0})
        h = e["health"] or {}
        strip.append({
            "edge_id": str(e["edge_id"]),
            "name": e["name"],
            "status": st,
            "last_heartbeat_at": rfc3339(e["last_heartbeat_at"]),
            "uploads_15m": pe["files"],
            "bytes_15m": pe["bytes"],
            "recalls_open": open_n.get(e["edge_id"], 0),
            "outbox_depth": h.get("outbox_depth") if isinstance(h.get("outbox_depth"), int)
            else None,
        })  # fmt: skip
    online = sum(1 for e in strip if e["status"] in ("ok", "attention"))
    return WebResult(200, {
        "cursor": rfc3339(now),
        "events": events,
        "counters": {
            "uploads_per_min": up_files[-1],
            "uploads_15m": sum(up_files[-15:]),
            "bytes_moved_15m": sum(moved[-15:]),
            "recalls_in_flight": sum(open_n.values()),
            "edges_online": online,
            "edges_total": sum(1 for e in strip if e["status"] != "pending"),
        },
        "series": {
            "from": rfc3339(start),
            "step_s": 60,
            "uploads": up_files,
            "bytes_moved": moved,
        },
        "edges": strip,
        "live": {
            "waited": waited,
            "capped": capped,
            "next_poll_s": CAPPED_POLL_S if capped else MIN_POLL_S,
        },
    })  # fmt: skip
