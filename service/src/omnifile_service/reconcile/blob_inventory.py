"""Azure Blob Inventory reports as reconcile's inventory (native features
first: one blob read per report file instead of a LIST per 5,000 blobs).

Azure writes each rule's run into the rule's destination container, in the
same account [D]:

    YYYY/MM/DD/HH-MM-SS/<rule>/<rule>-manifest.json
    YYYY/MM/DD/HH-MM-SS/<rule>/<rule>-manifest.checksum   (written last)
    YYYY/MM/DD/HH-MM-SS/<rule>/<report files>               (CSV or Parquet)

The manifest names the report files (``files[].blob``, ``files[].size``),
the run's ``status``, start and completion times, and the rule definition
(``format``, ``schemaFields``, ``filters``). The checksum file is the MD5 of
the manifest's bytes, and marks the run complete [D].

- **CSV:** the first row is the schema row [D]; RFC 4180 quoting. Read as a
  stream of chunks: only complete records are parsed, a record may span
  chunks (a quoted name with a comma or a newline).
- **Parquet:** dates are ``timestamp_millis`` [D]. Read with pyarrow through
  a file object that fetches byte ranges on demand, row group by row group,
  so a report is never held in memory whole.
- ``Name`` is ``<container>/<blob>`` [D] (Microsoft's own example query splits
  the container off it): rows of other containers are skipped.
- Snapshots and soft-deleted blobs are not stored versions to reconcile and
  are skipped. Rows may repeat [D]; reconcile drops duplicates.

A report is used only when it is complete for the container: the newest run
whose checksum matches its manifest, ``status`` ``Succeeded``, at most
`max_age` old, a blob rule over block blobs with no prefix filter that
leaves part of the container out, ``Name``, ``Content-Length`` and
``AccessTier`` in its schema, and on a versioned account ``includeBlobVersions``
with ``VersionId`` and ``IsCurrentVersion``. Otherwise `latest_report` says
why and reconcile lists the container instead.

Accounts with version-level immutability can't run Blob Inventory [D][W];
they keep the change feed and listings (`reconcile/run.py`).

VERIFY on real Azure (pilot 4): CSV value formats (timestamps, booleans,
empty fields), whether ``Name`` is ever URL-encoded, the checksum file's
encoding (hex or base64 are both accepted), the ``AccessTier`` of a blob on
its account's default tier (empty is read as the default), and the Parquet
column names.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import csv
import hashlib
import io
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..backends.base import ObjectNotFound, ObjectRef, StorageBackend
from .inventory import InventoryItem

log = logging.getLogger(__name__)

WHOLE = 1 << 62
#: Days of runs `latest_report` looks back over (a daily rule makes one a day).
LOOKBACK_DAYS = 8
#: Bytes fetched per range read for Parquet (pyarrow asks for many small ranges).
READ_AHEAD = 4 << 20
_RUN = re.compile(r"^(\d{4})/(\d{2})/(\d{2})/(\d{2})-(\d{2})-(\d{2})/([^/]+)/")


class InventoryError(Exception):
    """A report that can't be used to reconcile this container (why)."""


@dataclass(frozen=True)
class ReportFile:
    blob: str
    size: int | None


@dataclass(frozen=True)
class Report:
    """One completed inventory run, checked."""

    destination: str  # "account/container" holding the report
    manifest_blob: str
    fmt: str  # "csv" | "parquet"
    files: Sequence[ReportFile]
    started_at: datetime
    completed_at: datetime
    versions: bool  # the rule lists every blob version
    #: The manifest's `summary.objectCount`: the rows its files hold, all
    #: together. None when the manifest gives none.
    object_count: int | None = None

    def required(self) -> set[str]:
        """The fields reconcile reads: each report file must have them."""
        return set(REQUIRED) | ({"VersionId", "IsCurrentVersion"} if self.versions else set())


#: Fields reconcile can't do without (the manifest's schema and each file's).
REQUIRED = ("Name", "Content-Length", "AccessTier")


def _time(raw: Any) -> datetime | None:
    """A report timestamp: ISO 8601 (up to 7 fractional digits, Z or an
    offset), epoch milliseconds, or a datetime (pyarrow)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw / 1000, UTC)
    s = str(raw).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, UTC)
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*?\.\d{6})\d+(.*)$", s)  # Python takes 6 fractional digits
    if m:
        s = m[1] + m[2]
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def _md5_matches(manifest: bytes, checksum: bytes) -> bool:
    want = checksum.decode("ascii", "replace").strip().strip('"')
    digest = hashlib.md5(manifest).digest()
    if want.lower() == digest.hex():
        return True
    try:
        return base64.b64decode(want, validate=True) == digest
    except ValueError:
        return False


def check_manifest(m: Mapping[str, Any], container: str, *, versioned: bool) -> None:
    """Raise `InventoryError` unless the run is complete for `container`
    (the short name, without the account)."""
    if m.get("status") != "Succeeded":
        raise InventoryError(f"the run's status is {m.get('status')!r}, not 'Succeeded'")
    rule = m.get("ruleDefinition") or {}
    if rule.get("objectType", "blob") != "blob":
        raise InventoryError("the rule lists containers, not blobs")
    fmt = str(rule.get("format", "")).lower()
    if fmt not in ("csv", "parquet"):
        raise InventoryError(f"unknown report format {rule.get('format')!r}")
    fields = set(rule.get("schemaFields") or [])
    need = set(REQUIRED)
    if versioned:
        need |= {"VersionId", "IsCurrentVersion"}
    if missing := sorted(need - fields):
        raise InventoryError(f"the rule's schema has no {', '.join(missing)}")
    filters = rule.get("filters") or {}
    if versioned and not filters.get("includeBlobVersions"):
        raise InventoryError("the account keeps versions and the rule leaves them out")
    types = filters.get("blobTypes")
    if types is not None and "blockBlob" not in types:
        raise InventoryError("the rule leaves block blobs out")
    if filters.get("creationTime") is not None:
        # Only blobs created in the last N days: every older one would read
        # as missing.
        raise InventoryError(f"the rule lists only blobs created in the last"
                             f" {filters['creationTime']} days")  # fmt: skip
    prefixes = filters.get("prefixMatch") or []
    # A prefix is a container name prefix, or a container and a blob prefix
    # [D]: only the first kind covers the whole container.
    if prefixes and not any("/" not in p and container.startswith(p) for p in prefixes):
        raise InventoryError(f"the rule's prefixes {prefixes} don't cover all of {container}")
    for e in filters.get("excludePrefix") or []:
        head, sep, _ = e.partition("/")
        if (not sep and container.startswith(e)) or (sep and head == container):
            raise InventoryError(f"the rule excludes {e!r}, all or part of {container}")
    if not isinstance(m.get("files"), list):
        raise InventoryError("the manifest names no files")


async def _read_all(backend: StorageBackend, ref: ObjectRef) -> bytes:
    return b"".join([c async for c in backend.get_range(ref, 0, WHOLE)])


async def latest_report(
    backend: StorageBackend,
    *,
    destination: str,
    rule: str,
    container: str,
    versioned: bool,
    now: datetime,
    max_age: timedelta = timedelta(hours=48),
) -> Report:
    """The newest usable run of `rule` for `container` ("account/container"),
    or `InventoryError` saying why there is none."""
    short = container.split("/", 1)[-1]
    why = "no completed run"
    for days in range(min(LOOKBACK_DAYS, max_age.days + 2)):
        day = now - timedelta(days=days)
        runs: list[str] = []
        cursor: str | None = None
        while True:
            page = await backend.list(destination, f"{day:%Y/%m/%d}/", cursor)
            runs += [
                h.ref.key for h in page.items
                if h.ref.key.endswith(f"/{rule}/{rule}-manifest.checksum")
                and (m := _RUN.match(h.ref.key)) and m[7] == rule
            ]  # fmt: skip
            if page.cursor is None:
                break
            cursor = page.cursor
        for checksum_key in sorted(runs, reverse=True):
            manifest_key = checksum_key[: -len(".checksum")] + ".json"
            try:
                raw = await _read_all(backend, ObjectRef(backend.backend_id, destination,
                                                         manifest_key))  # fmt: skip
                checksum = await _read_all(
                    backend, ObjectRef(backend.backend_id, destination, checksum_key)
                )
            except ObjectNotFound:
                why = f"{manifest_key} is missing"
                continue
            if not _md5_matches(raw, checksum):
                why = f"{manifest_key}: the MD5 doesn't match its checksum file"
                continue
            try:
                m = json.loads(raw)
                check_manifest(m, short, versioned=versioned)
                done = _time(m.get("inventoryCompletionTime"))
                started = _time(m.get("inventoryStartTime"))
            except (InventoryError, ValueError) as e:
                why = f"{manifest_key}: {e}"
                continue
            if done is None or started is None:
                why = f"{manifest_key}: no start or completion time"
                continue
            if now - done > max_age:
                raise InventoryError(f"the newest usable run ({manifest_key}) is older than"
                                     f" {max_age}")  # fmt: skip
            files = [ReportFile(str(f["blob"]), f.get("size")) for f in m["files"]]
            # Each file whole: the length its manifest gives (a file cut short
            # would read as blobs gone missing).
            short_file = None
            for f in files:
                ref = ObjectRef(backend.backend_id, destination, f.blob)
                try:
                    size = (await backend.head(ref)).size
                except ObjectNotFound:
                    short_file = f"{f.blob} is missing"
                    break
                if f.size is not None and size != int(f.size):
                    short_file = f"{f.blob} is {size} bytes, its manifest says {f.size}"
                    break
            if short_file is not None:
                why = f"{manifest_key}: {short_file}"
                continue
            filters = (m.get("ruleDefinition") or {}).get("filters") or {}
            count = (m.get("summary") or {}).get("objectCount")
            return Report(
                destination=destination,
                manifest_blob=manifest_key,
                fmt=str(m["ruleDefinition"]["format"]).lower(),
                files=files,
                started_at=started,
                completed_at=done,
                versions=bool(filters.get("includeBlobVersions")),
                object_count=int(count) if count is not None else None,
            )
    raise InventoryError(why)


# --- CSV ------------------------------------------------------------------------


async def csv_records(chunks: AsyncIterator[bytes]) -> AsyncIterator[list[str]]:
    """Records of a CSV stream, header included. Only text up to the last
    newline outside quotes is parsed each time, so a quoted field that spans
    chunks waits for the rest."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buf = ""
    async for chunk in chunks:
        buf += decoder.decode(chunk)
        cut = _record_end(buf)
        if cut:
            for rec in csv.reader(io.StringIO(buf[:cut], newline="")):
                yield rec
            buf = buf[cut:]
    buf += decoder.decode(b"", final=True)
    if buf.strip():
        if buf.count('"') % 2:
            raise InventoryError("the report ends inside a quoted field")
        for rec in csv.reader(io.StringIO(buf, newline="")):
            yield rec


def _record_end(buf: str) -> int:
    """The end of the last complete record in `buf` (which starts at a record
    boundary): after a newline with an even number of quotes before it."""
    end = buf.rfind("\n")
    while end >= 0:
        if buf.count('"', 0, end) % 2 == 0:
            return end + 1
        end = buf.rfind("\n", 0, end)
    return 0


# --- Parquet --------------------------------------------------------------------


class RangeFile(io.RawIOBase):
    """A read-only, seekable file over `fetch(offset, length) -> bytes`, with
    a read-ahead buffer. pyarrow reads a Parquet file through it (footer, then
    each row group's column chunks), never the whole file at once."""

    def __init__(
        self, size: int, fetch: Callable[[int, int], bytes], read_ahead: int = READ_AHEAD
    ) -> None:
        self._size = size
        self._fetch = fetch
        self._read_ahead = read_ahead
        self._pos = 0
        self._buf = b""
        self._buf_at = 0
        self.fetched: list[tuple[int, int]] = []  # (offset, length) of each range read

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, b: Any) -> int:
        want = min(len(b), self._size - self._pos)
        if want <= 0:
            return 0
        end = self._pos + want
        if not (self._buf_at <= self._pos and end <= self._buf_at + len(self._buf)):
            n = min(max(want, self._read_ahead), self._size - self._pos)
            self._buf = self._fetch(self._pos, n)
            self._buf_at = self._pos
            self.fetched.append((self._pos, n))
            if len(self._buf) < want:
                raise InventoryError("the report is shorter than its size")
        start = self._pos - self._buf_at
        b[:want] = self._buf[start : start + want]
        self._pos = end
        return want


#: Report fields reconcile reads, by name.
COLUMNS = ("Name", "VersionId", "IsCurrentVersion", "Content-Length", "AccessTier", "Deleted",
           "Snapshot", "ImmutabilityPolicyUntilDate", "LegalHold")  # fmt: skip


async def parquet_rows(
    backend: StorageBackend,
    ref: ObjectRef,
    size: int,
    *,
    batch_rows: int = 8192,
    read_ahead: int = READ_AHEAD,
    reads: list[tuple[int, int]] | None = None,
    required: set[str] | frozenset[str] = frozenset(),
) -> AsyncIterator[dict[str, Any]]:
    """The rows of a Parquet report, one record batch at a time. pyarrow runs
    in a thread; each range it reads is fetched on this event loop (and noted
    in `reads`, when given). A file without every `required` column is
    refused (`InventoryError`)."""
    import pyarrow.parquet as pq

    loop = asyncio.get_running_loop()

    async def fetch_async(offset: int, length: int) -> bytes:
        return b"".join([c async for c in backend.get_range(ref, offset, length)])

    def fetch(offset: int, length: int) -> bytes:
        return asyncio.run_coroutine_threadsafe(fetch_async(offset, length), loop).result()

    f = RangeFile(size, fetch, read_ahead)
    if reads is not None:
        f.fetched = reads
    pf = await asyncio.to_thread(pq.ParquetFile, f, pre_buffer=False)
    if missing := sorted(set(required) - set(pf.schema_arrow.names)):
        raise InventoryError(f"{ref.key}: the file has no {', '.join(missing)} column")
    cols = [c for c in COLUMNS if c in pf.schema_arrow.names]
    # One thread reads, in order: RangeFile's position is not shared safely.
    batches: Iterator[Any] = iter(
        pf.iter_batches(batch_size=batch_rows, columns=cols, use_threads=False)
    )
    while True:
        batch = await asyncio.to_thread(next, batches, None)
        if batch is None:
            return
        for row in batch.to_pylist():
            yield row


# --- the inventory source -------------------------------------------------------


class BlobInventory:
    """One checked report, read as reconcile's `InventorySource`."""

    method = "inventory"
    complete_versions = True  # the report lists every version (checked)
    #: Taken before reconcile runs, and may list a blob twice [D] (run.py).
    stale = True

    def __init__(
        self,
        backend: StorageBackend,
        report: Report,
        *,
        default_tier: str | None = None,
    ) -> None:
        self._backend = backend
        self.report = report
        self._default_tier = default_tier
        self.rows_read = 0
        #: What was read, for the run.
        self.detail = f"report {report.destination}/{report.manifest_blob}"

    @property
    def snapshot_at(self) -> datetime:
        # A blob written after the run started may or may not be listed, so
        # only what was there before it counts as missing.
        return self.report.started_at

    async def _rows(self, f: ReportFile) -> AsyncIterator[Mapping[str, Any]]:
        """One report file's rows. The file must be whole (its manifest's
        length) and carry every field reconcile reads, whatever the
        manifest's schema says."""
        ref = ObjectRef(self._backend.backend_id, self.report.destination, f.blob)
        required = self.report.required()
        if self.report.fmt == "parquet":
            size = (await self._backend.head(ref)).size
            if f.size is not None and size != int(f.size):
                raise InventoryError(f"{f.blob} is {size} bytes, its manifest says {f.size}")
            async for row in parquet_rows(self._backend, ref, int(size), required=required):
                yield row
            return
        got = 0

        async def counted() -> AsyncIterator[bytes]:
            nonlocal got
            async for chunk in self._backend.get_range(ref, 0, WHOLE):
                got += len(chunk)
                yield chunk

        header: list[str] | None = None
        async for rec in csv_records(counted()):
            if header is None:
                header = [h.strip() for h in rec]
                if missing := sorted(required - set(header)):
                    raise InventoryError(f"{f.blob}: its header has no {', '.join(missing)}")
                continue
            if not rec:
                continue
            if len(rec) != len(header):
                raise InventoryError(
                    f"{f.blob}: a record has {len(rec)} fields, the header {len(header)}"
                )
            yield dict(zip(header, rec, strict=True))
        if header is None:
            raise InventoryError(f"{f.blob}: no header row")
        if f.size is not None and got != int(f.size):
            raise InventoryError(f"{f.blob}: read {got} bytes, its manifest says {f.size}")

    def _item(self, row: Mapping[str, Any], short: str) -> InventoryItem | None:
        name = str(row.get("Name") or "")
        if not name.startswith(short + "/"):
            return None  # another container of the account
        if _bool(row.get("Deleted") or False) or (row.get("Snapshot") or ""):
            return None
        tier = str(row.get("AccessTier") or self._default_tier or "")
        if not tier:
            raise InventoryError(f"{name}: no AccessTier and no default tier for the account")
        version = row.get("VersionId") or None
        return InventoryItem(
            key=name[len(short) + 1 :],
            version_id=str(version) if version is not None else None,
            size=int(row.get("Content-Length") or 0),
            native_class=tier,
            retention_until=_time(row.get("ImmutabilityPolicyUntilDate")),
            legal_hold=_bool(row.get("LegalHold") or False),
            is_current=(_bool(row.get("IsCurrentVersion")) if self.report.versions else True),
        )

    async def items(self, container: str) -> AsyncIterator[InventoryItem]:
        short = container.split("/", 1)[-1]
        for f in self.report.files:
            async for row in self._rows(f):
                self.rows_read += 1
                try:
                    it = self._item(row, short)
                except (TypeError, ValueError) as e:
                    raise InventoryError(f"{f.blob}: {e}") from None
                if it is not None:
                    yield it
        count = self.report.object_count
        if count is not None and self.rows_read != count:
            # VERIFY: that objectCount counts the files' rows (duplicates too).
            raise InventoryError(
                f"{self.report.manifest_blob}: read {self.rows_read} rows, its summary says {count}"
            )

    async def probe(self, container: str, keys: Sequence[str]) -> list[InventoryItem]:
        return []


__all__ = [
    "BlobInventory",
    "InventoryError",
    "RangeFile",
    "Report",
    "ReportFile",
    "check_manifest",
    "csv_records",
    "latest_report",
    "parquet_rows",
]
