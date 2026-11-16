"""Read the Azure Blob change feed (`$blobchangefeed`) as plain blobs.

Why not `azure-storage-blob-changefeed`: it is still a beta (12.0.0b5, April
2024) with no release since, and it is sync only. The format is documented
and small, so the service reads it itself over the same private endpoint as
every other blob call (Event Grid can't push into a private network [A]).

Layout, per Microsoft's change-feed page (updated 2026-08-25):

- ``meta/segments.json``: ``lastConsumable``. Segments dated after it must
  not be consumed.
- ``idx/segments/YYYY/MM/DD/hhmm/meta.json``: one hourly segment, with
  ``status`` (``Publishing`` or ``Finalized``) and ``chunkFilePaths``, one
  path per shard (``$blobchangefeed/log/<shard>/YYYY/MM/DD/hhmm/``).
  ``idx/segments/1601/...`` is an empty initialisation file; it is skipped.
- Under each shard path, append blobs ``00000.avro``, ``00001.avro``, ...
  holding Avro records (schema v1-v6).

Ordering: records are ordered **per blob**; across blobs the order is
undefined, and a shard holds a disjoint set of blobs. All records of a segment
come before those of later segments. Records appear "within a few minutes".
This reader merges shards by `eventTime` within a page, so a page is roughly
time ordered and strictly ordered per blob, which is what catalog sync and
recall status need.

The cursor is opaque base64 JSON: the segment being read, and per shard the
chunk file and how many records of it were consumed. A ``None`` cursor means
"from now": the reader starts at the segment an hour before now (segment
times are approximate within 15 minutes) and drops records not newer than
now.

Mapping to `ChangeKind` (schema v4+). Microsoft's rehydrate page (updated
2026-08-31) says a copy-rehydrate emits BlobCreated (tier Archive) at copy
start and BlobTierChanged (Archive -> online) at completion; the exact
change-feed fields for those records are UNVERIFIED against real Azure (check
them in pilot 4 / the real-Azure contract lane). Every record keeps the raw
``eventType`` in ``detail["event_type"]``.

| eventType | becomes |
|---|---|
| BlobCreated, tier Archive, async copy to online | RESTORE_STARTED (copy target) |
| BlobCreated otherwise | CREATED |
| BlobDeleted | DELETED |
| BlobTierChanged from Archive to an online tier | RESTORE_COMPLETED |
| BlobTierChanged otherwise | TIER_CHANGED |
| BlobAsyncOperationInitiated to an online tier | RESTORE_STARTED |
| BlobAsyncOperationInitiated otherwise | TIER_CHANGED |
| Control, BlobPropertiesUpdated, BlobSnapshotCreated, RestorePointMarkerCreated | skipped |

Cost note: each poll re-downloads the shard's current chunk and skips the
records already consumed. Chunks are small append blobs, so this is fine for
v0; byte-offset resumption (Avro block offsets) is an M-later optimisation.
"""

from __future__ import annotations

import base64
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import fastavro
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob.aio import ContainerClient

from ..base import ChangeKind, ChangePage, ChangeRecord, ObjectRef
from .capabilities import AZ_ARCHIVE, ONLINE_TIERS

FEED_CONTAINER = "$blobchangefeed"
SEGMENT_PREFIX = "idx/segments/"
_SEGMENT_RE = re.compile(r"^idx/segments/(\d{4})/(\d{2})/(\d{2})/(\d{2})(\d{2})/meta\.json$")
_SUBJECT_RE = re.compile(r"^/blobServices/default/containers/([^/]+)/blobs/(.+)$")
SKIPPED_EVENTS = frozenset(
    {"Control", "BlobPropertiesUpdated", "BlobSnapshotCreated", "RestorePointMarkerCreated"}
)


def parse_time(raw: str | datetime) -> datetime:
    """Azure times carry 7 fractional digits; Python takes at most 6."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    s = raw.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?\.\d{1,6})\d*(.*)$", s)
    if m:
        s = m[1] + m[2]
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def segment_time(name: str) -> datetime | None:
    m = _SEGMENT_RE.match(name)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    if y == 1601:
        return None
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@dataclass
class ShardPos:
    chunk: str | None = None  # blob name inside $blobchangefeed, e.g. log/00/.../00000.avro
    n: int = 0  # records of `chunk` already consumed


@dataclass
class FeedCursor:
    segment: str | None = None  # segment meta.json name
    after: datetime | None = None  # drop records at or before this time
    shards: dict[str, ShardPos] = field(default_factory=dict)

    def encode(self) -> str:
        doc = {
            "v": 1,
            "seg": self.segment,
            "after": self.after.isoformat() if self.after else None,
            "shards": {k: [p.chunk, p.n] for k, p in sorted(self.shards.items())},
        }
        raw = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> FeedCursor:
        pad = "=" * (-len(token) % 4)
        try:
            doc = json.loads(base64.urlsafe_b64decode(token + pad))
            if doc.get("v") != 1:
                raise ValueError("unknown cursor version")
            return cls(
                segment=doc["seg"],
                after=datetime.fromisoformat(doc["after"]) if doc["after"] else None,
                shards={k: ShardPos(v[0], int(v[1])) for k, v in doc["shards"].items()},
            )
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"bad change-feed cursor: {e}") from None


def map_event(
    rec: Mapping[str, Any], backend_id: int, account: str, container: str | None
) -> ChangeRecord | None:
    """One Avro record -> a `ChangeRecord`, or None when skipped or filtered."""
    event = rec.get("eventType")
    if not event or event in SKIPPED_EVENTS:
        return None
    m = _SUBJECT_RE.match(rec.get("subject") or "")
    if not m:
        return None
    rec_container, blob = m[1], m[2]
    if container is not None and rec_container != container:
        return None
    data: Mapping[str, Any] = rec.get("data") or {}
    tier = data.get("blobTier")
    previous: Mapping[str, Any] = data.get("previousInfo") or {}
    async_info: Mapping[str, Any] = data.get("asyncOperationInfo") or {}
    dest_tier = async_info.get("DestinationTier")
    if event == "BlobCreated" and tier == AZ_ARCHIVE and dest_tier in ONLINE_TIERS:
        # A rehydrate copy's target appears at copy start, still Archive.
        kind = ChangeKind.RESTORE_STARTED
    elif event == "BlobCreated":
        kind = ChangeKind.CREATED
    elif event == "BlobDeleted":
        kind = ChangeKind.DELETED
    elif event == "BlobTierChanged":
        if previous.get("PreviousTier") == AZ_ARCHIVE and tier in ONLINE_TIERS:
            kind = ChangeKind.RESTORE_COMPLETED
        else:
            kind = ChangeKind.TIER_CHANGED
    elif event == "BlobAsyncOperationInitiated":
        kind = ChangeKind.RESTORE_STARTED if dest_tier in ONLINE_TIERS else ChangeKind.TIER_CHANGED
    else:
        return None
    detail = {"event_type": str(event)}
    for key, value in (
        ("api", data.get("api")),
        ("etag", data.get("etag")),
        ("previous_tier", previous.get("PreviousTier")),
        ("destination_tier", dest_tier),
        ("copy_id", async_info.get("CopyId")),
        ("sequencer", data.get("sequencer")),
        ("id", rec.get("id")),
    ):
        if value:
            detail[key] = str(value)
    if kind is ChangeKind.CREATED and previous.get("LastVersion"):
        detail["overwrite"] = "true"
    size = data.get("contentLength")
    return ChangeRecord(
        kind=kind,
        ref=ObjectRef(backend_id, f"{account}/{rec_container}", blob, data.get("blobVersion")),
        at=parse_time(rec["eventTime"]),
        native_class=str(tier) if tier else None,
        size=int(size) if size is not None else None,
        detail=detail,
    )


def read_avro(raw: bytes) -> list[dict[str, Any]]:
    """Records of one chunk file. A chunk still being appended may end in a
    partial block; what precedes it is complete and returned."""
    out: list[dict[str, Any]] = []
    try:
        for rec in fastavro.reader(io.BytesIO(raw)):
            out.append(rec)  # type: ignore[arg-type]
    except (EOFError, ValueError, StopIteration, OSError):
        pass
    return out


@dataclass
class _Candidate:
    shard: str
    chunk: str
    index_after: int  # the shard position (records of `chunk` consumed) after this record
    at: datetime
    raw: dict[str, Any]


class ChangeFeedReader:
    """Pages through one account's change feed."""

    def __init__(
        self, feed: ContainerClient, *, account: str, backend_id: int, page_size: int
    ) -> None:
        self._feed = feed
        self._account = account
        self._backend_id = backend_id
        self._page_size = page_size

    async def _json(self, name: str) -> dict[str, Any] | None:
        try:
            stream = await self._feed.download_blob(name)
            return json.loads(await stream.readall())
        except ResourceNotFoundError:
            return None

    async def _segments(self, from_year: int, to_year: int) -> list[tuple[datetime, str]]:
        found: list[tuple[datetime, str]] = []
        for year in range(from_year, to_year + 1):
            async for b in self._feed.list_blobs(name_starts_with=f"{SEGMENT_PREFIX}{year:04d}/"):
                t = segment_time(b.name)
                if t is not None:
                    found.append((t, b.name))
        found.sort()
        return found

    async def _chunks(self, shard: str) -> list[str]:
        prefix = shard.removeprefix(f"{FEED_CONTAINER}/")
        names = [b.name async for b in self._feed.list_blobs(name_starts_with=prefix)]
        return sorted(n for n in names if n.endswith(".avro"))

    async def _records(self, chunk: str) -> list[dict[str, Any]]:
        stream = await self._feed.download_blob(chunk)
        return read_avro(await stream.readall())

    async def read(self, container: str | None, cursor: str | None, now: datetime) -> ChangePage:
        if cursor is None:
            start = FeedCursor(after=now)
            return ChangePage([], start.encode(), caught_up=True)
        pos = FeedCursor.decode(cursor)
        seg_index = await self._json("meta/segments.json")
        if not seg_index or not seg_index.get("lastConsumable"):
            return ChangePage([], cursor, caught_up=True)
        last = parse_time(seg_index["lastConsumable"])
        first_year = (
            segment_time(pos.segment).year  # type: ignore[union-attr]
            if pos.segment and segment_time(pos.segment)
            else (pos.after or now).year
        )
        segments = [s for s in await self._segments(first_year, last.year) if s[0] <= last]
        if pos.segment is None:
            floor = (pos.after or now).replace(minute=0, second=0, microsecond=0)
            floor -= timedelta(hours=1)
            segments = [s for s in segments if s[0] >= floor]
        else:
            current = segment_time(pos.segment)
            segments = [s for s in segments if current is not None and s[0] >= current]
        records: list[ChangeRecord] = []
        budget = self._page_size
        caught_up = True
        for i, (_, seg_name) in enumerate(segments):
            if seg_name != pos.segment:
                pos.segment, pos.shards = seg_name, {}
            meta = await self._json(seg_name) or {}
            shards: Sequence[str] = meta.get("chunkFilePaths") or []
            taken, exhausted = await self._read_segment(pos, shards, budget, container, records)
            budget -= taken
            if not exhausted:
                caught_up = False
                break
            has_next = i + 1 < len(segments)
            if meta.get("status") != "Finalized" or not has_next:
                # Publishing: more may still arrive here; stay on it.
                break
            if budget <= 0:
                # Next call starts the next segment.
                pos.segment, pos.shards = segments[i + 1][1], {}
                caught_up = False
                break
        return ChangePage(records, pos.encode(), caught_up=caught_up)

    async def _read_segment(
        self,
        pos: FeedCursor,
        shards: Sequence[str],
        budget: int,
        container: str | None,
        out: list[ChangeRecord],
    ) -> tuple[int, bool]:
        """Read up to `budget` records across the segment's shards, merged by
        time. Returns (records consumed, whether every shard is exhausted)."""
        if budget <= 0:
            return 0, False
        candidates: list[_Candidate] = []
        exhausted = True
        for shard in shards:
            sp = pos.shards.setdefault(shard, ShardPos())
            chunks = await self._chunks(shard)
            if sp.chunk is not None and sp.chunk in chunks:
                chunks = chunks[chunks.index(sp.chunk) :]
            got = 0
            for chunk in chunks:
                skip = sp.n if chunk == sp.chunk else 0
                recs = await self._records(chunk)
                for idx in range(skip, len(recs)):
                    if got >= budget:
                        break
                    raw = recs[idx]
                    at = parse_time(raw.get("eventTime") or datetime.min.replace(tzinfo=UTC))
                    candidates.append(_Candidate(shard, chunk, idx + 1, at, raw))
                    got += 1
                if got >= budget:
                    exhausted = False
                    break
        candidates.sort(key=lambda c: c.at)  # stable: per-shard order is kept
        chosen = candidates[:budget]
        if len(chosen) < len(candidates):
            exhausted = False
        for c in chosen:
            sp = pos.shards[c.shard]
            sp.chunk, sp.n = c.chunk, c.index_after
            if pos.after is not None and c.at <= pos.after:
                continue
            rec = map_event(c.raw, self._backend_id, self._account, container)
            if rec is not None:
                out.append(rec)
        return len(chosen), exhausted
