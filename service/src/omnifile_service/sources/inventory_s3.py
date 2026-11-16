"""S3 Inventory (CSV) as a seed source: the native listing S3 writes daily or
weekly, so seeding never LISTs a large bucket (CLAUDE.md, native features
first).

An inventory is a ``manifest.json`` next to a ``manifest.checksum`` (the
manifest's MD5), naming gzip CSV files [V]:

    {"sourceBucket": "b", "creationTimestamp": "1514944800000",
     "fileFormat": "CSV",
     "fileSchema": "Bucket, Key, VersionId, IsLatest, IsDeleteMarker, Size,
                    LastModifiedDate, ETag, StorageClass, ...",
     "files": [{"key": ".../data/x.csv.gz", "size": 123, "MD5checksum": "..."}]}

Each CSV row is one object version, its columns in `fileSchema` order, with
no header. Keys are URL-encoded in CSV inventories.

VERIFY on real AWS (docs/milestones/real-aws-checks.md): that keys are
form-encoded (a space as ``+``), as S3 event notifications are; and the
spelling of `IntelligentTieringAccessTier` values.

Parquet and ORC inventories are refused (the setup guide asks for CSV).
pyarrow is a dependency now (sources/metadata_s3.py), so Parquet is a small
addition if a customer needs it.

**Open streams.** A gzip stream can't be entered in the middle, so a batch
that stops inside a data file leaves the file's stream open in this process
(`_streams`), and the next batch of the same source continues it. Only a
batch that lands elsewhere (a worker restart, another replica, a rolled-back
batch) decompresses the file again up to its line, once.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import time
import zlib
from collections import OrderedDict
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote_plus

from ..backends.base import LogicalTier, ObjectRef, StorageBackend
from ..backends.s3.capabilities import ARCHIVED_CLASSES, NATIVE_TO_LOGICAL
from .model import Batch, SeedObject, SourceError

WHOLE = 1 << 62  # "to the end" for get_range
#: Intelligent-Tiering access tiers that need a restore before reading.
IT_ARCHIVED = frozenset({"ARCHIVE", "ARCHIVE_ACCESS", "DEEP_ARCHIVE", "DEEP_ARCHIVE_ACCESS"})
#: EncryptionStatus values under which a single-part ETag is the MD5 [V].
MD5_ETAG_ENCRYPTION = frozenset({"NOT-SSE", "SSE-S3"})
REQUIRED = ("Key", "Size", "LastModifiedDate")
#: Open data-file streams kept between batches, and for how long.
STREAMS_MAX = 8
STREAM_IDLE_S = 300.0


@dataclass
class _Stream:
    line: int  # lines consumed
    lines: AsyncGenerator[str, None]
    loop: asyncio.AbstractEventLoop
    at: float


#: (backend, source bucket, source prefix, data file key) -> its open stream.
_streams: OrderedDict[tuple[int, str, str, str], _Stream] = OrderedDict()


async def _close(s: _Stream) -> None:
    if s.loop is asyncio.get_running_loop():
        await s.lines.aclose()


async def close_streams() -> None:
    """Close every kept stream (tests)."""
    while _streams:
        await _close(_streams.popitem()[1])


def logical_tier(native: str, it_tier: str | None) -> LogicalTier:
    if native == "INTELLIGENT_TIERING" and it_tier in IT_ARCHIVED:
        return LogicalTier.ARCHIVE
    return NATIVE_TO_LOGICAL.get(native, LogicalTier.HOT)


def _parse_time(raw: str) -> datetime:
    t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class S3InventoryReader:
    def __init__(
        self,
        backend: StorageBackend,
        bucket: str,
        manifest_key: str,
        *,
        source_bucket: str,
        source_prefix: str = "",
    ) -> None:
        self.backend = backend
        self.bucket = bucket
        self.manifest_key = manifest_key
        self.source_bucket = source_bucket
        self.source_prefix = source_prefix
        self._manifest: Mapping[str, Any] | None = None

    def _ref(self, key: str) -> ObjectRef:
        return ObjectRef(self.backend.backend_id, self.bucket, key)

    async def _read_all(self, key: str) -> bytes:
        return b"".join([c async for c in self.backend.get_range(self._ref(key), 0, WHOLE)])

    async def manifest(self) -> Mapping[str, Any]:
        if self._manifest is not None:
            return self._manifest
        raw = await self._read_all(self.manifest_key)
        if self.manifest_key.endswith("manifest.json"):
            checksum_key = self.manifest_key[: -len("manifest.json")] + "manifest.checksum"
            want = (await self._read_all(checksum_key)).decode().strip().lower()
            if want != hashlib.md5(raw).hexdigest():
                raise SourceError(f"{self.manifest_key}: the MD5 doesn't match manifest.checksum")
        try:
            m = json.loads(raw)
        except ValueError as e:
            raise SourceError(f"{self.manifest_key} is not JSON: {e}") from None
        fmt = str(m.get("fileFormat", "")).upper()
        if fmt != "CSV":
            raise SourceError(
                f"{self.manifest_key}: {fmt or 'unknown'} inventories aren't supported; configure"
                " the S3 Inventory as CSV (setup guide)"
            )
        if m.get("sourceBucket") not in (None, self.source_bucket):
            raise SourceError(
                f"{self.manifest_key} is an inventory of {m.get('sourceBucket')!r},"
                f" not {self.source_bucket!r}"
            )
        columns = [c.strip() for c in str(m.get("fileSchema", "")).split(",")]
        missing = [c for c in REQUIRED if c not in columns]
        if missing:
            raise SourceError(f"{self.manifest_key}: the inventory has no {', '.join(missing)}")
        if not isinstance(m.get("files"), list):
            raise SourceError(f"{self.manifest_key}: no files")
        self._manifest = {**m, "_columns": columns}
        return self._manifest

    async def snapshot_at(self) -> datetime:
        m = await self.manifest()
        try:
            return datetime.fromtimestamp(int(m["creationTimestamp"]) / 1000, UTC)
        except (KeyError, TypeError, ValueError):
            raise SourceError(f"{self.manifest_key}: no creationTimestamp") from None

    async def _lines(self, key: str) -> AsyncGenerator[str, None]:
        """The decompressed lines of one gzip CSV file, streamed. gzip's own
        CRC32 catches a corrupt file at its end."""
        z = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        tail = b""
        async with aclosing(self.backend.get_range(self._ref(key), 0, WHOLE)) as chunks:
            async for chunk in chunks:
                try:
                    data = tail + z.decompress(chunk)
                except zlib.error as e:
                    raise SourceError(f"{key}: not gzip: {e}") from None
                *lines, tail = data.split(b"\n")
                for line in lines:
                    yield line.decode()
        try:
            rest = tail + z.flush()
        except zlib.error as e:
            raise SourceError(f"{key}: truncated gzip: {e}") from None
        if not z.eof:
            raise SourceError(f"{key}: truncated gzip")
        for line in rest.split(b"\n"):
            if line:
                yield line.decode()

    def _object(self, row: dict[str, str]) -> SeedObject | None:
        """A current object, or None for an old version or a delete marker."""
        if row.get("IsLatest", "true").lower() != "true":
            return None
        if row.get("IsDeleteMarker", "false").lower() == "true":
            return None
        native = row.get("StorageClass") or "STANDARD"
        etag = (row.get("ETag") or "").strip('"') or None
        enc = row.get("EncryptionStatus")
        md5 = bool(etag and len(etag) == 32 and "-" not in etag and enc in MD5_ETAG_ENCRYPTION)
        return SeedObject(
            key=unquote_plus(row["Key"]),
            version_id=(row.get("VersionId") or None) if row.get("VersionId") != "null" else None,
            size=int(row["Size"] or 0),
            last_modified=_parse_time(row["LastModifiedDate"]),
            native_class=native,
            logical_tier=logical_tier(native, row.get("IntelligentTieringAccessTier")),
            etag=etag,
            etag_is_md5=md5,
        )

    def _stream_key(self, key: str) -> tuple[int, str, str, str]:
        return (self.backend.backend_id, self.source_bucket, self.source_prefix, key)

    async def _open(self, key: str, line: int) -> _Stream:
        """The data file's stream at `line`: the one the previous batch left
        open if it stopped there, else a new one from the start (the caller
        skips to `line`)."""
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        for k in [k for k, s in _streams.items() if now - s.at > STREAM_IDLE_S]:
            await _close(_streams.pop(k))
        kept = _streams.pop(self._stream_key(key), None)
        if kept is not None:
            if kept.line == line and kept.loop is loop:
                return kept
            await _close(kept)
        return _Stream(0, self._lines(key), loop, now)

    async def _keep(self, key: str, s: _Stream) -> None:
        s.at = time.monotonic()
        _streams[self._stream_key(key)] = s
        while len(_streams) > STREAMS_MAX:
            await _close(_streams.popitem(last=False)[1])

    async def read(self, resume: Mapping[str, Any] | None, limit: int) -> Batch:
        m = await self.manifest()
        columns: list[str] = m["_columns"]
        files = m["files"]
        index = int((resume or {}).get("file", 0))
        skip = int((resume or {}).get("line", 0))
        out: list[SeedObject] = []
        rows = 0
        while index < len(files):
            key = files[index]["key"]
            s = await self._open(key, skip)
            kept = False
            try:
                async for line in s.lines:
                    s.line += 1
                    line_no = s.line
                    if line_no <= skip or not line:
                        continue
                    values = next(csv.reader([line]))
                    if len(values) != len(columns):
                        raise SourceError(
                            f"{key} line {line_no}: {len(values)} fields,"
                            f" the schema has {len(columns)}"
                        )
                    rows += 1
                    try:
                        obj = self._object(dict(zip(columns, values, strict=True)))
                    except (KeyError, ValueError) as e:
                        raise SourceError(f"{key} line {line_no}: {e}") from None
                    if obj is not None:
                        out.append(obj)
                    if len(out) >= limit:
                        await self._keep(key, s)
                        kept = True
                        return Batch(out, {"file": index, "line": line_no}, rows)
            finally:
                if not kept:
                    await s.lines.aclose()
            index, skip = index + 1, 0
        return Batch(out, None, rows)


__all__ = ["ARCHIVED_CLASSES", "S3InventoryReader", "close_streams", "logical_tier"]
