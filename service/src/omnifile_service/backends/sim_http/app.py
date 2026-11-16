"""An Azure Blob data-plane HTTP facade over `SimBackend`.

Exactly the operations the edge uses, per Microsoft's REST reference
(https://learn.microsoft.com/en-us/rest/api/storageservices/, read 2026-10-05):

- Put Block (`PUT ?comp=block&blockid=`): verifies Content-MD5 or
  x-ms-content-crc64 (base64 of the little-endian CRC-64/NVME of the body).
- Put Block List (`PUT ?comp=blocklist`): commits blocks in order into
  `SimBackend.put`, so sim records one CRC per block. Honours
  x-ms-blob-content-md5, x-ms-blob-content-type, x-ms-meta-* and
  x-ms-access-tier. With no tier header, a new blob takes the account default
  (Smart on the two-account layout) and an overwrite inherits the old blob's
  tier.
- Get Blob (`GET`): with `versionid`, Range / x-ms-range, and
  x-ms-range-get-content-crc64 / -md5 for ranges up to 4 MiB.
- Get Blob Properties (`HEAD`).
- Get Block List (`GET ?comp=blocklist`): the committed blocks (name and
  size, in order) of the blob or, with `versionid`, of that version; the
  service checks them at upload `complete`. Uncommitted blocks are listed
  for `blocklisttype=uncommitted|all`.

Everything else answers 501 NotImplemented. Auth is user-delegation SAS only
(`sas.py`); an Authorization header (Bearer or SharedKey) is refused.

URLs are path-style, as for Azurite: `http://host:port/<account>/<container>/<blob>`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..base import (
    Immutable,
    LogicalTier,
    ObjectHead,
    ObjectNotFound,
    ObjectRef,
    RestoreState,
    Throttled,
    UnsupportedOperation,
)
from ..sim import SimBackend, crc64nvme
from ..sim.faults import Unavailable
from . import errors, sas
from .errors import AzureError

DEFAULT_VERSION = sas.DEFAULT_SIGNED_VERSION
MiB = 1024 * 1024
MAX_BLOCK_BYTES = 4000 * MiB  # Put Block, version 2019-12-12 and later
MAX_COMMITTED_BLOCKS = 50_000
MAX_UNCOMMITTED_BLOCKS = 100_000
MAX_RANGE_CHECKSUM = 4 * MiB  # x-ms-range-get-content-crc64 / -md5
MAX_BLOCK_LIST_BODY = 8 * MiB  # [sim] guard; 50,000 ids fit easily
CREATE_PERMISSION_VERSION = "2026-04-06"  # 'c' for Put Block / Put Block List
# Metadata names "must adhere to the naming rules for C# identifiers" (Put Block List).
_META_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: The S3 request each RPC on the S3 sim stands for (`BlobFacade.count_s3_call`).
S3_REQUEST_OF = {
    "head": "HEAD",
    "restore_status": "HEAD",
    "committed_blocks": "HEAD",
    "list": "LIST",
    "list_versions": "LIST",
    "list_all_versions": "LIST",
    "get_tags": "GET_TAGS",
    "read_changes": "EVENTS",
    "restore": "RESTORE",  # RestoreObject: a temporary copy, the object itself unchanged
    "put_bytes": "PUT",
    "put_synthetic": "PUT",
    "copy_object": "COPY",  # counted as COPY_FROM on the source, COPY on the destination
    "set_class": "COPY",  # an S3 class change is CopyObject onto the same key
    "set_metadata": "COPY",  # so is a metadata replace
    "delete": "DELETE",
    "set_tags": "TAG",
    "set_retention": "RETENTION",
    "set_legal_hold": "LEGAL_HOLD",
    "create_container": "CREATE_BUCKET",
    "set_container_policy": "BUCKET_POLICY",
    "delete_container_policy": "BUCKET_POLICY",
}
#: The ones that change a bucket (decision 40: never a source bucket's).
S3_WRITES = ("PUT", "COPY", "DELETE", "TAG", "RETENTION", "LEGAL_HOLD", "CREATE_BUCKET",
             "BUCKET_POLICY")  # fmt: skip
_TIERS = {
    "hot": LogicalTier.HOT,
    "cool": LogicalTier.WARM,
    "cold": LogicalTier.COLD,
    "archive": LogicalTier.ARCHIVE,
    "smart": LogicalTier.SMART,
}


def azure_crc64(data: bytes) -> str:
    """The x-ms-content-crc64 value for `data`: base64 of the little-endian
    CRC-64/NVME (azure-storage-blob `calculate_crc64_bytes`)."""
    return base64.b64encode(crc64nvme(data).to_bytes(8, "little")).decode()


def _http_date(t: datetime) -> str:
    return format_datetime(t.astimezone(UTC), usegmt=True)


@dataclass
class _Staging:
    blocks: dict[str, bytes] = field(default_factory=dict)  # base64 id -> bytes


@dataclass
class _Committed:
    etag: str | None
    blocks: list[tuple[str, int]]  # (base64 id, length) in blob order


@dataclass
class _Props:
    content_type: str = "application/octet-stream"
    content_md5: bytes | None = None


class BlobFacade:
    """State behind the HTTP app: the sim, the delegation keys, staged blocks."""

    def __init__(
        self,
        sim: SimBackend,
        *,
        key_lifetime: timedelta = sas.MAX_KEY_LIFETIME,
        key_file: str | None = None,
        s3: SimBackend | None = None,
    ) -> None:
        self.sim = sim
        #: An S3-flavoured sim served as presigned GETs under /_s3/
        #: (`s3get.py`); the RPC reaches it with ``"backend": "s3"``.
        self.s3 = s3
        #: Bytes of each S3 object sent over presigned GETs ("bucket/key" ->
        #: bytes), so a harness can tell a resumed fill from a restarted one.
        self.s3_served: dict[str, int] = {}
        #: Requests to the S3 sim per bucket and S3 request (`count_s3`):
        #: OmniFile's (the service's RPCs and the edge's presigned GETs) in
        #: `omnifile`, a test harness's own (RPC bodies marked
        #: ``"harness": true``) in `harness`, so a test can show that only the
        #: harness ever wrote a source bucket (decision 40).
        self.s3_requests: dict[str, dict[str, dict[str, int]]] = {"omnifile": {}, "harness": {}}
        self._key_lifetime = key_lifetime
        #: Where the newest user delegation key is published for other
        #: processes (the api and the worker read it); rewritten on rotation.
        self.key_file = key_file
        self.keys: list[sas.UserDelegationKey] = []
        self._staging: dict[tuple[str, str], _Staging] = {}
        self._committed: dict[tuple[str, str], _Committed] = {}
        #: (container, blob, version_id) -> committed [(block id, size)], for Get Block List.
        self._block_lists: dict[tuple[str, str, str | None], list[tuple[str, int]]] = {}
        self._props: dict[tuple[str, str, str | None], _Props] = {}

    def count_s3(self, who: str, bucket: str, request: str) -> None:
        per = self.s3_requests[who].setdefault(bucket, {})
        per[request] = per.get(request, 0) + 1

    def count_s3_call(self, who: str, op: str, args: list[Any]) -> None:
        """Count one RPC on the S3 sim as the S3 request(s) it stands for."""
        request = S3_REQUEST_OF.get(op)
        if request is None or not args:
            return
        first = args[0]
        ref = getattr(first, "ref", first)  # a RestoreHandle names its object
        bucket = ref.container if isinstance(ref, ObjectRef) else str(first)
        if op == "copy_object":
            # CopyObject reads its source and writes its destination.
            self.count_s3(who, bucket, "COPY_FROM")
            if len(args) > 1:
                self.count_s3(who, str(args[1]), "COPY")
            return
        self.count_s3(who, bucket, request)

    # --- keys and SAS (test helpers) -------------------------------------

    @property
    def key(self) -> sas.UserDelegationKey:
        """The newest valid user delegation key; a new one is issued when it lapses."""
        now = self.sim.clock.now()
        if not self.keys or not self.keys[-1].signed_start <= now < self.keys[-1].signed_expiry:
            self.keys.append(sas.UserDelegationKey.issue(now, self._key_lifetime))
            self.publish_key()
        return self.keys[-1]

    def publish_key(self) -> None:
        """Write the newest key to `key_file` (mode 0600), atomically."""
        if not self.key_file or not self.keys:
            return
        import json
        import os

        tmp = f"{self.key_file}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.keys[-1].to_json(), fh)
        os.replace(tmp, self.key_file)

    def mint_sas(
        self,
        account: str,
        container: str,
        blob: str | None,
        permissions: str,
        ttl: timedelta = timedelta(hours=1),
        **kw: Any,
    ) -> str:
        """A user-delegation SAS query string, signed with this facade's key."""
        expiry = kw.pop("expiry", None) or self.sim.clock.now() + ttl
        return sas.mint(self.key, account, container, blob, permissions, expiry, **kw)

    # --- helpers -----------------------------------------------------------

    def _sim_container(self, account: str, container: str) -> str:
        return f"{account}/{container}"

    def known_account(self, account: str) -> bool:
        return account in self.sim._accounts

    async def head(
        self, account: str, container: str, blob: str, version: str | None
    ) -> ObjectHead:
        ref = ObjectRef(self.sim.backend_id, self._sim_container(account, container), blob, version)
        try:
            return await self.sim.head(ref)
        except ObjectNotFound:
            raise errors.blob_not_found() from None

    async def exists(self, account: str, container: str, blob: str) -> ObjectHead | None:
        try:
            return await self.head(account, container, blob, None)
        except AzureError:
            return None

    def props(self, head: ObjectHead) -> _Props:
        r = head.ref
        return self._props.get((r.container, r.key, r.version_id), _Props())


# --- request plumbing ------------------------------------------------------


@dataclass
class _Ctx:
    request: Request
    facade: BlobFacade
    request_id: str
    version: str
    account: str
    container: str
    blob: str
    query: dict[str, str]


def _common_headers(ctx_version: str, request_id: str, now: datetime, req: Request) -> dict:
    h = {
        "x-ms-request-id": request_id,
        "x-ms-version": ctx_version,
        "Date": _http_date(now),
        "Server": "Windows-Azure-Blob/1.0 omnifile-sim_http",
    }
    client_id = req.headers.get("x-ms-client-request-id")
    if client_id and len(client_id) <= 1024:
        h["x-ms-client-request-id"] = client_id
    return h


def _error_response(
    err: AzureError, common: dict[str, str], method: str, now: datetime
) -> Response:
    headers = {**common, **err.headers, "x-ms-error-code": err.code}
    if method == "HEAD":  # HEAD errors carry no body; the code is in x-ms-error-code
        return Response(status_code=err.status, headers=headers)
    body = errors.error_body(err, common["x-ms-request-id"], now.isoformat())
    return Response(body, status_code=err.status, headers=headers, media_type="application/xml")


def _map_backend_error(e: BaseException) -> AzureError:
    if isinstance(e, AzureError):
        return e
    if isinstance(e, ObjectNotFound):
        return errors.blob_not_found()
    if isinstance(e, Throttled):
        secs = max(1, int((e.retry_after or timedelta(seconds=1)).total_seconds() + 0.999))
        return AzureError(
            503,
            "ServerBusy",
            "Operations per second is over the account limit.",
            headers={"Retry-After": str(secs)},
        )
    if isinstance(e, Unavailable):
        return AzureError(503, "ServerBusy", "The server is currently unable to receive requests.")
    if isinstance(e, Immutable):
        return AzureError(
            409,
            "BlobImmutableDueToPolicy",
            "This operation is not permitted as the blob is immutable due to a policy.",
        )
    if isinstance(e, UnsupportedOperation):
        return AzureError(
            409, "BlobAccessTierNotSupportedForAccountType", f"{e}"
        )  # [sim] status; the code is in the REST spec's StorageErrorCode enum
    return AzureError(
        500, "InternalError", "The server encountered an internal error. Please retry the request."
    )


def _require_perm(
    ctx: _Ctx, grant: sas.Grant, *, read: bool = False, create_ok: bool = False
) -> None:
    p = grant.permissions
    if read:
        if "r" not in p:
            raise errors.permission_mismatch()
        return
    if grant.resource == "bv":
        raise errors.permission_mismatch()  # a version SAS cannot write
    if "w" in p:
        return
    if create_ok and "c" in p and ctx.version >= CREATE_PERMISSION_VERSION:
        return
    raise errors.permission_mismatch()


def _b64_digest(value: str, size: int, header: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != size:
        if header.lower().endswith("md5"):
            raise errors.invalid_md5()
        raise errors.invalid_header(header, "not base64 of the expected length")
    return raw


def _verify_body(req: Request, body: bytes) -> dict[str, str]:
    """Transactional Content-MD5 / x-ms-content-crc64; returns the response checksum header."""
    md5_h = req.headers.get("content-md5")
    crc_h = req.headers.get("x-ms-content-crc64")
    if md5_h and crc_h:
        raise errors.invalid_header(
            "Content-MD5, x-ms-content-crc64", "only one of them may be present"
        )
    if md5_h:
        want = _b64_digest(md5_h, 16, "Content-MD5")
        got = hashlib.md5(body).digest()
        if want != got:
            raise errors.md5_mismatch()
        return {"Content-MD5": base64.b64encode(got).decode()}
    computed = azure_crc64(body)
    if crc_h:
        want = _b64_digest(crc_h, 8, "x-ms-content-crc64")
        if want != base64.b64decode(computed):
            raise errors.crc64_mismatch()
    return {"x-ms-content-crc64": computed}


async def _read_body(req: Request, limit: int) -> bytes:
    length = req.headers.get("content-length")
    if length is None:
        raise AzureError(
            411, "MissingContentLengthHeader", "The Content-Length header was not specified."
        )
    try:
        n = int(length)
    except ValueError:
        raise errors.invalid_header("Content-Length") from None
    if n > limit:
        raise AzureError(
            413,
            "RequestBodyTooLarge",
            "The size of the request body exceeds the maximum size permitted.",
            headers={"x-ms-max-body-size": str(limit)},
        )
    return await req.body()


# --- operations --------------------------------------------------------------


async def put_block(ctx: _Ctx, grant: sas.Grant) -> Response:
    f = ctx.facade
    blockid = ctx.query.get("blockid")
    if not blockid:
        raise AzureError(
            400, "MissingRequiredQueryParameter", "A required query parameter was not specified."
        )
    try:
        raw_id = base64.b64decode(blockid, validate=True)
    except (binascii.Error, ValueError):
        raw_id = None
    if raw_id is None or len(raw_id) > 64:
        raise AzureError(
            400,
            "InvalidBlockId",
            "The specified block ID is invalid. The block ID must be Base64-encoded.",
        )
    existing = await f.exists(ctx.account, ctx.container, ctx.blob)
    _require_perm(ctx, grant, create_ok=existing is None)
    body = await _read_body(ctx.request, MAX_BLOCK_BYTES)
    checksum = _verify_body(ctx.request, body)
    staging = f._staging.setdefault(
        (f._sim_container(ctx.account, ctx.container), ctx.blob), _Staging()
    )
    if staging.blocks and blockid not in staging.blocks:
        if len(next(iter(staging.blocks))) != len(blockid):
            raise AzureError(
                400, "InvalidBlobOrBlock", "The specified blob or block content is invalid."
            )
        if len(staging.blocks) >= MAX_UNCOMMITTED_BLOCKS:
            raise AzureError(
                409,
                "BlockCountExceedsLimit",
                "The uncommitted block count cannot exceed the maximum limit of 100,000 blocks.",
            )
    staging.blocks[blockid] = body
    return Response(
        status_code=201,
        headers={**checksum, "x-ms-request-server-encrypted": "true"},
    )


def _parse_block_list(body: bytes) -> list[tuple[str, str]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise AzureError(
            400, "InvalidXmlDocument", "XML specified is not syntactically valid."
        ) from None
    if root.tag != "BlockList":
        raise AzureError(400, "InvalidXmlDocument", "The root element must be BlockList.")
    out = []
    for el in root:
        if el.tag not in ("Committed", "Uncommitted", "Latest"):
            raise AzureError(400, "InvalidXmlDocument", f"Unexpected element {el.tag}.")
        out.append((el.tag, (el.text or "").strip()))
    if len(out) > MAX_COMMITTED_BLOCKS:
        raise AzureError(
            400, "BlockListTooLong", "The block list may not contain more than 50,000 blocks."
        )
    return out


async def put_block_list(ctx: _Ctx, grant: sas.Grant) -> Response:
    f, req, sim = ctx.facade, ctx.request, ctx.facade.sim
    sim_container = f._sim_container(ctx.account, ctx.container)
    existing = await f.exists(ctx.account, ctx.container, ctx.blob)
    _require_perm(ctx, grant, create_ok=existing is None)
    body = await _read_body(req, MAX_BLOCK_LIST_BODY)
    checksum = _verify_body(req, body)
    wanted = _parse_block_list(body)

    tier_h = req.headers.get("x-ms-access-tier")
    if tier_h is not None:
        tier: LogicalTier | None = _TIERS.get(tier_h.lower())
        if tier is None:
            raise AzureError(400, "InvalidBlobTier", "The specified blob tier is invalid.")
    elif existing is not None:
        # "a blob inherits the tier from the old blob if an x-ms-access-tier
        # header isn't provided" (Put Block List remarks).
        tier = None if existing.logical_tier is LogicalTier.SMART else existing.logical_tier
    else:
        tier = None
    if existing is not None and existing.logical_tier is LogicalTier.ARCHIVE:
        raise errors.blob_archived()  # "Overwriting an archive blob fails."

    metadata: dict[str, str] = {}
    for name, value in req.headers.items():
        if name.startswith("x-ms-meta-"):
            mname = name[len("x-ms-meta-") :]
            if not _META_NAME.match(mname):
                raise AzureError(
                    400,
                    "InvalidMetadata",
                    "The metadata specified is invalid. It has characters that are not permitted.",
                )
            metadata[mname] = value
    content_md5 = None
    if md5_h := req.headers.get("x-ms-blob-content-md5"):
        content_md5 = _b64_digest(md5_h, 16, "x-ms-blob-content-md5")  # stored, not validated

    key = (sim_container, ctx.blob)
    staging = f._staging.get(key, _Staging())
    committed = f._committed.get(key)
    if committed is not None and (existing is None or existing.etag != committed.etag):
        committed = None  # the blob changed outside the facade
    offsets: dict[str, tuple[int, int]] = {}
    pos = 0
    for bid, length in committed.blocks if committed else []:
        offsets.setdefault(bid, (pos, length))
        pos += length

    chunks: list[bytes] = []
    new_list: list[tuple[str, int]] = []
    for kind, bid in wanted:
        data: bytes | None = None
        if kind in ("Uncommitted", "Latest") and bid in staging.blocks:
            data = staging.blocks[bid]
        elif kind in ("Committed", "Latest") and bid in offsets and existing is not None:
            off, length = offsets[bid]
            data = b"".join([c async for c in sim.get_range(existing.ref, off, length)])
        if data is None:
            raise AzureError(400, "InvalidBlockList", "The specified block list is invalid.")
        chunks.append(data)
        new_list.append((bid, len(data)))

    async def stream() -> AsyncIterator[bytes]:
        for c in chunks:
            yield c

    ref = await sim.put(sim_container, ctx.blob, stream(), tier, metadata)
    head = await sim.head(ref)
    f._staging.pop(key, None)  # uncommitted blocks not in the list are discarded
    f._committed[key] = _Committed(head.etag, new_list)
    f._block_lists[(head.ref.container, head.ref.key, head.ref.version_id)] = new_list
    f._props[(head.ref.container, head.ref.key, head.ref.version_id)] = _Props(
        req.headers.get("x-ms-blob-content-type") or "application/octet-stream", content_md5
    )
    headers = {
        **checksum,
        "ETag": head.etag or "",
        "Last-Modified": _http_date(head.last_modified or sim.clock.now()),
        "x-ms-request-server-encrypted": "true",
    }
    if ref.version_id:
        headers["x-ms-version-id"] = ref.version_id
    return Response(status_code=201, headers=headers)


async def get_block_list(ctx: _Ctx, grant: sas.Grant) -> Response:
    _require_perm(ctx, grant, read=True)
    f = ctx.facade
    kind = ctx.query.get("blocklisttype", "committed").lower()
    if kind not in ("committed", "uncommitted", "all"):
        raise errors.invalid_header("blocklisttype", "must be committed, uncommitted or all")
    head = await f.head(ctx.account, ctx.container, ctx.blob, ctx.query.get("versionid"))
    committed = f._block_lists.get((head.ref.container, head.ref.key, head.ref.version_id))
    if committed is None:  # written by the sim directly, not through this facade
        try:
            committed = [(b.block_id, b.size) for b in await f.sim.committed_blocks(head.ref)]
        except UnsupportedOperation:
            committed = []
    staging = f._staging.get((f._sim_container(ctx.account, ctx.container), ctx.blob))
    uncommitted = [(b, len(d)) for b, d in staging.blocks.items()] if staging else []

    def blocks(items: list[tuple[str, int]]) -> str:
        return "".join(f"<Block><Name>{b}</Name><Size>{n}</Size></Block>" for b, n in items)

    body = '<?xml version="1.0" encoding="utf-8"?><BlockList>'
    if kind in ("committed", "all"):
        body += f"<CommittedBlocks>{blocks(committed)}</CommittedBlocks>"
    if kind in ("uncommitted", "all"):
        body += f"<UncommittedBlocks>{blocks(uncommitted)}</UncommittedBlocks>"
    body += "</BlockList>"
    h = {
        "Content-Type": "application/xml",
        "ETag": head.etag or "",
        "Last-Modified": _http_date(head.last_modified or f.sim.clock.now()),
        "x-ms-blob-content-length": str(head.size),
    }
    return Response(content=body.encode(), status_code=200, headers=h)


def _parse_range(req: Request) -> tuple[int, int | None] | None:
    value = req.headers.get("x-ms-range") or req.headers.get("range")
    if not value:
        return None
    m = re.fullmatch(r"bytes=(\d+)-(\d*)", value.strip())
    if not m:
        raise errors.invalid_header("x-ms-range", "use bytes=start-end or bytes=start-")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    if end is not None and end < start:
        raise errors.invalid_header("x-ms-range", "end before start")
    return start, end


async def _blob_headers(ctx: _Ctx, head: ObjectHead) -> dict[str, str]:
    f = ctx.facade
    props = f.props(head)
    h: dict[str, str] = {
        "Content-Type": props.content_type,
        "ETag": head.etag or "",
        "Last-Modified": _http_date(head.last_modified or f.sim.clock.now()),
        "x-ms-blob-type": "BlockBlob",
        "Accept-Ranges": "bytes",
        "x-ms-server-encrypted": "true",
        "x-ms-access-tier": head.native_class,
        "x-ms-lease-state": "available",
        "x-ms-lease-status": "unlocked",
    }
    if head.native_class == "Smart":
        h["x-ms-smart-access-tier"] = "Hot"  # [sim] Smart's inner movement is not modelled
    if head.restore_state is RestoreState.IN_PROGRESS:
        h["x-ms-archive-status"] = "rehydrate-pending-to-hot"
    for k, v in head.metadata.items():
        h[f"x-ms-meta-{k}"] = v
    if head.ref.version_id:
        h["x-ms-version-id"] = head.ref.version_id
        cur = await f.exists(ctx.account, ctx.container, ctx.blob)
        is_current = cur is not None and cur.ref.version_id == head.ref.version_id
        h["x-ms-is-current-version"] = "true" if is_current else "false"
    if head.retention_until is not None:
        h["x-ms-immutability-policy-until-date"] = _http_date(head.retention_until)
    if head.legal_hold:
        h["x-ms-legal-hold"] = "true"
    tags = await f.sim.get_tags(head.ref)
    if tags:
        h["x-ms-tag-count"] = str(len(tags))
    return h


async def get_blob_properties(ctx: _Ctx, grant: sas.Grant) -> Response:
    _require_perm(ctx, grant, read=True)
    head = await ctx.facade.head(ctx.account, ctx.container, ctx.blob, ctx.query.get("versionid"))
    h = await _blob_headers(ctx, head)
    h["Content-Length"] = str(head.size)
    md5 = ctx.facade.props(head).content_md5
    if md5:
        h["Content-MD5"] = base64.b64encode(md5).decode()
    return Response(status_code=200, headers=h)


async def get_blob(ctx: _Ctx, grant: sas.Grant) -> Response:
    _require_perm(ctx, grant, read=True)
    req, f = ctx.request, ctx.facade
    head = await f.head(ctx.account, ctx.container, ctx.blob, ctx.query.get("versionid"))
    if head.restore_state is RestoreState.IN_PROGRESS:
        raise errors.blob_being_rehydrated()
    if head.restore_state is not RestoreState.NOT_NEEDED:
        raise errors.blob_archived()  # "A Get Blob operation on an archived block blob will fail."
    rng = _parse_range(req)
    want_crc = req.headers.get("x-ms-range-get-content-crc64", "").lower() == "true"
    want_md5 = req.headers.get("x-ms-range-get-content-md5", "").lower() == "true"
    if want_crc and want_md5:
        raise errors.invalid_header(
            "x-ms-range-get-content-crc64", "cannot be combined with x-ms-range-get-content-md5"
        )
    if (want_crc or want_md5) and rng is None:
        raise errors.invalid_header("x-ms-range-get-content-crc64", "needs a Range header")
    size = head.size
    h = await _blob_headers(ctx, head)
    props = f.props(head)
    if rng is None:
        start, length, status = 0, size, 200
        if props.content_md5:
            h["Content-MD5"] = base64.b64encode(props.content_md5).decode()
    else:
        start, end = rng
        if start >= size:
            raise AzureError(
                416,
                "InvalidRange",
                "The range specified is invalid for the current size of the resource.",
                headers={"Content-Range": f"bytes */{size}"},
            )
        last = size - 1 if end is None else min(end, size - 1)
        length, status = last - start + 1, 206
        h["Content-Range"] = f"bytes {start}-{last}/{size}"
        if props.content_md5:
            h["x-ms-blob-content-md5"] = base64.b64encode(props.content_md5).decode()
        if (want_crc or want_md5) and length > MAX_RANGE_CHECKSUM:
            raise errors.invalid_header("x-ms-range-get-content-crc64", "the range exceeds 4 MiB")
    h["Content-Length"] = str(length)
    it = f.sim.get_range(head.ref, start, length)
    if want_crc or want_md5:
        data = b"".join([c async for c in it])
        if want_crc:
            h["x-ms-content-crc64"] = azure_crc64(data)
        else:
            h["Content-MD5"] = base64.b64encode(hashlib.md5(data).digest()).decode()
        return Response(data, status_code=status, headers=h)
    # Pull the first chunk now so backend errors become proper responses.
    first = await anext(it, None)

    async def body() -> AsyncIterator[bytes]:
        if first is not None:
            yield first
        async for chunk in it:
            yield chunk

    return StreamingResponse(body(), status_code=status, headers=h)


# --- dispatch ------------------------------------------------------------------


def _split(request: Request, facade: BlobFacade) -> tuple[str, str, str]:
    path = request.path_params.get("path", "")
    host = (request.headers.get("host") or "").split(":")[0]
    label = host.split(".")[0]
    parts = path.split("/", 2)
    if ".blob." in host and facade.known_account(label) and parts[0] != label:
        parts = [label, *path.split("/", 1)]  # host-style: <account>.blob.<domain>
    if len(parts) < 3 or not all(parts):
        raise errors.not_implemented("account or container operations")
    account, container, blob = parts
    if not facade.known_account(account):
        raise AzureError(
            400, "InvalidUri", "The requested URI does not represent any resource on the server."
        )
    return account, container, blob


def create_app(facade: BlobFacade, *, admin: bool = False) -> Starlette:
    """The ASGI app. `admin=True` adds /_sim/ helpers for non-Python test
    harnesses: POST /_sim/sas (mint a SAS), POST /_sim/advance and GET
    /_sim/clock (virtual clock), POST /_sim/faults (fault injection,
    `sim/faults.py`), POST /_sim/backend (`rpc.py`: the backend's control
    calls for the worker), GET /_sim/s3-served (bytes sent per S3
    object) and GET /_sim/s3-requests (requests to the S3 sim per bucket)."""

    async def blob_endpoint(request: Request) -> Response:
        now = facade.sim.clock.now()
        request_id = str(uuid.uuid4())
        version = (
            request.headers.get("x-ms-version") or request.query_params.get("sv") or DEFAULT_VERSION
        )
        common = _common_headers(version, request_id, now, request)
        try:
            if "authorization" in request.headers:
                raise errors.authentication_failed(
                    "sim_http accepts only user delegation SAS; Authorization headers "
                    "(Bearer, SharedKey) are rejected"
                )
            account, container, blob = _split(request, facade)
            query = dict(request.query_params)
            grant = sas.validate(
                facade.keys,
                query,
                account=account,
                container=container,
                blob=blob,
                request_version_id=query.get("versionid"),
                now=now,
                scheme=request.url.scheme,
                client_ip=request.client.host if request.client else None,
            )
            ctx = _Ctx(request, facade, request_id, version, account, container, blob, query)
            comp = query.get("comp")
            method = request.method
            if method == "PUT" and comp == "block":
                resp = await put_block(ctx, grant)
            elif method == "PUT" and comp == "blocklist":
                resp = await put_block_list(ctx, grant)
            elif method == "GET" and comp is None:
                resp = await get_blob(ctx, grant)
            elif method == "GET" and comp == "blocklist":
                resp = await get_block_list(ctx, grant)
            elif method == "HEAD" and comp is None:
                resp = await get_blob_properties(ctx, grant)
            else:
                raise errors.not_implemented(f"{method} comp={comp}")
        except Exception as e:  # AzureError, BackendError, injected faults (TimeoutError...)
            return _error_response(_map_backend_error(e), common, request.method, now)
        resp.headers.update(common)
        return resp

    async def admin_sas(request: Request) -> Response:
        body = await request.json()
        query = facade.mint_sas(
            body["account"],
            body["container"],
            body.get("blob"),
            body.get("permissions", "r"),
            timedelta(seconds=float(body.get("ttl_seconds", 3600))),
            version_id=body.get("version_id"),
        )
        base = str(request.base_url).rstrip("/")
        path = "/".join(p for p in (body["account"], body["container"], body.get("blob")) if p)
        return JSONResponse({"sas": query, "url": f"{base}/{path}?{query}"})

    async def admin_advance(request: Request) -> Response:
        """Move the clock forward. With an `OffsetClock` this moves every
        process sharing its file (api, worker, Postgres under libfaketime).
        A user delegation key that lapsed is replaced and republished."""
        body = await request.json()
        advance = getattr(facade.sim.clock, "advance", None)
        if advance is None:
            return JSONResponse({"error": "the clock is not virtual"}, status_code=409)
        advance(timedelta(seconds=float(body["seconds"])))
        _ = facade.key  # rotate (and publish) the key if it lapsed
        return JSONResponse({"now": facade.sim.clock.now().isoformat()})

    async def admin_clock(request: Request) -> Response:
        return JSONResponse({"now": facade.sim.clock.now().isoformat()})

    async def admin_backend(request: Request) -> Response:
        """One `SimBackend` call for another process (`rpc.py`)."""
        import inspect

        from . import rpc

        body = await request.json()
        op = body.get("op")
        sim = facade.s3 if body.get("backend") == "s3" else facade.sim
        if sim is None:
            return JSONResponse({"error": {"type": "UnsupportedOperation",
                                           "message": "this facade hosts no S3 sim"}},
                                status_code=400)  # fmt: skip
        if op not in rpc.OPS:
            return JSONResponse({"error": {"type": "UnsupportedOperation",
                                           "message": f"{op!r} is not a sim RPC"}},
                                status_code=400)  # fmt: skip
        try:
            args = rpc.decode(body.get("args") or [])
            kwargs = {k: rpc.decode(v) for k, v in (body.get("kwargs") or {}).items()}
            if sim is facade.s3:
                facade.count_s3_call("harness" if body.get("harness") else "omnifile", op, args)
            result = getattr(sim, op)(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:  # noqa: BLE001 - returned to the caller, raised there
            return JSONResponse({"error": rpc.error_body(e)})
        return JSONResponse({"result": rpc.encode(result)})

    async def admin_faults(request: Request) -> Response:
        """Register a fault, for harnesses in another process (the M1 e2e test):

        - ``{"clear": true}``: drop every fault;
        - ``{"op": "get_range", "fail_nth": 2}``: that call (from now) fails;
        - ``{"op": "get_range", "latency_ms": 300, "count": 10}``: delay calls
          (real time; ``count`` omitted = until cleared);
        - ``{"op": "get_range", "outage_s": 20}``: calls fail as Unavailable
          for the next 20 s of the backend clock (``op`` omitted = all ops).

        ``"backend": "s3"`` aims it at the S3 sim (its presigned GETs) instead.
        """
        from ..sim.faults import Op

        body = await request.json()
        sim = facade.s3 if body.get("backend") == "s3" else facade.sim
        if sim is None:
            return JSONResponse({"error": "this facade hosts no S3 sim"}, status_code=400)
        faults = sim.faults
        try:
            if body.get("clear"):
                faults.clear()
            elif "fail_nth" in body:
                faults.fail_nth(Op(body["op"]), int(body["fail_nth"]))
            elif "latency_ms" in body:
                count = body.get("count")
                faults.latency(
                    Op(body["op"]),
                    timedelta(milliseconds=float(body["latency_ms"])),
                    count=None if count is None else int(count),
                )
            elif "outage_s" in body:
                start = sim.clock.now()
                ops = {Op(body["op"])} if body.get("op") else None
                faults.outage(start, start + timedelta(seconds=float(body["outage_s"])), ops)
            else:
                return JSONResponse({"error": "no fault given"}, status_code=400)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def admin_s3_served(request: Request) -> Response:
        return JSONResponse(facade.s3_served)

    async def admin_s3_requests(request: Request) -> Response:
        return JSONResponse({**facade.s3_requests, "writes": list(S3_WRITES)})

    routes = []
    if admin:
        routes += [
            Route("/_sim/sas", admin_sas, methods=["POST"]),
            Route("/_sim/advance", admin_advance, methods=["POST"]),
            Route("/_sim/clock", admin_clock, methods=["GET"]),
            Route("/_sim/backend", admin_backend, methods=["POST"]),
            Route("/_sim/faults", admin_faults, methods=["POST"]),
            Route("/_sim/s3-served", admin_s3_served, methods=["GET"]),
            Route("/_sim/s3-requests", admin_s3_requests, methods=["GET"]),
        ]

    async def s3_endpoint(request: Request) -> Response:
        from . import s3get

        return await s3get.get_object(request, facade)

    routes.append(Route("/_s3/{path:path}", s3_endpoint, methods=["GET", "HEAD"]))
    routes.append(
        Route("/{path:path}", blob_endpoint, methods=["GET", "HEAD", "PUT", "POST", "DELETE"])
    )
    return Starlette(routes=routes)
