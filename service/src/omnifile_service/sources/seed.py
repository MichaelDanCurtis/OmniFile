"""Write one batch of a source's objects into the catalog
(docs/design/catalog-seeding.md sections 2-4 and 6).

Two ways to write the files, with the same result (`tests/sources/
test_bulk_seed.py` compares the catalogs they leave):

- **Row by row** (small batches, and S3 events through `follow.py`): one
  INSERT per table for the files, their versions and their primary
  locations, then one UPDATE committing the versions. The catalog's per-row
  triggers link each file to its version and location and set WORM
  retention and legal holds; the statement-level feed trigger emits the edge
  events.
- **Bulk** (seed batches): the files are COPYed into the unlogged staging
  table `catalog.seed_stage` and merged by `catalog.seed_merge` (migration
  0033), which writes every row in its final, committed form in a few
  set-based statements. The per-row INSERT triggers are skipped while it
  runs, and their work is done once per folder or once per batch.

Both share everything around that step: the present check, key to path,
folders level by level, conflicts within the batch, and the drift rows.

Idempotent: an object whose `(backend, container, key, version)` already
has a live location is counted as present and skipped, so a replayed batch
changes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from ..reconcile.drift import record_drift
from .acl import DIR_MODE, FILE_MODE, Groups, source_acls
from .model import SeedObject
from .paths import Placement, Unmappable, place, split_path

SEED_ACTOR = "system:seed"
#: The `file.origin` of seeded rows, by source kind.
ORIGIN = {"source": "s3_inventory", "adopt": "adopt"}


@dataclass(frozen=True)
class Source:
    source_id: int
    dataset_id: int
    path: str
    backend_id: int
    container: str
    prefix: str
    groups: Groups
    snapshot_at: datetime
    inventory: Mapping[str, Any] | None = None
    resume: Mapping[str, Any] | None = None
    state: str = "registered"
    kind: str = "source"

    @property
    def origin(self) -> str:
        return ORIGIN[self.kind]

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> Source:
        return cls(
            source_id=r["source_id"],
            dataset_id=r["dataset_id"],
            path=r["path"],
            backend_id=r["backend_id"],
            container=r["container"],
            prefix=r["prefix"],
            groups=Groups(r["owners_sid"], r["writers_sid"], r["readers_sid"]),
            snapshot_at=r["snapshot_at"],
            inventory=r["inventory"],
            resume=r["resume"],
            state=r["state"],
            kind=r.get("kind", "source"),
        )


@dataclass
class Written:
    files: int = 0
    dirs: int = 0
    present: int = 0
    skipped: int = 0
    drift: list[tuple[SeedObject, str, str]] = field(default_factory=list)


def mtime_ns(t: datetime) -> int:
    return int(t.timestamp()) * 1_000_000_000 + t.microsecond * 1000


async def _uid_gid(conn: psycopg.AsyncConnection, dataset_id: int) -> tuple[int, int, int]:
    cur = await conn.execute(
        "SELECT posix_defaults, (SELECT file_id FROM catalog.file"
        "  WHERE dataset_id = d.dataset_id AND parent_id IS NULL)"
        " FROM catalog.dataset d WHERE dataset_id = %s",
        (dataset_id,),
    )
    row = await cur.fetchone()
    if row is None or row[1] is None:
        raise ValueError(f"dataset {dataset_id} has no root folder")
    defaults = row[0] or {}
    return int(defaults.get("uid", 0)), int(defaults.get("gid", 0)), int(row[1])


async def _present(
    conn: psycopg.AsyncConnection, src: Source, objects: Sequence[SeedObject]
) -> set[tuple[str, str]]:
    cur = await conn.execute(
        "SELECT object_key, coalesce(backend_version_id, '') FROM catalog.location"
        " WHERE backend_id = %s AND container = %s AND object_key = ANY(%s::text[])"
        "   AND state IN ('pending', 'committed')",
        (src.backend_id, src.container, list({o.key for o in objects})),
    )
    return {(k, v) for k, v in await cur.fetchall()}


async def _children(
    conn: psycopg.AsyncConnection, pairs: Sequence[tuple[int, str]]
) -> dict[tuple[int, str], tuple[int, str]]:
    """(parent_id, name) -> (file_id, kind) for live entries."""
    if not pairs:
        return {}
    cur = await conn.execute(
        "SELECT f.parent_id, f.name, f.file_id, f.kind FROM catalog.file f"
        "  JOIN unnest(%s::bigint[], %s::text[]) AS t(p, n)"
        "    ON f.parent_id = t.p AND f.name = t.n"
        " WHERE f.deleted_at IS NULL",
        ([p for p, _ in pairs], [n for _, n in pairs]),
    )
    return {(p, n): (fid, kind) for p, n, fid, kind in await cur.fetchall()}


async def _folders(
    conn: psycopg.AsyncConnection,
    src: Source,
    paths: set[tuple[str, ...]],
    root: int,
    acl: int,
    uid: int,
    gid: int,
    out: Written,
) -> dict[tuple[str, ...], int | None]:
    """Every folder in `paths` (and their parents), created if missing.
    None marks a path blocked by a file (a path conflict)."""
    need: set[tuple[str, ...]] = set()
    for p in paths:
        need.update(p[:i] for i in range(1, len(p) + 1))
    resolved: dict[tuple[str, ...], int | None] = {(): root}
    for depth in range(1, max((len(p) for p in need), default=0) + 1):
        level = sorted(p for p in need if len(p) == depth)
        todo = []
        for p in level:
            parent = resolved[p[:-1]]
            if parent is None:
                resolved[p] = None
            else:
                todo.append((parent, p))
        found = await _children(conn, [(parent, p[-1]) for parent, p in todo])
        missing = [(parent, p) for parent, p in todo if (parent, p[-1]) not in found]
        if missing:
            cur = await conn.execute(
                """
                INSERT INTO catalog.file
                    (dataset_id, parent_id, name, kind, acl_id, uid, gid, mode, origin)
                SELECT %s, t.p, t.n, 'dir', %s, %s, %s, %s, %s
                  FROM unnest(%s::bigint[], %s::text[]) AS t(p, n)
                ON CONFLICT (parent_id, name) WHERE deleted_at IS NULL DO NOTHING
                RETURNING parent_id, name, file_id
                """,
                (src.dataset_id, acl, uid, gid, DIR_MODE, src.origin,
                 [parent for parent, _ in missing], [p[-1] for _, p in missing]),
            )  # fmt: skip
            made = {(p, n): (fid, "dir") for p, n, fid in await cur.fetchall()}
            out.dirs += len(made)
            found.update(made)
            raced = [(parent, p) for parent, p in missing if (parent, p[-1]) not in found]
            found.update(await _children(conn, [(parent, p[-1]) for parent, p in raced]))
        for parent, p in todo:
            hit = found.get((parent, p[-1]))
            resolved[p] = hit[0] if hit is not None and hit[1] == "dir" else None
    return resolved


async def write_batch(
    conn: psycopg.AsyncConnection,
    src: Source,
    objects: Sequence[SeedObject],
    now: datetime,
    *,
    seen_at: datetime | None = None,
    drift_source: str = "seed",
    actor: str | None = SEED_ACTOR,
    check_present: bool = True,
    bulk: bool = False,
) -> Written:
    """Write `objects` on `conn`'s current transaction. `seen_at` is the
    locations' `feed_seen_at` (default: the source's snapshot); `actor`
    None keeps the caller's. `check_present` False writes an object even if
    a location already names its version (the caller has checked the path).
    `bulk` writes the files through the staging table and `seed_merge`."""
    out = Written()
    if not objects:
        return out
    if actor is not None:
        await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (actor,))
    dir_acl, file_acl = await source_acls(conn, src.dataset_id, src.groups)
    uid, gid, root = await _uid_gid(conn, src.dataset_id)
    base = split_path(src.path)
    present = await _present(conn, src, objects) if check_present else set()

    placed: list[tuple[SeedObject, Placement]] = []
    for o in objects:
        if (o.key, o.version_id or "") in present:
            out.present += 1
            continue
        try:
            placed.append((o, place(o.key, src.prefix, base, o.size)))
        except Unmappable as e:
            out.drift.append((o, "unmappable", str(e)))

    folders = {pl.folders for _, pl in placed}
    resolved = await _folders(conn, src, folders, root, dir_acl, uid, gid, out)

    files: dict[tuple[int, str], SeedObject] = {}
    for o, pl in placed:
        parent = resolved.get(pl.folders)
        if pl.name is None:  # a folder marker: the folder is the object
            if parent is None:
                out.drift.append((o, "path_conflict", "a file is where this folder goes"))
            continue
        if parent is None:
            out.drift.append((o, "path_conflict", "a file is where a parent folder goes"))
        elif (parent, pl.name) in files or pl.folders + (pl.name,) in resolved:
            out.drift.append((o, "path_conflict", "a folder or another object has this path"))
        else:
            files[(parent, pl.name)] = o

    if files:
        keys = list(files)
        write = _merge if bulk else _insert_files
        made = await write(conn, src, keys, files, file_acl, uid, gid, seen_at or src.snapshot_at)
        for k in keys:
            if k not in made:
                out.drift.append((files[k], "path_conflict", "an existing entry has this path"))
        out.files = len(made)

    for o, kind, why in out.drift:
        await record_drift(
            conn, source=drift_source, backend_id=src.backend_id, container=src.container,
            key=o.key, version=o.version_id, kind=kind, now=now,
            observed={"source_id": src.source_id, "reason": why, "size": o.size},
        )  # fmt: skip
    out.skipped = len(out.drift)
    return out


async def _insert_files(
    conn: psycopg.AsyncConnection,
    src: Source,
    keys: Sequence[tuple[int, str]],
    files: Mapping[tuple[int, str], SeedObject],
    file_acl: int,
    uid: int,
    gid: int,
    seen_at: datetime,
) -> dict[tuple[int, str], int]:
    """Row by row: (parent, name) -> file id of each file written."""
    objs = [files[k] for k in keys]
    cur = await conn.execute(
        """
        INSERT INTO catalog.file
            (dataset_id, parent_id, name, kind, state, size, resident_bytes, acl_id,
             mtime_ns, uid, gid, mode, origin)
        SELECT %s, t.p, t.n, 'file', 'remote', t.s, 0, %s, t.m, %s, %s, %s, %s
          FROM unnest(%s::bigint[], %s::text[], %s::bigint[], %s::bigint[]) AS t(p, n, s, m)
        ON CONFLICT (parent_id, name) WHERE deleted_at IS NULL DO NOTHING
        RETURNING parent_id, name, file_id
        """,
        (src.dataset_id, file_acl, uid, gid, FILE_MODE, src.origin,
         [p for p, _ in keys], [n for _, n in keys], [o.size for o in objs],
         [mtime_ns(o.last_modified) for o in objs]),
    )  # fmt: skip
    made = {(p, n): fid for p, n, fid in await cur.fetchall()}
    rows = [(made[k], k, files[k]) for k in keys if k in made]
    if rows:
        await _versions_and_locations(conn, src, rows, file_acl, uid, gid, seen_at)
    return made


#: The columns COPYed into catalog.seed_stage, in order.
STAGE_COLUMNS = (
    "stage_id", "ord", "parent_id", "name", "object_key", "backend_version_id", "size",
    "mtime_ns", "last_modified", "logical_tier", "storage_class", "etag", "md5",
)  # fmt: skip


async def _merge(
    conn: psycopg.AsyncConnection,
    src: Source,
    keys: Sequence[tuple[int, str]],
    files: Mapping[tuple[int, str], SeedObject],
    file_acl: int,
    uid: int,
    gid: int,
    seen_at: datetime,
) -> dict[tuple[int, str], int]:
    """Bulk: COPY the files into catalog.seed_stage, then one
    `catalog.seed_merge` writes them with their versions, locations, feed
    events and retention audit rows. (parent, name) -> file id written."""
    cur = await conn.execute("SELECT nextval('catalog.seed_stage_id_seq')")
    (stage,) = await cur.fetchone()  # type: ignore[misc]
    async with conn.cursor() as c:
        async with c.copy(f"COPY catalog.seed_stage ({', '.join(STAGE_COLUMNS)}) FROM STDIN") as cp:
            for i, k in enumerate(keys):
                o = files[k]
                md5 = bytes.fromhex(o.etag) if o.etag_is_md5 and o.etag else None
                await cp.write_row((
                    stage, i, k[0], k[1], o.key, o.version_id, o.size, mtime_ns(o.last_modified),
                    o.last_modified, str(o.logical_tier), o.native_class, o.etag, md5,
                ))  # fmt: skip
    cur = await conn.execute(
        "SELECT ord, file_id FROM catalog.seed_merge(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (stage, src.dataset_id, src.backend_id, src.container, file_acl, uid, gid, FILE_MODE,
         src.origin, seen_at),
    )  # fmt: skip
    return {keys[i]: fid for i, fid in await cur.fetchall()}


async def _versions_and_locations(
    conn: psycopg.AsyncConnection,
    src: Source,
    rows: Sequence[tuple[int, tuple[int, str], SeedObject]],
    acl: int,
    uid: int,
    gid: int,
    seen_at: datetime,
) -> None:
    """A committed content version and primary location per row, committed
    at the object's LastModified."""
    cur = await conn.execute(
        """
        INSERT INTO catalog.file_version
            (file_id, dataset_id, parent_id, name, kind, state, size, acl_id, mtime_ns,
             uid, gid, mode)
        SELECT t.f, %s, t.p, t.n, 'content', 'pending', t.s, %s, t.m, %s, %s, %s
          FROM unnest(%s::bigint[], %s::bigint[], %s::text[], %s::bigint[], %s::bigint[])
               AS t(f, p, n, s, m)
        RETURNING file_id, version_id
        """,
        (src.dataset_id, acl, uid, gid, FILE_MODE,
         [f for f, _, _ in rows], [k[0] for _, k, _ in rows], [k[1] for _, k, _ in rows],
         [o.size for _, _, o in rows], [mtime_ns(o.last_modified) for _, _, o in rows]),
    )  # fmt: skip
    version = dict(await cur.fetchall())
    objs = [o for _, _, o in rows]
    await conn.execute(
        """
        INSERT INTO catalog.location
            (file_id, version_id, backend_id, container, object_key, backend_version_id,
             role, state, logical_tier, storage_class, size, etag, md5, committed_at,
             tier_changed_at, feed_seen_at)
        SELECT t.f, t.v, %s, %s, t.k, t.bv, 'primary', 'committed', t.tier::catalog.logical_tier,
               t.cls, t.s, t.etag, decode(t.md5, 'hex'), t.at, t.at, %s
          FROM unnest(%s::bigint[], %s::bigint[], %s::text[], %s::text[], %s::text[],
                      %s::text[], %s::bigint[], %s::text[], %s::text[], %s::timestamptz[])
               AS t(f, v, k, bv, tier, cls, s, etag, md5, at)
        """,
        (src.backend_id, src.container, seen_at,
         [f for f, _, _ in rows], [version[f] for f, _, _ in rows], [o.key for o in objs],
         [o.version_id for o in objs], [str(o.logical_tier) for o in objs],
         [o.native_class for o in objs], [o.size for o in objs], [o.etag for o in objs],
         [o.etag if o.etag_is_md5 else None for o in objs], [o.last_modified for o in objs]),
    )  # fmt: skip
    await conn.execute(
        "UPDATE catalog.file_version v SET state = 'committed', committed_at = t.at"
        "  FROM unnest(%s::bigint[], %s::timestamptz[]) AS t(id, at)"
        " WHERE v.version_id = t.id",
        ([version[f] for f, _, _ in rows], [o.last_modified for o in objs]),
    )
