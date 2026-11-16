"""Reading `$blobchangefeed` segments (Avro) through the SDK, no network."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import fastavro
import pytest
from azure_fakes import FakeAzure

from omnifile_service.backends.azure import AzureBlobBackend
from omnifile_service.backends.azure.changefeed import FeedCursor, map_event, parse_time
from omnifile_service.backends.base import ChangeKind, ObjectRef

T0 = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
FEED = "$blobchangefeed"

_STR_MAP = ["null", {"type": "map", "values": "string"}]
SCHEMA = fastavro.parse_schema(
    {
        "type": "record",
        "name": "BlobChangeEvent",
        "fields": [
            {"name": "schemaVersion", "type": "int"},
            {"name": "topic", "type": "string"},
            {"name": "subject", "type": "string"},
            {"name": "eventType", "type": "string"},
            {"name": "eventTime", "type": "string"},
            {"name": "id", "type": "string"},
            {
                "name": "data",
                "type": {
                    "type": "record",
                    "name": "Data",
                    "fields": [
                        {"name": "api", "type": "string"},
                        {"name": "etag", "type": ["null", "string"], "default": None},
                        {"name": "contentLength", "type": ["null", "long"], "default": None},
                        {"name": "blobVersion", "type": ["null", "string"], "default": None},
                        {"name": "blobTier", "type": ["null", "string"], "default": None},
                        {"name": "sequencer", "type": "string"},
                        {"name": "previousInfo", "type": _STR_MAP, "default": None},
                        {"name": "asyncOperationInfo", "type": _STR_MAP, "default": None},
                    ],
                },
            },
        ],
    }
)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


_seq = iter(range(1, 10**6))


def event(
    kind: str,
    blob: str,
    at: datetime,
    *,
    container: str = "data",
    tier: str | None = "Hot",
    version: str | None = None,
    previous: dict[str, str] | None = None,
    async_info: dict[str, str] | None = None,
    size: int | None = 10,
) -> dict[str, Any]:
    return {
        "schemaVersion": 6,
        "topic": "/subscriptions/s/resourceGroups/r/providers/Microsoft.Storage"
        "/storageAccounts/live",
        "subject": f"/blobServices/default/containers/{container}/blobs/{blob}",
        "eventType": kind,
        "eventTime": at.strftime("%Y-%m-%dT%H:%M:%S.%f") + "1Z",  # 7 digits, as Azure writes
        "id": f"id-{next(_seq)}",
        "data": {
            "api": "PutBlockList",
            "etag": "0x1",
            "contentLength": size,
            "blobVersion": version,
            "blobTier": tier,
            "sequencer": "0",
            "previousInfo": previous,
            "asyncOperationInfo": async_info,
        },
    }


def avro(records: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    fastavro.writer(buf, SCHEMA, records)
    return buf.getvalue()


def seg_name(t: datetime) -> str:
    return f"idx/segments/{t:%Y/%m/%d/%H%M}/meta.json"


def write_segment(
    fake: FakeAzure, t: datetime, shards: dict[str, list[list[dict[str, Any]]]], status: str
) -> None:
    paths = []
    for shard, chunks in shards.items():
        path = f"log/{shard}/{t:%Y/%m/%d/%H%M}/"
        paths.append(f"{FEED}/{path}")
        for i, recs in enumerate(chunks):
            fake.put_raw("live", FEED, f"{path}{i:05d}.avro", avro(recs))
    meta = {"version": 0, "begin": t.isoformat(), "status": status, "chunkFilePaths": paths}
    fake.put_raw("live", FEED, seg_name(t), json.dumps(meta).encode())


def set_last_consumable(fake: FakeAzure, t: datetime) -> None:
    fake.put_raw(
        "live", FEED, "meta/segments.json", json.dumps({"lastConsumable": t.isoformat()}).encode()
    )


@pytest.fixture
def feed(fake: FakeAzure) -> FakeAzure:
    fake.container("live", FEED)
    fake.put_raw("live", FEED, "idx/segments/1601/01/01/0000/meta.json", b"{}")
    return fake


def drain(b: AzureBlobBackend, container: str, cursor: str) -> tuple[list[Any], str]:
    out: list[Any] = []
    for _ in range(50):
        page = run(b.read_changes(container, cursor))
        out += page.records
        cursor = page.cursor
        if page.caught_up:
            return out, cursor
    raise AssertionError("never caught up")


def test_none_cursor_starts_from_now(two: AzureBlobBackend) -> None:
    page = run(two.read_changes("data", None))
    assert page.records == [] and page.caught_up
    assert FeedCursor.decode(page.cursor).after == two.clock.now()


def test_reads_segments_and_shards_in_per_blob_order(
    two: AzureBlobBackend, feed: FakeAzure
) -> None:
    s1, s2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)  # 12:00, 13:00
    a, b = "a.segy", "b.segy"
    write_segment(
        feed,
        s1,
        {
            "00": [
                [
                    event("BlobCreated", a, s1 + timedelta(minutes=1), version="v1"),
                    event("Control", a, s1 + timedelta(minutes=2)),
                ],
                [
                    event(
                        "BlobAsyncOperationInitiated",
                        a,
                        s1 + timedelta(minutes=5),
                        tier="Archive",
                        async_info={"DestinationTier": "Hot", "CopyId": "c1"},
                    ),
                    event(
                        "BlobTierChanged",
                        a,
                        s1 + timedelta(minutes=50),
                        tier="Hot",
                        previous={"PreviousTier": "Archive"},
                    ),
                ],
            ],
            "01": [
                [
                    event("BlobCreated", b, s1 + timedelta(minutes=3)),
                    event("BlobCreated", "x", s1 + timedelta(minutes=4), container="other"),
                    event(
                        "BlobTierChanged",
                        b,
                        s1 + timedelta(minutes=6),
                        tier="Cool",
                        previous={"PreviousTier": "Hot"},
                    ),
                    event("BlobDeleted", b, s1 + timedelta(minutes=7)),
                ]
            ],
        },
        "Finalized",
    )
    write_segment(
        feed, s2, {"00": [[event("BlobCreated", "c", s2 + timedelta(minutes=1))]]}, "Publishing"
    )
    set_last_consumable(feed, s2)
    two.config = replace(two.config, feed_page_size=3)
    start = FeedCursor(after=T0 + timedelta(minutes=30)).encode()
    records, cursor = drain(two, "data", start)
    got = [(r.kind, r.ref.key) for r in records]
    assert got == [
        (ChangeKind.CREATED, a),
        (ChangeKind.CREATED, b),
        (ChangeKind.RESTORE_STARTED, a),
        (ChangeKind.TIER_CHANGED, b),
        (ChangeKind.DELETED, b),
        (ChangeKind.RESTORE_COMPLETED, a),
        (ChangeKind.CREATED, "c"),
    ]
    assert records[0].ref == ObjectRef(7, "live/data", a, "v1")
    assert records[0].detail["event_type"] == "BlobCreated"
    assert records[2].detail["copy_id"] == "c1"
    assert records[3].native_class == "Cool" and records[3].detail["previous_tier"] == "Hot"
    times = [r.at for r in records]
    assert times == sorted(times)
    # Caught up: nothing new comes back, and the cursor stays put.
    again = run(two.read_changes("data", cursor))
    assert again.records == [] and again.caught_up
    # A record appended to the publishing segment's shard appears next.
    write_segment(
        feed,
        s2,
        {
            "00": [
                [
                    event("BlobCreated", "c", s2 + timedelta(minutes=1)),
                    event("BlobCreated", "d", s2 + timedelta(minutes=2)),
                ]
            ]
        },
        "Publishing",
    )
    more, _ = drain(two, "data", again.cursor)
    assert [(r.kind, r.ref.key) for r in more] == [(ChangeKind.CREATED, "d")]


def test_records_at_or_before_the_start_are_dropped(two: AzureBlobBackend, feed: FakeAzure) -> None:
    s1 = T0 + timedelta(hours=1)
    write_segment(
        feed,
        s1,
        {
            "00": [
                [
                    event("BlobCreated", "old", s1 + timedelta(minutes=1)),
                    event("BlobCreated", "new", s1 + timedelta(minutes=9)),
                ]
            ]
        },
        "Publishing",
    )
    set_last_consumable(feed, s1)
    start = FeedCursor(after=s1 + timedelta(minutes=5)).encode()
    records, _ = drain(two, "data", start)
    assert [r.ref.key for r in records] == ["new"]


def test_segments_after_last_consumable_are_not_read(
    two: AzureBlobBackend, feed: FakeAzure
) -> None:
    s1, s2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    write_segment(feed, s1, {"00": [[event("BlobCreated", "one", s1)]]}, "Finalized")
    write_segment(feed, s2, {"00": [[event("BlobCreated", "two", s2)]]}, "Publishing")
    set_last_consumable(feed, s1)
    records, _ = drain(two, "data", FeedCursor(after=T0).encode())
    assert [r.ref.key for r in records] == ["one"]


def test_no_change_feed_yet_is_an_empty_page(two: AzureBlobBackend, feed: FakeAzure) -> None:
    cursor = FeedCursor(after=T0).encode()
    page = run(two.read_changes("data", cursor))
    assert page.records == [] and page.caught_up and page.cursor == cursor


def test_bad_cursor_is_refused(two: AzureBlobBackend) -> None:
    with pytest.raises(Exception, match="cursor"):
        run(two.read_changes("data", "not-a-cursor"))


def test_mapping_and_time_parsing() -> None:
    assert parse_time("2022-02-17T12:59:41.4003102Z") == datetime(
        2022, 2, 17, 12, 59, 41, 400310, tzinfo=UTC
    )
    rec = event("BlobPropertiesUpdated", "a", T0)
    assert map_event(rec, 1, "live", None) is None
    created = event("BlobCreated", "a/b c", T0, previous={"LastVersion": "v0"})
    mapped = map_event(created, 1, "live", "data")
    assert mapped is not None and mapped.detail["overwrite"] == "true"
    assert mapped.ref.key == "a/b c"
    assert map_event(created, 1, "live", "elsewhere") is None


def test_rehydrate_copy_target_creation_is_a_restore_start() -> None:
    rec = event(
        "BlobCreated",
        "data/a.segy@v1",
        T0,
        container="recall",
        tier="Archive",
        async_info={"DestinationTier": "Hot", "WasAsyncOperation": "true", "CopyId": "c9"},
    )
    mapped = map_event(rec, 1, "arch", "recall")
    assert mapped is not None and mapped.kind is ChangeKind.RESTORE_STARTED
    assert mapped.detail["copy_id"] == "c9"
