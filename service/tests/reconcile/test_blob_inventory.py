"""Reconcile from Azure Blob Inventory reports (reconcile/blob_inventory.py):
CSV and Parquet reports generated in the documented layout, read as streams,
checked before use, and compared with the catalog as a listing would be."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from psycopg.types.json import Jsonb
from retention.support import (
    _one,
    add_file,
    clock_days_ago,
    commit_content,
    run,
    seed,
    with_conn,
)

from omnifile_service.backends.base import LogicalTier
from omnifile_service.backends.sim import VirtualClock
from omnifile_service.reconcile import open_drift, reconcile_container
from omnifile_service.reconcile.blob_inventory import (
    BlobInventory,
    InventoryError,
    check_manifest,
    csv_records,
    latest_report,
    parquet_rows,
)
from omnifile_service.reconcile.inventory import ListingInventory, choose_inventory

H = timedelta(hours=1)
DEST = "live/inventory"
RULE = "omnifile-reconcile"
FIELDS = ["Name", "Creation-Time", "Last-Modified", "Content-Length", "Content-MD5",
          "AccessTier", "VersionId", "IsCurrentVersion", "Deleted", "Snapshot"]  # fmt: skip
RULE_DEF = {
    "filters": {"blobTypes": ["blockBlob"], "includeBlobVersions": True,
                "includeSnapshots": True, "includeDeleted": True, "prefixMatch": ["data"]},
    "format": "csv", "objectType": "blob", "schedule": "daily", "schemaFields": FIELDS,
}  # fmt: skip


# --- fixtures: reports as Azure writes them -------------------------------------


async def listed(w) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Report rows for every stored version of the world's container, as
    the inventory would list them now."""
    rows: list[dict[str, Any]] = []
    cursor = None
    short = w.container.split("/", 1)[1]
    while True:
        page = await w.sim.list_all_versions(w.container, "", cursor)
        for lv in page.items:
            h = lv.head
            rows.append({
                "Name": f"{short}/{h.ref.key}", "Creation-Time": h.last_modified,
                "Last-Modified": h.last_modified, "Content-Length": h.size,
                "Content-MD5": base64.b64encode(h.md5).decode() if h.md5 else "",
                "AccessTier": h.native_class, "VersionId": h.ref.version_id or "",
                "IsCurrentVersion": lv.is_current, "Deleted": False, "Snapshot": "",
            })  # fmt: skip
        if page.cursor is None:
            return rows
        cursor = page.cursor


def csv_bytes(rows: Iterable[Mapping[str, Any]], fields: list[str] = FIELDS) -> bytes:
    out = io.StringIO(newline="")
    wr = csv.writer(out, lineterminator="\n")
    wr.writerow(fields)
    for r in rows:
        vals = []
        for f in fields:
            v = r.get(f, "")
            if isinstance(v, datetime):
                v = v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")  # 7 digits, as .NET
            elif isinstance(v, bool):
                v = "true" if v else "false"
            vals.append(v)
        wr.writerow(vals)
    return out.getvalue().encode()


def parquet_bytes(rows: list[Mapping[str, Any]], *, row_group: int = 1000) -> bytes:
    def ts(v: Any) -> Any:
        return v.astimezone(UTC).replace(tzinfo=None) if isinstance(v, datetime) else None

    table = pa.table({
        "Name": pa.array([r["Name"] for r in rows], pa.string()),
        "Creation-Time": pa.array([ts(r.get("Creation-Time")) for r in rows], pa.timestamp("ms")),
        "Last-Modified": pa.array([ts(r.get("Last-Modified")) for r in rows], pa.timestamp("ms")),
        "Content-Length": pa.array([int(r["Content-Length"]) for r in rows], pa.int64()),
        "Content-MD5": pa.array([r.get("Content-MD5", "") for r in rows], pa.string()),
        "AccessTier": pa.array([r.get("AccessTier") or None for r in rows], pa.string()),
        "VersionId": pa.array([r.get("VersionId") or None for r in rows], pa.string()),
        "IsCurrentVersion": pa.array([bool(r.get("IsCurrentVersion")) for r in rows], pa.bool_()),
        "Deleted": pa.array([bool(r.get("Deleted")) for r in rows], pa.bool_()),
        "Snapshot": pa.array([r.get("Snapshot") or None for r in rows], pa.string()),
    })  # fmt: skip
    buf = io.BytesIO()
    pq.write_table(table, buf, row_group_size=row_group)
    return buf.getvalue()


def write_report(
    w: Any,
    files: list[bytes],
    *,
    started: datetime,
    fmt: str = "csv",
    rule: Mapping[str, Any] | None = None,
    status: str = "Succeeded",
    checksum: str = "hex",
    sizes: list[int] | None = None,
    object_count: int | None = None,
) -> str:
    """One inventory run in the destination container, in Azure's layout
    (report files, manifest, then the checksum file). Returns the manifest.
    `sizes` and `object_count` put other numbers in the manifest than the
    files' own (a manifest that doesn't match its files)."""
    base = f"{started:%Y/%m/%d/%H-%M-%S}/{RULE}"
    entries = []
    rows = 0
    for i, body in enumerate(files):
        blob = f"{base}/{RULE}_{i + 1}.{fmt}"
        run(w.sim.put(DEST, blob, _one(body), None, {}))
        entries.append({"blob": blob, "size": sizes[i] if sizes else len(body)})
        if fmt == "parquet":
            rows += pq.read_metadata(io.BytesIO(body)).num_rows
        else:
            rows += max(0, len(list(csv.reader(io.StringIO(body.decode(), newline="")))) - 1)
    if object_count is not None:
        rows = object_count
    definition = dict(rule or {**RULE_DEF, "format": fmt})
    manifest = json.dumps({
        "destinationContainer": "inventory", "endpoint": "https://live.blob.core.windows.net",
        "files": entries, "inventoryStartTime": started.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "inventoryCompletionTime": (started + timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "ruleDefinition": definition, "ruleName": RULE, "status": status,
        "summary": {"objectCount": rows, "totalObjectSize": 0}, "version": "1.0",
    }).encode()  # fmt: skip
    run(w.sim.put(DEST, f"{base}/{RULE}-manifest.json", _one(manifest), None, {}))
    digest = hashlib.md5(manifest).digest()
    text = digest.hex() if checksum == "hex" else base64.b64encode(digest).decode()
    if checksum == "bad":
        text = "0" * 32
    run(w.sim.put(DEST, f"{base}/{RULE}-manifest.checksum", _one(text.encode()), None, {}))
    return f"{base}/{RULE}-manifest.json"


def configure(db: psycopg.Connection, w: Any, **extra: Any) -> None:
    db.execute(
        "UPDATE catalog.backend SET config = config || %s WHERE backend_id = %s",
        (Jsonb({"inventory": {"container": "inventory", "rule": RULE, **extra}}), w.backend_id),
    )


def _reconcile(connect, w, clock):  # type: ignore[no-untyped-def]
    return with_conn(
        connect, lambda c: reconcile_container(c, w.registry, w.backend_id, w.container,
                                               clock.now()),
    )  # fmt: skip


def _kinds(connect, w) -> dict[str, str]:  # type: ignore[no-untyped-def]
    rows = with_conn(connect, lambda c: open_drift(c, w.backend_id, w.container))
    return {f"{r['object_key']}:{r['kind']}": r["source"] for r in rows}


# --- reading -----------------------------------------------------------------------


async def _chunks(data: bytes, size: int, pulled: list[int]) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        pulled.append(i)
        yield data[i : i + size]


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 1 << 20])
def test_csv_records_survive_any_chunking(chunk: int) -> None:
    rows = [
        {"Name": 'data/a "quoted", name', "Content-Length": 1},
        {"Name": "data/line\nbreak", "Content-Length": 2},
        {"Name": "data/ünïcødé/ファイル", "Content-Length": 3},
        {"Name": "data/plain", "Content-Length": 4},
    ]
    data = b"\xef\xbb\xbf" + csv_bytes(rows, ["Name", "Content-Length"])  # with a BOM

    async def go() -> list[list[str]]:
        return [r async for r in csv_records(_chunks(data, chunk, []))]

    got = asyncio.run(go())
    assert got == [["Name", "Content-Length"]] + [[r["Name"], str(r["Content-Length"])]
                                                  for r in rows]  # fmt: skip


def test_csv_records_are_streamed_not_read_whole() -> None:
    data = csv_bytes([{"Name": f"data/f{i}", "Content-Length": i} for i in range(5000)])
    pulled: list[int] = []

    async def first() -> int:
        async for _ in csv_records(_chunks(data, 4096, pulled)):
            return len(pulled)
        return -1

    assert asyncio.run(first()) == 1  # the header came from the first chunk
    assert len(data) > 20 * 4096


def test_a_csv_report_ending_inside_quotes_is_refused() -> None:
    async def go() -> None:
        async for _ in csv_records(_chunks(b'Name\n"data/never closed\n', 5, [])):
            pass

    with pytest.raises(InventoryError, match="quoted"):
        asyncio.run(go())


def test_parquet_reports_are_read_row_group_by_row_group(db, connect) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(1)
    w = seed(db, clock)
    rows = [{"Name": f"data/f{i:06d}", "Content-Length": i, "AccessTier": "Hot",
             "VersionId": f"v{i}", "IsCurrentVersion": True} for i in range(20000)]  # fmt: skip
    body = parquet_bytes(rows, row_group=2000)
    ref = run(w.sim.put(DEST, "p.parquet", _one(body), None, {}))
    reads: list[tuple[int, int]] = []

    async def go() -> list[dict]:
        return [r async for r in parquet_rows(w.sim, ref, len(body), batch_rows=1000,
                                              read_ahead=16 << 10, reads=reads)]  # fmt: skip

    got = run(go())
    assert [r["Name"] for r in got] == [r["Name"] for r in rows]
    assert got[5]["Content-Length"] == 5 and got[5]["IsCurrentVersion"] is True
    # Many small range reads, none of them the whole file.
    assert len(reads) > 10 and max(n for _, n in reads) < len(body) // 4, (len(body), reads)


# --- choosing a report -------------------------------------------------------------


def test_a_manifest_is_used_only_when_it_covers_the_container() -> None:
    base = {"status": "Succeeded", "ruleDefinition": RULE_DEF, "files": []}
    check_manifest(base, "data", versioned=True)

    def bad(**rule: Any) -> dict:
        return {**base, "ruleDefinition": {**RULE_DEF, **rule}}

    def flt(**f: Any) -> dict:
        return bad(filters={**RULE_DEF["filters"], **f})

    cases = {
        "status": {**base, "status": "PartiallySucceeded"},
        "containers": bad(objectType="container"),
        "format": bad(format="orc"),
        "AccessTier": bad(schemaFields=[f for f in FIELDS if f != "AccessTier"]),
        "VersionId": bad(schemaFields=[f for f in FIELDS if f != "VersionId"]),
        "versions": flt(includeBlobVersions=False),
        "block": flt(blobTypes=["appendBlob"]),
        "prefixes": flt(prefixMatch=["data/raw"]),
        "excludes": flt(excludePrefix=["data/logs"]),
        "excludes ": flt(excludePrefix=["dat"]),
        # Only blobs created in the last 3 days: older ones would read as
        # missing (review on #35).
        "recent only": flt(creationTime=3),
        "files": {k: v for k, v in base.items() if k != "files"},
    }
    for why, m in cases.items():
        with pytest.raises(InventoryError):
            check_manifest(m, "data", versioned=True)
        assert why
    # Fine: all containers, or a container-name prefix; an unversioned account.
    check_manifest(flt(prefixMatch=[]), "data", versioned=True)
    check_manifest(flt(prefixMatch=["other", "da"]), "data", versioned=True)
    check_manifest(flt(excludePrefix=["database", "other/x"]), "data", versioned=True)
    check_manifest(flt(includeBlobVersions=False), "data", versioned=False)


def test_the_newest_good_run_is_chosen_and_bad_ones_are_skipped(db, connect) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(1)
    w = seed(db, clock)
    now = clock.now()

    def pick(at: datetime, **kw: Any):  # type: ignore[no-untyped-def]
        return run(latest_report(w.sim, destination=DEST, rule=RULE, container=w.container,
                                 versioned=True, now=at, **kw))  # fmt: skip

    with pytest.raises(InventoryError, match="no completed run"):
        pick(now)
    yesterday = write_report(w, [parquet_bytes([])], started=now - 30 * H, fmt="parquet",
                             checksum="base64")  # fmt: skip
    got = pick(now)
    assert got.manifest_blob == yesterday and got.fmt == "parquet"
    # Newer runs that failed, or whose manifest doesn't match its checksum,
    # are passed over.
    write_report(w, [csv_bytes([])], started=now - 3 * H, status="Failed")
    write_report(w, [csv_bytes([])], started=now - 2 * H, checksum="bad")
    assert pick(now).manifest_blob == yesterday
    today = write_report(w, [csv_bytes([])], started=now - 5 * H)
    got = pick(now)
    assert got.manifest_blob == today and got.fmt == "csv" and got.versions
    assert got.started_at == now - 5 * H
    # Too old: nothing within its max age.
    with pytest.raises(InventoryError):
        pick(now + 3 * 24 * H)
    with pytest.raises(InventoryError, match="older than"):
        pick(now + 2 * H, max_age=H)


def test_without_a_usable_report_reconcile_lists(db, connect, caplog) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(1)
    w = seed(db, clock)
    configure(db, w)
    with caplog.at_level(logging.WARNING):
        got = run(choose_inventory(w.sim, w.container, {"inventory": {"container": "inventory",
                                                                       "rule": RULE}},
                                   clock.now()))  # fmt: skip
    assert isinstance(got, ListingInventory) and "listing instead" in caplog.text
    write_report(w, [csv_bytes([])], started=clock.now() - H)
    got = run(choose_inventory(w.sim, w.container,
                               {"inventory": {"container": "inventory", "rule": RULE}},
                               clock.now()))  # fmt: skip
    assert isinstance(got, BlobInventory)
    # Not configured: a listing, as before.
    assert isinstance(run(choose_inventory(w.sim, w.container, {}, clock.now())),
                      ListingInventory)  # fmt: skip


def test_a_run_that_fell_back_to_a_listing_records_it_and_why(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Inventory configured, no usable report: the run is recorded as a
    listing, with the reason, not as an inventory run (migration 0034)."""
    clock = clock_days_ago(1)
    w = seed(db, clock)
    configure(db, w)
    commit_content(db, w, add_file(db, w, "a"), b"a", clock.now())
    clock.advance(H)
    write_report(w, [csv_bytes([])], started=clock.now(), status="PartiallySucceeded")
    clock.advance(H)
    report = _reconcile(connect, w, clock)
    assert report.method == "listing"
    assert report.method_detail is not None
    assert report.method_detail.startswith("no usable Blob Inventory report:")
    assert "PartiallySucceeded" in report.method_detail
    assert db.execute(
        "SELECT method, method_detail, state FROM catalog.reconcile_run WHERE run_id = %s",
        (report.run_id,),
    ).fetchone() == ("listing", report.method_detail, "completed")
    assert report.objects_seen == 1  # it did list


def test_runs_of_worm_and_source_containers_say_how_they_read(db, connect) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    clock.advance(H)
    report = _reconcile(connect, w, clock)
    assert db.execute(
        "SELECT method FROM catalog.reconcile_run WHERE run_id = %s", (report.run_id,)
    ).fetchone() == ("listing_changefeed",)
    assert report.method == "listing_changefeed"


def test_a_worm_account_never_uses_an_inventory(db, caplog) -> None:  # type: ignore[no-untyped-def]
    """Version-level immutability blocks Blob Inventory on the account: it
    reconciles from the change feed and listings."""
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    with caplog.at_level(logging.WARNING):
        got = run(choose_inventory(w.sim, w.container,
                                   {"inventory": {"container": "inventory", "rule": RULE}},
                                   clock.now()))  # fmt: skip
    assert isinstance(got, ListingInventory) and "WORM" in caplog.text


# --- reconciling from a report ---------------------------------------------------


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_reconcile_from_a_report_finds_the_same_drift_as_a_listing(db, connect, fmt) -> None:  # type: ignore[no-untyped-def]
    clock = clock_days_ago(2)
    w = seed(db, clock)
    configure(db, w)
    ok = commit_content(db, w, add_file(db, w, "ok"), b"fine", clock.now())
    gone = commit_content(db, w, add_file(db, w, "gone"), b"lost", clock.now())
    tier = commit_content(db, w, add_file(db, w, "tier"), b"moved", clock.now())
    size = commit_content(db, w, add_file(db, w, "size"), b"12345", clock.now(), catalog_size=9)
    clock.advance(H)
    run(w.sim.delete(gone.ref))
    run(w.sim.set_class(tier.ref, LogicalTier.COLD))
    run(w.sim.put(w.container, "stray/object", _one(b"?"), None, {}))
    rows = run(listed(w))
    # What else the report holds: another container, a snapshot, a soft-
    # deleted blob, and a duplicate row (Azure may repeat one).
    extra = [
        {**rows[0], "Name": "other/" + rows[0]["Name"].split("/", 1)[1]},
        {**rows[0], "Snapshot": "2026-09-01T00:00:00.0000000Z"},
        {**rows[0], "Name": "data/soft-deleted", "Deleted": True},
        dict(rows[1]),
    ]
    body = csv_bytes(rows + extra) if fmt == "csv" else parquet_bytes(rows + extra)
    clock.advance(H)
    write_report(w, [body], started=clock.now(), fmt=fmt)
    # Written after the report: a listing would see it; the report doesn't.
    run(w.sim.put(w.container, "after/report", _one(b"!"), None, {}))
    clock.advance(H)

    report = _reconcile(connect, w, clock)
    assert report.method == "inventory"
    assert report.method_detail is not None and report.method_detail.startswith(
        f"report {DEST}/"
    ) and report.method_detail.endswith(f"{RULE}-manifest.json")  # fmt: skip
    assert db.execute(
        "SELECT method, method_detail FROM catalog.reconcile_run WHERE run_id = %s",
        (report.run_id,),
    ).fetchone() == ("inventory", report.method_detail)
    assert report.objects_seen == len(rows) + 1  # the duplicate is loaded, then dropped
    assert _kinds(connect, w) == {
        gone.ref.key + ":missing": "reconcile",
        tier.ref.key + ":tier_mismatch": "reconcile",
        size.ref.key + ":size_mismatch": "reconcile",
        "stray/object:unexpected": "reconcile",
    }
    assert ok.ref.key not in {k.split(":")[0] for k in _kinds(connect, w)}
    assert len(report.drift_open) == 4  # the duplicate didn't double a row


def test_a_report_is_not_believed_about_what_changed_after_it(db, connect) -> None:  # type: ignore[no-untyped-def]
    """The report was taken before reconcile runs: a blob OmniFile deleted
    since, or a tier OmniFile changed since, is not drift. A delete before
    the report that left the blob is (a failed delete)."""
    clock = VirtualClock(datetime.now(UTC).replace(microsecond=0))
    w = seed(db, clock)
    configure(db, w)
    early = commit_content(db, w, add_file(db, w, "early"), b"a", clock.now())
    late = commit_content(db, w, add_file(db, w, "late"), b"b", clock.now())
    moved = commit_content(db, w, add_file(db, w, "moved"), b"c", clock.now())
    for s in (early,):
        db.execute("UPDATE catalog.location SET state = 'deleting', verified = false"
                   " WHERE location_id = %s",
                   (s.location_id,))  # fmt: skip
        db.execute("UPDATE catalog.location SET state = 'deleted' WHERE location_id = %s",
                   (s.location_id,))  # fmt: skip
    time.sleep(0.05)
    (taken,) = db.execute("SELECT now()").fetchone()
    time.sleep(0.05)
    db.execute("UPDATE catalog.location SET state = 'deleting', verified = false"
                   " WHERE location_id = %s",
               (late.location_id,))  # fmt: skip
    db.execute("UPDATE catalog.location SET state = 'deleted' WHERE location_id = %s",
               (late.location_id,))  # fmt: skip
    rows = run(listed(w))  # still lists all three: the blobs were never removed
    # The catalog moved one to Cool after the report; the report says Smart.
    db.execute(
        "UPDATE catalog.location SET storage_class = 'Cool', logical_tier = 'warm',"
        " tier_changed_at = %s WHERE location_id = %s",
        (taken + timedelta(seconds=1), moved.location_id),
    )
    write_report(w, [csv_bytes(rows)], started=taken)
    clock.advance(H)
    _reconcile(connect, w, clock)
    assert _kinds(connect, w) == {early.ref.key + ":unexpected": "reconcile"}


def _six(db, connect):  # type: ignore[no-untyped-def]
    """Six committed files, and the rows a whole report of them would hold."""
    clock = clock_days_ago(2)
    w = seed(db, clock)
    configure(db, w)
    for i in range(6):
        commit_content(db, w, add_file(db, w, f"f{i}"), b"x%d" % i, clock.now())
    clock.advance(H)
    return clock, w, run(listed(w))


def _listed_instead(db, connect, w, clock, why: str):  # type: ignore[no-untyped-def]
    """The run read no part of the report: it listed (and says why), and
    found no drift, where the report would have made some up."""
    clock.advance(H)
    report = _reconcile(connect, w, clock)
    assert report.method == "listing", report
    assert report.method_detail is not None and why in report.method_detail, report
    assert db.execute(
        "SELECT method, state FROM catalog.reconcile_run WHERE run_id = %s", (report.run_id,)
    ).fetchone() == ("listing", "completed")
    assert _kinds(connect, w) == {}
    return report


def test_a_report_file_cut_short_is_not_used(db, connect) -> None:  # type: ignore[no-untyped-def]
    """Review on #35: a CSV holding half its records (cut at a record
    boundary) while the manifest gives the whole file's size made three
    blobs look missing."""
    clock, w, rows = _six(db, connect)
    full, cut = csv_bytes(rows), csv_bytes(rows[: len(rows) // 2])
    write_report(w, [cut], started=clock.now(), sizes=[len(full)], object_count=len(rows) // 2)
    _listed_instead(db, connect, w, clock, "its manifest says")


def test_a_report_file_that_streams_short_is_not_used(db, connect) -> None:  # type: ignore[no-untyped-def]
    """The file's size is right, but the stream ends early: caught while
    reading, so the run starts again from a listing."""
    clock, w, rows = _six(db, connect)
    write_report(w, [csv_bytes(rows)], started=clock.now(), object_count=3)
    stop = len(csv_bytes(rows[:3]))  # a record boundary: every record read is whole
    real = w.sim.get_range

    async def short(ref, offset, length):  # type: ignore[no-untyped-def]
        sent = 0
        async for chunk in real(ref, offset, length):
            if ref.container == DEST and ref.key.endswith(".csv"):
                chunk = chunk[: max(0, stop - sent)]
                if not chunk:
                    return
            sent += len(chunk)
            yield chunk

    w.sim.get_range = short  # type: ignore[method-assign]
    _listed_instead(db, connect, w, clock, "read ")


def test_a_report_with_fewer_rows_than_its_summary_is_not_used(db, connect) -> None:  # type: ignore[no-untyped-def]
    clock, w, rows = _six(db, connect)
    write_report(w, [csv_bytes(rows[:3])], started=clock.now(), object_count=len(rows))
    _listed_instead(db, connect, w, clock, "its summary says")


@pytest.mark.parametrize("fmt", ["csv", "parquet"])
def test_a_report_file_without_a_field_it_needs_is_not_used(db, connect, fmt) -> None:  # type: ignore[no-untyped-def]
    """The manifest's schema names every field; the file itself lacks
    AccessTier (or, in CSV, VersionId). The file is believed, not the
    manifest."""
    clock, w, rows = _six(db, connect)
    if fmt == "csv":
        body = csv_bytes(rows, [f for f in FIELDS if f != "VersionId"])
        why = "its header has no VersionId"
    else:
        table = pq.read_table(io.BytesIO(parquet_bytes(rows))).drop_columns(["AccessTier"])
        buf = io.BytesIO()
        pq.write_table(table, buf)
        body, why = buf.getvalue(), "the file has no AccessTier column"
    write_report(w, [body], started=clock.now(), fmt=fmt)
    _listed_instead(db, connect, w, clock, why)


def test_a_report_with_a_record_of_another_shape_is_not_used(db, connect) -> None:  # type: ignore[no-untyped-def]
    clock, w, rows = _six(db, connect)
    body = csv_bytes(rows) + b"data/x,1,extra\n"
    write_report(w, [body], started=clock.now(), object_count=len(rows) + 1)
    _listed_instead(db, connect, w, clock, "fields")


def test_per_account_settings_and_the_default_tier(db, connect) -> None:  # type: ignore[no-untyped-def]
    """`accounts` overrides the rule per account; a blob on its account's
    default tier has no AccessTier and is read as that tier."""
    clock = clock_days_ago(1)
    w = seed(db, clock)
    commit_content(db, w, add_file(db, w, "a"), b"a", clock.now())
    clock.advance(H)
    rows = [{**r, "AccessTier": ""} for r in run(listed(w))]
    write_report(w, [csv_bytes(rows)], started=clock.now())
    clock.advance(H)
    cfg = {"inventory": {"container": "nowhere", "rule": "nope",
                         "accounts": {"live": {"container": "inventory", "rule": RULE,
                                               "default_tier": "Smart"}}}}  # fmt: skip
    got = run(choose_inventory(w.sim, w.container, cfg, clock.now()))
    assert isinstance(got, BlobInventory)

    async def items() -> list:
        return [i async for i in got.items(w.container)]

    assert {i.native_class for i in run(items())} == {"Smart"}
