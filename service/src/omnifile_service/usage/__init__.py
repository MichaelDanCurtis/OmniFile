"""Usage: bytes and monthly storage cost by tier, share and dataset
(M5; portal-api.md section 5.11, GET /v1/usage).

`snapshot` counts the catalog once a day into `catalog.usage_snapshot`
(migration 0014); `report` reads the latest one and prices it.

- **Datasets:** every physical object a committed location names (once,
  however many dedupe references share it), by backend, role, logical tier
  and native class: what the provider bills, including older versions kept
  for rewind and recall copies.
- **Shares:** the live files under each share (its deepest covering share,
  as access works), by logical tier: the current bytes users see.
- **Cost:** storage only, per month, at the class's `gb_month` in the
  backend kind's price table (an ArchiveAdmin's override, or the built-in
  one), for the account the tier lives in. Classes the table doesn't price
  are listed in `unpriced`, never guessed. Operations, retrievals and early
  deletion depend on activity and are the policy preview's business.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..backends.base import LogicalTier
from ..policy.cost import CENT, GiB, PriceTable, PriceTableError, account_for, load_price_table

#: Snapshots kept, for trends.
KEEP = timedelta(days=35)


async def snapshot(conn: psycopg.AsyncConnection, now: datetime) -> int:
    """Write one snapshot at `now` on `conn`'s transaction. Returns its rows."""
    cur = await conn.execute(
        """
        INSERT INTO catalog.usage_snapshot
            (computed_at, dataset_id, share_id, backend_id, role, logical_tier, storage_class,
             objects, bytes)
        SELECT %(at)s, o.dataset_id, NULL, o.backend_id, o.role, o.logical_tier::text,
               o.storage_class, count(*), coalesce(sum(o.size), 0)
          FROM (
              -- One row per physical object: whole-file dedupe references
              -- one blob from several versions, and the provider bills it once.
              SELECT DISTINCT ON (l.backend_id, l.container, l.object_key, l.backend_version_id)
                     f.dataset_id, l.backend_id, l.role, l.logical_tier, l.storage_class,
                     coalesce(l.size, f.size) AS size
                FROM catalog.location l
                JOIN catalog.file f ON f.file_id = l.file_id
               WHERE l.state = 'committed'
               ORDER BY l.backend_id, l.container, l.object_key, l.backend_version_id,
                        l.location_id
          ) o
         GROUP BY o.dataset_id, o.backend_id, o.role, o.logical_tier, o.storage_class
        """,
        {"at": now},
    )
    rows = cur.rowcount
    cur = await conn.execute(
        """
        WITH RECURSIVE under(file_id, share_id) AS (
            SELECT coalesce(s.root_dir_id, r.file_id), s.share_id
              FROM catalog.share s
              JOIN catalog.file r ON r.dataset_id = s.dataset_id AND r.parent_id IS NULL
            UNION ALL
            SELECT f.file_id, u.share_id
              FROM under u
              JOIN catalog.file f ON f.parent_id = u.file_id
             WHERE f.deleted_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM catalog.share n WHERE n.root_dir_id = f.file_id)
        )
        INSERT INTO catalog.usage_snapshot
            (computed_at, dataset_id, share_id, logical_tier, objects, bytes)
        SELECT %(at)s, f.dataset_id, u.share_id, f.remote_tier::text, count(*),
               coalesce(sum(f.size), 0)
          FROM under u
          JOIN catalog.file f ON f.file_id = u.file_id
         WHERE f.kind = 'file'
         GROUP BY f.dataset_id, u.share_id, f.remote_tier
        """,
        {"at": now},
    )
    rows += cur.rowcount
    await conn.execute("DELETE FROM catalog.usage_snapshot WHERE computed_at < %s", (now - KEEP,))
    return rows


def _gib(b: int) -> Decimal:
    return Decimal(b) / GiB


async def _tables(conn: psycopg.AsyncConnection, kinds: set[str]) -> dict[str, PriceTable]:
    cur = conn.cursor(row_factory=dict_row)
    out: dict[str, PriceTable] = {}
    for kind in sorted(kinds):
        await cur.execute(
            "SELECT body FROM catalog.price_table WHERE backend_kind = %s AND region = ''",
            (kind,),
        )
        row = await cur.fetchone()
        try:
            out[kind] = load_price_table(kind, row["body"] if row else None)
        except PriceTableError:
            continue  # an unknown kind: every class of it is unpriced
    return out


async def report(conn: psycopg.AsyncConnection, dataset_id: int | None = None) -> dict[str, Any]:
    """The latest snapshot, priced (the API's `Usage`)."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT max(computed_at) AS at FROM catalog.usage_snapshot")
    row = await cur.fetchone()
    at = row["at"] if row else None
    if at is None:
        return {"computed_at": None, "datasets": [], "shares": [], "prices": [], "unpriced": []}
    await cur.execute(
        """
        SELECT u.*, d.storage_layout, b.kind AS backend_kind, s.name AS share_name
          FROM catalog.usage_snapshot u
          JOIN catalog.dataset d ON d.dataset_id = u.dataset_id
          LEFT JOIN catalog.backend b ON b.backend_id = u.backend_id
          LEFT JOIN catalog.share s ON s.share_id = u.share_id
         WHERE u.computed_at = %s AND (%s::bigint IS NULL OR u.dataset_id = %s::bigint)
         ORDER BY u.dataset_id, u.share_id NULLS FIRST, u.backend_id, u.role, u.logical_tier,
                  u.storage_class
        """,
        (at, dataset_id, dataset_id),
    )
    rows = await cur.fetchall()
    tables = await _tables(conn, {r["backend_kind"] for r in rows if r["backend_kind"]})
    unpriced: list[str] = []
    datasets: dict[int, dict[str, Any]] = {}
    shares: dict[int, dict[str, Any]] = {}
    totals: dict[int, dict[str, Decimal]] = defaultdict(dict)
    for r in rows:
        if r["share_id"] is not None:
            sh = shares.setdefault(r["share_id"], {
                "share_id": str(r["share_id"]), "dataset_id": str(r["dataset_id"]),
                "name": r["share_name"], "files": 0, "bytes": 0, "by_tier": []})  # fmt: skip
            sh["files"] += r["objects"]
            sh["bytes"] += r["bytes"]
            sh["by_tier"].append({"tier": r["logical_tier"], "files": r["objects"],
                                  "bytes": r["bytes"]})  # fmt: skip
            continue
        blank = {"dataset_id": str(r["dataset_id"]), "objects": 0, "bytes": 0, "stored": []}
        ds = datasets.setdefault(r["dataset_id"], blank)
        cost = _monthly(tables.get(r["backend_kind"]), r, unpriced)
        if cost is not None:
            cur_tot = totals[r["dataset_id"]]
            cur_tot[cost[1]] = cur_tot.get(cost[1], Decimal(0)) + cost[0]
        ds["objects"] += r["objects"]
        ds["bytes"] += r["bytes"]
        ds["stored"].append({
            "backend_id": str(r["backend_id"]), "backend_kind": r["backend_kind"],
            "role": r["role"], "tier": r["logical_tier"], "storage_class": r["storage_class"],
            "objects": r["objects"], "bytes": r["bytes"],
            "monthly_cost": None if cost is None else _money(cost[0], cost[1]),
        })  # fmt: skip
    for ds_id, ds in datasets.items():
        ds["monthly_cost"] = [_money(v, c) for c, v in sorted(totals[ds_id].items())]
    return {
        "computed_at": at,
        "datasets": list(datasets.values()),
        "shares": list(shares.values()),
        "prices": [t.ref() for t in tables.values()],
        "unpriced": unpriced,
    }


def _monthly(
    table: PriceTable | None, r: Mapping[str, Any], unpriced: list[str]
) -> tuple[Decimal, str] | None:
    """(cost, currency) of one stored row per month, or None (and the class
    named in `unpriced`) when the table has no `gb_month` for it."""
    native = r["storage_class"]
    tier = r["logical_tier"]
    account = None
    if tier is not None:
        try:
            account = account_for(r["storage_layout"], LogicalTier(tier))
        except ValueError:
            account = None
    price = None if table is None or native is None else table.price(native, account)
    if price is None or price.gb_month is None:
        label = f"{r['backend_kind']}: {native or tier or 'unknown'}"
        if account:
            label += f" ({account} account)"
        if label not in unpriced:
            unpriced.append(label)
        return None
    return price.gb_month * _gib(r["bytes"]), table.currency  # type: ignore[union-attr]


def _money(v: Decimal, currency: str) -> dict[str, str]:
    return {"amount": str(v.quantize(CENT, ROUND_HALF_UP)), "currency": currency}


__all__ = ["KEEP", "report", "snapshot"]
