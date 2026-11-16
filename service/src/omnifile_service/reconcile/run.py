"""Nightly reconcile: compare what a backend holds with the catalog.

Per container:
- **non-WORM** accounts: an inventory (`inventory_for`: Blob Inventory on
  Azure, an emulated one on sim);
- **WORM** accounts, where version-level immutability blocks Blob Inventory:
  catch the change feed up first, then list.

The items are loaded into a temporary table and compared with the
container's locations in a few set-based queries. Every difference opens
(or refreshes) a `catalog.reconcile_drift` row; open rows this run no longer
sees are resolved. Reconcile reports and never repairs: a missing copy of a
retained version is an incident, not something to paper over.

| Drift | Meaning |
|---|---|
| missing | a committed location whose object is gone |
| unexpected | an object no live location knows (a failed delete, a foreign write) |
| size_mismatch | sizes differ |
| tier_mismatch | the native class differs from the catalog's |
| retention_mismatch | WORM: the backend retains the version for less than the catalog says |
| hold_mismatch | WORM: legal hold differs |

**Buckets with sources** (M5, catalog-seeding.md section 9): only the source
prefixes are OmniFile's, so only they are listed and compared; the rest of
the bucket is not `unexpected`, and neither are versions older than the
current one that no location names (seeding adopts current versions only).
While a source on it is still seeding, the bucket is not reconciled at all
(`SourcesSeeding`): half-seeded, everything would look missing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from ..backends.base import StorageBackend
from ..retention.common import data_container
from ..worker.backends import BackendRegistry
from .drift import record_drift
from .inventory import (
    InventoryItem,
    InventorySource,
    ListingInventory,
    PrefixedInventory,
    choose_inventory,
    inventory_for,
)

log = logging.getLogger(__name__)


class SourcesSeeding(Exception):  # noqa: N818 - a "not now", not an error
    """A source on the container is still seeding; reconcile it later."""


@dataclass
class ReconcileReport:
    run_id: int
    #: ``inventory`` (a Blob Inventory report), ``listing`` or, on WORM,
    #: ``listing_changefeed``; recorded on the run (migration 0034).
    method: str
    objects_seen: int = 0
    locations_checked: int = 0
    drift_found: int = 0  # newly opened
    drift_open: list[dict] = field(default_factory=list)  # every difference this run saw
    drift_resolved: int = 0
    #: What was read (the report), or why the run listed.
    method_detail: str | None = None


def known_containers(config: Mapping[str, Any]) -> list[str]:
    """The containers a dataset's backend config implies: data, archive and
    recall. `config` keys: `container` / `live_account` + `data_container`,
    `archive_container` or `archive_account`, `recall_container`; sim
    layouts add their defaults."""
    data = data_container(config)
    out = [data]
    short = data.split("/", 1)[-1]
    layout = config.get("layout")
    if config.get("archive_container"):
        out.append(str(config["archive_container"]))
    elif config.get("archive_account"):
        out.append(f"{config['archive_account']}/{short}")
    elif layout == "azure_two_account":
        out.append(f"archive/{short}")
    if config.get("recall_container"):
        out.append(str(config["recall_container"]))
    elif layout in ("azure_two_account", "azure_worm"):
        out.append(f"{data.split('/', 1)[0]}/recall")
    return list(dict.fromkeys(out))


#: Rows written to the temporary table at a time.
LOAD_BATCH = 10_000

_LOAD = """
CREATE TEMP TABLE recon_obj (
    key             text COLLATE "C" NOT NULL,
    version_id      text,
    size            bigint NOT NULL,
    native_class    text NOT NULL,
    retention_until timestamptz,
    legal_hold      boolean NOT NULL,
    is_current      boolean NOT NULL
)
"""

# A location names one stored version: its backend_version_id, or (no
# version id: an unversioned container) the current one.
_MATCH = """o.key = l.object_key
       AND (o.version_id = l.backend_version_id
            OR (l.backend_version_id IS NULL AND o.is_current))"""

# The locations in scope: all of the container, or only its source prefixes.
_SCOPE = """
   AND (%(pfx)s::text[] IS NULL
        OR EXISTS (SELECT 1 FROM unnest(%(pfx)s::text[]) AS x(p)
                    WHERE starts_with(l.object_key, x.p)))"""

_MISSING = (
    """
SELECT l.location_id, l.object_key, l.backend_version_id, l.role, l.size
  FROM catalog.location l
 WHERE l.backend_id = %(bid)s AND l.container = %(c)s AND l.state = 'committed'
   AND l.committed_at < %(snap)s"""
    + _SCOPE
    + """
   AND NOT EXISTS (SELECT 1 FROM recon_obj o WHERE """
    + _MATCH
    + """)
"""
)

# Keys with no listed version at all (candidates for `probe`).
_UNLISTED_KEYS = (
    """
SELECT DISTINCT l.object_key
  FROM catalog.location l
 WHERE l.backend_id = %(bid)s AND l.container = %(c)s AND l.state = 'committed'"""
    + _SCOPE
    + """
   AND NOT EXISTS (SELECT 1 FROM recon_obj o WHERE o.key = l.object_key)
"""
)

# Under source prefixes, only current objects can be unexpected: seeding
# adopts current versions only, so older versions from before adoption are
# the bucket's history, not OmniFile's (they are still listed, so a
# location naming one is matched).
#
# A report taken before reconcile runs (a Blob Inventory, `stale`) still lists
# what OmniFile deleted since: an object whose location was deleted after the
# report started is not unexpected.
_UNEXPECTED = (
    """
SELECT o.key, o.version_id, o.size, o.native_class
  FROM recon_obj o
 WHERE (%(pfx)s::text[] IS NULL OR o.is_current)
   AND NOT EXISTS (SELECT 1 FROM catalog.location l
                    WHERE """
    + _MATCH
    + """ AND l.container = %(c)s
                      AND l.backend_id = %(bid)s
                      AND (l.state <> 'deleted' OR (%(stale)s AND l.updated_at >= %(snap)s)))
"""
)

_MISMATCH = (
    """
SELECT l.location_id, l.object_key, o.version_id, l.role, l.size, o.size,
       l.storage_class, o.native_class, v.retention_until, o.retention_until,
       v.legal_hold, o.legal_hold, l.worm_sync_needed,
       (%(stale)s AND l.tier_changed_at >= %(snap)s) AS tier_changed_since,
       EXISTS (SELECT 1 FROM catalog.restore s
                WHERE s.location_id = l.location_id
                  AND s.state IN ('requested', 'in_progress')) AS restoring
  FROM catalog.location l
  JOIN recon_obj o ON """
    + _MATCH
    + """
  JOIN catalog.file_version v ON v.version_id = l.version_id
 WHERE l.backend_id = %(bid)s AND l.container = %(c)s AND l.state = 'committed'"""
    + _SCOPE
    + """
"""
)


async def reconcile_container(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    backend_id: int,
    container: str,
    now: datetime,
    *,
    source: InventorySource | None = None,
) -> ReconcileReport:
    backend: StorageBackend = registry.get(backend_id)
    prefixes = await source_prefixes(conn, backend_id, container)
    worm = backend.capabilities().worm
    if worm:
        from ..worker.changefeed_sync import register_container, sync_container

        await register_container(conn, backend_id, container)
        await sync_container(conn, registry, backend_id, container, now, max_pages=10_000)
    if source is None:
        if prefixes is not None:
            source = PrefixedInventory(backend, prefixes)
        elif worm:
            source = ListingInventory(backend)
        else:
            cur = await conn.execute(
                "SELECT config FROM catalog.backend WHERE backend_id = %s", (backend_id,)
            )
            row = await cur.fetchone()
            source = await choose_inventory(backend, container, row[0] if row else None, now)
    # How the backend was read: a report, a listing (and why: no report
    # configured, none usable, or a bucket's source prefixes), or on WORM the
    # change feed and a listing.
    method = "listing_changefeed" if worm else source.method
    detail = getattr(source, "detail", None)
    cur = await conn.execute(
        "INSERT INTO catalog.reconcile_run (backend_id, container, method, method_detail,"
        " started_at) VALUES (%s, %s, %s, %s, %s) RETURNING run_id",
        (backend_id, container, method, detail, now),
    )
    run_id = (await cur.fetchone())[0]  # type: ignore[index]
    report = ReconcileReport(run_id, method, method_detail=detail)
    from .blob_inventory import BlobInventory, InventoryError

    try:
        try:
            await _compare(conn, backend_id, container, now, source, worm, report, prefixes)
        except InventoryError as e:
            if not isinstance(source, BlobInventory):
                raise
            # The report proved unusable while it was read (a file cut short,
            # a field missing from a file, fewer rows than its summary):
            # nothing it loaded was kept (the transaction rolled back), so
            # the run lists the container instead and says why.
            log.warning("reconcile %s: report unusable (%s); listing instead", container, e)
            source = inventory_for(backend, f"report unusable: {e}"[:1000])
            method, detail = "listing", source.detail  # type: ignore[attr-defined]
            await conn.execute(
                "UPDATE catalog.reconcile_run SET method = %s, method_detail = %s"
                " WHERE run_id = %s",
                (method, detail, run_id),
            )
            report = ReconcileReport(run_id, method, method_detail=detail)
            await _compare(conn, backend_id, container, now, source, worm, report, prefixes)
    except Exception as e:
        await conn.execute(
            "UPDATE catalog.reconcile_run SET state = 'failed', completed_at = %s, failure = %s"
            " WHERE run_id = %s",
            (now, f"{type(e).__name__}: {e}"[:2000], run_id),
        )
        raise
    log.info(
        "reconciled %s/%s by %s (%s): %d objects, %d locations, %d new drift, %d resolved",
        backend_id, container, method, detail or "-", report.objects_seen,
        report.locations_checked, report.drift_found, report.drift_resolved,
    )  # fmt: skip
    return report


async def _compare(
    conn: psycopg.AsyncConnection,
    backend_id: int,
    container: str,
    now: datetime,
    source: InventorySource,
    worm: bool,
    report: ReconcileReport,
    prefixes: list[str] | None = None,
) -> None:
    """Load what the backend holds into a temporary table, then compare it with the
    catalog in one transaction.

    The load can take hours (a bucket's whole listing, or a report), and a transaction
    id held for them would stop the edge feed for every edge, so each batch is written
    on its own, with nothing open while the backend is read. The table lasts as long
    as this call, whichever way it ends."""
    await conn.execute("DROP TABLE IF EXISTS pg_temp.recon_obj")
    await conn.execute(_LOAD)
    try:
        await _load_and_compare(
            conn, backend_id, container, now, source, worm, report, prefixes
        )  # fmt: skip
    finally:
        # In a caller's failed transaction it can't, and the next run's DROP does.
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            await conn.execute("DROP TABLE IF EXISTS pg_temp.recon_obj")


async def _load_batch(conn: psycopg.AsyncConnection, rows: list[tuple]) -> None:
    async with conn.cursor() as cur:
        async with cur.copy(
            "COPY recon_obj (key, version_id, size, native_class, retention_until, legal_hold,"
            " is_current) FROM STDIN"
        ) as copy:
            for row in rows:
                await copy.write_row(row)


async def _load_and_compare(
    conn: psycopg.AsyncConnection,
    backend_id: int,
    container: str,
    now: datetime,
    source: InventorySource,
    worm: bool,
    report: ReconcileReport,
    prefixes: list[str] | None,
) -> None:
    seen = 0
    rows: list[tuple] = []
    async for it in source.items(container):
        rows.append(_row(it))
        seen += 1
        if len(rows) >= LOAD_BATCH:
            await _load_batch(conn, rows)
            rows = []
    if rows:
        await _load_batch(conn, rows)
    await conn.execute("CREATE INDEX ON recon_obj (key)")
    stale = bool(getattr(source, "stale", False))
    if stale:
        # A report may list a blob twice [D]: keep one row per version.
        await conn.execute(
            "DELETE FROM recon_obj a USING recon_obj b WHERE a.key = b.key"
            " AND a.version_id IS NOT DISTINCT FROM b.version_id AND a.ctid > b.ctid"
        )
    snap = source.snapshot_at
    p = {"bid": backend_id, "c": container, "snap": snap, "pfx": prefixes, "stale": stale}
    if not source.complete_versions:
        # The listing can't see versions of keys with no current version
        # (a versioned delete keeps them): ask about each such key.
        cur = await conn.execute(_UNLISTED_KEYS, p)
        keys = [r[0] for r in await cur.fetchall()]
        for it in await source.probe(container, keys) if keys else []:
            await conn.execute(
                "INSERT INTO recon_obj (key, version_id, size, native_class, retention_until,"
                " legal_hold, is_current) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                _row(it),
            )
            seen += 1
    await conn.execute("ANALYZE recon_obj")
    # Nothing below calls the backend.
    async with conn.transaction():
        await _compare_loaded(conn, backend_id, container, now, worm, report, seen, snap, p)


async def _compare_loaded(
    conn: psycopg.AsyncConnection,
    backend_id: int,
    container: str,
    now: datetime,
    worm: bool,
    report: ReconcileReport,
    seen: int,
    snap: datetime | None,
    p: dict,
) -> None:
    run_id = report.run_id

    async def drift(key: str, ver: str | None, kind: str, lid: int | None, exp, obs) -> None:
        report.drift_open.append({"key": key, "kind": kind, "location_id": lid})
        report.drift_found += await record_drift(
            conn, source="reconcile", backend_id=backend_id, container=container, key=key,
            version=ver, kind=kind, location_id=lid, expected=exp, observed=obs, now=now,
            run_id=run_id,
        )  # fmt: skip

    cur = await conn.execute(_MISSING, p)
    for lid, key, ver, role, size in await cur.fetchall():
        await drift(key, ver, "missing", lid, {"role": role, "size": size}, {"present": False})
    cur = await conn.execute(_UNEXPECTED, p)
    for key, ver, size, cls in await cur.fetchall():
        await drift(key, ver, "unexpected", None, {"present": False},
                    {"size": size, "class": cls})  # fmt: skip
    cur = await conn.execute(_MISMATCH, p)
    for (lid, key, over, role, lsize, osize, lcls, ocls, vuntil, ountil, vhold, ohold,
         pending, tier_since, restoring) in await cur.fetchall():  # fmt: skip
        if lsize is not None and lsize != osize:
            await drift(key, over, "size_mismatch", lid, {"size": lsize}, {"size": osize})
        if (not restoring and not tier_since and lcls is not None
                and lcls.lower() != ocls.lower()):  # fmt: skip
            await drift(key, over, "tier_mismatch", lid, {"class": lcls}, {"class": ocls})
        if worm and role in ("primary", "secondary") and not pending:
            if vuntil is not None and (ountil is None or ountil < vuntil - timedelta(seconds=1)):
                seen_until = ountil.isoformat() if ountil else None
                await drift(key, over, "retention_mismatch", lid,
                            {"retention_until": vuntil.isoformat()},
                            {"retention_until": seen_until})  # fmt: skip
            if bool(vhold) != bool(ohold):
                await drift(key, over, "hold_mismatch", lid, {"legal_hold": bool(vhold)},
                            {"legal_hold": bool(ohold)})  # fmt: skip
    cur = await conn.execute(
        "SELECT count(*) FROM catalog.location l WHERE l.backend_id = %(bid)s"
        " AND l.container = %(c)s AND l.state = 'committed'" + _SCOPE,
        p,
    )
    report.locations_checked = (await cur.fetchone())[0]  # type: ignore[index]
    report.objects_seen = seen
    cur = await conn.execute(
        "UPDATE catalog.reconcile_drift SET resolved_at = %s"
        " WHERE backend_id = %s AND container = %s AND source = 'reconcile'"
        " AND resolved_at IS NULL AND last_run_id IS DISTINCT FROM %s",
        (now, backend_id, container, run_id),
    )
    report.drift_resolved = cur.rowcount
    # Change-feed drift about a location is resolved once the location is no
    # longer committed, or its version is present again.
    cur = await conn.execute(
        """
        UPDATE catalog.reconcile_drift d SET resolved_at = %(now)s
         WHERE d.backend_id = %(bid)s AND d.container = %(c)s AND d.source = 'changefeed'
           AND d.resolved_at IS NULL AND d.location_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM catalog.location l
                            WHERE l.location_id = d.location_id AND l.state = 'committed'
                              AND NOT EXISTS (SELECT 1 FROM recon_obj o WHERE """
        + _MATCH
        + """))
        """,
        {**p, "now": now},
    )
    report.drift_resolved += cur.rowcount
    await conn.execute(
        """
        UPDATE catalog.reconcile_run
           SET state = 'completed', completed_at = %s, snapshot_at = %s, objects_seen = %s,
               locations_checked = %s, drift_found = %s, drift_resolved = %s
         WHERE run_id = %s
        """,
        (now, snap, seen, report.locations_checked, len(report.drift_open),
         report.drift_resolved, run_id),
    )  # fmt: skip


def _row(it: InventoryItem) -> tuple:
    return (it.key, it.version_id, it.size, it.native_class, it.retention_until, it.legal_hold,
            it.is_current)  # fmt: skip


async def source_prefixes(
    conn: psycopg.AsyncConnection, backend_id: int, container: str
) -> list[str] | None:
    """The container's source prefixes, or None if it has no sources (all
    of it is OmniFile's). Raises `SourcesSeeding` while one is unfinished."""
    cur = await conn.execute(
        "SELECT prefix, state FROM catalog.source WHERE backend_id = %s AND container = %s"
        " ORDER BY prefix",
        (backend_id, container),
    )
    rows = await cur.fetchall()
    if not rows:
        return None
    busy = [f"{prefix or '(all)'} is {state}" for prefix, state in rows if state != "seeded"]
    if busy:
        raise SourcesSeeding(f"{container}: {', '.join(busy)}")
    return [prefix for prefix, _ in rows]


async def register_known_containers(conn: psycopg.AsyncConnection) -> int:
    """Follow (change feed) and reconcile every container the datasets'
    backend configs imply. Idempotent. Returns how many are registered."""
    from ..worker.changefeed_sync import register_container

    cur = await conn.execute(
        "SELECT DISTINCT b.backend_id, b.config FROM catalog.dataset d"
        " JOIN catalog.backend b ON b.backend_id = d.backend_id"
    )
    n = 0
    for bid, config in await cur.fetchall():
        for c in known_containers(config or {}):
            await register_container(conn, bid, c)
            n += 1
    return n


async def reconcile_due(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    now: datetime,
    *,
    every: timedelta = timedelta(hours=20),
) -> list[ReconcileReport]:
    """The nightly sweep: reconcile each registered container whose last
    completed run is older than `every`."""
    cur = await conn.execute(
        """
        SELECT c.backend_id, c.container
          FROM catalog.changefeed_cursor c
         WHERE NOT EXISTS (SELECT 1 FROM catalog.reconcile_run r
                            WHERE r.backend_id = c.backend_id AND r.container = c.container
                              AND r.state IN ('completed', 'running')
                              AND r.started_at > %s)
           AND NOT EXISTS (SELECT 1 FROM catalog.source s
                            WHERE s.backend_id = c.backend_id AND s.container = c.container
                              AND s.state <> 'seeded')
         ORDER BY 1, 2
        """,
        (now - every,),
    )
    reports = []
    for bid, container in await cur.fetchall():
        try:
            reports.append(await reconcile_container(conn, registry, bid, container, now))
        except Exception as e:  # noqa: BLE001 - one container must not stop the rest
            log.warning("reconcile %s/%s failed: %s", bid, container, e)
    return reports
