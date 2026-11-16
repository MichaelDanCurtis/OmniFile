"""The Amazon S3 backend (M5): `StorageBackend` over the AWS SDK (aiobotocore).

S3 is a migration source or destination (decision 23); Azure stays the home
backend. What this implements, per `ARCHITECTURE.md` section 3 and the
verified S3 column ([V] docs/research/2026-09-26-verification.md):

- **put:** one PutObject up to `part_size`, else a multipart upload; either
  way with a full-object CRC64NVME computed here and checked by S3, so the
  object's stored checksum is the one the catalog keeps.
- **reads:** ranged GetObject. An archived object without a live restored
  copy raises `NotRestored`, and so does a restored copy past its expiry on
  the backend's clock (S3 removes it asynchronously).
- **tiers:** a class change is CopyObject onto the same key with the new
  StorageClass (UploadPartCopy above 5 GiB). On a versioned bucket that adds
  a version; the returned ref names it.
- **restores:** RestoreObject with `Days` (none for Intelligent-Tiering
  archive tiers), within a token bucket that keeps this process under the
  account's 1,000 restore requests per second [V]. Bulk restore waves through
  Batch Operations, and the catalog's expiry tracking, are the recall
  orchestrator's (later in M5).
- **events:** `read_changes` pulls EventBridge S3 events from SQS (see
  events.py). No push: nothing is delivered into the private network [A].
- **credentials:** no keys anywhere. The service assumes its role with
  STS AssumeRoleWithWebIdentity and its managed identity's Entra token
  (credentials.py). The edge gets a pointer and signs with its own IAM
  Roles Anywhere session; a presigned URL only as the operator's fallback
  [A].
- **TLS:** 1.2 allowed, as PrivateLink interface endpoints require [V]; the
  config's context (`S3Config.ssl_context`) pins 1.2 by default and checks
  certificates against ``ca_bundle``, else system trust.

S3 Object Lock is not built (S3 is a migration source), so `worm` is False
and retention calls raise `UnsupportedOperation` [W].

**Read-only outside OmniFile's own keys.** A migration source is the
client's bucket: OmniFile never deletes, overwrites, re-tiers, re-tags or
rewrites an object there (catalog-seeding.md section 10). The catalog asks
first (`catalog.source_readonly`); this backend refuses too, as a second
line: `put`, `copy_object` (its destination), `set_class` and
`set_metadata` (in-place copies), `set_tags`, `delete` and a write
credential raise `WriteRefused` for any key that `S3Config.owns` doesn't
name (the staging prefix, the Batch Operations manifests, an owned bucket),
and log it. Restores and reads are not writes to an object, and go ahead.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import io
import itertools
import json
import logging
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote_plus

from aiobotocore.config import AioConfig
from azure.storage.extensions import checksums

from ... import metrics
from ..base import (
    BackendError,
    BackendKind,
    Capabilities,
    ChangePage,
    ChangeRecord,
    Clock,
    CommittedBlock,
    CredentialMode,
    EdgeAccess,
    EdgeCredential,
    ListedVersion,
    LogicalTier,
    NotRestored,
    ObjectHead,
    ObjectNotFound,
    ObjectPage,
    ObjectRef,
    ObjectVersion,
    RestoreHandle,
    RestoreSpeed,
    RestoreState,
    RestoreStatus,
    SystemClock,
    Throttled,
    UnsupportedOperation,
    VersionPage,
    WriteRefused,
)
from . import events
from .capabilities import (
    ARCHIVED_CLASSES,
    IT_DEEP_ARCHIVE_ACCESS,
    NATIVE_TO_LOGICAL,
    S3_DEEP_ARCHIVE,
    S3_EXPEDITED_MAX_BYTES,
    S3_INTELLIGENT_TIERING,
    S3_STANDARD,
    capabilities_for,
    native_class_for,
)
from .config import AwsDeployment, S3Config
from .credentials import TokenProvider, _tls_session_cls, entra_token_provider, session_for
from .errors import code_of, translate

log = logging.getLogger("omnifile_service.backends.s3")

READ_CHUNK = 1024 * 1024
_SPEED_TIER = {
    RestoreSpeed.EXPEDITED: "Expedited",
    RestoreSpeed.STANDARD: "Standard",
    RestoreSpeed.BULK: "Bulk",
}
_ONGOING = re.compile(r'ongoing-request\s*=\s*"(true|false)"')
_EXPIRY = re.compile(r'expiry-date\s*=\s*"([^"]+)"')


def crc64_b64(crc: int) -> str:
    """S3's x-amz-checksum-crc64nvme: base64 of the big-endian CRC [V]."""
    return base64.b64encode(crc.to_bytes(8, "big")).decode()


def parse_restore(raw: str | None) -> tuple[bool | None, datetime | None]:
    """The x-amz-restore header -> (ongoing, expiry). (None, None) if absent."""
    if not raw:
        return None, None
    ongoing = _ONGOING.search(raw)
    expiry = _EXPIRY.search(raw)
    return (
        ongoing.group(1) == "true" if ongoing else None,
        parsedate_to_datetime(expiry.group(1)) if expiry else None,
    )


class RestoreBudget:
    """A token bucket on the backend clock: at most `rate` restore requests
    per second, bursting to one second's worth. Above it, `Throttled`, as
    S3's ThrottlingException would be [V]. Per process; the recall
    orchestrator shares the account's budget across workers."""

    def __init__(self, rate: float, clock: Clock) -> None:
        self.rate = rate
        self._clock = clock
        self._tokens = rate
        self._at = clock.now()

    def take(self) -> None:
        now = self._clock.now()
        elapsed = max(0.0, (now - self._at).total_seconds())
        self._tokens = min(self.rate, self._tokens + elapsed * self.rate)
        self._at = now
        if self._tokens < 1:
            wait = timedelta(seconds=(1 - self._tokens) / self.rate)
            metrics.throttled("s3", "restore_budget")
            raise Throttled("S3 restore request budget spent", retry_after=wait)
        self._tokens -= 1


class S3Backend:
    """One S3 binding. Safe for concurrent use within one event loop; SDK
    clients are made per event loop on first use."""

    def __init__(
        self,
        backend_id: int,
        config: S3Config,
        *,
        clock: Clock | None = None,
        on_client: Callable[[Any], None] | None = None,
        session: Any = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        self._id = backend_id
        self.config = config
        self._clock = clock or SystemClock()
        self._caps = capabilities_for(config)
        self._session = session or session_for(
            config, token_provider=token_provider, clock=self._clock, on_client=on_client
        )
        self._on_client = on_client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stack: AsyncExitStack | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._s3: Any = None
        self._sqs: Any = None
        self._control: Any = None
        self._presign: Any = None
        self._budget = RestoreBudget(config.restore_rate_per_s, self._clock)
        #: read_changes cursor -> SQS receipt handles, deleted when that cursor
        #: comes back (the caller has kept the page). Lost on restart, so SQS
        #: redelivers after the visibility timeout: at least once.
        self._unacked: dict[str, list[tuple[str, str]]] = {}
        self._seq = itertools.count(1)

    @classmethod
    def from_config(
        cls,
        backend_id: int,
        raw: Mapping[str, Any],
        *,
        clock: Clock | None = None,
        private_network: bool | None = None,
        credential: Any = None,
        deployment: AwsDeployment | None = None,
    ) -> S3Backend:
        """A backend for one catalog row: the row's harmless fields merged
        into the operator's account settings (`S3Config.for_backend`).
        `credential` is the service's Azure credential (its managed
        identity), the source of the web identity token."""
        config = S3Config.for_backend(raw, private_network=private_network, deployment=deployment)
        token = None
        if config.credentials == "web_identity":
            assert config.web_identity_audience is not None  # S3Config checks it
            token = entra_token_provider(credential, config.web_identity_audience)
        return cls(backend_id, config, clock=clock, token_provider=token)

    # --- identity ---------------------------------------------------------

    @property
    def backend_id(self) -> int:
        return self._id

    @property
    def kind(self) -> BackendKind:
        return BackendKind.S3

    @property
    def clock(self) -> Clock:
        return self._clock

    def capabilities(self) -> Capabilities:
        return self._caps

    # --- clients ----------------------------------------------------------

    def _aio_config(self) -> AioConfig:
        return AioConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if self.config.path_style else "virtual"},
            retries={"max_attempts": self.config.max_attempts, "mode": "standard"},
            # Checksums are ours: CRC64NVME computed here, never botocore's
            # default CRC32 (decision 24 keeps one algorithm end to end).
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            http_session_cls=_tls_session_cls(self.config),
        )

    async def _clients(self) -> tuple[Any, Any]:
        loop = asyncio.get_running_loop()
        if self._loop is loop and self._s3 is not None:
            return self._s3, self._sqs
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        async with self._lock:  # concurrent first calls build one pair
            if self._loop is loop and self._s3 is not None:
                return self._s3, self._sqs
            return await self._open_clients(loop)

    async def _open_clients(self, loop: asyncio.AbstractEventLoop) -> tuple[Any, Any]:
        stack = AsyncExitStack()
        common: dict[str, Any] = {
            "region_name": self.config.region,
            "config": self._aio_config(),
            "verify": self.config.ca_bundle or True,
        }
        s3 = await stack.enter_async_context(
            self._session.create_client("s3", endpoint_url=self.config.endpoint_url, **common)
        )
        sqs = await stack.enter_async_context(
            self._session.create_client("sqs", endpoint_url=self.config.sqs_endpoint_url, **common)
        )
        presign = s3
        if self.config.edge_endpoint:
            # URLs the edge reads are signed for its own endpoint's host;
            # presigning makes no call, so this client never connects.
            presign = await stack.enter_async_context(
                self._session.create_client(
                    "s3", endpoint_url=f"https://{self.config.edge_endpoint}", **common
                )
            )
        control = None
        if self.config.batch is not None:
            control = await stack.enter_async_context(
                self._session.create_client(
                    "s3control", endpoint_url=self.config.batch.control_endpoint_url, **common
                )
            )
        if self._on_client is not None:
            self._on_client(s3)
            self._on_client(sqs)
            if control is not None:
                self._on_client(control)
        # A client from an earlier loop can't be closed from this one; tests
        # run each call in its own loop, production runs one loop.
        self._loop, self._stack, self._s3, self._sqs = loop, stack, s3, sqs
        self._control, self._presign = control, presign
        return s3, sqs

    async def close(self) -> None:
        if self._stack is not None and self._loop is asyncio.get_running_loop():
            await self._stack.aclose()
        self._loop = self._stack = self._s3 = self._sqs = self._control = None
        self._presign = None

    async def _s3_call(self, op: str, what: str, *, reading: bool = False, **kw: Any) -> Any:
        s3, _ = await self._clients()
        try:
            return await getattr(s3, op)(**kw)
        except Exception as e:  # noqa: BLE001 - every SDK failure maps to a contract error
            err = translate(e, what, reading=reading)
            if isinstance(err, Throttled):
                metrics.throttled("s3", op)
            raise err from e

    async def _sqs_call(self, op: str, what: str, **kw: Any) -> Any:
        _, sqs = await self._clients()
        try:
            return await getattr(sqs, op)(**kw)
        except Exception as e:  # noqa: BLE001
            err = translate(e, what)
            if isinstance(err, Throttled):
                metrics.throttled("s3", op)
            raise err from e

    def _writable(self, op: str, container: str, key: str) -> str:
        """The bucket, if OmniFile may write `key` in it; else `WriteRefused`."""
        bucket = self._bucket(container)
        if not self.config.owns(bucket, key):
            log.error(
                "refused a write to an S3 object OmniFile doesn't own",
                extra={"op": op, "backend_id": self._id, "bucket": bucket, "key": key},
            )
            raise WriteRefused(
                f"{op} s3://{bucket}/{key}: refused; OmniFile never writes to a client's bucket"
                " (only its staging prefix and Batch Operations manifests)"
            )
        return bucket

    def _bucket(self, container: str) -> str:
        if not self.config.allows(container):
            raise BackendError(f"bucket {container!r} is not part of this binding")
        return container

    @staticmethod
    def _what(ref: ObjectRef) -> str:
        return f"s3://{ref.container}/{ref.key}" + (
            f"?versionId={ref.version_id}" if ref.version_id else ""
        )

    @staticmethod
    def _version_kw(ref: ObjectRef) -> dict[str, str]:
        return {"VersionId": ref.version_id} if ref.version_id else {}

    def _ref(self, bucket: str, key: str, version_id: str | None) -> ObjectRef:
        # "null" is S3's version id on unversioned buckets; it names no version.
        vid = version_id if version_id and version_id != "null" else None
        return ObjectRef(self._id, bucket, key, vid)

    # --- state ------------------------------------------------------------

    def _restore_state(
        self, native: str, archive_status: str | None, restore: str | None
    ) -> tuple[RestoreState, datetime | None]:
        ongoing, expiry = parse_restore(restore)
        if native in ARCHIVED_CLASSES:
            if ongoing:
                return RestoreState.IN_PROGRESS, None
            if expiry is not None:
                if self._clock.now() < expiry:
                    return RestoreState.COMPLETED, expiry
                return RestoreState.EXPIRED, None
            return RestoreState.ARCHIVED, None
        if native == S3_INTELLIGENT_TIERING and archive_status:
            # Intelligent-Tiering archive tiers: a restore moves the object back
            # to the frequent tier for good, so there is no expiry [V].
            return (RestoreState.IN_PROGRESS if ongoing else RestoreState.ARCHIVED), None
        return RestoreState.NOT_NEEDED, None

    def _head_from(self, ref: ObjectRef, r: Mapping[str, Any]) -> ObjectHead:
        native = r.get("StorageClass") or S3_STANDARD
        state, expires = self._restore_state(native, r.get("ArchiveStatus"), r.get("Restore"))
        crc = None
        if r.get("ChecksumCRC64NVME"):
            try:
                crc = base64.b64decode(r["ChecksumCRC64NVME"])
            except (binascii.Error, ValueError):
                crc = None
        etag = (r.get("ETag") or "").strip('"') or None
        md5 = None
        # A single-part upload's ETag is its MD5, unless SSE-KMS/C encrypted it.
        if (
            etag
            and re.fullmatch(r"[0-9a-f]{32}", etag)
            and r.get("ServerSideEncryption") in (None, "AES256")
            and not r.get("SSECustomerAlgorithm")
        ):
            md5 = bytes.fromhex(etag)
        return ObjectHead(
            ref=self._ref(ref.container, ref.key, r.get("VersionId") or ref.version_id),
            size=int(r.get("ContentLength", r.get("Size", 0)) or 0),
            native_class=native,
            logical_tier=NATIVE_TO_LOGICAL.get(native, LogicalTier.HOT),
            restore_state=state,
            restore_expires_at=expires,
            crc64nvme=crc if crc and len(crc) == 8 else None,
            md5=md5,
            etag=etag,
            last_modified=r.get("LastModified"),
            metadata=dict(r.get("Metadata") or {}),
        )

    async def _head_raw(self, ref: ObjectRef) -> tuple[ObjectHead, Mapping[str, Any]]:
        bucket = self._bucket(ref.container)
        r = await self._s3_call(
            "head_object",
            self._what(ref),
            Bucket=bucket,
            Key=ref.key,
            ChecksumMode="ENABLED",
            **self._version_kw(ref),
        )
        return self._head_from(ref, r), r

    # --- StorageBackend: objects ----------------------------------------

    async def head(self, ref: ObjectRef) -> ObjectHead:
        return (await self._head_raw(ref))[0]

    def _class_for(self, tier: LogicalTier, native_class: str | None = None) -> str:
        try:
            return native_class_for(self._caps, tier, native_class, self.config)
        except ValueError as e:
            raise UnsupportedOperation(str(e)) from None

    async def put(
        self,
        container: str,
        key: str,
        stream: AsyncIterable[bytes],
        tier: LogicalTier | None,
        metadata: Mapping[str, str],
        *,
        crc64nvme: bytes | None = None,
    ) -> ObjectRef:
        bucket = self._writable("put", container, key)
        what = f"s3://{bucket}/{key}"
        extra: dict[str, Any] = {"Metadata": dict(metadata)}
        if tier is not None:
            extra["StorageClass"] = self._class_for(tier)
        size = self.config.part_size
        buf = bytearray()
        crc = checksums.crc64.compute(b"", 0)
        upload_id: str | None = None
        parts: list[dict[str, Any]] = []

        async def send_part(data: bytes) -> None:
            nonlocal upload_id
            if upload_id is None:
                r = await self._s3_call(
                    "create_multipart_upload",
                    what,
                    Bucket=bucket,
                    Key=key,
                    ChecksumAlgorithm="CRC64NVME",
                    ChecksumType="FULL_OBJECT",
                    **extra,
                )
                upload_id = r["UploadId"]
            n = len(parts) + 1
            part_crc = crc64_b64(checksums.crc64.compute(data, 0))
            r = await self._s3_call(
                "upload_part",
                what,
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=n,
                Body=data,
                ChecksumAlgorithm="CRC64NVME",
                ChecksumCRC64NVME=part_crc,
            )
            parts.append({"PartNumber": n, "ETag": r["ETag"], "ChecksumCRC64NVME": part_crc})

        try:
            async for chunk in stream:
                if not chunk:
                    continue
                crc = checksums.crc64.compute(bytes(chunk), crc)
                buf += chunk
                while len(buf) >= size:
                    await send_part(bytes(buf[:size]))
                    del buf[:size]
            if crc64nvme is not None and crc64nvme != crc.to_bytes(8, "big"):
                raise BackendError(f"{what}: the bytes don't match the expected CRC64NVME")
            if upload_id is None:
                r = await self._s3_call(
                    "put_object",
                    what,
                    Bucket=bucket,
                    Key=key,
                    Body=bytes(buf),
                    ChecksumAlgorithm="CRC64NVME",
                    ChecksumCRC64NVME=crc64_b64(crc),
                    **extra,
                )
                return self._ref(bucket, key, r.get("VersionId"))
            if buf or not parts:
                await send_part(bytes(buf))
            r = await self._s3_call(
                "complete_multipart_upload",
                what,
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                ChecksumCRC64NVME=crc64_b64(crc),
                ChecksumType="FULL_OBJECT",
            )
            return self._ref(bucket, key, r.get("VersionId"))
        except BaseException:
            if upload_id is not None:
                await self._abort(bucket, key, upload_id)
            raise

    async def _abort(self, bucket: str, key: str, upload_id: str) -> None:
        try:
            await self._s3_call(
                "abort_multipart_upload",
                f"s3://{bucket}/{key}",
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
            )
        except BackendError as e:
            # A bucket lifecycle rule should abort stale uploads too (setup guide).
            log.warning("could not abort a multipart upload", extra={"reason": str(e)})

    async def get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        if length <= 0:
            return
        bucket = self._bucket(ref.container)
        what = self._what(ref)
        try:
            r = await self._s3_call(
                "get_object",
                what,
                reading=True,
                Bucket=bucket,
                Key=ref.key,
                Range=f"bytes={offset}-{offset + length - 1}",
                **self._version_kw(ref),
            )
        except BackendError as e:
            cause = e.__cause__
            if cause is not None and code_of_safe(cause) == "InvalidRange":
                return  # at or past the end: nothing to read
            raise
        body = r["Body"]
        try:
            native = r.get("StorageClass") or S3_STANDARD
            state, _ = self._restore_state(native, r.get("ArchiveStatus"), r.get("Restore"))
            if state in (RestoreState.EXPIRED, RestoreState.ARCHIVED, RestoreState.IN_PROGRESS):
                # S3 deletes an expired restored copy asynchronously and may
                # still serve it for a while; the clock decides [V].
                raise NotRestored(f"{what} has no live restored copy")
            while True:
                try:
                    chunk = await body.read(READ_CHUNK)
                except Exception as e:  # noqa: BLE001
                    raise translate(e, what, reading=True) from e
                if not chunk:
                    break
                yield chunk
        finally:
            await body.aclose()  # both aiobotocore bodies (aiohttp, httpx) have it

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        raise UnsupportedOperation("S3 keeps no committed block list")

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        bucket = self._bucket(container)
        kw: dict[str, Any] = {"ContinuationToken": cursor} if cursor else {}
        r = await self._s3_call(
            "list_objects_v2",
            f"s3://{bucket}/{prefix}",
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=self.config.list_page_size,
            OptionalObjectAttributes=["RestoreStatus"],
            **kw,
        )
        items = [self._listed_head(bucket, o) for o in r.get("Contents", [])]
        nxt = r.get("NextContinuationToken") if r.get("IsTruncated") else None
        return ObjectPage(items, nxt)

    def _listed_head(self, bucket: str, o: Mapping[str, Any]) -> ObjectHead:
        status = o.get("RestoreStatus") or {}
        restore = None
        if status.get("IsRestoreInProgress"):
            restore = 'ongoing-request="true"'
        elif status.get("RestoreExpiryDate"):
            exp: datetime = status["RestoreExpiryDate"]
            restore = f'ongoing-request="false", expiry-date="{exp:%a, %d %b %Y %H:%M:%S GMT}"'
        fields = {
            "StorageClass": o.get("StorageClass"),
            "Restore": restore,
            "Size": o.get("Size", 0),
            "ETag": o.get("ETag"),
            "LastModified": o.get("LastModified"),
            "VersionId": o.get("VersionId"),
        }
        return self._head_from(ObjectRef(self._id, bucket, o["Key"]), fields)

    async def _all_versions(self, bucket: str, key: str) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        kw: dict[str, Any] = {}
        while True:
            r = await self._s3_call(
                "list_object_versions", f"s3://{bucket}/{key}", Bucket=bucket, Prefix=key, **kw
            )
            for v in r.get("Versions", []):
                if v["Key"] == key:
                    out.append({**v, "_marker": False})
            for m in r.get("DeleteMarkers", []):
                if m["Key"] == key:
                    out.append({**m, "_marker": True})
            if not r.get("IsTruncated"):
                break
            kw = {"KeyMarker": r["NextKeyMarker"], "VersionIdMarker": r["NextVersionIdMarker"]}
            if r["NextKeyMarker"] != key:
                break
        out.sort(key=lambda v: (bool(v.get("IsLatest")), v["LastModified"]), reverse=True)
        return out

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        bucket = self._bucket(ref.container)
        found = await self._all_versions(bucket, ref.key)
        if not found:
            raise ObjectNotFound(self._what(ref))
        return [
            ObjectVersion(
                ref=ObjectRef(self._id, bucket, ref.key, v.get("VersionId")),
                is_current=bool(v.get("IsLatest")),
                is_delete_marker=v["_marker"],
                size=int(v.get("Size", 0)),
                last_modified=v["LastModified"],
                native_class=v.get("StorageClass") or S3_STANDARD,
            )
            for v in found
        ]

    async def list_all_versions(
        self, container: str, prefix: str, cursor: str | None
    ) -> VersionPage:
        """`VersionListing`: every stored version under `prefix` (not delete
        markers), for reconcile on versioned buckets."""
        bucket = self._bucket(container)
        kw: dict[str, Any] = {}
        if cursor:
            kw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        r = await self._s3_call(
            "list_object_versions",
            f"s3://{bucket}/{prefix}",
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=self.config.list_page_size,
            **kw,
        )
        items = [
            ListedVersion(self._listed_head(bucket, v), bool(v.get("IsLatest")))
            for v in r.get("Versions", [])
        ]
        nxt = None
        if r.get("IsTruncated"):
            nxt = base64.urlsafe_b64encode(
                json.dumps(
                    {"KeyMarker": r["NextKeyMarker"], "VersionIdMarker": r["NextVersionIdMarker"]}
                ).encode()
            ).decode()
        return VersionPage(items, nxt)

    # --- StorageBackend: copies and classes ------------------------------

    async def _copy(
        self,
        src: ObjectHead,
        raw: Mapping[str, Any],
        bucket: str,
        key: str,
        *,
        storage_class: str | None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        """CopyObject, or UploadPartCopy above `single_copy_limit` [V].

        `metadata=None` keeps the source's; otherwise it replaces it. The
        content type and tags are kept either way."""
        what = f"{self._what(src.ref)} -> s3://{bucket}/{key}"
        source = {"Bucket": src.ref.container, "Key": src.ref.key}
        if src.ref.version_id:
            source["VersionId"] = src.ref.version_id
        kw: dict[str, Any] = {"ChecksumAlgorithm": "CRC64NVME"}
        if storage_class is not None:
            kw["StorageClass"] = storage_class
        if raw.get("ContentType"):
            kw["ContentType"] = raw["ContentType"]
        if src.size <= self.config.single_copy_limit:
            if metadata is not None:
                kw["MetadataDirective"] = "REPLACE"
                kw["Metadata"] = dict(metadata)
            r = await self._s3_call(
                "copy_object", what, Bucket=bucket, Key=key, CopySource=source, **kw
            )
            return self._ref(bucket, key, r.get("VersionId"))
        kw.pop("ChecksumAlgorithm")
        tags = await self.get_tags(src.ref)
        r = await self._s3_call(
            "create_multipart_upload",
            what,
            Bucket=bucket,
            Key=key,
            Metadata=dict(src.metadata if metadata is None else metadata),
            ChecksumAlgorithm="CRC64NVME",
            ChecksumType="FULL_OBJECT",
            **kw,
        )
        upload_id = r["UploadId"]
        parts: list[dict[str, Any]] = []
        try:
            step = self.config.copy_part_size
            for n, start in enumerate(range(0, src.size, step), start=1):
                end = min(start + step, src.size) - 1
                p = await self._s3_call(
                    "upload_part_copy",
                    what,
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=n,
                    CopySource=source,
                    CopySourceRange=f"bytes={start}-{end}",
                )
                part = {"PartNumber": n, "ETag": p["CopyPartResult"]["ETag"]}
                if p["CopyPartResult"].get("ChecksumCRC64NVME"):
                    part["ChecksumCRC64NVME"] = p["CopyPartResult"]["ChecksumCRC64NVME"]
                parts.append(part)
            done = await self._s3_call(
                "complete_multipart_upload",
                what,
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                ChecksumType="FULL_OBJECT",
            )
        except BaseException:
            await self._abort(bucket, key, upload_id)
            raise
        new = self._ref(bucket, key, done.get("VersionId"))
        if tags:
            await self.set_tags(new, tags)
        return new

    def _readable_archive_check(self, head: ObjectHead, what: str) -> None:
        if head.restore_state in (
            RestoreState.ARCHIVED,
            RestoreState.IN_PROGRESS,
            RestoreState.EXPIRED,
        ):
            raise UnsupportedOperation(f"{what}: archived; restore it first (InvalidObjectState)")

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        self._writable("set_class", ref.container, ref.key)
        target = self._class_for(tier, native_class)
        head, raw = await self._head_raw(ref)
        if head.native_class == target and not raw.get("ArchiveStatus"):
            return head.ref
        self._readable_archive_check(head, self._what(ref))
        return await self._copy(head, raw, ref.container, ref.key, storage_class=target)

    async def copy_object(
        self,
        src: ObjectRef,
        dst_container: str,
        dst_key: str,
        tier: LogicalTier | None = None,
    ) -> ObjectRef:
        dst = self._writable("copy_object", dst_container, dst_key)
        head, raw = await self._head_raw(src)
        self._readable_archive_check(head, self._what(src))
        cls = self._class_for(tier) if tier is not None else S3_STANDARD
        return await self._copy(head, raw, dst, dst_key, storage_class=cls)

    # --- StorageBackend: restore ----------------------------------------

    def _typical_hours(self, speed: RestoreSpeed, native: str) -> float | None:
        for opt in self._caps.restore_options:
            if opt.speed is speed and native in opt.from_classes:
                return opt.typical_hours[1]
        return None

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        if speed not in _SPEED_TIER:
            raise UnsupportedOperation(f"S3 restores are Expedited, Standard or Bulk, not {speed}")
        head, raw = await self._head_raw(ref)
        what = self._what(head.ref)
        it_tier = raw.get("ArchiveStatus") if head.native_class == S3_INTELLIGENT_TIERING else None
        if head.native_class in ARCHIVED_CLASSES:
            if days is None or days < 1:
                raise UnsupportedOperation("RestoreObject from Glacier needs Days >= 1")
            request: dict[str, Any] = {"Days": days}
            deep = head.native_class == S3_DEEP_ARCHIVE
        elif it_tier:
            if days is not None:
                raise UnsupportedOperation("Intelligent-Tiering archive restores take no Days")
            request = {}
            deep = it_tier == IT_DEEP_ARCHIVE_ACCESS
        else:
            raise UnsupportedOperation(f"{what} ({head.native_class}) needs no restore")
        if speed is RestoreSpeed.EXPEDITED:
            if deep:
                raise UnsupportedOperation("no Expedited restore from Deep Archive")
            if head.size >= S3_EXPEDITED_MAX_BYTES:
                raise UnsupportedOperation("Expedited restore is only for objects under 250 MB")
        request["GlacierJobParameters"] = {"Tier": _SPEED_TIER[speed]}
        self._budget.take()
        try:
            await self._s3_call(
                "restore_object",
                what,
                Bucket=head.ref.container,
                Key=ref.key,
                RestoreRequest=request,
                **self._version_kw(head.ref),
            )
        except BackendError as e:
            # Already running: the existing restore is the one to track.
            if e.__cause__ is None or code_of_safe(e.__cause__) != "RestoreAlreadyInProgress":
                raise
        return RestoreHandle(
            ref=head.ref,
            speed=speed,
            provider_ref=events.restore_ref(head.ref),
            requested_at=self._clock.now(),
            days=days,
        )

    # --- Batch Operations: restore waves (migration-engine.md section 8) -------

    async def _control_call(self, op: str, what: str, **kw: Any) -> Any:
        await self._clients()
        if self._control is None or self.config.batch is None:
            raise UnsupportedOperation("S3 Batch Operations are not configured (batch)")
        try:
            return await getattr(self._control, op)(AccountId=self.config.batch.account_id, **kw)
        except Exception as e:  # noqa: BLE001
            raise translate(e, what) from e

    async def batch_restore(
        self, refs: Sequence[ObjectRef], speed: RestoreSpeed, days: int, *, name: str
    ) -> str:
        """Restore `refs` in one Batch Operations job (S3InitiateRestoreObject):
        a CSV manifest in the batch manifest bucket, then CreateJob. Batch
        restores share the account's 1,000 requests/s with single ones [V].
        Glacier Flexible Retrieval and Deep Archive only: Intelligent-Tiering
        archive tiers take no `Days`, so they don't share a manifest with them.
        Returns the job id."""
        b = self.config.batch
        if b is None:
            raise UnsupportedOperation("S3 Batch Operations are not configured (batch)")
        if speed not in (RestoreSpeed.STANDARD, RestoreSpeed.BULK):
            raise UnsupportedOperation(f"batch restores are Standard or Bulk, not {speed}")
        if not refs:
            raise ValueError("a restore wave needs objects")
        key, etag, versioned = await self._manifest(refs, name)
        report = b.report_bucket or b.manifest_bucket
        out = await self._control_call(
            "create_job", f"batch restore {name}",
            ConfirmationRequired=False,
            Operation={"S3InitiateRestoreObject": {
                "ExpirationInDays": days, "GlacierJobTier": _SPEED_TIER[speed].upper()}},
            Manifest=self._manifest_spec(key, etag, versioned),
            Report={"Bucket": f"arn:aws:s3:::{report}", "Format": "Report_CSV_20180820",
                    "Enabled": True, "Prefix": self._report_prefix(name),
                    "ReportScope": "FailedTasksOnly"},
            Priority=b.priority,
            RoleArn=b.role_arn,
            ClientRequestToken=name,
            Description=f"OmniFile restore wave {name}"[:256],
        )  # fmt: skip
        return str(out["JobId"])

    async def batch_checksum(self, refs: Sequence[ObjectRef], *, name: str) -> str:
        """Compute every object's full-object CRC64NVME at rest, any class,
        no restore (`S3ComputeObjectChecksum`) [V]; the values come back in
        the all-tasks completion report (`batch_checksums`). Up to about a
        week for Glacier Flexible Retrieval and Deep Archive [V]. Returns the
        job id."""
        b = self.config.batch
        if b is None:
            raise UnsupportedOperation("S3 Batch Operations are not configured (batch)")
        if not refs:
            raise ValueError("a checksum job needs objects")
        key, etag, versioned = await self._manifest(refs, name)
        out = await self._control_call(
            "create_job", f"batch checksum {name}",
            ConfirmationRequired=False,
            Operation={"S3ComputeObjectChecksum": {
                "ChecksumAlgorithm": "CRC64NVME", "ChecksumType": "FULL_OBJECT"}},
            Manifest=self._manifest_spec(key, etag, versioned),
            Report={"Bucket": f"arn:aws:s3:::{b.report_bucket or b.manifest_bucket}",
                    "Format": "Report_CSV_20180820", "Enabled": True,
                    "Prefix": self._report_prefix(name), "ReportScope": "AllTasks",
                    "ExpectedBucketOwner": b.account_id},
            Priority=b.priority, RoleArn=b.role_arn, ClientRequestToken=name,
            Description=f"OmniFile checksums {name}"[:256],
        )  # fmt: skip
        return str(out["JobId"])

    async def batch_checksums(
        self, job_id: str, *, name: str
    ) -> dict[tuple[str, str, str | None], bytes]:
        """A finished checksum job's values: (bucket, key, version id) ->
        the 8-byte CRC64NVME. Failed tasks (SSE-C, gone) are absent."""
        b = self.config.batch
        if b is None:
            raise UnsupportedOperation("S3 Batch Operations are not configured (batch)")
        bucket = b.report_bucket or b.manifest_bucket
        base = f"{self._report_prefix(name).rstrip('/')}/job-{job_id}"
        manifest = json.loads(await self._read_small(bucket, f"{base}/manifest.json"))
        out: dict[tuple[str, str, str | None], bytes] = {}
        for result in manifest.get("Results") or []:
            if result.get("TaskExecutionStatus") != "succeeded":
                continue
            text = (await self._read_small(result["Bucket"], result["Key"])).decode()
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 7 or row[3] != "succeeded":
                    continue
                try:
                    msg = json.loads(row[6])
                    if msg.get("checksumAlgorithm") != "CRC64NVME":
                        continue
                    if "checksum_hex" in msg:
                        crc = bytes.fromhex(str(msg["checksum_hex"]))
                    else:
                        crc = base64.b64decode(str(msg["checksum_base64"]), validate=True)
                except (ValueError, KeyError, TypeError, binascii.Error):
                    continue
                if len(crc) == 8:
                    # A report's key column is form-encoded, whatever encoding the job's
                    # manifest used: a space is `+`, a literal plus `%2B`, a percent sign
                    # `%25`, a comma `%2C`, non-ASCII its UTF-8 bytes in `%XX`, and slashes
                    # are kept. That is `quote_plus(key, safe="/")`, and `unquote_plus`
                    # reads all of it back. Verified on real S3, 2026-11-14 PST (runs
                    # rr-202611150626-34b3 and rr-202611150718-62e9): `with space-01.segy`
                    # came back as `with+space-01.segy`, which plain `unquote` leaves as it
                    # is, so the file was never matched or verified.
                    out[(row[0], unquote_plus(row[1]), row[2] or None)] = crc
        return out

    async def _read_small(self, bucket: str, key: str) -> bytes:
        """A report or manifest object, whole (the batch config names its bucket)."""
        what = f"s3://{bucket}/{key}"
        r = await self._s3_call("get_object", what, Bucket=bucket, Key=key, reading=True)
        body = r["Body"]
        parts = []
        try:
            while chunk := await body.read(READ_CHUNK):
                parts.append(chunk)
        except Exception as e:  # noqa: BLE001
            raise translate(e, what, reading=True) from e
        finally:
            await body.aclose()
        return b"".join(parts)

    def _report_prefix(self, name: str) -> str:
        assert self.config.batch is not None
        return f"{self.config.batch.manifest_prefix}reports/{name}"

    async def _manifest(self, refs: Sequence[ObjectRef], name: str) -> tuple[str, str, bool]:
        """Write a job's CSV manifest; returns (key, ETag, with version ids)."""
        b = self.config.batch
        assert b is not None
        versioned = all(r.version_id for r in refs)
        rows = []
        for r in refs:
            cols = [self._bucket(r.container), quote(r.key, safe="/")]
            if versioned:
                cols.append(str(r.version_id))
            rows.append(",".join(cols))
        key = f"{b.manifest_prefix}{name}.csv"
        self._writable("put", b.manifest_bucket, key)
        put = await self._s3_call(
            "put_object", f"s3://{b.manifest_bucket}/{key}", Bucket=b.manifest_bucket, Key=key,
            Body=("\n".join(rows) + "\n").encode(),
        )  # fmt: skip
        return key, str(put.get("ETag", "")).strip('"'), versioned

    def _manifest_spec(self, key: str, etag: str, versioned: bool) -> dict[str, Any]:
        assert self.config.batch is not None
        return {
            "Spec": {"Format": "S3BatchOperations_CSV_20180820",
                     "Fields": ["Bucket", "Key", "VersionId"] if versioned else ["Bucket", "Key"]},
            "Location": {"ObjectArn": f"arn:aws:s3:::{self.config.batch.manifest_bucket}/{key}",
                         "ETag": etag},
        }  # fmt: skip

    async def batch_job(self, job_id: str) -> Mapping[str, Any]:
        """A Batch Operations job's status and task counts: ``{"status",
        "total", "succeeded", "failed"}``; status is S3's (Complete, Failed,
        Cancelled are final)."""
        out = await self._control_call("describe_job", f"batch job {job_id}", JobId=job_id)
        job = out.get("Job") or {}
        prog = job.get("ProgressSummary") or {}
        return {
            "status": str(job.get("Status", "")),
            "total": int(prog.get("TotalNumberOfTasks") or 0),
            "succeeded": int(prog.get("NumberOfTasksSucceeded") or 0),
            "failed": int(prog.get("NumberOfTasksFailed") or 0),
        }

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        """From HeadObject's x-amz-restore. Completion normally arrives as an
        EventBridge "Object Restore Completed" through SQS; this is for the
        one file a user is waiting on [V]."""
        head, raw = await self._head_raw(handle.ref)
        state = head.restore_state
        if state is RestoreState.IN_PROGRESS:
            native = head.native_class
            if raw.get("ArchiveStatus") == IT_DEEP_ARCHIVE_ACCESS:
                native = S3_DEEP_ARCHIVE
            hours = self._typical_hours(handle.speed, native)
            eta = None
            if hours is not None:
                eta = max(handle.requested_at + timedelta(hours=hours), self._clock.now())
            return RestoreStatus(RestoreState.IN_PROGRESS, eta=eta)
        if state is RestoreState.COMPLETED:
            return RestoreStatus(RestoreState.COMPLETED, expires_at=head.restore_expires_at)
        if state is RestoreState.NOT_NEEDED:
            # An Intelligent-Tiering restore puts the object back online for good.
            return RestoreStatus(RestoreState.COMPLETED)
        if state is RestoreState.ARCHIVED:
            # Accepted, so it ran; no x-amz-restore any more means S3 removed
            # the temporary copy.
            return RestoreStatus(RestoreState.EXPIRED, detail="the restored copy is gone")
        return RestoreStatus(state)

    # --- StorageBackend: delete, WORM, tags, metadata --------------------

    async def delete(self, ref: ObjectRef) -> None:
        bucket = self._writable("delete", ref.container, ref.key)
        # S3 answers 204 for a key that isn't there; the contract wants
        # ObjectNotFound, so look first.
        try:
            await self.head(ref)
        except BackendError as e:
            marker = (
                ref.version_id is not None
                and e.__cause__ is not None
                and code_of_safe(e.__cause__) in ("MethodNotAllowed", "405")
            )
            if not marker:  # deleting a delete-marker version is allowed
                raise
        await self._s3_call(
            "delete_object", self._what(ref), Bucket=bucket, Key=ref.key, **self._version_kw(ref)
        )

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        raise UnsupportedOperation("S3 Object Lock is not used (S3 is a migration source)")

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        raise UnsupportedOperation("S3 Object Lock is not used (S3 is a migration source)")

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        if len(tags) > self._caps.max_tags:
            raise BackendError(f"S3 allows {self._caps.max_tags} tags per object, not {len(tags)}")
        bucket = self._writable("set_tags", ref.container, ref.key)
        await self._s3_call(
            "put_object_tagging",
            self._what(ref),
            Bucket=bucket,
            Key=ref.key,
            Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]},
            **self._version_kw(ref),
        )

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        bucket = self._bucket(ref.container)
        r = await self._s3_call(
            "get_object_tagging",
            self._what(ref),
            Bucket=bucket,
            Key=ref.key,
            **self._version_kw(ref),
        )
        return {t["Key"]: t["Value"] for t in r.get("TagSet", [])}

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        """CopyObject onto itself with MetadataDirective=REPLACE, keeping the
        storage class (a copy without one would land in STANDARD)."""
        self._writable("set_metadata", ref.container, ref.key)
        head, raw = await self._head_raw(ref)
        if head.native_class in ARCHIVED_CLASSES or raw.get("ArchiveStatus"):
            if head.restore_state is not RestoreState.COMPLETED:
                raise UnsupportedOperation(
                    f"{self._what(ref)}: S3 can't copy an archived object that isn't restored"
                )
        return await self._copy(
            head, raw, ref.container, ref.key, storage_class=head.native_class, metadata=metadata
        )

    # --- StorageBackend: edge, events ----------------------------------

    async def edge_credential(
        self,
        ref: ObjectRef,
        access: EdgeAccess,
        ttl: timedelta,
        *,
        if_match: str | None = None,
    ) -> EdgeCredential:
        """A pointer for the edge's own IAM Roles Anywhere session (the
        default: the edge signs, its key in its TPM), or, when the operator
        chose the fallback (`edge_credentials = presigned`), a presigned GET
        for one object version. Neither is an account secret [A]. Edges read
        S3; a write credential is only ever for OmniFile's own keys (`owns`),
        never a source's (decision 40).

        A presigned GET with `if_match` (the catalog's quoted ETag) signs it
        as a header: the edge must send exactly that `If-Match`, and an
        object replaced since answers 412, so ranges of two objects are
        never mixed (Codex review on #23). Without a version id and without
        an ETag the URL pins nothing; the edge then reads it only by
        whole-file, CRC-verified fills."""
        if access is not EdgeAccess.READ:
            # A client's key is refused as every write to it is (logged);
            # OmniFile's own keys too: edges never write to S3, new writes
            # go to the dataset's own backend (decision 14).
            self._writable("edge_credential", ref.container, ref.key)
            raise UnsupportedOperation("edges only read S3: writes go to the dataset's backend")
        bucket = self._bucket(ref.container)
        if self.config.edge_credentials == "presigned":
            await self._clients()
            params = {"Bucket": bucket, "Key": ref.key}
            if ref.version_id:
                params["VersionId"] = ref.version_id
            if self.config.requester_pays:
                params["RequestPayer"] = "requester"
            if if_match:
                params["IfMatch"] = if_match
            seconds = max(1, int(ttl.total_seconds()))
            url = await self._presign.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=seconds
            )
            return EdgeCredential(
                CredentialMode.S3_PRESIGNED_URL,
                access,
                self._clock.now() + timedelta(seconds=seconds),
                url=url,
                details={"bucket": bucket, "key": ref.key}
                | ({"if_match": if_match} if if_match else {}),
            )
        details: dict[str, Any] = {
            "bucket": bucket,
            "key": ref.key,
            "region": self.config.region,
            "endpoint": self.config.edge_endpoint_name,
            "addressing": "path" if self.config.path_style else "virtual_host",
            "tls_max_version": self.config.tls_max_version,
            "requester_pays": self.config.requester_pays,
            "private_network": self.config.private_network,
        }
        if ref.version_id:
            details["version_id"] = ref.version_id
        return EdgeCredential(
            CredentialMode.S3_ROLES_ANYWHERE,
            access,
            self._clock.now() + ttl,
            url=None,
            details=details,
        )

    def change_cursor_at(self, container: str, at: datetime) -> str:
        """A `read_changes` cursor that starts at `at` instead of now: events
        older than `at` are dropped, later ones (still on the queue) are
        applied. Seeding starts the feed at its inventory's snapshot
        (docs/design/catalog-seeding.md section 5)."""
        bucket = self._bucket(container)
        if self.config.queue_for(bucket) is None:
            raise UnsupportedOperation(f"no event queue is configured for bucket {bucket}")
        return f"s3ev:{bucket}:{int(at.timestamp())}:0"

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        """EventBridge S3 events for `container`, from its SQS queue.

        The cursor names the previous page: passing it back deletes that
        page's messages, so a caller that crashes before keeping a page gets
        it again after the visibility timeout (at least once; catalog updates
        are idempotent). An empty page returns the same cursor.

        ``None`` means from now: the first cursor carries the time it was
        made, and events older than that (already on the queue when the
        container was registered) are dropped, never applied. The bound is
        to the second, as EventBridge times are."""
        bucket = self._bucket(container)
        queue = self.config.queue_for(bucket)
        if queue is None:
            raise UnsupportedOperation(f"no event queue is configured for bucket {bucket}")
        if cursor is None:
            since = int(self._clock.now().timestamp())
            return ChangePage([], f"s3ev:{bucket}:{since}:0", True)
        since_at = self._cursor_since(cursor, bucket)
        acked = self._unacked.pop(cursor, [])
        if acked:
            await self._delete_messages(acked)
        n = self.config.events_per_receive
        r = await self._sqs_call(
            "receive_message",
            f"SQS {queue}",
            QueueUrl=queue,
            MaxNumberOfMessages=n,
            VisibilityTimeout=self.config.events_visibility_s,
            WaitTimeSeconds=0,
        )
        messages = r.get("Messages", [])
        records: list[ChangeRecord] = []
        keep: list[tuple[str, str]] = []
        drop: list[tuple[str, str]] = []
        release: list[tuple[str, str]] = []
        for m in messages:
            handle = (queue, m["ReceiptHandle"])
            try:
                rec = events.to_record(self._id, m.get("Body", ""))
            except ValueError as e:
                log.warning("dropping an unreadable S3 event", extra={"reason": str(e)})
                drop.append(handle)
                continue
            if rec is None:
                drop.append(handle)
            elif rec.ref.container != bucket:
                release.append(handle)  # another bucket's, on a shared queue
            elif rec.at < since_at:
                drop.append(handle)  # from before this reader started
            else:
                records.append(rec)
                keep.append(handle)
        if drop:
            await self._delete_messages(drop)
        for q, h in release:
            await self._sqs_call(
                "change_message_visibility",
                f"SQS {q}",
                QueueUrl=q,
                ReceiptHandle=h,
                VisibilityTimeout=0,
            )
        # A full receive with nothing of ours (another bucket's events on a
        # shared queue, or junk) also counts as caught up, so a reader that
        # loops until caught up can't spin on messages it releases.
        caught_up = len(messages) < n or not records
        if not records:
            return ChangePage([], cursor, caught_up)
        nxt = f"s3ev:{bucket}:{int(since_at.timestamp())}:{next(self._seq)}"
        self._unacked[nxt] = keep
        records.sort(key=lambda rec: rec.at)
        return ChangePage(records, nxt, caught_up)

    @staticmethod
    def _cursor_since(cursor: str, bucket: str) -> datetime:
        """The from-now bound a cursor carries (``s3ev:<bucket>:<epoch>:<n>``)."""
        parts = cursor.split(":")
        if len(parts) != 4 or parts[0] != "s3ev" or parts[1] != bucket:
            raise BackendError(f"{cursor!r} is not an S3 event cursor for bucket {bucket}")
        try:
            return datetime.fromtimestamp(int(parts[2]), UTC)
        except ValueError:
            raise BackendError(f"{cursor!r} is not an S3 event cursor") from None

    async def _delete_messages(self, handles: Sequence[tuple[str, str]]) -> None:
        by_queue: dict[str, list[str]] = {}
        for q, h in handles:
            by_queue.setdefault(q, []).append(h)
        for q, hs in by_queue.items():
            for i in range(0, len(hs), 10):  # DeleteMessageBatch takes 10
                batch = [{"Id": str(j), "ReceiptHandle": h} for j, h in enumerate(hs[i : i + 10])]
                r = await self._sqs_call(
                    "delete_message_batch", f"SQS {q}", QueueUrl=q, Entries=batch
                )
                for f in r.get("Failed", []):
                    log.warning("an S3 event message was not deleted", extra={"reason": f})


def code_of_safe(e: BaseException) -> str:
    """The AWS error code of `e`, or '' if it isn't a ClientError."""
    from botocore.exceptions import ClientError

    return code_of(e) if isinstance(e, ClientError) else ""
