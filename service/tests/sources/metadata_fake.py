"""S3 Metadata tables over the S3 fake, on a local PyIceberg SQL catalog
(SQLite and a file warehouse) in place of the S3 Tables REST endpoint.

The live inventory table and the journal table have the columns of S3
Metadata's published schemas ("live inventory tables schema", "journal
tables schema"); `last_modified_date` and `record_timestamp` are Iceberg
`timestamp` (no zone, UTC) [D]. `snapshot()` rewrites the inventory table
from the fake bucket (every version and delete marker, one new snapshot);
the journal writers append the row S3 would add for a change the test has
just made through the backend. Reading the fake's state never goes through
the S3 API, so the fake's call log counts only what the service did.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from pyiceberg.expressions import AlwaysTrue
from s3.s3_fakes import ACCOUNT, REGION, FakeAws, Version

TABLE_BUCKET_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT}:bucket/aws-s3"

_COMMON = [
    ("bucket", "string"),
    ("key", "string"),
    ("sequence_number", "string"),
    ("version_id", "string"),
    ("is_delete_marker", "boolean"),
    ("size", "long"),
    ("last_modified_date", "timestamp"),
    ("e_tag", "string"),
    ("storage_class", "string"),
    ("is_multipart", "boolean"),
    ("encryption_status", "string"),
    ("is_bucket_key_enabled", "boolean"),
    ("kms_key_arn", "string"),
    ("checksum_algorithm", "string"),
    ("object_tags", "map"),
    ("user_metadata", "map"),
]
INVENTORY_FIELDS = list(_COMMON)
JOURNAL_FIELDS = [
    *_COMMON[:3],
    ("record_type", "string"),
    ("record_timestamp", "timestamp"),
    *_COMMON[3:],
    ("requester", "string"),
    ("source_ip_address", "string"),
    ("request_id", "string"),
]


def _schema(fields: Sequence[tuple[str, str]]) -> Any:
    from pyiceberg.schema import Schema
    from pyiceberg.types import (
        BooleanType,
        LongType,
        MapType,
        NestedField,
        StringType,
        TimestampType,
    )

    ids = itertools.count(1)
    out = []
    for name, kind in fields:
        fid = next(ids)
        if kind == "map":
            t: Any = MapType(next(ids), StringType(), next(ids), StringType(), value_required=False)
        else:
            t = {"string": StringType(), "boolean": BooleanType(), "long": LongType(),
                 "timestamp": TimestampType()}[kind]  # fmt: skip
        out.append(NestedField(fid, name, t, required=False))
    return Schema(*out)


def _naive(t: datetime) -> datetime:
    return t.astimezone(UTC).replace(tzinfo=None) if t.tzinfo else t


class FakeMetadataTables:
    """One bucket's S3 Metadata configuration."""

    def __init__(self, root: Path, aws: FakeAws, bucket: str) -> None:
        from pyiceberg.catalog.sql import SqlCatalog

        root.mkdir(parents=True, exist_ok=True)
        (root / "warehouse").mkdir(exist_ok=True)
        self.aws = aws
        self.bucket = bucket
        #: The `bucket` column (a test can make the tables another bucket's).
        self.bucket_column = bucket
        self.namespace = f"b_{bucket.replace('-', '_')}"
        self.catalog = SqlCatalog(
            "omnifile-test", uri=f"sqlite:///{root / 'catalog.db'}",
            warehouse=f"file://{root / 'warehouse'}",
        )  # fmt: skip
        self.catalog.create_namespace(self.namespace)
        self.inventory = self.catalog.create_table(
            f"{self.namespace}.inventory", _schema(INVENTORY_FIELDS)
        )
        self.journal_table = self.catalog.create_table(
            f"{self.namespace}.journal", _schema(JOURNAL_FIELDS)
        )
        self._counter = itertools.count(1)
        #: id(Version) -> its sequence number: one per version, increasing.
        self._seqs: dict[int, str] = {}
        self.journal_rows = 0

    @property
    def config(self) -> dict[str, str]:
        """What a source registers as its `inventory`."""
        return {"kind": "s3_metadata", "table_bucket_arn": TABLE_BUCKET_ARN,
                "namespace": self.namespace}  # fmt: skip

    def _seq(self, v: Version) -> str:
        if id(v) not in self._seqs:
            self._seqs[id(v)] = f"{next(self._counter):040d}"
        return self._seqs[id(v)]

    def _versions(self, key: str) -> list[Version]:
        return self.aws.buckets[self.bucket].keys.get(key, [])

    def _row(self, key: str, v: Version) -> dict[str, Any]:
        multipart = "-" in v.etag.strip('"')
        return {
            "bucket": self.bucket_column,
            "key": key,
            "sequence_number": self._seq(v),
            "version_id": None if v.vid == "null" else v.vid,
            "is_delete_marker": v.marker,
            "size": 0 if v.marker else len(v.data),
            "last_modified_date": _naive(v.last_modified),
            "e_tag": None if v.marker else v.etag,
            "storage_class": None if v.marker else v.storage_class,
            "is_multipart": multipart,
            "encryption_status": None if v.marker else "SSE-S3",
            "is_bucket_key_enabled": False,
            "kms_key_arn": None,
            "checksum_algorithm": None if v.marker else "CRC64NVME",
            "object_tags": None if v.marker else list(v.tags.items()),
            "user_metadata": None if v.marker else list(v.metadata.items()),
        }

    def snapshot(self, *, shuffle: bool = True) -> int:
        """Rewrite the live inventory table from the bucket: every version and
        delete marker. `shuffle` puts a key's versions in different data files
        (newest first), as nothing promises they are together. Returns rows."""
        rows = [
            self._row(key, v)
            for key, chain in sorted(self.aws.buckets[self.bucket].keys.items())
            for v in chain
        ]
        schema = self.inventory.schema().as_arrow()
        tx = self.inventory.transaction()
        if self.inventory.current_snapshot() is not None:
            tx.delete(AlwaysTrue())
        if not shuffle:
            tx.append(pa.Table.from_pylist(rows, schema=schema))
        else:
            # One data file per generation: newest versions first.
            by_gen: dict[int, list[dict[str, Any]]] = {}
            for key, chain in self.aws.buckets[self.bucket].keys.items():
                for i, v in enumerate(reversed(chain)):
                    by_gen.setdefault(i, []).append(self._row(key, v))
            for i in sorted(by_gen):
                tx.append(pa.Table.from_pylist(by_gen[i], schema=schema))
        tx.commit_transaction()
        return len(rows)

    def _journal(self, record_type: str, key: str, v: Version, at: datetime) -> None:
        row = self._row(key, v)
        row.update(record_type=record_type, record_timestamp=_naive(at),
                   requester=ACCOUNT, source_ip_address="10.0.0.1",
                   request_id=row["sequence_number"])  # fmt: skip
        if record_type == "DELETE":
            row.update(object_tags=None, user_metadata=None)
        schema = self.journal_table.schema().as_arrow()
        self.journal_table.append(pa.Table.from_pylist([row], schema=schema))
        self.journal_rows += 1

    def created(self, key: str, at: datetime) -> None:
        """A put (or a copy): the key's newest version."""
        self._journal("CREATE", key, self._versions(key)[-1], at)

    def delete_marker(self, key: str, at: datetime) -> None:
        v = self._versions(key)[-1]
        assert v.marker
        self._journal("DELETE", key, v, at)

    def version_deleted(self, key: str, version: Version, at: datetime) -> None:
        """A version (or, unversioned, the object) deleted for good."""
        self._journal("DELETE", key, version, at)

    def class_changed(self, key: str, at: datetime) -> None:
        self._journal("UPDATE_METADATA", key, self._versions(key)[-1], at)
