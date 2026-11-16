"""A catalog with a home backend and an S3 source backend over the fake."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import io
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from backend_contract.clock import ManualClock
from psycopg.types.json import Jsonb
from s3.s3_fakes import ACCOUNT, REGION, WEB_IDENTITY, FakeAws

from omnifile_service.backends.s3 import S3Backend, S3Config
from omnifile_service.backends.s3.config import BatchOps
from omnifile_service.sources import Groups
from omnifile_service.worker.backends import BackendRegistry

BUCKET = "seismic-raw"
INV = "inventory"
OWNERS = "S-1-5-21-10-20-30-1101"
WRITERS = "S-1-5-21-10-20-30-1102"
READERS = "S-1-5-21-10-20-30-1103"
GROUPS = Groups(OWNERS, WRITERS, READERS)


@dataclass
class World:
    dataset_id: int
    root_id: int
    home_id: int
    s3_id: int
    #: The service's binding: read-only on the source, as a customer's is.
    s3: S3Backend
    #: The client's own tools (uploads, overwrites, deletes in its bucket).
    client: S3Backend
    fake: FakeAws
    clock: ManualClock
    registry: BackendRegistry


def world(
    c: psycopg.Connection,
    *,
    clock: ManualClock | None = None,
    versioning: bool = True,
    batch: bool = False,
    staging: bool = False,
) -> World:
    clock = clock or ManualClock(datetime.now(UTC).replace(microsecond=0))
    tag = uuid.uuid4().hex[:8]
    (home,) = c.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'sim', %s, true) RETURNING backend_id",
        (f"home-{tag}", Jsonb({"layout": "azure_two_account"})),
    ).fetchone()
    (s3_id,) = c.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, false) RETURNING backend_id",
        (f"s3-{tag}", Jsonb({"region": REGION, "buckets": [BUCKET, INV]})),
    ).fetchone()
    (edge,) = c.execute(
        "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
        " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
        (f"edge-{tag}", uuid.uuid4().bytes * 2),
    ).fetchone()
    (ds,) = c.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout, posix_defaults)"
        " VALUES (%s, %s, %s, 'two_account', %s) RETURNING dataset_id",
        (f"ds-{tag}", home, edge, Jsonb({"uid": 5000, "gid": 5001})),
    ).fetchone()
    (root,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
        (ds,),
    ).fetchone()
    fake = FakeAws(clock.now)
    queue = fake.queue_url(f"events-{tag}")
    fake.add_bucket(BUCKET, queue=queue, versioning=versioning)
    fake.add_bucket(INV)
    config = S3Config(
        region=REGION,
        buckets=(BUCKET, INV),
        private_network=False,
        **WEB_IDENTITY,
        event_queues={BUCKET: queue},
        list_page_size=2,
        batch=BatchOps(ACCOUNT, f"arn:aws:iam::{ACCOUNT}:role/omnifile-batch", INV)
        if batch
        else None,
        staging_bucket=INV if staging else None,
    )
    s3 = S3Backend(s3_id, config, clock=clock, on_client=fake.attach)
    client = S3Backend(
        s3_id,
        dataclasses.replace(config, owned_buckets=(BUCKET, INV)),
        clock=clock,
        on_client=fake.attach,
    )
    registry = BackendRegistry.from_mapping({s3_id: s3})
    return World(ds, root, home, s3_id, s3, client, fake, clock, registry)


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def put(w: World, key: str, data: bytes = b"x", tier: Any = None, bucket: str = BUCKET):
    return await w.client.put(bucket, key, _one(data), tier, {})


COLUMNS = (
    "Bucket, Key, VersionId, IsLatest, IsDeleteMarker, Size, LastModifiedDate, ETag,"
    " StorageClass, EncryptionStatus, IntelligentTieringAccessTier"
)


def csv_gz(rows: Sequence[Sequence[str]]) -> bytes:
    text = "".join(",".join(f'"{v}"' for v in r) + "\n" for r in rows)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        g.write(text.encode())
    return buf.getvalue()


async def inventory(
    w: World,
    files: Sequence[Sequence[Sequence[str]]],
    *,
    created: datetime | None = None,
    fmt: str = "CSV",
    schema: str = COLUMNS,
    corrupt_checksum: bool = False,
) -> str:
    """Write an S3 Inventory (manifest, checksum, gzip CSV files) into the
    inventory bucket. Returns the manifest's key."""
    base = f"{BUCKET}/daily/{uuid.uuid4().hex[:6]}"
    entries = []
    for i, rows in enumerate(files):
        key = f"{base}/data/part-{i}.csv.gz"
        body = csv_gz(rows)
        await put(w, key, body, bucket=INV)
        entries.append(
            {"key": key, "size": len(body), "MD5checksum": hashlib.md5(body).hexdigest()}
        )
    at = created or w.clock.now() - timedelta(hours=6)
    manifest = json.dumps(
        {
            "sourceBucket": BUCKET,
            "destinationBucket": f"arn:aws:s3:::{INV}",
            "version": "2016-11-30",
            "creationTimestamp": str(int(at.timestamp() * 1000)),
            "fileFormat": fmt,
            "fileSchema": schema,
            "files": entries,
        }
    ).encode()
    mkey = f"{base}/manifest.json"
    await put(w, mkey, manifest, bucket=INV)
    digest = hashlib.md5(manifest).hexdigest()
    await put(
        w,
        f"{base}/manifest.checksum",
        (("0" * 32) if corrupt_checksum else digest).encode(),
        bucket=INV,
    )
    return mkey


def row(
    key: str,
    size: int = 10,
    *,
    version: str = "v1",
    latest: bool = True,
    marker: bool = False,
    cls: str = "STANDARD",
    etag: str = "0" * 32,
    enc: str = "SSE-S3",
    it: str = "",
    when: str = "2026-09-01T10:00:00.000Z",
) -> list[str]:
    return [
        BUCKET,
        key,
        version,
        str(latest).lower(),
        str(marker).lower(),
        str(size),
        when,
        etag,
        cls,
        enc,
        it,
    ]


def deployment(monkeypatch: Any, db: psycopg.Connection, w: World, **account: Any) -> None:
    """Give the world's S3 backend row an operator account
    (``OMNIFILE_AWS_ACCOUNTS``) with these settings: OmniFile's own buckets,
    S3 Metadata tables and the like are deployment settings, never a row's
    (backends/s3/config.py). The row names only the account."""
    acct = {
        "region": REGION,
        "role_arn": WEB_IDENTITY["role_arn"],
        "private_network": False,
        **account,
    }
    monkeypatch.setenv("OMNIFILE_AWS_ACCOUNTS", json.dumps({ACCOUNT: acct}))
    monkeypatch.setenv("OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE", WEB_IDENTITY["web_identity_audience"])
    monkeypatch.setenv("OMNIFILE_AWS_PRIVATE_NETWORK", "0")
    db.execute(
        "UPDATE catalog.backend SET config = %s WHERE backend_id = %s",
        (Jsonb({"account_id": ACCOUNT, "region": REGION, "buckets": [BUCKET, INV]}), w.s3_id),
    )
