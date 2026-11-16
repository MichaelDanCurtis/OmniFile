"""Register a source and seed it in batches (docs/design/catalog-seeding.md
sections 1, 5 and 6)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from ..backends.base import BackendError, StorageBackend, UnsupportedOperation
from ..backends.s3.config import ops_buckets_of
from ..worker.queue import Message, Retry, outside, send
from . import metadata_s3
from .acl import Groups
from .inventory_s3 import S3InventoryReader
from .listing import ListingReader
from .metadata_s3 import S3MetadataReader
from .model import Reader, SourceError
from .paths import Unmappable, split_path
from .seed import SEED_ACTOR, Source, write_batch

log = logging.getLogger("omnifile_service.sources")

SEED_QUEUE = "seed"
#: Objects per seed batch: one transaction, so a crash replays at most one.
BATCH = 20_000
#: Batches at least this big go through the bulk path (COPY and
#: `catalog.seed_merge`); smaller ones are written row by row
#: (catalog-seeding.md section 6).
BULK_MIN = 1000
#: A read of a batch fetches an inventory file, or pages of a listing.
READ_HIDE_SECONDS = 3600
#: SQS keeps a message at most 14 days, so an older inventory's gap to now
#: can't be replayed from the queue [V].
MAX_INVENTORY_AGE = timedelta(days=14)
#: Refresh planner statistics before the first batches, then every 20.
ANALYZE_FIRST = 10
ANALYZE_EVERY = 20


def inventory_kind(inventory: Mapping[str, Any] | None) -> str:
    """`s3_metadata`, `s3_inventory` or `listing` (NULL lists the prefix)."""
    return "listing" if inventory is None else str(inventory.get("kind"))


def reader_for(
    backend: StorageBackend, container: str, prefix: str, inventory: Mapping[str, Any] | None
) -> Reader:
    kind = inventory_kind(inventory)
    if kind == "listing":
        return ListingReader(backend, container, prefix)
    if kind == metadata_s3.KIND:
        assert inventory is not None
        return S3MetadataReader(backend, container, prefix, inventory)
    if kind != "s3_inventory":
        raise SourceError(f"unknown inventory kind {kind!r}")
    assert inventory is not None
    return S3InventoryReader(
        backend,
        str(inventory["bucket"]),
        str(inventory["manifest_key"]),
        source_bucket=container,
        source_prefix=prefix,
    )


def _check_shape(prefix: str, path: str) -> None:
    if prefix and not prefix.endswith("/"):
        raise SourceError(f"prefix {prefix!r} must be empty or end in '/'")
    if path.startswith("/") or path.endswith("/"):
        raise SourceError(f"path {path!r} must not start or end with '/'")
    try:
        split_path(path)
    except Unmappable as e:
        raise SourceError(f"path {path!r}: {e}") from None


#: v1 migrates S3 -> Azure only (decision 23); sim plays either side.
MIGRATE_FROM = frozenset({"s3", "sim"})
MIGRATE_TO = frozenset({"azure_blob", "sim"})


def _check_inventory(
    inventory: Mapping[str, Any] | None, *, region: str | None = None
) -> dict[str, Any] | None:
    """The inventory as stored: None (list, unless the bucket has Metadata
    tables), `{"kind": "listing"}` (list, chosen), an S3 Inventory's manifest,
    or S3 Metadata tables."""
    if inventory is None:
        return None
    kind = inventory.get("kind")
    if kind == "listing":
        if set(inventory) != {"kind"}:
            raise SourceError("a listing takes no settings")
        return {"kind": "listing"}
    if kind == metadata_s3.KIND:
        return metadata_s3.check_config(inventory, region=region)
    if kind != "s3_inventory":
        raise SourceError(f"unknown inventory kind {kind!r}")
    for k in ("bucket", "manifest_key"):
        if not isinstance(inventory.get(k), str) or not inventory[k]:
            raise SourceError(f"the inventory needs {k}")
    return dict(inventory)


def _preferred_inventory(
    kind: str, config: Mapping[str, Any], container: str, inventory: dict[str, Any] | None
) -> dict[str, Any] | None:
    """A source that names no inventory seeds from its bucket's S3 Metadata
    tables when its backend has them (catalog-seeding.md section 7): Metadata
    tables, then an S3 Inventory (named by the source), and a listing last."""
    if inventory is not None or kind != "s3":
        return inventory
    tables = (config.get("metadata_tables") or {}).get(container)
    if not isinstance(tables, Mapping):
        return None
    return metadata_s3.check_config(
        {**tables, "kind": metadata_s3.KIND}, region=config.get("region")
    )


async def create_source(
    conn: psycopg.AsyncConnection,
    *,
    dataset_id: int,
    backend_id: int,
    container: str,
    groups: Groups,
    prefix: str = "",
    path: str = "",
    inventory: Mapping[str, Any] | None = None,
    kind: str = "source",
) -> int:
    """The catalog half of registering a source, on the caller's
    transaction, with no storage call: check it, set the dataset
    `migrating`, insert the source (`registered`, no snapshot yet) and queue
    its seed. The worker's first seed step starts it (`start_source`).
    `kind` 'adopt' takes data already in the dataset's own backend (no
    migration). Returns the source id."""
    _check_shape(prefix, path)
    if kind not in ("source", "adopt"):
        raise SourceError(f"unknown source kind {kind!r}")
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT kind, config, private_network FROM catalog.backend WHERE backend_id = %s",
        (backend_id,),
    )
    b = await cur.fetchone()
    if b is None:
        raise SourceError(f"no backend {backend_id}")
    config = b["config"] or {}
    s3 = None
    if b["kind"] == "s3":
        # Endpoints, roles, private networking and OmniFile's own buckets are
        # operator settings; a row that names one is never used for a source
        # (backends/s3/config.py).
        from ..backends.s3.config import ConfigError, S3Config, check_row_keys

        try:
            check_row_keys(config)
        except ConfigError as e:
            raise SourceError(f"backend {backend_id}: {e}") from None
        try:
            s3 = S3Config.for_backend(config, private_network=b.get("private_network"))
        except ConfigError:
            # Not loadable here (the worker refuses it the same way): only
            # the row's own fields are checked now.
            s3 = None
    buckets = config.get("buckets")
    if isinstance(buckets, list) and container not in buckets:
        raise SourceError(f"bucket {container!r} is not one of backend {backend_id}'s buckets")
    ops = ops_buckets_of(config) | (s3.ops_buckets if s3 is not None else frozenset())
    if b["kind"] == "s3" and container in ops:
        # OmniFile writes there (staging, Batch manifests and reports), and a
        # source is read-only to it (catalog-seeding.md section 10).
        raise SourceError(
            f"bucket {container!r} is one of backend {backend_id}'s operations buckets"
            " (staging, Batch manifests or reports); a source must be another bucket"
        )
    inventory = _check_inventory(
        inventory, region=config.get("region") if b["kind"] == "s3" else None
    )
    if inventory_kind(inventory) != "listing" and b["kind"] != "s3":
        raise SourceError(f"an S3 inventory can't describe a {b['kind']} backend")
    inventory = _preferred_inventory(
        b["kind"],
        {"metadata_tables": dict(s3.metadata_tables), "region": s3.region} if s3 else {},
        container,
        inventory,
    )
    await cur.execute(
        "SELECT d.backend_id, d.migration_state, d.source_backend_id, b.kind AS home_kind"
        "  FROM catalog.dataset d JOIN catalog.backend b ON b.backend_id = d.backend_id"
        " WHERE d.dataset_id = %s FOR UPDATE OF d",
        (dataset_id,),
    )
    ds = await cur.fetchone()
    if ds is None:
        raise SourceError(f"no dataset {dataset_id}")
    if (
        kind == "source"
        and ds["backend_id"] != backend_id
        and not (b["kind"] in MIGRATE_FROM and ds["home_kind"] in MIGRATE_TO)
    ):
        raise SourceError(
            f"v1 migrates S3 to Azure only (decision 23): backend {backend_id} is"
            f" {b['kind']} and dataset {dataset_id} lives on {ds['home_kind']}"
        )
    if kind == "adopt":
        if ds["backend_id"] != backend_id:
            raise SourceError(
                f"adopt takes data already in dataset {dataset_id}'s own backend"
                f" ({ds['backend_id']}); register a source to migrate from backend"
                f" {backend_id}"
            )
    elif ds["backend_id"] != backend_id:
        # The dataset migrates from this backend (decision 23); one source
        # backend at a time.
        if ds["migration_state"] == "steady":
            await cur.execute(
                "UPDATE catalog.dataset SET migration_state = 'migrating',"
                " source_backend_id = %s WHERE dataset_id = %s",
                (backend_id, dataset_id),
            )
        elif ds["migration_state"] != "migrating" or ds["source_backend_id"] != backend_id:
            raise SourceError(
                f"dataset {dataset_id} is {ds['migration_state']} from backend"
                f" {ds['source_backend_id']}; it can't also take sources from backend"
                f" {backend_id}",
                "source_conflict",
            )
    await _check_path(conn, dataset_id, path)
    # One registration per bucket at a time, whatever the dataset: the
    # overlap check below must see every committed prefix.
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"omnifile.source:{backend_id}:{container}",),
    )
    await cur.execute(
        "SELECT source_id, prefix FROM catalog.source WHERE backend_id = %s AND container = %s"
        "   AND (starts_with(prefix, %s) OR starts_with(%s, prefix))",
        (backend_id, container, prefix, prefix),
    )
    clash = await cur.fetchone()
    if clash is not None:
        raise SourceError(
            f"{container}/{prefix} overlaps source {clash['source_id']}"
            f" ({container}/{clash['prefix']})",
            "source_conflict",
        )
    await cur.execute(
        """
        INSERT INTO catalog.source
            (dataset_id, path, backend_id, container, prefix, inventory, owners_sid,
             writers_sid, readers_sid, kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING source_id
        """,
        (dataset_id, path, backend_id, container, prefix,
         Jsonb(dict(inventory)) if inventory is not None else None,
         groups.owners, groups.writers, groups.readers, kind),
    )  # fmt: skip
    row = await cur.fetchone()
    assert row is not None
    source_id = int(row["source_id"])
    await send(conn, SEED_QUEUE, {"source_id": source_id})
    return source_id


async def start_source(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    source_id: int,
    snapshot: datetime | None = None,
) -> datetime:
    """The storage half, on the caller's transaction: read the snapshot time
    (the inventory's manifest, or now for a listing), refuse an inventory
    older than the event queue keeps, and start the change feed at the
    snapshot. Returns the snapshot time. `snapshot`: the time, if the caller has
    read it already, so no provider call is made here."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT container, prefix, inventory, snapshot_at FROM catalog.source"
        " WHERE source_id = %s FOR UPDATE",
        (source_id,),
    )
    src = await cur.fetchone()
    if src is None:
        raise SourceError(f"no source {source_id}")
    if src["snapshot_at"] is not None:
        return src["snapshot_at"]
    container, inventory = src["container"], src["inventory"]
    reader = reader_for(backend, container, src["prefix"], inventory)
    snapshot_at = snapshot if snapshot is not None else await reader.snapshot_at()
    kind = inventory_kind(inventory)
    if kind != "listing" and backend.clock.now() - snapshot_at > MAX_INVENTORY_AGE:
        raise SourceError(
            f"the inventory is from {snapshot_at:%Y-%m-%d}; changes since then can't all be"
            f" replayed (the event queue keeps {MAX_INVENTORY_AGE.days} days). Use a newer one."
        )
    at = getattr(backend, "change_cursor_at", None)
    if at is not None:
        try:
            cursor = at(container, snapshot_at)
        except UnsupportedOperation as e:
            log.warning("source has no change feed", extra={"reason": str(e)})
        else:
            # An S3 Inventory's gap must be replayed from the queue; Metadata
            # tables replay their own from the journal (journal_sync.py).
            await _start_feed(
                conn, backend.backend_id, container, cursor, snapshot_at, kind == "s3_inventory"
            )
    await cur.execute(
        "UPDATE catalog.source SET snapshot_at = %s, updated_at = %s WHERE source_id = %s",
        (snapshot_at, backend.clock.now(), source_id),
    )
    return snapshot_at


async def register_source(
    conn: psycopg.AsyncConnection,
    backend: StorageBackend,
    *,
    dataset_id: int,
    container: str,
    groups: Groups,
    prefix: str = "",
    path: str = "",
    inventory: Mapping[str, Any] | None = None,
) -> int:
    """Register a source and start it now, in one transaction (the CLI):
    `create_source`, then `start_source`, so a bad inventory is refused
    here rather than failing the source later. Returns the source id. The manifest is
    read first: the new row takes a transaction id, which must not wait on the provider."""
    snapshot = await reader_for(backend, container, prefix, inventory).snapshot_at()
    async with conn.transaction():
        await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (SEED_ACTOR,))
        source_id = await create_source(
            conn, dataset_id=dataset_id, backend_id=backend.backend_id, container=container,
            groups=groups, prefix=prefix, path=path, inventory=inventory,
        )  # fmt: skip
        await start_source(conn, backend, source_id, snapshot)
    return source_id


async def _start_feed(
    conn: psycopg.AsyncConnection,
    backend_id: int,
    container: str,
    cursor: str,
    snapshot_at: datetime,
    gap: bool,
) -> None:
    """Start the container's change feed at the snapshot, or keep the cursor
    of a container already followed. Its events are held from now until
    every source on it is seeded (`changefeed_sync`), so a seeded row never
    misses one. An inventory's gap (snapshot to now) can only be replayed if
    none of it has been read already."""
    cur = await conn.execute(
        "INSERT INTO catalog.changefeed_cursor (backend_id, container, cursor)"
        " VALUES (%s, %s, %s)"
        " ON CONFLICT (backend_id, container) DO UPDATE SET cursor = EXCLUDED.cursor"
        " WHERE catalog.changefeed_cursor.cursor IS NULL"
        " RETURNING cursor",
        (backend_id, container, cursor),
    )
    if await cur.fetchone() is not None or not gap:
        return  # a new feed, or a listing (it reads after now)
    # The row is locked by the upsert, so no page can be read meanwhile.
    cur = await conn.execute(
        "SELECT last_record_at FROM catalog.changefeed_cursor"
        " WHERE backend_id = %s AND container = %s",
        (backend_id, container),
    )
    row = await cur.fetchone()
    newest = row[0] if row is not None else None
    if newest is not None and newest > snapshot_at:
        raise SourceError(
            f"events on {container} up to {newest:%Y-%m-%d %H:%M}Z have already been applied,"
            f" after the inventory ({snapshot_at:%Y-%m-%d %H:%M}Z), so its gap can't be replayed"
            " for this prefix. Seed it from a listing (no inventory), or register a bucket's"
            " inventory sources while one of them is still seeding."
        )


async def seed_step(
    conn: psycopg.AsyncConnection,
    backend_for: Any,
    source_id: int,
    now: datetime,
    *,
    batch: int = BATCH,
    bulk_min: int = BULK_MIN,
    msg: Message | None = None,
) -> str:
    """Seed one batch of a source on `conn`'s current transaction (the queue
    handler's), and queue the next. Returns the source's state. A batch of
    `bulk_min` objects or more is written through the bulk path.

    With `msg`, the reads of the bucket run `outside` the transaction: the source's
    row lock takes a transaction id, which would hold the edge feed back for them."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT * FROM catalog.source WHERE source_id = %s FOR UPDATE", (source_id,))
    row = await cur.fetchone()
    if row is None:
        raise SourceError(f"no source {source_id}")
    if row["state"] in ("seeded", "failed"):
        return str(row["state"])
    backend = backend_for(row["backend_id"])
    if row["snapshot_at"] is None:  # registered through the API: start it here
        try:
            snapshot = await outside(
                msg,
                "snapshot",
                lambda: reader_for(
                    backend, row["container"], row["prefix"], row["inventory"]
                ).snapshot_at(),
                hide_for=READ_HIDE_SECONDS,
            )
            async with conn.transaction():  # a savepoint: a refusal leaves nothing
                row = {
                    **row,
                    "snapshot_at": await start_source(conn, backend, source_id, snapshot),
                }
        except SourceError as e:
            await _fail(conn, source_id, str(e))
            return "failed"
        except BackendError as e:
            raise Retry(60) from e
    src = Source.from_row(row)
    try:
        reader = reader_for(backend, src.container, src.prefix, src.inventory)
        got = await outside(
            msg, "read", lambda: reader.read(src.resume, batch), hide_for=READ_HIDE_SECONDS
        )
    except SourceError as e:
        await _fail(conn, source_id, str(e))
        return "failed"
    except BackendError as e:
        raise Retry(60) from e
    done = int(row["objects_seen"]) // batch
    if done < ANALYZE_FIRST or done % ANALYZE_EVERY == 0:
        await _analyze(conn)
    written = await write_batch(conn, src, got.objects, now, bulk=len(got.objects) >= bulk_min)
    state = "seeded" if got.resume is None else "seeding"
    await conn.execute(
        """
        UPDATE catalog.source
           SET state = %s, resume = %s, objects_seen = objects_seen + %s,
               files_created = files_created + %s, dirs_created = dirs_created + %s,
               already_present = already_present + %s, skipped = skipped + %s,
               updated_at = %s, seeded_at = CASE WHEN %s = 'seeded' THEN %s END,
               last_error = NULL
         WHERE source_id = %s
        """,
        (state, Jsonb(dict(got.resume)) if got.resume is not None else None, len(got.objects),
         written.files, written.dirs, written.present, written.skipped, now, state, now,
         source_id),
    )  # fmt: skip
    if got.resume is not None:
        await send(conn, SEED_QUEUE, {"source_id": source_id})
    log.info(
        "seeded a batch",
        extra={"source_id": source_id, "objects": len(got.objects), "files": written.files,
               "state": state},
    )  # fmt: skip
    return state


async def _analyze(conn: psycopg.AsyncConnection) -> None:
    """Refresh planner statistics: seeding fills the tables faster than
    autovacuum re-analyses them, and the catalog's triggers plan their
    lookups from those statistics. ANALYZE's lock is held until the batch
    commits, so a seed that finds another one analysing skips it."""
    cur = await conn.execute("SELECT current_setting('lock_timeout')")
    old = (await cur.fetchone())[0]  # type: ignore[index]
    await conn.execute("SELECT set_config('lock_timeout', '50ms', true)")
    try:
        async with conn.transaction():  # a savepoint
            # Runs as the tables' owner (migration 0024): the worker owns none.
            await conn.execute("SELECT catalog.analyze_namespace()")
    except psycopg.errors.LockNotAvailable:
        log.info("another seed is analysing; skipped")
    finally:
        await conn.execute("SELECT set_config('lock_timeout', %s, true)", (old,))


async def _check_path(conn: psycopg.AsyncConnection, dataset_id: int, path: str) -> None:
    """The source's folder may not run through an existing file: every
    object would be a path conflict."""
    cur = conn.cursor(row_factory=tuple_row)
    await cur.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
        (dataset_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise SourceError(f"dataset {dataset_id} has no root folder")
    parent = row[0]
    for name in split_path(path):
        await cur.execute(
            "SELECT file_id, kind FROM catalog.file"
            " WHERE parent_id = %s AND name = %s AND deleted_at IS NULL",
            (parent, name),
        )
        hit = await cur.fetchone()
        if hit is None:
            return
        if hit[1] != "dir":
            raise SourceError(f"path {path!r}: {name!r} is a file, not a folder", "source_conflict")
        parent = hit[0]


async def retry_source(conn: psycopg.AsyncConnection, source_id: int) -> None:
    """Resume a failed source from where it stopped."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE catalog.source SET state = 'seeding', last_error = NULL, updated_at = now()"
            " WHERE source_id = %s AND state = 'failed' RETURNING source_id",
            (source_id,),
        )
        if await cur.fetchone() is None:
            raise SourceError(f"source {source_id} is not failed", "source_conflict")
        await send(conn, SEED_QUEUE, {"source_id": source_id})


async def _fail(conn: psycopg.AsyncConnection, source_id: int, why: str) -> None:
    await conn.execute(
        "UPDATE catalog.source SET state = 'failed', last_error = %s, updated_at = now()"
        " WHERE source_id = %s",
        (why, source_id),
    )
