"""Catalog scale (M7): a dataset of OMNIFILE_SCALE_FILES files, seeded from an S3
Inventory through the real seeder, then the edge's calls that walk it, through
the api app under its own database role.

Skipped unless OMNIFILE_SCALE_FILES is set (for example 1000000). The target is
10^8 files per dataset (catalog-seeding.md); a laptop holds about 10^6. What
this checks at any size is that the cost per row stays flat as the catalog
grows (an index scan per page, not a scan of the dataset), so the time for
10^8 is 100 times the time for 10^6:

- seeding: the time per batch of the last tenth of the batches against the
  first tenth;
- the paged snapshot (GET .../snapshot): the time per page at the end against
  the start;
- one change (POST .../changes) and one as-of folder listing on the full catalog.

Seeding takes the bulk path (batches of 20,000 through `catalog.seed_merge`,
catalog-seeding.md section 6). With OMNIFILE_SCALE_MIN_RATE set (files per
second, for example 10000), the seeding rate must reach it.

Timings go to stdout and, with OMNIFILE_SCALE_OUT, to a JSON file.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
import uuid
from typing import Any

import psycopg
import pytest
from backend_contract.clock import ManualClock
from s3.s3_fakes import WEB_IDENTITY, FakeAws

from omnifile_service.backends.s3 import S3Backend, S3Config
from omnifile_service.catalog.privileges import as_app
from omnifile_service.sources import Groups, register_source, seed_step
from omnifile_service.worker.backends import BackendRegistry

from .conftest import E, create_file

N = int(os.environ.get("OMNIFILE_SCALE_FILES", "0"))
pytestmark = pytest.mark.skipif(N <= 0, reason="OMNIFILE_SCALE_FILES is not set")

BUCKET = "scale-src"
INV = "scale-inv"
PER_DIR = 1000
PART_ROWS = 100_000
BATCH = 20_000
GROUPS = Groups("S-1-5-21-1-2-3-512", "S-1-5-21-1-2-3-513", "S-1-5-21-1-2-3-514")


def _inventory_parts(n: int) -> list[bytes]:
    import gzip

    parts: list[bytes] = []
    rows: list[str] = []
    for i in range(n):
        key = f"d{i // PER_DIR:05d}/f{i % PER_DIR:04d}.segy"
        rows.append(
            f'"{BUCKET}","{key}","v1","true","false","{1 << 20}",'
            f'"2026-09-01T10:00:00.000Z","{"0" * 32}","STANDARD","SSE-S3",""\n'
        )
        if len(rows) == PART_ROWS or i == n - 1:
            parts.append(gzip.compress("".join(rows).encode(), compresslevel=1))
            rows = []
    return parts


async def _seed(db_url: str, dataset_id: int, s3_id: int, clock_now) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    import hashlib
    from collections.abc import AsyncIterator

    fake = FakeAws(clock_now)
    queue = fake.queue_url("scale-events")
    fake.add_bucket(BUCKET, queue=queue, versioning=True)
    fake.add_bucket(INV)

    s3 = S3Backend(
        s3_id,
        S3Config(
            region="us-east-1",
            buckets=(BUCKET, INV),
            private_network=False,
            **WEB_IDENTITY,
            event_queues={BUCKET: queue},
            # The test writes the inventory itself (S3 would); the source
            # bucket stays read-only to this binding.
            owned_buckets=(INV,),
        ),
        clock=ManualClock(clock_now()),
        on_client=fake.attach,
    )

    async def one(data: bytes) -> AsyncIterator[bytes]:
        yield data

    t0 = time.monotonic()
    entries = []
    for i, body in enumerate(_inventory_parts(N)):
        key = f"{BUCKET}/daily/scale/data/part-{i}.csv.gz"
        await s3.put(INV, key, one(body), None, {})
        entries.append(
            {"key": key, "size": len(body), "MD5checksum": hashlib.md5(body).hexdigest()}
        )
    created = int((clock_now().timestamp() - 6 * 3600) * 1000)
    manifest = json.dumps({
        "sourceBucket": BUCKET, "destinationBucket": f"arn:aws:s3:::{INV}",
        "version": "2016-11-30", "creationTimestamp": str(created), "fileFormat": "CSV",
        "fileSchema": "Bucket, Key, VersionId, IsLatest, IsDeleteMarker, Size,"
        " LastModifiedDate, ETag, StorageClass, EncryptionStatus, IntelligentTieringAccessTier",
        "files": entries,
    }).encode()  # fmt: skip
    mkey = f"{BUCKET}/daily/scale/manifest.json"
    await s3.put(INV, mkey, one(manifest), None, {})
    await s3.put(
        INV, f"{BUCKET}/daily/scale/manifest.checksum",
        one(hashlib.md5(manifest).hexdigest().encode()), None, {},
    )  # fmt: skip
    build_s = time.monotonic() - t0

    registry = BackendRegistry.from_mapping({s3_id: s3})
    conn = await psycopg.AsyncConnection.connect(as_app(db_url, "worker"), autocommit=True)
    try:
        source_id = await register_source(
            conn, s3, dataset_id=dataset_id, container=BUCKET, groups=GROUPS,
            inventory={"kind": "s3_inventory", "bucket": INV, "manifest_key": mkey},
        )  # fmt: skip
        batches: list[float] = []
        prof = None
        if os.environ.get("OMNIFILE_SCALE_PROFILE"):
            import cProfile

            prof = cProfile.Profile()
            prof.enable()
        t0 = time.monotonic()
        while True:
            t = time.monotonic()
            async with conn.transaction():
                state = await seed_step(conn, registry.get, source_id, clock_now(), batch=BATCH)
            batches.append(time.monotonic() - t)
            if state in ("seeded", "failed"):
                break
        seed_s = time.monotonic() - t0
        if prof is not None:
            import pstats

            prof.disable()
            pstats.Stats(prof).sort_stats("tottime").print_stats(15)
    finally:
        await conn.close()
    assert state == "seeded", state
    return {"inventory_build_s": build_s, "seed_s": seed_s, "batches": batches}


def _tenth(xs: list[float], last: bool) -> float:
    k = max(1, len(xs) // 10)
    return statistics.median(xs[-k:] if last else xs[:k])


def test_the_edge_walks_a_large_catalog_at_a_flat_cost_per_row(make_world, sql, db_url) -> None:
    from datetime import UTC, datetime

    w = make_world()
    (s3_id,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, false) RETURNING backend_id",
        (
            f"s3-{uuid.uuid4().hex[:8]}",
            json.dumps({"region": "us-east-1", "buckets": [BUCKET, INV]}),
        ),
    ).fetchone()
    out: dict[str, Any] = {
        "files": N,
        "server": sql.execute("SELECT version()").fetchone()[0],
    }

    seeded = asyncio.run(_seed(db_url, w.dataset_id, s3_id, lambda: datetime.now(UTC)))
    b = seeded.pop("batches")
    # The first batch also reads the manifest, and the last only finishes:
    # compare the full batches between.
    steady = b[1:-1] if len(b) > 3 else b
    out.update(seeded)
    if os.environ.get("OMNIFILE_SCALE_PROFILE"):
        # Needs track_functions = 'pl' on the server.
        for name, calls, self_ms, total_ms in sql.execute(
            "SELECT funcname, calls, self_time, total_time FROM pg_stat_user_functions"
            " ORDER BY self_time DESC LIMIT 12"
        ).fetchall():
            print(f"FN {name:32} calls={calls:8} self_ms={self_ms:10.0f} total_ms={total_ms:10.0f}")
    out["seed_rows_per_s"] = N / seeded["seed_s"]
    out["seed_batch_s_first_tenth"] = _tenth(steady, last=False)
    out["seed_batch_s_last_tenth"] = _tenth(steady, last=True)
    (count,) = sql.execute(
        "SELECT count(*) FROM catalog.file WHERE dataset_id = %s AND kind = 'file'",
        (w.dataset_id,),
    ).fetchone()
    assert count == N
    out["db_size_mb"] = sql.execute("SELECT pg_database_size(current_database())").fetchone()[0] / (
        1 << 20
    )

    # The paged snapshot, as an edge loads it (10,000 rows a page).
    pages: list[float] = []  # full pages only (the last may be short)
    after: str | None = None
    rows = 0
    t0 = time.monotonic()
    while True:
        q: dict[str, Any] = {"dataset_id": str(w.dataset_id), "limit": 10000}
        if after:
            q["after_file_id"] = after
        t = time.monotonic()
        # check=False: validating a 6 MB page against the OpenAPI schema is
        # the test harness's cost, not the service's.
        page = w.edge.get(f"{E}/snapshot", q, check=False).json()
        pages.append(time.monotonic() - t)
        rows += len(page["files"])
        if not page["more"]:
            pages.pop()
            break
        after = page["files"][-1]["file_id"]
    out["snapshot_s"] = time.monotonic() - t0
    pages = pages or [0.0]
    out["snapshot_page_s_first_tenth"] = _tenth(pages[1:] or pages, last=False)
    out["snapshot_page_s_last_tenth"] = _tenth(pages, last=True)
    assert rows >= N

    # The same page in SQL alone, mid-dataset: what of a page is the database's.
    from omnifile_service.api.rows import FILE_SELECT

    (mid,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s ORDER BY file_id OFFSET %s LIMIT 1",
        (w.dataset_id, N // 2),
    ).fetchone()
    plan = sql.execute(
        "EXPLAIN (ANALYZE, FORMAT JSON) "
        + FILE_SELECT
        + " WHERE f.dataset_id = %s AND f.deleted_at IS NULL AND f.file_id > %s"
        " ORDER BY f.file_id LIMIT 10001",
        (w.dataset_id, mid),
    ).fetchone()[0][0]
    out["snapshot_page_sql_ms"] = plan["Execution Time"]
    if os.environ.get("OMNIFILE_SCALE_PROFILE"):
        import cProfile
        import pstats

        from psycopg.rows import dict_row

        from omnifile_service.api.rows import file_row

        with psycopg.connect(db_url) as c2:
            c2.row_factory = dict_row
            rows = c2.execute(
                FILE_SELECT + " WHERE f.dataset_id = %s AND f.file_id > %s"
                " ORDER BY f.file_id LIMIT 10000",
                (w.dataset_id, mid),
            ).fetchall()
        pr = cProfile.Profile()
        pr.enable()
        built = [file_row(r) for r in rows]
        body = json.dumps({"files": built})
        pr.disable()
        pstats.Stats(pr).sort_stats("cumulative").print_stats(12)
        print("body bytes", len(body))

    # The digest the edge checks daily.
    t = time.monotonic()
    w.edge.get(f"{E}/digest", {"dataset_id": str(w.dataset_id)}, check=False)
    out["digest_s"] = time.monotonic() - t

    # One folder's as-of listing (1,000 entries), and one change, on the full catalog.
    (dir_id,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND kind = 'dir' AND name = %s",
        (w.dataset_id, f"d{(N // PER_DIR) // 2:05d}"),
    ).fetchone()
    t = time.monotonic()
    w.edge.get(
        f"{E}/asof", {"dir_id": str(dir_id), "t": datetime.now(UTC).isoformat()}, check=False
    )
    out["asof_folder_s"] = time.monotonic() - t
    t = time.monotonic()
    create_file(w, "new.segy", parent=dir_id)
    out["change_s"] = time.monotonic() - t

    print("\nSCALE " + json.dumps(out, indent=2))
    if path := os.environ.get("OMNIFILE_SCALE_OUT"):
        with open(path, "w") as f:
            json.dump(out, f, indent=2)
    # Flat per row: the end is no more than 3x the start (noise allowed; a
    # scan of the dataset per batch or page would grow with it).
    assert out["seed_batch_s_last_tenth"] <= 3 * out["seed_batch_s_first_tenth"] + 0.05, out
    assert out["snapshot_page_s_last_tenth"] <= 3 * out["snapshot_page_s_first_tenth"] + 0.05, out
    if rate := float(os.environ.get("OMNIFILE_SCALE_MIN_RATE", "0")):
        assert out["seed_rows_per_s"] >= rate, out
