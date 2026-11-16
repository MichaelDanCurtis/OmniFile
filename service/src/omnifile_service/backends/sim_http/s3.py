"""An S3-shaped HTTP facade over sim's S3 flavour (M5): ``--layout s3``.

Ported from line B's ``sim_http/s3.py`` onto this tree's `SimBackend`. Its
client is the edge's Rust S3 reader (``omnifile-blob``, ``tests/s3_sim.rs``):
ranged GETs and HEADs signed with SigV4, using credentials from the fake
IMDSv2 endpoint that plays ``aws_signing_helper serve``. It also answers STS
``AssumeRoleWithWebIdentity`` (unsigned, as on AWS), so the service's web
identity credentials can be exercised against it.

Every S3 request is checked with SigV4 (`sigv4.py`) against static test
credentials or sessions this facade issued (STS, IMDS). The presigned GETs of
the Azure layout's ``--s3`` option (`s3get.py`) are a separate thing and stay
as they were.

**Routing** (one port):
- ``/latest/...``: the fake IMDSv2 (`imds_call`);
- ``/_sim/...``: admin helpers, only with ``admin=True``;
- ``POST /`` with a form body ``Action=AssumeRoleWithWebIdentity``: STS;
- anything else: S3, path style ``/<bucket>/<key>``, or virtual host when
  the first label of ``Host`` is an existing bucket and the host is not an
  IP address or ``localhost``.

**S3 operations:** GetObject (``Range``, ``versionId``, ``If-Match`` before
the archived check, ``x-amz-checksum-mode`` on whole reads), HeadObject
(``x-amz-restore``, ``x-amz-archive-status``, ``x-amz-storage-class``,
``x-amz-meta-*``), Get/Put/DeleteObjectTagging, PutObject (single part, with
``x-amz-checksum-crc64nvme`` and ``Content-MD5`` checked), CopyObject (no
metadata replace), RestoreObject, DeleteObject, CreateBucket. Not here (they
need sim features this tree's `SimBackend` lacks; B has them): multipart
uploads, ListObjects, S3 Control Batch jobs and the EventBridge -> SQS
queue. They answer ``501 NotImplemented``.

Error bodies follow S3: ``<Error><Code/><Message/><RequestId/></Error>``
with ``x-amz-request-id``; HEAD errors have no body. Sources: the Amazon S3
API reference, STS, and the IMDSv2 description in the EC2 user guide (which
``aws_signing_helper serve`` follows), as read for line B on 2026-10-12.

The facade reaches into `SimBackend`'s internals (``_lock``, ``_commit``,
``_resolve``) for three things its public API doesn't offer: an explicit S3
storage class on a put, the Intelligent-Tiering archive status, and whether
a bucket exists. It holds the sim's lock while it does, as the sim's own
methods do.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import itertools
import json
import secrets
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qs, parse_qsl, unquote
from xml.sax.saxutils import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..base import (
    BackendError,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    RestoreSpeed,
    RestoreState,
    Throttled,
    UnsupportedOperation,
)
from ..sim import SimBackend, crc64nvme
from ..sim.backend import Flavour
from ..sim.capabilities import S3_STANDARD, S3_TIERS, native_info
from ..sim.crc64 import to_bytes
from ..sim.faults import Unavailable
from ..sim.payload import MemoryPayload, Payload, SyntheticPayload
from . import sigv4
from .sigv4 import Credential, SigV4Error

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
STS_NS = "https://sts.amazonaws.com/doc/2011-06-15/"

#: Static test credentials (never real ones): the edge's and the service's.
EDGE_ACCESS_KEY = "AKIASIMEDGE000000000"
EDGE_SECRET_KEY = "simsecret/edge/0000000000000000000000"
SERVICE_ACCESS_KEY = "AKIASIMSERVICE000000"
SERVICE_SECRET_KEY = "simsecret/service/00000000000000000000"
DEFAULT_REGION = "eu-west-1"
DEFAULT_ACCOUNT = "123456789012"
IMDS_ROLE = "omnifile-edge"
MAX_SINGLE_PUT = 5 * 1024**3
COPY_TO_ITSELF = (
    "This copy request is illegal because it is trying to copy an object to itself without "
    "changing the object's metadata, storage class, website redirect location or encryption "
    "attributes."
)
_NATIVE = native_info(S3_TIERS)
_TIERS = {"Expedited": RestoreSpeed.EXPEDITED, "Standard": RestoreSpeed.STANDARD,
          "Bulk": RestoreSpeed.BULK}  # fmt: skip


class S3Error(Exception):
    def __init__(
        self, status: int, code: str, message: str = "", *, extra: str = "", **headers: str
    ) -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        #: More XML elements for the error body (e.g. <Condition>).
        self.extra = extra
        self.headers = headers


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _http_date(t: datetime) -> str:
    return format_datetime(t.astimezone(UTC), usegmt=True)


def _xml(root: str, inner: str, ns: str = S3_NS) -> Response:
    body = f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} xmlns="{ns}">{inner}</{root}>'
    return Response(body, media_type="application/xml")


def _e(tag: str, value: object) -> str:
    return f"<{tag}>{escape(str(value))}</{tag}>"


def _parse_xml(body: bytes) -> ET.Element:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise S3Error(400, "MalformedXML", "The XML you provided was not well-formed") from None
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


@dataclass
class _Bucket:
    requester_pays: bool = False


@dataclass
class ImdsState:
    """The fake ``aws_signing_helper serve``: IMDSv2 tokens and session
    credentials, both on the facade's clock."""

    ttl: timedelta = timedelta(hours=1)
    tokens: dict[str, datetime] = field(default_factory=dict)
    token_puts: int = 0
    credential_fetches: int = 0
    issued: int = 0


class S3Facade:
    """State behind the S3 app: the sim, credentials, buckets, stats."""

    def __init__(
        self,
        sim: SimBackend,
        *,
        region: str = DEFAULT_REGION,
        account_id: str = DEFAULT_ACCOUNT,
        check_skew: bool = False,
    ) -> None:
        if sim.flavour is not Flavour.S3:
            raise ValueError("the S3 facade needs sim's S3 flavour")
        self.sim = sim
        self.region = region
        self.account_id = account_id
        self.check_skew = check_skew
        self.credentials: dict[str, Credential] = {
            EDGE_ACCESS_KEY: Credential(EDGE_ACCESS_KEY, EDGE_SECRET_KEY, principal="edge"),
            SERVICE_ACCESS_KEY: Credential(
                SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY, principal="service"
            ),
        }
        self.buckets: dict[str, _Bucket] = defaultdict(_Bucket)
        self.content_types: dict[tuple[str, str, str | None], str] = {}
        #: Requests per bucket: GET, HEAD, PUT, DELETE, TAG, COPY_FROM,
        #: RESTORE, LIST, and bytes_out (bytes asked for by GETs).
        self.stats: dict[str, Counter[str]] = defaultdict(Counter)
        self.imds = ImdsState()
        #: STS: tokens refused, and every call (role, session name, token).
        self.sts_calls: list[tuple[str, str, str]] = []
        self.refused_tokens: set[str] = {"invalid"}
        self._seq = itertools.count(1)

    def now(self) -> datetime:
        return self.sim.clock.now()

    def issue_session(self, ttl: timedelta, principal: str) -> Credential:
        n = next(self._seq)
        cred = Credential(
            access_key_id=f"ASIASIM{n:013d}",
            secret_access_key=secrets.token_urlsafe(30),
            session_token=secrets.token_urlsafe(48),
            expires_at=self.now() + ttl,
            principal=principal,
        )
        self.credentials[cred.access_key_id] = cred
        return cred

    # --- the sim, where its public API stops ------------------------------

    def bucket_exists(self, bucket: str) -> bool:
        with self.sim._lock:
            return bucket in self.sim._containers

    def create_bucket(
        self, bucket: str, *, versioning: bool = True, requester_pays: bool = False
    ) -> str:
        if not self.bucket_exists(bucket):
            self.sim.create_container(bucket, versioning=versioning)
        self.buckets[bucket].requester_pays = requester_pays
        return bucket

    def put(
        self,
        bucket: str,
        key: str,
        payload: Payload,
        *,
        native_class: str | None = None,
        metadata: Mapping[str, str] | None = None,
        crc64: int | None = None,
        md5: bytes | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        """PutObject with an explicit S3 storage class (or the bucket's
        default, STANDARD)."""
        if native_class is not None and native_class not in _NATIVE:
            raise UnsupportedOperation(f"unknown storage class {native_class}")
        sim = self.sim
        with sim._lock:
            sim._settle()
            return sim._commit(
                bucket,
                key,
                payload,
                None,
                dict(metadata or {}),
                block_crcs=[],
                crc=crc64,
                md5=md5,
                native=native_class or S3_STANDARD,
                explicit=native_class is not None,
                tags=tags,
            )

    def it_tier(self, ref: ObjectRef) -> str | None:
        """An Intelligent-Tiering object's archive access tier, if any."""
        sim = self.sim
        with sim._lock:
            sim._settle()
            _, _, v = sim._resolve(ref)
            return v.it_tier


# --- request plumbing ---------------------------------------------------------


def _headers(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for k, v in request.headers.raw:
        out[k.decode("latin-1").lower()].append(v.decode("latin-1"))
    return out


def _auth(request: Request, facade: S3Facade, body: bytes | None) -> sigv4.Verified:
    raw_path = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1")
    raw_path = raw_path.split("?", 1)[0]
    try:
        return sigv4.verify(
            method=request.method,
            raw_path=raw_path,
            raw_query=request.scope.get("query_string", b"").decode("latin-1"),
            headers=_headers(request),
            body=body,
            lookup=facade.credentials,
            now=facade.now(),
            check_skew=facade.check_skew,
        )
    except SigV4Error as e:
        raise S3Error(e.status, e.code, e.message) from None


def _split(request: Request, facade: S3Facade) -> tuple[str, str]:
    """(bucket, key) from a path-style or virtual-host request."""
    path = unquote(request.scope.get("raw_path", request.url.path.encode()).decode("latin-1"))
    path = path.split("?", 1)[0].lstrip("/")
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    label = host.split(".", 1)[0]
    is_ip = False
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_ip = True
    except ValueError:
        pass
    if host and not is_ip and host != "localhost" and "." in host and facade.bucket_exists(label):
        return label, path
    bucket, _, key = path.partition("/")
    return bucket, key


def _version(request: Request) -> str | None:
    v = request.query_params.get("versionId")
    return None if v in (None, "", "null") else v


def _ref(facade: S3Facade, bucket: str, key: str, version: str | None) -> ObjectRef:
    return ObjectRef(facade.sim.backend_id, bucket, key, version)


def _error_response(e: S3Error, method: str, request_id: str) -> Response:
    headers = {"x-amz-request-id": request_id, **e.headers}
    if method == "HEAD":
        return Response(status_code=e.status, headers=headers)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Error>'
        f"{_e('Code', e.code)}{_e('Message', e.message)}{e.extra}"
        f"{_e('RequestId', request_id)}</Error>"
    )
    return Response(body, status_code=e.status, media_type="application/xml", headers=headers)


def _map_backend(e: BaseException, what: str) -> S3Error:
    if isinstance(e, ObjectNotFound):
        text = str(e)
        if text.startswith("no container"):
            return S3Error(404, "NoSuchBucket", text)
        if "@" in text:
            return S3Error(404, "NoSuchVersion", text)
        return S3Error(404, "NoSuchKey", f"{what}: The specified key does not exist.")
    if isinstance(e, NotRestored):
        return S3Error(
            403, "InvalidObjectState", "The operation is not valid for the object's storage class"
        )
    if isinstance(e, Throttled):
        retry = e.retry_after.total_seconds() if e.retry_after else 1
        return S3Error(
            503,
            "SlowDown",
            "Please reduce your request rate.",
            **{"retry-after": f"{max(1, int(retry + 0.999))}"},
        )
    if isinstance(e, Unavailable):
        return S3Error(503, "ServiceUnavailable", str(e))
    if isinstance(e, UnsupportedOperation):
        text = str(e)
        if "needs no restore" in text or "archived" in text:
            return S3Error(403, "InvalidObjectState", text)
        return S3Error(400, "InvalidRequest", text)
    if isinstance(e, BackendError):
        return S3Error(400, "InvalidRequest", str(e))
    return S3Error(500, "InternalError", str(e))


def _payer(request: Request, facade: S3Facade, bucket: str) -> None:
    if facade.buckets[bucket].requester_pays and (
        request.headers.get("x-amz-request-payer", "").lower() != "requester"
    ):
        raise S3Error(403, "AccessDenied", "This bucket is requester-pays")


def _meta_headers(request: Request) -> dict[str, str]:
    return {
        k[len("x-amz-meta-") :]: v
        for k, v in request.headers.items()
        if k.lower().startswith("x-amz-meta-")
    }


def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise S3Error(400, "InvalidArgument", "bad Range")
    a, _, b = value[6:].partition("-")
    try:
        if a == "":
            n = int(b)
            return (max(0, size - n), size - 1) if size else None
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        raise S3Error(400, "InvalidArgument", "bad Range") from None
    if start >= size or end < start:
        raise S3Error(416, "InvalidRange", "The requested range is not satisfiable")
    return start, min(end, size - 1)


def _if_match(wanted_header: str | None, etag: str | None, condition: str) -> None:
    if wanted_header is None:
        return
    wanted = {t.strip().strip('"') for t in wanted_header.split(",")}
    if "*" not in wanted and (etag or "").strip('"') not in wanted:
        raise S3Error(
            412,
            "PreconditionFailed",
            "At least one of the pre-conditions you specified did not hold",
            extra=_e("Condition", condition),
        )


def _copy_source(value: str) -> tuple[str, str, str | None]:
    path, _, query = value.partition("?")
    path = unquote(path).lstrip("/")
    bucket, _, key = path.partition("/")
    version = dict(parse_qsl(query)).get("versionId")
    if not bucket or not key:
        raise S3Error(400, "InvalidArgument", "bad x-amz-copy-source")
    return bucket, key, version


async def _object_headers(
    facade: S3Facade, request: Request, bucket: str, key: str, version: str | None
) -> tuple[dict[str, str], Any]:
    sim = facade.sim
    ref = _ref(facade, bucket, key, version)
    try:
        head = await sim.head(ref)
        it_tier = facade.it_tier(head.ref)
    except BaseException as e:
        raise _map_backend(e, f"{bucket}/{key}") from None
    # Preconditions come before the archived-state check (VERIFY on S3).
    _if_match(request.headers.get("if-match"), head.etag, "If-Match")
    h: dict[str, str] = {
        "content-type": facade.content_types.get((bucket, key, head.ref.version_id))
        or "binary/octet-stream",
        "last-modified": _http_date(head.last_modified) if head.last_modified else "",
        "etag": head.etag or "",
        "accept-ranges": "bytes",
    }
    if head.ref.version_id:
        h["x-amz-version-id"] = head.ref.version_id
    if head.native_class != S3_STANDARD:
        h["x-amz-storage-class"] = head.native_class
    state = head.restore_state
    if state is RestoreState.IN_PROGRESS and not it_tier:
        h["x-amz-restore"] = 'ongoing-request="true"'
    elif state is RestoreState.COMPLETED and head.restore_expires_at is not None:
        h["x-amz-restore"] = (
            f'ongoing-request="false", expiry-date="{_http_date(head.restore_expires_at)}"'
        )
    if it_tier:
        h["x-amz-archive-status"] = it_tier
    for k, v in head.metadata.items():
        h[f"x-amz-meta-{k}"] = v
    if facade.buckets[bucket].requester_pays:
        h["x-amz-request-charged"] = "requester"
    if (
        request.headers.get("x-amz-checksum-mode", "").upper() == "ENABLED"
        and head.crc64nvme is not None
    ):
        h["x-amz-checksum-crc64nvme"] = base64.b64encode(head.crc64nvme).decode()
        h["x-amz-checksum-type"] = "FULL_OBJECT"
    return h, head


# --- S3 operations -------------------------------------------------------------


async def s3_head(facade: S3Facade, request: Request, bucket: str, key: str) -> Response:
    if not key:
        if not facade.bucket_exists(bucket):
            raise S3Error(404, "NoSuchBucket")
        return Response(status_code=200)
    h, head = await _object_headers(facade, request, bucket, key, _version(request))
    facade.stats[bucket]["HEAD"] += 1
    h["content-length"] = str(head.size)
    return Response(status_code=200, headers=h)


async def s3_get(facade: S3Facade, request: Request, bucket: str, key: str) -> Response:
    q = request.query_params
    if not key:
        raise S3Error(501, "NotImplemented", "listings are not in this sim layout")
    version = _version(request)
    if "tagging" in q:
        try:
            tags = await facade.sim.get_tags(_ref(facade, bucket, key, version))
        except BaseException as e:
            raise _map_backend(e, f"{bucket}/{key}") from None
        inner = "".join(f"<Tag>{_e('Key', k)}{_e('Value', v)}</Tag>" for k, v in tags.items())
        return _xml("Tagging", f"<TagSet>{inner}</TagSet>")
    h, head = await _object_headers(facade, request, bucket, key, version)
    rng = _parse_range(request.headers.get("range"), head.size)
    start, end = rng if rng is not None else (0, head.size - 1)
    length = max(0, end - start + 1)
    it = facade.sim.get_range(head.ref, start, length)
    # The first chunk before the status line, so an archived object or an
    # injected fault is an HTTP error, as S3 answers one, not a cut stream.
    try:
        first = await anext(it, b"")
    except BaseException as e:
        raise _map_backend(e, f"{bucket}/{key}") from None
    facade.stats[bucket]["GET"] += 1
    facade.stats[bucket]["bytes_out"] += length
    h["content-length"] = str(length)
    status = 200
    if rng is not None:
        status = 206
        h["content-range"] = f"bytes {start}-{end}/{head.size}"
        h.pop("x-amz-checksum-crc64nvme", None)  # S3 sends full-object checksums only
        h.pop("x-amz-checksum-type", None)

    async def body() -> AsyncIterator[bytes]:
        if first:
            yield first
        async for chunk in it:
            yield chunk

    return StreamingResponse(body(), status_code=status, headers=h)


def _check_body_checksum(request: Request, body: bytes) -> int:
    crc = crc64nvme(body)
    sent = request.headers.get("x-amz-checksum-crc64nvme")
    if sent and base64.b64decode(sent) != to_bytes(crc):
        raise S3Error(
            400, "BadDigest", "The CRC64NVME you specified did not match the calculated checksum."
        )
    md5 = request.headers.get("content-md5")
    if md5 and base64.b64decode(md5) != hashlib.md5(body).digest():
        raise S3Error(400, "BadDigest", "The Content-MD5 you specified did not match.")
    return crc


def _storage_class(request: Request) -> str | None:
    sc = request.headers.get("x-amz-storage-class")
    if sc is not None and sc not in _NATIVE:
        raise S3Error(
            400, "InvalidStorageClass", f"The storage class you specified is not valid: {sc}"
        )
    return sc


def _decode_aws_chunked(body: bytes) -> bytes:
    """aws-chunked bodies (``<hex size>[;chunk-signature=...]\\r\\n<data>\\r\\n``
    then trailers). Chunk signatures are not checked [sim]."""
    out = bytearray()
    pos = 0
    while True:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            raise S3Error(400, "IncompleteBody", "bad aws-chunked body")
        size = int(body[pos:eol].split(b";", 1)[0], 16)
        pos = eol + 2
        if size == 0:
            return bytes(out)
        out += body[pos : pos + size]
        pos += size + 2


async def _read_body(request: Request) -> bytes:
    body = await request.body()
    if "aws-chunked" in request.headers.get("content-encoding", "") or request.headers.get(
        "x-amz-content-sha256", ""
    ).startswith("STREAMING-"):
        body = _decode_aws_chunked(body)
    return body


async def s3_put(
    facade: S3Facade, request: Request, bucket: str, key: str, body: bytes
) -> Response:
    q = request.query_params
    sim = facade.sim
    if not key:
        facade.create_bucket(bucket)
        return Response(status_code=200, headers={"location": f"/{bucket}"})
    if not facade.bucket_exists(bucket):
        raise S3Error(404, "NoSuchBucket", bucket)
    if "tagging" in q:
        # Tag writes are counted: an exit test can assert none reaches a
        # migration source.
        facade.stats[bucket]["TAG"] += 1
        root = _parse_xml(body)
        tags = {t.findtext("Key", ""): t.findtext("Value", "") for t in root.iter("Tag")}
        try:
            await sim.set_tags(_ref(facade, bucket, key, _version(request)), tags)
        except BaseException as e:
            raise _map_backend(e, f"{bucket}/{key}") from None
        return Response(status_code=200)
    if "uploadId" in q:
        raise S3Error(501, "NotImplemented", "multipart uploads are not in this sim layout")
    sc = _storage_class(request)
    copy_source = request.headers.get("x-amz-copy-source")
    if copy_source:
        return await _copy(facade, request, bucket, key, copy_source, sc)
    if len(body) > MAX_SINGLE_PUT:
        raise S3Error(400, "EntityTooLarge")
    crc = _check_body_checksum(request, body)
    try:
        ref = facade.put(
            bucket,
            key,
            MemoryPayload(body),
            native_class=sc,
            metadata=_meta_headers(request),
            crc64=crc,
            md5=hashlib.md5(body).digest(),
            tags=dict(parse_qsl(request.headers.get("x-amz-tagging") or "")),
        )
        head = await sim.head(ref)
    except BaseException as e:
        raise _map_backend(e, f"{bucket}/{key}") from None
    if request.headers.get("content-type"):
        facade.content_types[(bucket, key, ref.version_id)] = request.headers["content-type"]
    facade.stats[bucket]["PUT"] += 1
    headers = {
        "etag": head.etag or "",
        "x-amz-checksum-crc64nvme": base64.b64encode(to_bytes(crc)).decode(),
        "x-amz-checksum-type": "FULL_OBJECT",
    }
    if ref.version_id:
        headers["x-amz-version-id"] = ref.version_id
    return Response(status_code=200, headers=headers)


async def _copy(
    facade: S3Facade, request: Request, bucket: str, key: str, copy_source: str, sc: str | None
) -> Response:
    sim = facade.sim
    sb, sk, sv = _copy_source(copy_source)
    if request.headers.get("x-amz-metadata-directive", "COPY").upper() == "REPLACE":
        raise S3Error(501, "NotImplemented", "metadata REPLACE is not in this sim layout")
    src = _ref(facade, sb, sk, sv)
    try:
        head = await sim.head(src)
        _if_match(
            request.headers.get("x-amz-copy-source-if-match"),
            head.etag,
            "x-amz-copy-source-If-Match",
        )
        if head.size > MAX_SINGLE_PUT:
            raise S3Error(
                400,
                "InvalidRequest",
                "The specified copy source is larger than the maximum allowable size "
                "for a copy source: 5368709120",
            )
        if (sb, sk) == (bucket, key) and sc in (None, head.native_class):
            raise S3Error(400, "InvalidRequest", COPY_TO_ITSELF)
        tier = _NATIVE[sc].logical if sc is not None else None
        new = await sim.copy_object(head.ref, bucket, key, tier)
        nh = await sim.head(new)
    except S3Error:
        raise
    except BaseException as e:
        raise _map_backend(e, f"{sb}/{sk}") from None
    ct = facade.content_types.get((sb, sk, head.ref.version_id))
    if ct:
        facade.content_types[(bucket, key, new.version_id)] = ct
    facade.stats[bucket]["PUT"] += 1
    # A server-side copy reads the source bucket.
    facade.stats[sb]["COPY_FROM"] += 1
    crc = _e("ChecksumCRC64NVME", base64.b64encode(nh.crc64nvme).decode()) if nh.crc64nvme else ""
    resp = _xml(
        "CopyObjectResult",
        f"{_e('ETag', nh.etag)}{_e('LastModified', _iso(nh.last_modified or facade.now()))}{crc}",
    )
    resp.headers["x-amz-copy-source-version-id"] = head.ref.version_id or ""
    if new.version_id:
        resp.headers["x-amz-version-id"] = new.version_id
    return resp


async def s3_post(
    facade: S3Facade, request: Request, bucket: str, key: str, body: bytes
) -> Response:
    q = request.query_params
    if "restore" in q:
        return await _restore(facade, request, bucket, key, body)
    raise S3Error(501, "NotImplemented", "only RestoreObject is a POST in this sim layout")


async def _restore(
    facade: S3Facade, request: Request, bucket: str, key: str, body: bytes
) -> Response:
    root = _parse_xml(body) if body else ET.Element("RestoreRequest")
    days_text = root.findtext("Days")
    tier = root.findtext("GlacierJobParameters/Tier") or root.findtext("Tier") or "Standard"
    speed = _TIERS.get(tier)
    if speed is None:
        raise S3Error(400, "MalformedXML", f"bad Tier {tier}")
    sim = facade.sim
    ref = _ref(facade, bucket, key, _version(request))
    facade.stats[bucket]["RESTORE"] += 1
    try:
        head = await sim.head(ref)
        if head.restore_state is RestoreState.IN_PROGRESS:
            raise S3Error(409, "RestoreAlreadyInProgress", "Object restore is already in progress")
        already = head.restore_state is RestoreState.COMPLETED
        await sim.restore(ref, speed, int(days_text) if days_text else None)
    except S3Error:
        raise
    except BaseException as e:
        raise _map_backend(e, f"{bucket}/{key}") from None
    return Response(status_code=200 if already else 202)


async def s3_delete(facade: S3Facade, request: Request, bucket: str, key: str) -> Response:
    q = request.query_params
    sim = facade.sim
    ref = _ref(facade, bucket, key, _version(request))
    if "tagging" in q:
        facade.stats[bucket]["TAG"] += 1
        try:
            await sim.set_tags(ref, {})
        except BaseException as e:
            raise _map_backend(e, f"{bucket}/{key}") from None
        return Response(status_code=204)
    headers: dict[str, str] = {}
    # Every DeleteObject is counted, found or not: an exit test can assert
    # none ever reaches a migration source.
    facade.stats[bucket]["DELETE"] += 1
    try:
        await sim.delete(ref)
    except ObjectNotFound:
        pass  # S3 deletes are idempotent: 204 for a key that isn't there
    except BaseException as e:
        raise _map_backend(e, f"{bucket}/{key}") from None
    if ref.version_id is not None:
        headers["x-amz-version-id"] = ref.version_id
    return Response(status_code=204, headers=headers)


# --- STS ----------------------------------------------------------------------------


def sts_call(facade: S3Facade, form: Mapping[str, list[str]]) -> Response:
    def one(name: str) -> str:
        return (form.get(name) or [""])[0]

    action = one("Action")
    if action != "AssumeRoleWithWebIdentity":
        raise S3Error(400, "InvalidAction", f"{action} is not in sim")
    token = one("WebIdentityToken")
    role = one("RoleArn")
    session = one("RoleSessionName")
    if not token or token in facade.refused_tokens:
        body = (
            f'<ErrorResponse xmlns="{STS_NS}"><Error><Type>Sender</Type>'
            "<Code>InvalidIdentityToken</Code><Message>token refused</Message></Error>"
            f"<RequestId>{uuid.uuid4()}</RequestId></ErrorResponse>"
        )
        return Response(body, status_code=400, media_type="text/xml")
    seconds = int(one("DurationSeconds") or "3600")
    facade.sts_calls.append((role, session, token))
    cred = facade.issue_session(timedelta(seconds=seconds), f"sts:{session}")
    assert cred.expires_at is not None and cred.session_token is not None
    inner = (
        "<AssumeRoleWithWebIdentityResult><Credentials>"
        f"{_e('AccessKeyId', cred.access_key_id)}{_e('SecretAccessKey', cred.secret_access_key)}"
        f"{_e('SessionToken', cred.session_token)}{_e('Expiration', _iso(cred.expires_at))}"
        "</Credentials>"
        f"{_e('SubjectFromWebIdentityToken', 'sim-subject')}"
        f"<AssumedRoleUser>{_e('Arn', f'{role}/{session}')}"
        f"{_e('AssumedRoleId', 'AROASIM:' + session)}</AssumedRoleUser>"
        f"{_e('Provider', 'sim')}{_e('Audience', 'sim')}"
        "</AssumeRoleWithWebIdentityResult>"
        f"<ResponseMetadata>{_e('RequestId', uuid.uuid4())}</ResponseMetadata>"
    )
    return Response(
        f'<AssumeRoleWithWebIdentityResponse xmlns="{STS_NS}">{inner}'
        "</AssumeRoleWithWebIdentityResponse>",
        media_type="text/xml",
    )


# --- IMDSv2 (aws_signing_helper serve) ---------------------------------------------


def imds_call(facade: S3Facade, request: Request, path: str) -> Response:
    st = facade.imds
    now = facade.now()
    if path == "/latest/api/token":
        if request.method != "PUT":
            return Response(status_code=405)
        try:
            ttl = int(request.headers.get("x-aws-ec2-metadata-token-ttl-seconds", ""))
        except ValueError:
            return Response(status_code=400)
        if not 1 <= ttl <= 21600:
            return Response(status_code=400)
        token = secrets.token_urlsafe(32)
        st.tokens[token] = now + timedelta(seconds=ttl)
        st.token_puts += 1
        return Response(token, media_type="text/plain",
                        headers={"x-aws-ec2-metadata-token-ttl-seconds": str(ttl)})  # fmt: skip
    token = request.headers.get("x-aws-ec2-metadata-token", "")
    if st.tokens.get(token, now) <= now:
        return Response(status_code=401)
    base = "/latest/meta-data/iam/security-credentials/"
    if path == base:
        return Response(IMDS_ROLE, media_type="text/plain")
    if path == base + IMDS_ROLE:
        st.credential_fetches += 1
        cred = facade.issue_session(st.ttl, "imds")
        st.issued += 1
        assert cred.expires_at is not None
        return JSONResponse(
            {
                "Code": "Success",
                "LastUpdated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Type": "AWS-HMAC",
                "AccessKeyId": cred.access_key_id,
                "SecretAccessKey": cred.secret_access_key,
                "Token": cred.session_token,
                "Expiration": cred.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return Response(status_code=404)


# --- the app -------------------------------------------------------------------------


def create_s3_app(facade: S3Facade, *, admin: bool = False) -> Starlette:
    """The ASGI app. `admin=True` adds the /_sim/ helpers (dev and tests)."""

    async def endpoint(request: Request) -> Response:
        request_id = uuid.uuid4().hex[:16].upper()
        path = request.url.path
        try:
            if path.startswith("/latest/"):
                return imds_call(facade, request, path)
            if path.startswith("/_sim/"):
                if not admin:
                    raise S3Error(404, "NoSuchKey")
                return await _admin(facade, request, path)
            body = await _read_body(request)
            if path == "/" and request.method == "POST":
                form = parse_qs(body.decode("utf-8", "replace"))
                if form.get("Action"):
                    return sts_call(facade, form)
            _auth(request, facade, body)
            bucket, key = _split(request, facade)
            if not bucket:
                raise S3Error(501, "NotImplemented", "ListBuckets is not in sim")
            if key:
                _payer(request, facade, bucket)
            method = request.method
            if method == "HEAD":
                return await s3_head(facade, request, bucket, key)
            if method == "GET":
                return await s3_get(facade, request, bucket, key)
            if method == "PUT":
                return await s3_put(facade, request, bucket, key, body)
            if method == "POST":
                return await s3_post(facade, request, bucket, key, body)
            if method == "DELETE":
                return await s3_delete(facade, request, bucket, key)
            raise S3Error(405, "MethodNotAllowed")
        except S3Error as e:
            return _error_response(e, request.method, request_id)

    routes = [Route("/{path:path}", endpoint, methods=["GET", "HEAD", "PUT", "POST", "DELETE"])]
    return Starlette(routes=routes)


async def _admin(facade: S3Facade, request: Request, path: str) -> Response:
    sim = facade.sim
    data: dict[str, Any] = {}
    if request.method == "POST":
        raw = await request.body()
        data = json.loads(raw) if raw else {}
    if path == "/_sim/s3/buckets" and request.method == "POST":
        facade.create_bucket(
            data["bucket"],
            versioning=bool(data.get("versioning", True)),
            requester_pays=bool(data.get("requester_pays", False)),
        )
        return JSONResponse({"bucket": data["bucket"]})
    if path == "/_sim/s3/objects" and request.method == "POST":
        bucket, key = data["bucket"], data["key"]
        if not facade.bucket_exists(bucket):
            facade.create_bucket(bucket)
        sc = data.get("storage_class") or None
        if sc is not None and sc not in _NATIVE:
            return JSONResponse({"error": f"unknown storage_class {sc}"}, status_code=400)
        meta = dict(data.get("metadata") or {})
        payload: Payload
        if "data_b64" in data:
            raw_bytes = base64.b64decode(data["data_b64"])
            payload = MemoryPayload(raw_bytes)
            crc: int | None = crc64nvme(raw_bytes)
            md5: bytes | None = hashlib.md5(raw_bytes).digest()
        else:
            given = data.get("crc64nvme_b64")
            crc = int.from_bytes(base64.b64decode(given), "big") if given else None
            md5 = None
            # Never materialised: byte i is (seed + i) mod 251.
            payload = SyntheticPayload(int(data["synthetic_size"]), int(data.get("seed", 0)))
        ref = facade.put(bucket, key, payload, native_class=sc, metadata=meta, crc64=crc, md5=md5)
        if data.get("it_archive"):
            sim.it_archive(ref, deep=data["it_archive"] == "DEEP_ARCHIVE_ACCESS")
        head = await sim.head(ref)
        if data.get("content_type"):
            facade.content_types[(bucket, key, ref.version_id)] = data["content_type"]
        return JSONResponse(
            {
                "version_id": ref.version_id,
                "etag": head.etag,
                "crc64nvme_b64": base64.b64encode(head.crc64nvme).decode()
                if head.crc64nvme
                else None,
            }
        )
    if path == "/_sim/s3/restore" and request.method == "POST":
        ref = _ref(facade, data["bucket"], data["key"], data.get("version_id"))
        speed = RestoreSpeed(data.get("speed", "standard"))
        handle = await sim.restore(ref, speed, data.get("days"))
        return JSONResponse({"provider_ref": handle.provider_ref})
    if path == "/_sim/s3/stats":
        return JSONResponse({"buckets": {b: dict(c) for b, c in facade.stats.items()}})
    if path == "/_sim/s3/stats/reset" and request.method == "POST":
        facade.stats.clear()
        return JSONResponse({})
    if path == "/_sim/imds":
        if request.method == "POST" and "ttl_seconds" in data:
            facade.imds.ttl = timedelta(seconds=int(data["ttl_seconds"]))
        st = facade.imds
        return JSONResponse(
            {"token_puts": st.token_puts, "credential_fetches": st.credential_fetches,
             "issued": st.issued, "ttl_seconds": st.ttl.total_seconds()}
        )  # fmt: skip
    if path == "/_sim/sts":
        return JSONResponse({"calls": [{"role": r, "session": s} for r, s, _ in facade.sts_calls]})
    if path == "/_sim/advance" and request.method == "POST":
        advance = getattr(sim.clock, "advance", None)
        if advance is None:
            return JSONResponse({"error": "the clock can't be moved"}, status_code=409)
        advance(timedelta(seconds=float(data.get("seconds", 0))))
        return JSONResponse({"now": sim.clock.now().isoformat()})
    if path == "/_sim/clock":
        return JSONResponse({"now": sim.clock.now().isoformat()})
    if path == "/_sim/backend" and request.method == "POST":
        from . import rpc

        if data.get("op") not in rpc.OPS:
            return JSONResponse(
                {"error": {"type": "UnsupportedOperation", "message": "not a sim RPC"}},
                status_code=400,
            )
        try:
            args = rpc.decode(data.get("args") or [])
            kwargs = {k: rpc.decode(v) for k, v in (data.get("kwargs") or {}).items()}
            result = getattr(sim, data["op"])(*args, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return JSONResponse({"result": rpc.encode(result)})
        except Exception as e:  # noqa: BLE001 - returned to the caller, raised there
            return JSONResponse({"error": rpc.error_body(e)})
    return JSONResponse({"error": f"no admin route {request.method} {path}"}, status_code=404)
