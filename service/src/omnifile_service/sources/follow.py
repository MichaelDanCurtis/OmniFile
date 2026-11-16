"""Keep a seeded source current from its bucket's events
(docs/design/catalog-seeding.md section 8).

An S3 `Object Created` or `Object Deleted` event for a key under a seeded
source is a prompt, not the truth: events arrive at least once and out of
order. So each one HEADs the key's current object and makes the catalog
match it:

- **gone** (no object, or a delete marker): a file still served from this
  key is tombstoned (decision 30: deletes are tombstones, rewind still sees
  it). A permanently deleted version's location is marked `deleted`, as its
  bytes are gone.
- **new key** (or a key back after its delete marker was removed): a remote
  file, as seeding writes it (`write_batch`); a new folder marker, its
  folder.
- **overwritten** (another version, or on an unversioned bucket another
  ETag): a new content version and primary location on the same file. On an
  unversioned bucket the old location is `deleted`; a versioned bucket keeps
  the old version, so rewind can still reach it.
- **conflicts** are drift (`path_conflict`, source `changefeed`), never
  overwritten: a folder at the path, a file whose bytes no longer come from
  this key (written through OmniFile, or migrated), or one busy on the edge.

Only seeded sources are followed: while any source on a bucket is seeding,
its events are held (`changefeed_sync`). One HEAD per event.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..backends.base import (
    ChangeKind,
    ChangeRecord,
    ObjectHead,
    ObjectNotFound,
    ObjectRef,
    StorageBackend,
)
from ..reconcile.drift import record_drift
from .model import SeedObject
from .paths import Unmappable, place, split_path
from .seed import Source, _versions_and_locations, mtime_ns, write_batch

#: File states whose edge copy can simply be dropped for new bytes.
REPLACEABLE = frozenset({"remote", "stub"})


#: A key's HEAD: its answer, None if it isn't there, or the error it gave.
Heads = dict[tuple[str, str], "ObjectHead | Exception | None"]


async def seeded_source(
    conn: psycopg.AsyncConnection, backend_id: int, container: str, key: str
) -> Source | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT * FROM catalog.source WHERE backend_id = %s AND container = %s"
        "   AND state = 'seeded' AND starts_with(%s, prefix)"
        " ORDER BY length(prefix) DESC LIMIT 1",
        (backend_id, container, key),
    )
    row = await cur.fetchone()
    return Source.from_row(row) if row is not None else None


async def _lookup(
    conn: psycopg.AsyncConnection, dataset_id: int, names: Sequence[str]
) -> dict[str, Any] | None:
    """The live entry at `names` under the dataset root, or None."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
        (dataset_id,),
    )
    row = await cur.fetchone()
    for i, name in enumerate(names):
        if row is None:
            return None
        lock = " FOR UPDATE" if i == len(names) - 1 else ""
        await cur.execute(
            "SELECT * FROM catalog.file WHERE parent_id = %s AND name = %s"
            f"   AND deleted_at IS NULL{lock}",
            (row["file_id"], name),
        )
        row = await cur.fetchone()
    return row


async def _served_from(
    conn: psycopg.AsyncConnection, f: Mapping[str, Any], src: Source, key: str
) -> dict[str, Any] | None:
    """The location of the file's current bytes on this key, in any state,
    or None if its bytes come from somewhere else."""
    if f["kind"] != "file" or f["content_version_id"] is None:
        return None
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT location_id, backend_version_id, etag, state FROM catalog.location"
        " WHERE version_id = %s AND role = 'primary' AND backend_id = %s AND container = %s"
        "   AND object_key = %s"
        " ORDER BY location_id DESC LIMIT 1",
        (f["content_version_id"], src.backend_id, src.container, key),
    )
    return await cur.fetchone()


def _object(head: ObjectHead, at: datetime) -> SeedObject:
    return SeedObject(
        key=head.ref.key,
        version_id=head.ref.version_id,
        size=head.size,
        last_modified=head.last_modified or at,
        native_class=head.native_class,
        logical_tier=head.logical_tier,
        etag=head.etag,
        etag_is_md5=head.md5 is not None,
    )


async def _drift(
    conn: psycopg.AsyncConnection,
    src: Source,
    rec: ChangeRecord,
    kind: str,
    why: str,
    now: datetime,
) -> int:
    return await record_drift(
        conn, source="changefeed", backend_id=src.backend_id, container=src.container,
        key=rec.ref.key, version=rec.ref.version_id, kind=kind, now=now,
        observed={"source_id": src.source_id, "reason": why, "event": str(rec.kind),
                  "at": rec.at.isoformat()},
    )  # fmt: skip


async def _commit_floor(conn: psycopg.AsyncConnection, f: Mapping[str, Any]) -> datetime | None:
    """The current version's commit time: a new version can't commit before
    it (its predecessor is superseded at the new one's time)."""
    cur = await conn.execute(
        "SELECT committed_at FROM catalog.file_version WHERE version_id = %s",
        (f["current_version_id"],),
    )
    row = await cur.fetchone()
    return row[0] if row is not None else None


async def _tombstone(
    conn: psycopg.AsyncConnection, f: Mapping[str, Any], at: datetime, now: datetime
) -> None:
    floor = await _commit_floor(conn, f)
    await conn.execute(
        "UPDATE catalog.file SET deleted_at = %s WHERE file_id = %s", (now, f["file_id"])
    )
    await conn.execute(
        """
        INSERT INTO catalog.file_version
            (file_id, dataset_id, parent_id, name, kind, state, committed_at, size,
             mtime_ns, uid, gid, mode, acl_id)
        SELECT f.file_id, f.dataset_id, f.parent_id, f.name, 'tombstone', 'committed', %s,
               f.size, f.mtime_ns, f.uid, f.gid, f.mode, f.acl_id
          FROM catalog.file f WHERE f.file_id = %s
        """,
        (max(at, floor) if floor is not None else at, f["file_id"]),
    )


async def _overwrite(
    conn: psycopg.AsyncConnection,
    src: Source,
    f: Mapping[str, Any],
    old: Mapping[str, Any],
    obj: SeedObject,
    seen_at: datetime,
) -> None:
    if obj.version_id is None and old["state"] == "committed":
        # Unversioned: the old bytes are gone with the overwrite.
        await conn.execute(
            "UPDATE catalog.location SET state = 'deleted', feed_seen_at = %s"
            " WHERE location_id = %s",
            (seen_at, old["location_id"]),
        )
    floor = await _commit_floor(conn, f)
    if floor is not None and obj.last_modified < floor:
        obj = dataclasses.replace(obj, last_modified=floor)
    await conn.execute(
        "UPDATE catalog.file SET size = %s, mtime_ns = %s, state = 'remote', resident_bytes = 0,"
        " blake3 = NULL, crc64nvme = NULL WHERE file_id = %s",
        (obj.size, mtime_ns(obj.last_modified), f["file_id"]),
    )
    await _versions_and_locations(
        conn, src, [(f["file_id"], (f["parent_id"], f["name"]), obj)],
        f["acl_id"], f["uid"] or 0, f["gid"] or 0, seen_at,
    )  # fmt: skip


async def heads_to_follow(
    conn: psycopg.AsyncConnection, backend: StorageBackend, records: Sequence[ChangeRecord]
) -> Heads:
    """What `follow` needs from the provider for these records. A caller asks before
    it writes anything or locks a row: a transaction id held across a provider call
    would stop the edge feed for every edge. A failed HEAD is kept and raised when
    its record is applied, so it fails that record, as it always has."""
    heads: Heads = {}
    for rec in records:
        where = (rec.ref.container, rec.ref.key)
        if rec.kind not in (ChangeKind.CREATED, ChangeKind.DELETED) or where in heads:
            continue
        if await seeded_source(conn, backend.backend_id, *where) is None:
            continue
        try:
            heads[where] = await backend.head(ObjectRef(backend.backend_id, *where))
        except ObjectNotFound:
            heads[where] = None
        except Exception as e:  # noqa: BLE001 - raised with its record
            heads[where] = e
    return heads


async def follow(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    rec: ChangeRecord,
    now: datetime,
    locs: Sequence[tuple],
    heads: Heads | None = None,
) -> tuple[bool, int] | None:
    """Apply a CREATED or DELETED record for a key under a seeded source, in
    the caller's transaction. Returns (matched, drift rows), or None when no
    seeded source covers the key (the caller handles it as before). `heads`: from
    `heads_to_follow`; with none, the HEAD is made here."""
    if rec.kind not in (ChangeKind.CREATED, ChangeKind.DELETED):
        return None
    if rec.kind is ChangeKind.DELETED and any(state == "deleting" for _, _, state, *_ in locs):
        return None  # OmniFile's own delete: the caller completes it
    src = await seeded_source(conn, backend.backend_id, rec.ref.container, rec.ref.key)
    if src is None:
        return None
    key = rec.ref.key
    if heads is not None and (rec.ref.container, key) in heads:
        known = heads[(rec.ref.container, key)]
        if isinstance(known, Exception):
            raise known
        head: ObjectHead | None = known
    else:
        try:
            head = await backend.head(ObjectRef(backend.backend_id, rec.ref.container, key))
        except ObjectNotFound:
            head = None
    try:
        pl = place(key, src.prefix, split_path(src.path), head.size if head else 0)
    except Unmappable as e:
        return (False, await _drift(conn, src, rec, "unmappable", str(e), now) if head else 0)
    if pl.name is None:
        # A folder marker: its folder appears, as a seed creates it. A
        # deleted marker leaves the folder (it may hold files); removing it
        # is a delete through OmniFile.
        if head is None:
            return (False, 0)
        w = await write_batch(
            conn, src, [_object(head, rec.at)], now, seen_at=rec.at, drift_source="changefeed",
            actor=None, check_present=False,
        )  # fmt: skip
        return (w.dirs > 0, len(w.drift))
    f = await _lookup(conn, src.dataset_id, (*pl.folders, pl.name))
    served = await _served_from(conn, f, src, key) if f is not None else None
    changed = False
    if rec.kind is ChangeKind.DELETED and rec.detail.get("tombstone") != "true":
        if rec.ref.version_id is not None or head is None:
            # That version's bytes are gone (on an unversioned bucket, only
            # if nothing has been written there since).
            for lid, _, state, *_ in locs:
                if state == "committed":
                    await conn.execute(
                        "UPDATE catalog.location SET state = 'deleted', feed_seen_at = %s"
                        " WHERE location_id = %s",
                        (rec.at, lid),
                    )
                    changed = True
    if head is None:
        if f is not None and served is not None:
            await _tombstone(conn, f, rec.at, now)
            return (True, 0)
        return (changed, 0)
    obj = _object(head, rec.at)
    if f is None:
        # Nothing live at the path. The version may still have a location
        # (a tombstoned file's, for rewind, when its delete marker was
        # removed), so this is not a replay: the path comes back.
        w = await write_batch(
            conn, src, [obj], now, seen_at=rec.at, drift_source="changefeed", actor=None,
            check_present=False,
        )  # fmt: skip
        return (changed or w.files > 0, len(w.drift))
    if f["kind"] != "file":
        why = "a folder has this path"
    elif served is None:
        why = "the file at this path no longer comes from this object"
    elif (
        served["state"] == "committed"
        and served["backend_version_id"] == obj.version_id
        and (obj.version_id is not None or served["etag"] == obj.etag)
    ):
        return (changed, 0)  # up to date
    elif f["state"] not in REPLACEABLE:
        why = f"the file is {f['state']} on the edge"
    else:
        await _overwrite(conn, src, f, served, obj, rec.at)
        return (True, 0)
    return (changed, await _drift(conn, src, rec, "path_conflict", why, now))


__all__ = ["follow", "seeded_source"]
