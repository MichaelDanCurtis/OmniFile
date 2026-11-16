"""S3 Metadata tables as a seed source and as the journal that keeps a seeded
source current (docs/design/catalog-seeding.md sections 7 and 8,
docs/design/s3-metadata.md).

For a bucket with an S3 Metadata configuration, S3 keeps AWS-managed Apache
Iceberg tables in the account's `aws-s3` table bucket, in the namespace
`b_<bucket>` [D]:

- **inventory** (the live inventory table): one row per object version and
  delete marker, "typically" current within an hour [D]. Seeding reads it
  instead of an S3 Inventory report or a listing: no LIST of the bucket.
- **journal**: one row per change (`CREATE`, `UPDATE_METADATA`, `DELETE`),
  near real time [D]. Once a source is seeded, `worker/journal_sync.py`
  turns its rows into change records for `changefeed_sync.apply_record`, as
  the bucket's EventBridge events are: each is a prompt, and the key's
  current object is HEADed (`sources/follow.py`).

Both are read with PyIceberg's REST catalog against the S3 Tables Iceberg
endpoint, signed with SigV4 (signing name `s3tables`) [D]. The endpoint is
the backend's `s3tables_endpoint_url` (the `com.amazonaws.<region>.s3tables`
interface endpoint; with private networking there is no public fallback,
decision 22), and the tables' data files are read through the backend's S3
endpoint. Credentials come from the backend's own provider: web identity
from the service's managed identity (`backends/s3/credentials.py`,
`botocore_session_for`), and the endpoint and tables are the operator's
account settings, never a row's. Nothing
here writes to the source bucket (decision 40) or to the tables.

A source's `inventory` names the tables:

    {"kind": "s3_metadata",
     "table_bucket_arn": "arn:aws:s3tables:<region>:<account>:bucket/aws-s3",
     "namespace": "b_<bucket>", "table": "inventory", "journal_table": "journal"}

(`namespace`, `table` and `journal_table` default as shown).

**Current versions.** The inventory table holds every version and delete
marker of a key; a key's current object is its row with the largest
`sequence_number` [D], unless that row is a delete marker. The rows of one key
can be in any data file, so before the first batch the reader scans the
`key` and `sequence_number` columns of the snapshot once and keeps the newest
sequence number of every key that has more than one row (held in the
worker, per source and snapshot). Building it holds every key under the
prefix in memory once, about 200 bytes per object.

**Resume** is `{"snapshot_id", "task", "offset"}`: a seed pins the snapshot
it started from and walks its data files in path order. S3 keeps a snapshot
for at most about 24 hours [D]; if the pinned one is gone, the seed starts
again on the current snapshot (rows already seeded count as present).

VERIFY on real AWS (docs/milestones/real-aws-checks.md): the column names and
types, that the inventory table lists noncurrent versions and delete
markers, the `b_<bucket>` namespace spelling, the endpoint and TLS 1.2 over
the interface endpoints, and that the table bucket's data files are
readable through the S3 interface endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..backends.base import ChangeKind, ChangeRecord, ObjectRef, StorageBackend
from ..backends.s3.config import S3Config
from .inventory_s3 import logical_tier
from .model import Batch, SeedObject, SourceError

log = logging.getLogger("omnifile_service.sources")

KIND = "s3_metadata"
#: The inventory table is "typically" current within an hour [D], so its
#: snapshot is taken as that much older: the change feed and the journal
#: replay from there (replays are harmless, each is a HEAD).
INVENTORY_LAG = timedelta(hours=1)
#: Columns read when the table has them (S3 Metadata's published schemas).
INVENTORY_COLUMNS = (
    "bucket",
    "key",
    "sequence_number",
    "version_id",
    "is_delete_marker",
    "size",
    "last_modified_date",
    "e_tag",
    "storage_class",
    "is_multipart",
    "encryption_status",
)
JOURNAL_COLUMNS = (*INVENTORY_COLUMNS, "record_type", "record_timestamp")
REQUIRED = ("key", "sequence_number")
#: Encryption under which a single-part ETag is the object's MD5 (null:
#: unencrypted) [V].
MD5_ETAG_ENCRYPTION = frozenset({None, "", "SSE-S3"})
_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")
_ARN = re.compile(
    r"^arn:aws[a-z-]*:s3tables:([a-z0-9-]+):(\d{12}):bucket/([a-z0-9][a-z0-9-]{1,61})$"
)
#: Newest-sequence indexes kept between batches, by (backend, bucket,
#: prefix, snapshot).
INDEXES_MAX = 4
_indexes: OrderedDict[tuple[int, str, str, int], dict[str, str]] = OrderedDict()


def check_config(cfg: Mapping[str, Any], *, region: str | None = None) -> dict[str, str]:
    """A source's `s3_metadata` inventory, normalised. Raises SourceError."""
    arn = cfg.get("table_bucket_arn")
    if not isinstance(arn, str) or (m := _ARN.match(arn)) is None:
        raise SourceError(
            "s3_metadata needs table_bucket_arn, arn:aws:s3tables:<region>:<account>:bucket/<name>"
        )
    if region is not None and m[1] != region:
        raise SourceError(f"the table bucket is in {m[1]}, the backend in {region}")
    out = {"kind": KIND, "table_bucket_arn": arn}
    for k in ("namespace", "table", "journal_table"):
        v = cfg.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not re.fullmatch(r"[a-z0-9_]{1,255}", v):
            raise SourceError(f"s3_metadata {k} must be lower-case letters, digits and '_'")
        out[k] = v
    extra = set(cfg) - {"kind", "table_bucket_arn", "namespace", "table", "journal_table"}
    if extra:
        raise SourceError(f"s3_metadata doesn't take {', '.join(sorted(extra))}")
    return out


def namespace(cfg: Mapping[str, Any], bucket: str) -> str:
    return str(cfg.get("namespace") or f"b_{bucket}")


def load_catalog_for(backend: StorageBackend, cfg: Mapping[str, Any]) -> Any:
    """The Iceberg catalog holding the tables: S3 Tables' REST endpoint with
    SigV4, credentials and endpoints from the backend's own configuration,
    never from the source row. Tests replace this function."""
    from pyiceberg.catalog import load_catalog

    from ..backends.s3.credentials import botocore_session_for

    config = getattr(backend, "config", None)
    if not isinstance(config, S3Config):
        raise SourceError(
            f"S3 Metadata tables describe S3 buckets; backend {backend.backend_id} isn't S3"
        )
    if config.private_network and not config.s3tables_endpoint_url:
        raise SourceError(
            f"backend {backend.backend_id} uses private networking but has no"
            " s3tables_endpoint_url (the S3 Tables interface endpoint); there is no public fallback"
        )
    uri = config.s3tables_endpoint_url or f"https://s3tables.{config.region}.amazonaws.com"
    uri = uri.rstrip("/")
    if not uri.endswith("/iceberg"):
        uri += "/iceberg"
    session = botocore_session_for(config)
    creds = session.get_credentials()
    if creds is None:
        raise SourceError(f"backend {backend.backend_id} has no AWS credentials for S3 Tables")
    frozen = creds.get_frozen_credentials()
    props: dict[str, Any] = {
        "type": "rest",
        "uri": uri,
        "warehouse": cfg["table_bucket_arn"],
        "rest.sigv4-enabled": "true",
        "rest.signing-name": "s3tables",
        "rest.signing-region": config.region,
        "botocore_session": session,
        # The tables' data files, through the backend's S3 endpoint.
        "s3.region": config.region,
        "s3.access-key-id": frozen.access_key,
        "s3.secret-access-key": frozen.secret_key,
        "s3.session-token": frozen.token,
    }
    if config.endpoint_url:
        props["s3.endpoint"] = config.endpoint_url
    if config.ca_bundle:
        props["ssl.cabundle"] = config.ca_bundle
    return load_catalog("s3tables", **props)


def _utc(t: Any) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    if not isinstance(t, datetime):
        return datetime(1970, 1, 1, tzinfo=UTC)
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _version(v: Any) -> str | None:
    return None if v in (None, "", "null") else str(v)


def seed_object(r: Mapping[str, Any]) -> SeedObject:
    native = r.get("storage_class") or "STANDARD"
    etag = (r.get("e_tag") or "").strip('"') or None
    md5 = bool(
        etag
        and _MD5.match(etag)
        and not r.get("is_multipart")
        and r.get("encryption_status") in MD5_ETAG_ENCRYPTION
    )
    return SeedObject(
        key=str(r["key"]),
        version_id=_version(r.get("version_id")),
        size=int(r.get("size") or 0),
        last_modified=_utc(r.get("last_modified_date")),
        native_class=native,
        # The tables don't show the Intelligent-Tiering access tier [D]: an
        # archived one reads as `smart` until a GET says otherwise.
        logical_tier=logical_tier(native, None),
        etag=etag,
        etag_is_md5=md5,
    )


class _Tables:
    def __init__(
        self, backend: StorageBackend, container: str, prefix: str, cfg: Mapping[str, Any]
    ) -> None:
        self.backend = backend
        self.container = container
        self.prefix = prefix
        self.cfg = cfg

    def _load(self, which: str, default: str) -> Any:
        from pyiceberg.exceptions import NoSuchTableError

        name = (namespace(self.cfg, self.container), str(self.cfg.get(which) or default))
        try:
            return load_catalog_for(self.backend, self.cfg).load_table(name)
        except NoSuchTableError:
            raise SourceError(
                f"no S3 Metadata table {'.'.join(name)} in {self.cfg['table_bucket_arn']}"
            ) from None

    def _filter(self) -> Any:
        from pyiceberg.expressions import AlwaysTrue, StartsWith

        return StartsWith("key", self.prefix) if self.prefix else AlwaysTrue()

    def _columns(self, table: Any, wanted: tuple[str, ...]) -> tuple[str, ...]:
        have = {f.name for f in table.schema().fields}
        missing = [c for c in REQUIRED if c not in have]
        if missing:
            raise SourceError(f"S3 Metadata table {table.name()} has no {', '.join(missing)}")
        return tuple(c for c in wanted if c in have)

    def _check_bucket(self, r: Mapping[str, Any]) -> None:
        b = r.get("bucket")
        if b is not None and b != self.container:
            raise SourceError(
                f"the S3 Metadata table describes bucket {b!r}, not {self.container!r}"
            )


class S3MetadataReader(_Tables):
    """The live inventory table as a seed `Reader`."""

    async def snapshot_at(self) -> datetime:
        def go() -> datetime:
            snap = self._load("table", "inventory").current_snapshot()
            if snap is None:
                raise SourceError(
                    "the S3 Metadata inventory table has no snapshot yet (still backfilling?)"
                )
            return datetime.fromtimestamp(snap.timestamp_ms / 1000, UTC) - INVENTORY_LAG

        return await asyncio.to_thread(go)

    def _index(self, table: Any, snap_id: int) -> dict[str, str]:
        """key -> its newest sequence number, for keys with more than one row."""
        k = (self.backend.backend_id, self.container, self.prefix, snap_id)
        got = _indexes.get(k)
        if got is not None:
            _indexes.move_to_end(k)
            return got
        newest: dict[str, str] = {}
        rows: dict[str, int] = {}
        scan = table.scan(
            snapshot_id=snap_id, row_filter=self._filter(),
            selected_fields=("key", "sequence_number"),
        )  # fmt: skip
        for rb in scan.to_arrow_batch_reader():
            for key, seq in zip(
                rb.column("key").to_pylist(), rb.column("sequence_number").to_pylist(), strict=True
            ):
                rows[key] = rows.get(key, 0) + 1
                if seq is not None and seq > newest.get(key, ""):
                    newest[key] = seq
        index = {key: seq for key, seq in newest.items() if rows[key] > 1}
        _indexes[k] = index
        while len(_indexes) > INDEXES_MAX:
            _indexes.popitem(last=False)
        return index

    def _rows(self, resume: Mapping[str, Any] | None) -> Iterator[tuple[int, int, dict[str, Any]]]:
        """(task, offset, row) of the pinned snapshot from `resume`."""
        from pyiceberg.io.pyarrow import ArrowScan

        table = self._load("table", "inventory")
        snap_id = (resume or {}).get("snapshot_id")
        start_task = int((resume or {}).get("task", 0))
        start_offset = int((resume or {}).get("offset", 0))
        if snap_id is not None and table.snapshot_by_id(int(snap_id)) is None:
            log.warning(
                "the pinned S3 Metadata snapshot expired; seeding again from the current one",
                extra={"snapshot_id": snap_id},
            )
            snap_id, start_task, start_offset = None, 0, 0
        if snap_id is None:
            snap = table.current_snapshot()
            if snap is None:
                raise SourceError("the S3 Metadata inventory table has no snapshot yet")
            snap_id = snap.snapshot_id
        snap_id = int(snap_id)
        index = self._index(table, snap_id)
        cols = self._columns(table, INVENTORY_COLUMNS)
        scan = table.scan(snapshot_id=snap_id, row_filter=self._filter(), selected_fields=cols)
        tasks = sorted(scan.plan_files(), key=lambda t: t.file.file_path)
        arrow = ArrowScan(table.metadata, table.io, scan.projection(), self._filter())
        self.snapshot_id = snap_id
        for ti in range(start_task, len(tasks)):
            offset = 0
            skip = start_offset if ti == start_task else 0
            for rb in arrow.to_record_batches([tasks[ti]]):
                for r in rb.to_pylist():
                    offset += 1
                    if offset <= skip:
                        continue
                    r["_current"] = not r.get("is_delete_marker") and index.get(
                        r["key"], r.get("sequence_number")
                    ) == r.get("sequence_number")
                    yield ti, offset, r

    def _read(self, resume: Mapping[str, Any] | None, limit: int) -> Batch:
        out: list[SeedObject] = []
        rows = 0
        for ti, offset, r in self._rows(resume):
            rows += 1
            self._check_bucket(r)
            if r["_current"]:
                try:
                    out.append(seed_object(r))
                except (KeyError, TypeError, ValueError) as e:
                    raise SourceError(f"S3 Metadata row for {r.get('key')!r}: {e}") from None
            if len(out) >= limit:
                return Batch(
                    out, {"snapshot_id": self.snapshot_id, "task": ti, "offset": offset}, rows
                )
        return Batch(out, None, rows)

    async def read(self, resume: Mapping[str, Any] | None, limit: int) -> Batch:
        return await asyncio.to_thread(self._read, resume, limit)


# --- the journal --------------------------------------------------------------------


@dataclass(frozen=True)
class JournalRow:
    #: Total order within one read: (record_timestamp, key, sequence_number).
    order: tuple[str, str, str]
    record: ChangeRecord | None  # None: a record type nothing applies


@dataclass(frozen=True)
class JournalRead:
    rows: list[JournalRow]
    #: The source's journal cursor after row i is applied: `cursor_after(row)`.
    base: dict[str, Any]
    #: The cursor once every row is applied.
    end: dict[str, Any]

    def cursor_after(self, row: JournalRow) -> dict[str, Any]:
        return {**self.base, "after": list(row.order)}


def change_record(backend_id: int, bucket: str, r: Mapping[str, Any]) -> ChangeRecord | None:
    """A journal row as the change record an S3 event would be. The journal
    records puts and deletes, not restores (those stay on the SQS feed)."""
    kind = str(r.get("record_type") or "").upper()
    at = _utc(r.get("record_timestamp"))
    ref = ObjectRef(backend_id, bucket, str(r["key"]), _version(r.get("version_id")))
    detail = {"sequence_number": str(r.get("sequence_number") or ""), "source": "s3_metadata"}
    marker = bool(r.get("is_delete_marker"))
    if kind == "DELETE" or (kind == "CREATE" and marker):
        if marker:
            detail["tombstone"] = "true"  # a delete marker: every version is kept
        return ChangeRecord(ChangeKind.DELETED, ref, at, detail=detail)
    if kind == "CREATE":
        return ChangeRecord(
            ChangeKind.CREATED, ref, at, native_class=r.get("storage_class"),
            size=int(r["size"]) if r.get("size") is not None else None, detail=detail,
        )  # fmt: skip
    if kind == "UPDATE_METADATA" and r.get("storage_class"):
        return ChangeRecord(
            ChangeKind.TIER_CHANGED, ref, at, native_class=str(r["storage_class"]), detail=detail
        )
    return None


class MetadataJournal(_Tables):
    """The journal table, read for one source's prefix after its cursor.

    Cursor (`catalog.source.journal`): ``{"snapshot_id", "through"}`` once a
    read is applied: rows up to that journal snapshot are done, and
    `through` is the newest `record_timestamp` seen. While a read is being
    applied in batches it also holds ``"to"`` (the snapshot being read up to)
    and ``"after"`` (the last row applied, in read order), so a crash
    resumes after that row.

    Each pass reads the rows appended since `snapshot_id` (an incremental
    append scan; compaction snapshots add no rows). With no `snapshot_id`
    yet, or once it has expired, it scans rows with `record_timestamp` at
    or after `through` (or the source's snapshot time), which the journal's
    retention (7 days at least; the setup guide sets 14) covers [D].
    """

    def _read(self, cursor: Mapping[str, Any] | None, since: datetime, limit: int) -> JournalRead:
        from pyiceberg.expressions import And, GreaterThanOrEqual

        table = self._load("journal_table", "journal")
        c = dict(cursor or {})
        current = table.current_snapshot()
        if current is None:
            return JournalRead([], c, c)
        if c.get("to") is not None and table.snapshot_by_id(int(c["to"])) is None:
            # The snapshot a read was resuming to expired: read again to now.
            c = {k: v for k, v in c.items() if k not in ("to", "after")}
        target = int(c.get("to") or current.snapshot_id)
        start = c.get("snapshot_id")
        through = c.get("through") or _utc(since).isoformat(timespec="microseconds")
        base = {"snapshot_id": start, "through": through, "to": target}
        if start is not None and int(start) == target:
            return JournalRead([], c, {"snapshot_id": target, "through": through})
        cols = self._columns(table, JOURNAL_COLUMNS)
        arrow = None
        if start is not None:
            try:
                scan = table.incremental_append_scan(
                    from_snapshot_id_exclusive=int(start), to_snapshot_id_inclusive=target,
                    row_filter=self._filter(), selected_fields=cols,
                )  # fmt: skip
                arrow = scan.to_arrow()
            except ValueError:  # the snapshot expired: fall back to the time
                log.warning("journal snapshot %s expired; reading by time", start)
                base = {"snapshot_id": None, "through": through, "to": target}
        if arrow is None:
            t = _utc(through).replace(tzinfo=None).isoformat()
            scan = table.scan(
                snapshot_id=target,
                row_filter=And(self._filter(), GreaterThanOrEqual("record_timestamp", t)),
                selected_fields=cols,
            )
            arrow = scan.to_arrow()
        rows: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
        for r in arrow.to_pylist():
            self._check_bucket(r)
            ts = _utc(r.get("record_timestamp")).isoformat(timespec="microseconds")
            rows.append(((ts, str(r["key"]), str(r.get("sequence_number") or "")), r))
        rows.sort(key=lambda x: x[0])
        if c.get("to") == target and c.get("after") is not None:
            after = tuple(c["after"])
            rows = [x for x in rows if x[0] > after]
        newest = max([through, *(o[0] for o, _ in rows)], key=lambda s: _utc(s))
        take = rows[:limit]
        out = [
            JournalRow(o, change_record(self.backend.backend_id, self.container, r))
            for o, r in take
        ]
        if len(rows) <= limit:
            end: dict[str, Any] = {"snapshot_id": target, "through": newest}
        else:
            end = {**base, "after": list(take[-1][0])}
        return JournalRead(out, base, end)

    async def read(
        self, cursor: Mapping[str, Any] | None, since: datetime, limit: int
    ) -> JournalRead:
        """Rows after `cursor` (or from `since`, the source's snapshot time),
        at most `limit`, in order."""
        return await asyncio.to_thread(self._read, cursor, since, limit)


def clear_indexes() -> None:
    """Forget the kept indexes (tests)."""
    _indexes.clear()


__all__ = [
    "KIND",
    "JournalRead",
    "JournalRow",
    "MetadataJournal",
    "S3MetadataReader",
    "change_record",
    "check_config",
    "clear_indexes",
    "load_catalog_for",
    "seed_object",
]
