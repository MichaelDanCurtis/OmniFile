"""An in-memory S3 and SQS for the S3 backend's tests.

It answers botocore at `before-call`, the hook botocore's own Stubber uses, so
every request still goes through the SDK's parameter validation and
serialization, and nothing touches a network. Responses are the parsed shapes
botocore would return. Time comes from a `ManualClock`, so restores complete,
restored copies expire and SQS visibility timeouts lapse as tests move it.

This is a test double for the backend's logic and request plumbing, not a
model of S3 (that is the opt-in real-AWS lane, docs/milestones/real-aws-checks.md).
The rules it does model, because the backend depends on them:

- versioned buckets keep every version; a plain DELETE adds a delete marker;
- STANDARD is omitted from HeadObject/GetObject, as S3 omits it;
- GLACIER / DEEP_ARCHIVE reads answer 403 InvalidObjectState unless a restored
  copy is live; a restore completes after the tier's typical time and the
  copy expires `Days` after that, at the next midnight UTC; S3 removes the
  copy (and the x-amz-restore header) asynchronously, here `expiry_lag` later;
- every write, delete and restore step is an EventBridge event on the
  bucket's SQS queue, with SQS receive / visibility / delete semantics.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import itertools
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus, unquote

from aiobotocore.awsrequest import AioAWSResponse
from azure.storage.extensions import checksums

ACCOUNT = "123456789012"
REGION = "eu-west-1"
#: The service's web identity (no keys): S3Config kwargs for tests. The
#: fakes answer before a request is signed, so STS is never called unless a
#: test asks for credentials.
WEB_IDENTITY = {
    "role_arn": f"arn:aws:iam::{ACCOUNT}:role/omnifile-service",
    "web_identity_audience": "api://omnifile-aws",
}
MiB = 1024 * 1024

#: (class or Intelligent-Tiering archive tier, restore tier) -> hours to complete.
RESTORE_HOURS: dict[tuple[str, str], float] = {
    ("GLACIER", "Expedited"): 5 / 60,
    ("GLACIER", "Standard"): 4.0,
    ("GLACIER", "Bulk"): 8.0,
    ("DEEP_ARCHIVE", "Standard"): 12.0,
    ("DEEP_ARCHIVE", "Bulk"): 48.0,
    ("ARCHIVE_ACCESS", "Expedited"): 5 / 60,
    ("ARCHIVE_ACCESS", "Standard"): 4.0,
    ("ARCHIVE_ACCESS", "Bulk"): 8.0,
    ("DEEP_ARCHIVE_ACCESS", "Standard"): 12.0,
    ("DEEP_ARCHIVE_ACCESS", "Bulk"): 48.0,
}
ARCHIVED = ("GLACIER", "DEEP_ARCHIVE")


class AwsError(Exception):
    def __init__(self, code: str, status: int, message: str = "", **headers: str) -> None:
        super().__init__(code)
        self.code, self.status, self.message, self.headers = code, status, message, headers


def crc64(data: bytes) -> int:
    return checksums.crc64.compute(data, 0)


def b64crc(crc: int) -> str:
    return base64.b64encode(crc.to_bytes(8, "big")).decode()


def http_date(t: datetime) -> str:
    return t.strftime("%a, %d %b %Y %H:%M:%S GMT")


def next_midnight(t: datetime) -> datetime:
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return day if day == t else day + timedelta(days=1)


@dataclass
class Restore:
    ongoing: bool
    tier: str
    days: int | None
    expiry: datetime | None = None


@dataclass
class Version:
    vid: str
    data: bytes = b""
    storage_class: str = "STANDARD"
    metadata: dict[str, str] = field(default_factory=dict)
    content_type: str = "binary/octet-stream"
    tags: dict[str, str] = field(default_factory=dict)
    last_modified: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    etag: str = ""
    crc: int = 0
    marker: bool = False
    restore: Restore | None = None
    #: Intelligent-Tiering archive tier (ArchiveStatus), or None.
    archive_status: str | None = None
    #: S3 keeps a full-object CRC64NVME (False: written before it did).
    stored_checksum: bool = True


@dataclass
class Bucket:
    name: str
    versioning: bool
    queue: str | None
    keys: dict[str, list[Version]] = field(default_factory=dict)  # oldest first


@dataclass
class Upload:
    bucket: str
    key: str
    storage_class: str
    metadata: dict[str, str]
    content_type: str
    full_object_crc: bool
    parts: dict[int, tuple[bytes, str]] = field(default_factory=dict)  # n -> (data, etag)


@dataclass
class Message:
    mid: str
    body: str
    visible_at: datetime
    handle: str | None = None
    receives: int = 0


class FakeBody:
    """What GetObject's `Body` offers the backend: async read and aclose."""

    def __init__(self, data: bytes, fail_after: int | None = None) -> None:
        self._data, self._at, self.closed = data, 0, False
        self._fail_after = fail_after

    async def read(self, n: int = -1) -> bytes:
        if self._fail_after is not None and self._at >= self._fail_after:
            raise ConnectionResetError("fake: connection reset mid-body")
        end = len(self._data) if n < 0 else self._at + n
        if self._fail_after is not None:
            end = min(end, self._fail_after)
        chunk = self._data[self._at : end]
        self._at += len(chunk)
        return chunk

    async def aclose(self) -> None:
        self.closed = True


class FakeAws:
    def __init__(
        self, now: Callable[[], datetime], *, expiry_lag: timedelta = timedelta(hours=1)
    ) -> None:
        self.now = now
        self.expiry_lag = expiry_lag
        self.buckets: dict[str, Bucket] = {}
        self.queues: dict[str, list[Message]] = {}
        self.uploads: dict[str, Upload] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bodies: list[FakeBody] = []
        #: op -> errors to raise on its next calls, in order.
        self._fail: dict[str, list[AwsError]] = {}
        #: (due, seq, action): restore completions and expiries, run in order.
        self._timers: list[tuple[datetime, int, Callable[[datetime], None]]] = []
        self._seq = itertools.count(1)
        self.body_fail_after: int | None = None
        #: Batch Operations jobs by id, and idempotency tokens -> job id.
        self.batch_jobs: dict[str, dict[str, Any]] = {}
        self.batch_tokens: dict[str, str] = {}
        self.batch_token_etags: dict[str, Any] = {}
        #: Keys a checksum job can't checksum (as SSE-C objects), and the
        #: number of checksum jobs to fail outright.
        self.checksum_refused: set[str] = set()
        self.checksum_jobs_to_fail = 0
        #: AssumeRoleWithWebIdentity calls: (role, session name, token), and
        #: tokens STS refuses.
        self.sts_calls: list[tuple[str, str, str]] = []
        self.refused_tokens: set[str] = set()

    # --- setup ------------------------------------------------------------

    def forget_checksum(self, bucket: str, key: str) -> None:
        """Make the current version look written before S3 stored checksums."""
        self._find(bucket, key, None).stored_checksum = False

    def add_bucket(self, name: str, *, versioning: bool = True, queue: str | None = None) -> None:
        self.buckets[name] = Bucket(name, versioning, queue)
        if queue is not None:
            self.queues.setdefault(queue, [])

    def queue_url(self, name: str) -> str:
        return f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{name}"

    def fail_next(self, op: str, code: str, status: int, **headers: str) -> None:
        self._fail.setdefault(op, []).append(AwsError(code, status, "injected", **headers))

    def send_raw(self, queue: str, body: str) -> None:
        self.queues[queue].append(Message(uuid.uuid4().hex, body, self.now()))

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def attach(self, client: Any) -> None:
        """`S3Backend(on_client=...)`: answer this client's calls in memory."""
        client.meta.events.register_first("before-parameter-build", self._stash)
        client.meta.events.register_first("before-call", self._answer)

    # --- plumbing ---------------------------------------------------------

    def _stash(self, params: dict[str, Any], context: dict[str, Any], **_: Any) -> None:
        context["fake_params"] = dict(params)

    def _answer(self, model: Any, context: dict[str, Any], **_: Any) -> Any:
        op = model.name
        params = context["fake_params"]
        self.calls.append((op, params))
        self._tick()
        try:
            failures = self._fail.get(op)
            if failures:
                raise failures.pop(0)
            handler = getattr(self, f"_op_{op}", None)
            if handler is None:
                raise AwsError("NotImplemented", 501, f"fake has no {op}")
            parsed = handler(**params) or {}
            status = 200
        except AwsError as e:
            status = e.status
            parsed = {"Error": {"Code": e.code, "Message": e.message}}
            parsed["ResponseMetadata"] = {"HTTPStatusCode": status, "HTTPHeaders": e.headers}
        parsed.setdefault("ResponseMetadata", {"HTTPStatusCode": status, "HTTPHeaders": {}})
        return AioAWSResponse("https://fake.invalid/", status, {}, None), parsed

    def _tick(self) -> None:
        while self._timers:
            self._timers.sort(key=lambda t: (t[0], t[1]))
            due, _, action = self._timers[0]
            if due > self.now():
                return
            self._timers.pop(0)
            action(due)

    def _later(self, due: datetime, action: Callable[[datetime], None]) -> None:
        self._timers.append((due, next(self._seq), action))

    def _bucket(self, name: str) -> Bucket:
        if name not in self.buckets:
            raise AwsError("NoSuchBucket", 404)
        return self.buckets[name]

    def _new_vid(self, b: Bucket) -> str:
        return uuid.uuid4().hex if b.versioning else "null"

    def _store(self, b: Bucket, key: str, v: Version) -> None:
        chain = b.keys.setdefault(key, [])
        if not b.versioning:
            chain.clear()
        chain.append(v)

    def _find(self, bucket: str, key: str, vid: str | None, *, head: bool = False) -> Version:
        b = self._bucket(bucket)
        chain = b.keys.get(key, [])
        if vid is None:
            if not chain or chain[-1].marker:
                raise AwsError("404" if head else "NoSuchKey", 404, **_marker(chain))
            return chain[-1]
        for v in chain:
            if v.vid == vid:
                if v.marker:
                    raise AwsError("405" if head else "MethodNotAllowed", 405)
                return v
        raise AwsError("404" if head else "NoSuchVersion", 404)

    def _event(self, b: Bucket, kind: str, key: str, v: Version | None, at: datetime, **d: Any):
        if b.queue is None:
            return
        obj: dict[str, Any] = {"key": key, "sequencer": f"{next(self._seq):016X}"}
        if v is not None:
            obj["size"] = len(v.data)
            obj["etag"] = v.etag
            if v.vid != "null":
                obj["version-id"] = v.vid
        body = {
            "version": "0",
            "id": str(uuid.uuid4()),
            "detail-type": kind,
            "source": "aws.s3",
            "account": ACCOUNT,
            "time": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "region": REGION,
            "resources": [f"arn:aws:s3:::{b.name}"],
            "detail": {
                "version": "0",
                "bucket": {"name": b.name},
                "object": obj,
                "request-id": uuid.uuid4().hex[:16].upper(),
                "requester": ACCOUNT,
                **d,
            },
        }
        self.queues[b.queue].append(Message(uuid.uuid4().hex, json.dumps(body), self.now()))

    def _readable(self, v: Version) -> None:
        if v.storage_class in ARCHIVED and not (v.restore and not v.restore.ongoing):
            raise AwsError(
                "InvalidObjectState",
                403,
                "The operation is not valid for the object's storage class",
            )
        if v.archive_status:
            raise AwsError("InvalidObjectState", 403, "The object is in an archive access tier")

    @staticmethod
    def _restore_header(v: Version) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if v.restore is not None:
            if v.restore.ongoing:
                out["Restore"] = 'ongoing-request="true"'
            elif v.restore.expiry is not None:
                exp = http_date(v.restore.expiry)
                out["Restore"] = f'ongoing-request="false", expiry-date="{exp}"'
        if v.archive_status:
            out["ArchiveStatus"] = v.archive_status
        return out

    def _object_fields(self, v: Version, checksum: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ContentLength": len(v.data),
            "ETag": f'"{v.etag}"',
            "LastModified": v.last_modified,
            "ContentType": v.content_type,
            "Metadata": dict(v.metadata),
            "ServerSideEncryption": "AES256",
            **self._restore_header(v),
        }
        if v.vid != "null":
            out["VersionId"] = v.vid
        if v.storage_class != "STANDARD":
            out["StorageClass"] = v.storage_class
        if checksum and v.stored_checksum:
            out["ChecksumCRC64NVME"] = b64crc(v.crc)
            out["ChecksumType"] = "FULL_OBJECT"
        return out

    # --- S3: writes ---------------------------------------------------------

    def _op_PutObject(
        self,
        Bucket: str,
        Key: str,
        Body: bytes = b"",
        Metadata: dict[str, str] | None = None,
        StorageClass: str = "STANDARD",
        ChecksumCRC64NVME: str | None = None,
        ContentType: str = "binary/octet-stream",
        **_: Any,
    ) -> dict[str, Any]:
        b = self._bucket(Bucket)
        data = _body(Body)
        crc = crc64(data)
        if ChecksumCRC64NVME is not None and ChecksumCRC64NVME != b64crc(crc):
            raise AwsError("BadDigest", 400, "The CRC64NVME you specified did not match")
        v = Version(
            self._new_vid(b),
            data,
            StorageClass,
            {k.lower(): val for k, val in (Metadata or {}).items()},
            ContentType,
            last_modified=self.now(),
            etag=hashlib.md5(data).hexdigest(),
            crc=crc,
        )
        self._store(b, Key, v)
        self._event(b, "Object Created", Key, v, self.now(), reason="PutObject")
        out = {"ETag": f'"{v.etag}"', "ChecksumCRC64NVME": b64crc(crc)}
        if b.versioning:
            out["VersionId"] = v.vid
        return out

    def _op_CreateMultipartUpload(
        self,
        Bucket: str,
        Key: str,
        Metadata: dict[str, str] | None = None,
        StorageClass: str = "STANDARD",
        ContentType: str = "binary/octet-stream",
        ChecksumType: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self._bucket(Bucket)
        uid = uuid.uuid4().hex
        meta = {k.lower(): v for k, v in (Metadata or {}).items()}
        self.uploads[uid] = Upload(
            Bucket, Key, StorageClass, meta, ContentType, ChecksumType == "FULL_OBJECT"
        )
        return {"Bucket": Bucket, "Key": Key, "UploadId": uid}

    def _upload(self, uid: str) -> Upload:
        if uid not in self.uploads:
            raise AwsError("NoSuchUpload", 404)
        return self.uploads[uid]

    def _op_UploadPart(
        self,
        UploadId: str,
        PartNumber: int,
        Body: bytes = b"",
        ChecksumCRC64NVME: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        up = self._upload(UploadId)
        data = _body(Body)
        if ChecksumCRC64NVME is not None and ChecksumCRC64NVME != b64crc(crc64(data)):
            raise AwsError("BadDigest", 400, "part CRC64NVME mismatch")
        etag = hashlib.md5(data).hexdigest()
        up.parts[PartNumber] = (data, etag)
        return {"ETag": f'"{etag}"', "ChecksumCRC64NVME": b64crc(crc64(data))}

    def _op_UploadPartCopy(
        self,
        UploadId: str,
        PartNumber: int,
        CopySource: Any,
        CopySourceRange: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        up = self._upload(UploadId)
        src = self._find(*_copy_source(CopySource))
        self._readable(src)
        data = src.data
        if CopySourceRange:
            start, end = (int(x) for x in CopySourceRange.removeprefix("bytes=").split("-"))
            data = data[start : end + 1]
        etag = hashlib.md5(data).hexdigest()
        up.parts[PartNumber] = (data, etag)
        return {"CopyPartResult": {"ETag": f'"{etag}"', "LastModified": self.now()}}

    def _op_CompleteMultipartUpload(
        self,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
        ChecksumCRC64NVME: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        up = self._upload(UploadId)
        listed = MultipartUpload.get("Parts", [])
        numbers = [p["PartNumber"] for p in listed]
        if numbers != sorted(numbers) or not numbers:
            raise AwsError("InvalidPartOrder", 400)
        chunks: list[bytes] = []
        for i, p in enumerate(listed):
            if p["PartNumber"] not in up.parts:
                raise AwsError("InvalidPart", 400)
            data, etag = up.parts[p["PartNumber"]]
            if p["ETag"].strip('"') != etag:
                raise AwsError("InvalidPart", 400, "ETag mismatch")
            if i < len(listed) - 1 and len(data) < 5 * MiB:
                raise AwsError("EntityTooSmall", 400)
            chunks.append(data)
        data = b"".join(chunks)
        crc = crc64(data)
        if ChecksumCRC64NVME is not None and ChecksumCRC64NVME != b64crc(crc):
            raise AwsError("BadDigest", 400, "full-object CRC64NVME mismatch")
        b = self._bucket(Bucket)
        digest = hashlib.md5(b"".join(bytes.fromhex(up.parts[n][1]) for n in numbers))
        v = Version(
            self._new_vid(b),
            data,
            up.storage_class,
            up.metadata,
            up.content_type,
            last_modified=self.now(),
            etag=f"{digest.hexdigest()}-{len(numbers)}",
            crc=crc,
        )
        self._store(b, Key, v)
        del self.uploads[UploadId]
        self._event(b, "Object Created", Key, v, self.now(), reason="CompleteMultipartUpload")
        out = {"Bucket": Bucket, "Key": Key, "ETag": f'"{v.etag}"'}
        if b.versioning:
            out["VersionId"] = v.vid
        return out

    def _op_AbortMultipartUpload(self, UploadId: str, **_: Any) -> dict[str, Any]:
        self._upload(UploadId)
        del self.uploads[UploadId]
        return {}

    def _op_CopyObject(
        self,
        Bucket: str,
        Key: str,
        CopySource: Any,
        StorageClass: str = "STANDARD",
        MetadataDirective: str = "COPY",
        Metadata: dict[str, str] | None = None,
        ContentType: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        sb, sk, svid = _copy_source(CopySource)
        src = self._find(sb, sk, svid)
        self._readable(src)
        same = (sb, sk) == (Bucket, Key)
        if same and MetadataDirective == "COPY" and StorageClass == src.storage_class:
            raise AwsError(
                "InvalidRequest", 400, "copy to itself without changing metadata or class"
            )
        b = self._bucket(Bucket)
        meta = (
            dict(src.metadata)
            if MetadataDirective == "COPY"
            else {k.lower(): v for k, v in (Metadata or {}).items()}
        )
        v = Version(
            self._new_vid(b),
            src.data,
            StorageClass,
            meta,
            ContentType or src.content_type,
            dict(src.tags),
            last_modified=self.now(),
            etag=hashlib.md5(src.data).hexdigest(),
            crc=src.crc,
        )
        self._store(b, Key, v)
        self._event(b, "Object Created", Key, v, self.now(), reason="CopyObject")
        out: dict[str, Any] = {
            "CopyObjectResult": {
                "ETag": f'"{v.etag}"',
                "LastModified": v.last_modified,
                "ChecksumCRC64NVME": b64crc(v.crc),
            }
        }
        if b.versioning:
            out["VersionId"] = v.vid
        return out

    def _op_DeleteObject(
        self, Bucket: str, Key: str, VersionId: str | None = None, **_: Any
    ) -> dict[str, Any]:
        b = self._bucket(Bucket)
        chain = b.keys.get(Key, [])
        if VersionId is not None:
            kept = [v for v in chain if v.vid != VersionId]
            if len(kept) != len(chain):
                gone = next(v for v in chain if v.vid == VersionId)
                b.keys[Key] = kept
                self._event(
                    b,
                    "Object Deleted",
                    Key,
                    gone,
                    self.now(),
                    **{"deletion-type": "Permanently Deleted"},
                )
            return {"VersionId": VersionId}
        if not b.versioning:
            if chain:
                del b.keys[Key]
                self._event(
                    b,
                    "Object Deleted",
                    Key,
                    None,
                    self.now(),
                    **{"deletion-type": "Permanently Deleted"},
                )
            return {}
        marker = Version(uuid.uuid4().hex, last_modified=self.now(), marker=True)
        chain.append(marker)
        b.keys[Key] = chain
        self._event(
            b,
            "Object Deleted",
            Key,
            marker,
            self.now(),
            **{"deletion-type": "Delete Marker Created"},
        )
        return {"DeleteMarker": True, "VersionId": marker.vid}

    # --- S3: reads ----------------------------------------------------------

    def _op_HeadObject(
        self,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
        ChecksumMode: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        v = self._find(Bucket, Key, VersionId, head=True)
        return self._object_fields(v, ChecksumMode == "ENABLED")

    def _op_GetObject(
        self,
        Bucket: str,
        Key: str,
        VersionId: str | None = None,
        Range: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        v = self._find(Bucket, Key, VersionId)
        self._readable(v)
        data = v.data
        out = self._object_fields(v, False)
        if Range:
            start_s, end_s = Range.removeprefix("bytes=").split("-")
            start = int(start_s)
            end = min(int(end_s), len(data) - 1)
            if start >= len(data):
                raise AwsError("InvalidRange", 416, "The requested range is not satisfiable")
            data = data[start : end + 1]
            out["ContentRange"] = f"bytes {start}-{end}/{len(v.data)}"
        out["ContentLength"] = len(data)
        body = FakeBody(data, self.body_fail_after)
        self.bodies.append(body)
        out["Body"] = body
        return out

    def _current(self, b: Bucket, prefix: str) -> list[tuple[str, Version]]:
        return sorted(
            (k, chain[-1])
            for k, chain in b.keys.items()
            if k.startswith(prefix) and chain and not chain[-1].marker
        )

    def _op_ListObjectsV2(
        self,
        Bucket: str,
        Prefix: str = "",
        MaxKeys: int = 1000,
        ContinuationToken: str | None = None,
        OptionalObjectAttributes: list[str] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        b = self._bucket(Bucket)
        items = self._current(b, Prefix)
        if ContinuationToken:
            after = base64.b64decode(ContinuationToken).decode()
            items = [(k, v) for k, v in items if k > after]
        page, more = items[:MaxKeys], len(items) > MaxKeys
        contents = []
        for k, v in page:
            c: dict[str, Any] = {
                "Key": k,
                "LastModified": v.last_modified,
                "ETag": f'"{v.etag}"',
                "Size": len(v.data),
                "StorageClass": v.storage_class,
            }
            if "RestoreStatus" in (OptionalObjectAttributes or []) and v.restore:
                status: dict[str, Any] = {"IsRestoreInProgress": v.restore.ongoing}
                if v.restore.expiry is not None:
                    status["RestoreExpiryDate"] = v.restore.expiry
                c["RestoreStatus"] = status
            contents.append(c)
        out: dict[str, Any] = {
            "IsTruncated": more,
            "Contents": contents,
            "KeyCount": len(contents),
            "Prefix": Prefix,
        }
        if more:
            out["NextContinuationToken"] = base64.b64encode(page[-1][0].encode()).decode()
        return out

    def _op_ListObjectVersions(
        self,
        Bucket: str,
        Prefix: str = "",
        MaxKeys: int = 1000,
        KeyMarker: str | None = None,
        VersionIdMarker: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        b = self._bucket(Bucket)
        flat = [
            (k, v, i == len(chain) - 1)
            for k in sorted(b.keys)
            if k.startswith(Prefix)
            for chain in [b.keys[k]]
            for i, v in reversed(list(enumerate(chain)))
        ]
        if KeyMarker is not None:
            at = next(
                (
                    i
                    for i, (k, v, _) in enumerate(flat)
                    if k == KeyMarker and v.vid == VersionIdMarker
                ),
                None,
            )
            flat = flat[at + 1 :] if at is not None else [x for x in flat if x[0] > KeyMarker]
        page, more = flat[:MaxKeys], len(flat) > MaxKeys
        versions, markers = [], []
        for k, v, latest in page:
            row: dict[str, Any] = {
                "Key": k,
                "VersionId": v.vid,
                "IsLatest": latest,
                "LastModified": v.last_modified,
            }
            if v.marker:
                markers.append(row)
            else:
                row.update(ETag=f'"{v.etag}"', Size=len(v.data), StorageClass=v.storage_class)
                versions.append(row)
        out: dict[str, Any] = {"IsTruncated": more, "Versions": versions, "DeleteMarkers": markers}
        if more:
            out["NextKeyMarker"], out["NextVersionIdMarker"] = page[-1][0], page[-1][1].vid
        return out

    # --- S3: restore --------------------------------------------------------

    def _op_RestoreObject(
        self,
        Bucket: str,
        Key: str,
        RestoreRequest: dict[str, Any],
        VersionId: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        b = self._bucket(Bucket)
        v = self._find(Bucket, Key, VersionId)
        tier = RestoreRequest.get("GlacierJobParameters", {}).get("Tier", "Standard")
        days = RestoreRequest.get("Days")
        if v.storage_class in ARCHIVED:
            if days is None:
                raise AwsError("MalformedXML", 400, "Days is required")
            source = v.storage_class
        elif v.archive_status:
            if days is not None:
                raise AwsError("InvalidArgument", 400, "no Days for Intelligent-Tiering")
            source = v.archive_status
        else:
            raise AwsError("InvalidObjectState", 403, "Restore is not allowed for this class")
        if (source, tier) not in RESTORE_HOURS:
            raise AwsError("InvalidArgument", 400, f"{tier} is not offered for {source}")
        if v.restore is not None and v.restore.ongoing:
            raise AwsError("RestoreAlreadyInProgress", 409)
        if v.restore is not None and v.restore.expiry is not None and days is not None:
            # A live restored copy: S3 resets the expiry from now (200 OK).
            v.restore.expiry = next_midnight(self.now() + timedelta(days=days))
            return {}
        v.restore = Restore(True, tier, days)
        now = self.now()
        self._event(
            b, "Object Restore Initiated", Key, v, now, **{"source-storage-class": v.storage_class}
        )
        self._later(
            now + timedelta(hours=RESTORE_HOURS[(source, tier)]), self._completer(b, Key, v)
        )
        return {}

    def _completer(self, b: Bucket, key: str, v: Version) -> Callable[[datetime], None]:
        def done(at: datetime) -> None:
            assert v.restore is not None
            if v.archive_status:
                # Intelligent-Tiering: back in the frequent tier for good.
                v.restore, v.archive_status = None, None
                self._event(
                    b,
                    "Object Restore Completed",
                    key,
                    v,
                    at,
                    **{"source-storage-class": v.storage_class},
                )
                return
            v.restore.ongoing = False
            v.restore.expiry = next_midnight(at + timedelta(days=v.restore.days or 0))
            self._event(
                b,
                "Object Restore Completed",
                key,
                v,
                at,
                **{
                    "source-storage-class": v.storage_class,
                    "restore-expiry-time": v.restore.expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            self._later(v.restore.expiry + self.expiry_lag, self._expirer(b, key, v))

        return done

    def _expirer(self, b: Bucket, key: str, v: Version) -> Callable[[datetime], None]:
        def gone(at: datetime) -> None:
            if v.restore is None or v.restore.expiry is None or at < v.restore.expiry:
                return  # re-restored since; a later timer handles it
            if at < v.restore.expiry + self.expiry_lag:
                self._later(v.restore.expiry + self.expiry_lag, gone)
                return
            v.restore = None
            self._event(
                b, "Object Restore Expired", key, v, at, **{"source-storage-class": v.storage_class}
            )

        return gone

    def it_archive(self, bucket: str, key: str, tier: str = "ARCHIVE_ACCESS") -> None:
        """Move the current version of an Intelligent-Tiering object to an
        archive access tier, as S3 does after the configured days unread."""
        v = self._find(bucket, key, None)
        assert v.storage_class == "INTELLIGENT_TIERING"
        v.archive_status = tier

    # --- S3: tags -------------------------------------------------------------

    # --- S3 Control: Batch Operations (restore waves) ------------------------

    def _op_CreateJob(
        self,
        AccountId: str,
        Operation: dict[str, Any],
        Manifest: dict[str, Any],
        ClientRequestToken: str,
        **_: Any,
    ) -> dict[str, Any]:
        """Runs the job at once: each manifest row is a RestoreObject."""
        etag = Manifest["Location"].get("ETag")
        if self.batch_token_etags.setdefault(ClientRequestToken, etag) != etag:
            # VERIFY (real-aws-checks.md): a token used again with other parameters.
            raise AwsError("IdempotencyException", 409, "the token was used for another request")
        if ClientRequestToken in self.batch_tokens:
            return {"JobId": self.batch_tokens[ClientRequestToken]}
        arn = Manifest["Location"]["ObjectArn"].removeprefix("arn:aws:s3:::")
        mbucket, mkey = arn.split("/", 1)
        fields = Manifest["Spec"]["Fields"]
        rows = self._find(mbucket, mkey, None).data.decode().splitlines()
        if "S3ComputeObjectChecksum" in Operation:
            return self._checksum_job(rows, fields, ClientRequestToken, Report=_["Report"])
        op = Operation.get("S3InitiateRestoreObject")
        if op is None:
            raise AwsError("InvalidRequest", 400, "the fake runs restore and checksum jobs only")
        ok = failed = 0
        for line in rows:
            if not line:
                continue
            row = dict(zip(fields, line.split(","), strict=False))
            try:
                self._op_RestoreObject(
                    Bucket=row["Bucket"], Key=unquote(row["Key"]),
                    VersionId=row.get("VersionId") or None,
                    RestoreRequest={"Days": op["ExpirationInDays"],
                                    "GlacierJobParameters": {
                                        "Tier": op["GlacierJobTier"].title()}},
                )  # fmt: skip
                ok += 1
            except AwsError:
                failed += 1
        job_id = f"job-{next(self._seq):08x}"
        self.batch_tokens[ClientRequestToken] = job_id
        self.batch_jobs[job_id] = {"JobId": job_id, "Status": "Complete", "ProgressSummary": {
            "TotalNumberOfTasks": ok + failed, "NumberOfTasksSucceeded": ok,
            "NumberOfTasksFailed": failed}}  # fmt: skip
        return {"JobId": job_id}

    def _checksum_job(
        self, rows: list[str], fields: list[str], token: str, Report: dict[str, Any]
    ) -> dict[str, Any]:
        """Compute checksum: every object's full-object CRC64NVME (stored or
        not) in an all-tasks completion report, as S3 writes it.

        Verified against a real `S3ComputeObjectChecksum` report (run
        rr-202611150626-34b3, 2026-11-14 PST): the result message is the
        seventh column, with the field names below (`ETag` among them); a
        succeeded row has 200 in the fifth column and an empty sixth; and the
        key column is form-encoded whatever the job's manifest used.
        `manifest.json` carries each results file's `MD5Checksum`.

        The key column, character by character, verified on real S3 (run
        rr-202611150718-62e9): a space is `+`, a literal plus `%2B`, a percent
        sign `%25` (so a name that already looks encoded is encoded again), a
        comma `%2C`, non-ASCII its UTF-8 bytes in `%XX`, and slashes are kept.
        That is exactly `quote_plus(key, safe="/")`. Not verified: a failed
        row's columns."""
        job_id = f"job-{next(self._seq):08x}"
        out = io.StringIO()
        w = csv.writer(out)
        ok = failed = 0
        for line in rows:
            if not line:
                continue
            row = dict(zip(fields, line.split(","), strict=False))
            key = unquote(row["Key"])
            # As real S3 writes it (docstring), not the manifest's spelling.
            report_key = quote_plus(key, safe="/")
            try:
                v = self._find(row["Bucket"], key, row.get("VersionId") or None)
                if key in self.checksum_refused:
                    raise AwsError("InvalidRequest", 400, "SSE-C")
                crc = v.crc.to_bytes(8, "big")
                msg = json.dumps({
                    "checksum_base64": base64.b64encode(crc).decode(), "ETag": v.etag,
                    "checksumAlgorithm": "CRC64NVME", "checksumType": "FULL_OBJECT",
                    "checksum_hex": crc.hex().upper()})  # fmt: skip
                w.writerow([row["Bucket"], report_key, row.get("VersionId", ""), "succeeded", 200,
                            "", msg])  # fmt: skip
                ok += 1
            except AwsError as e:
                w.writerow([row["Bucket"], report_key, row.get("VersionId", ""), "failed", 200,
                            "PermanentFailure", f"error details: {e.code}"])  # fmt: skip
                failed += 1
        rbucket = Report["Bucket"].removeprefix("arn:aws:s3:::")
        base = f"{Report['Prefix'].rstrip('/')}/job-{job_id}"
        results = f"{base}/results/{hashlib.sha1(job_id.encode()).hexdigest()}.csv"
        body = out.getvalue().encode()
        self._op_PutObject(Bucket=rbucket, Key=results, Body=body)
        manifest = {"Format": "Report_CSV_20180820", "Results": [
            {"TaskExecutionStatus": "succeeded", "Bucket": rbucket,
             "MD5Checksum": hashlib.md5(body).hexdigest(), "Key": results}],
            "ReportSchema": "Bucket, Key, VersionId, TaskStatus, ErrorCode, HTTPStatusCode,"
                            " ResultMessage"}  # fmt: skip
        self._op_PutObject(Bucket=rbucket, Key=f"{base}/manifest.json",
                           Body=json.dumps(manifest).encode())  # fmt: skip
        status = "Complete"
        if self.checksum_jobs_to_fail:
            self.checksum_jobs_to_fail -= 1
            status = "Failed"
        self.batch_tokens[token] = job_id
        self.batch_jobs[job_id] = {"JobId": job_id, "Status": status, "ProgressSummary": {
            "TotalNumberOfTasks": ok + failed, "NumberOfTasksSucceeded": ok,
            "NumberOfTasksFailed": failed}}  # fmt: skip
        return {"JobId": job_id}

    def _op_DescribeJob(self, AccountId: str, JobId: str, **_: Any) -> dict[str, Any]:
        job = self.batch_jobs.get(JobId)
        if job is None:
            raise AwsError("NoSuchJob", 404)
        return {"Job": dict(job)}

    def _op_PutObjectTagging(
        self, Bucket: str, Key: str, Tagging: dict[str, Any], VersionId: str | None = None, **_: Any
    ) -> dict[str, Any]:
        v = self._find(Bucket, Key, VersionId)
        tags = {t["Key"]: t["Value"] for t in Tagging.get("TagSet", [])}
        if len(tags) > 10:
            raise AwsError("BadRequest", 400, "Object tags cannot be greater than 10")
        v.tags = tags
        return {"VersionId": v.vid}

    def _op_GetObjectTagging(
        self, Bucket: str, Key: str, VersionId: str | None = None, **_: Any
    ) -> dict[str, Any]:
        v = self._find(Bucket, Key, VersionId)
        return {"VersionId": v.vid, "TagSet": [{"Key": k, "Value": t} for k, t in v.tags.items()]}

    # --- SQS ------------------------------------------------------------------

    def _queue(self, url: str) -> list[Message]:
        if url not in self.queues:
            raise AwsError("AWS.SimpleQueueService.NonExistentQueue", 400)
        return self.queues[url]

    def _op_ReceiveMessage(
        self,
        QueueUrl: str,
        MaxNumberOfMessages: int = 1,
        VisibilityTimeout: int = 30,
        **_: Any,
    ) -> dict[str, Any]:
        now = self.now()
        out = []
        for m in self._queue(QueueUrl):
            if len(out) >= MaxNumberOfMessages:
                break
            if m.visible_at > now:
                continue
            m.handle = uuid.uuid4().hex  # each receive gets a new receipt handle
            m.visible_at = now + timedelta(seconds=VisibilityTimeout)
            m.receives += 1
            out.append(
                {
                    "MessageId": m.mid,
                    "ReceiptHandle": m.handle,
                    "MD5OfBody": hashlib.md5(m.body.encode()).hexdigest(),
                    "Body": m.body,
                }
            )
        return {"Messages": out} if out else {}

    def _op_DeleteMessageBatch(self, QueueUrl: str, Entries: list[dict[str, str]], **_: Any):
        q = self._queue(QueueUrl)
        ok, failed = [], []
        for e in Entries:
            hit = next((m for m in q if m.handle == e["ReceiptHandle"]), None)
            if hit is None:
                failed.append(
                    {"Id": e["Id"], "SenderFault": True, "Code": "ReceiptHandleIsInvalid"}
                )
                continue
            q.remove(hit)
            ok.append({"Id": e["Id"]})
        return {"Successful": ok, "Failed": failed}

    def _op_ChangeMessageVisibility(
        self, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int, **_: Any
    ) -> dict[str, Any]:
        for m in self._queue(QueueUrl):
            if m.handle == ReceiptHandle:
                m.visible_at = self.now() + timedelta(seconds=VisibilityTimeout)
                return {}
        raise AwsError("ReceiptHandleIsInvalid", 400)

    # --- STS ------------------------------------------------------------

    def _op_AssumeRoleWithWebIdentity(
        self,
        RoleArn: str,
        RoleSessionName: str,
        WebIdentityToken: str,
        DurationSeconds: int = 3600,
        **_: Any,
    ) -> dict[str, Any]:
        self.sts_calls.append((RoleArn, RoleSessionName, WebIdentityToken))
        if WebIdentityToken in self.refused_tokens:
            raise AwsError("InvalidIdentityToken", 400, "token refused")
        n = len(self.sts_calls)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIAFAKE{n}",
                "SecretAccessKey": f"secret-{n}",
                "SessionToken": f"session-{n}",
                "Expiration": self.now() + timedelta(seconds=DurationSeconds),
            },
            "AssumedRoleUser": {"Arn": f"{RoleArn}/{RoleSessionName}", "AssumedRoleId": "x"},
        }


def _body(body: Any) -> bytes:
    """botocore hands a bytes Body over as a file-like object."""
    if hasattr(body, "read"):
        body.seek(0)
        return body.read()
    return bytes(body)


def _marker(chain: list[Version]) -> dict[str, str]:
    return {"x-amz-delete-marker": "true"} if chain and chain[-1].marker else {}


def _copy_source(src: Any) -> tuple[str, str, str | None]:
    """CopySource as the dict the backend passes, or the header string."""
    if isinstance(src, dict):
        return src["Bucket"], src["Key"], src.get("VersionId")
    path, _, query = str(src).lstrip("/").partition("?versionId=")
    bucket, _, key = path.partition("/")
    return bucket, unquote(key), query or None
