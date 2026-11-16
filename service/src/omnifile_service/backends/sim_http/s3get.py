"""S3 GetObject over presigned URLs, for an S3-flavoured sim the facade hosts
(`BlobFacade.s3`): what the edge reads S3-resident files with in the
end-to-end stack (`docs/contracts/control-api.md` section 3,
`s3_presigned_url`).

Only what the edge uses: `GET` and `HEAD /_s3/<bucket>/<key>`, with
`versionId` and `Range: bytes=a-b` / `bytes=a-`. The answer carries
`x-amz-version-id`, as S3's does. An archived object that isn't restored
answers 403 `InvalidObjectState`, as S3 does [V].

**If-Match as a signed header** (as S3 presigning puts `if-match` in
`X-Amz-SignedHeaders`). A URL minted with `if_match` carries
`X-Sim-If-Match=1`, and its signature covers that ETag. A GET of it must send
exactly that `If-Match` header (else 403 `SignatureDoesNotMatch`), and when
the object's current ETag differs it answers 412 `PreconditionFailed`, as
S3 does: a replaced object never serves its bytes under the old URL.

**This is not SigV4.** The URL is signed by the facade's user delegation key:
`X-Amz-Signature` is the hex HMAC-SHA256 of `GET`, the bucket, the key, the
version and the expiry (`X-Sim-Expires`, Unix seconds), one per line. That
is enough to prove the service minted the URL for exactly this object
version and that it lapses; the edge treats the URL as opaque.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from ..base import BackendError, ObjectNotFound, ObjectRef, RestoreState
from ..sim.faults import Unavailable
from . import sas

PREFIX = "/_s3"
ARCHIVED = (RestoreState.ARCHIVED, RestoreState.IN_PROGRESS, RestoreState.EXPIRED)


def _signature(key: sas.UserDelegationKey, bucket: str, obj: str, version: str | None,
               expires: int, if_match: str | None = None) -> str:  # fmt: skip
    msg = f"GET\n{bucket}\n{obj}\n{version or ''}\n{expires}"
    if if_match is not None:
        # The signed header, as S3 signs `if-match` into a presigned URL.
        msg += f"\nif-match:{if_match}"
    return hmac.new(base64.b64decode(key.value), msg.encode(), hashlib.sha256).hexdigest()


def presign(
    key: sas.UserDelegationKey,
    base_url: str,
    bucket: str,
    obj: str,
    version: str | None,
    expires: datetime,
    *,
    if_match: str | None = None,
) -> str:
    """A presigned GET URL for one object version, served by the facade.
    With `if_match` the ETag is a signed header the GET must send."""
    exp = int(expires.timestamp())
    sig = _signature(key, bucket, obj, version, exp, if_match)
    query = f"X-Sim-Expires={exp}&X-Amz-Signature={sig}"
    if if_match is not None:
        query = f"X-Sim-If-Match=1&{query}"
    if version:
        query = f"versionId={quote(version, safe='')}&{query}"
    return f"{base_url.rstrip('/')}{PREFIX}/{quote(bucket)}/{quote(obj, safe='/')}?{query}"


def _xml_error(status: int, code: str, message: str) -> Response:
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code>'
        f"<Message>{message}</Message></Error>"
    )
    return Response(body, status_code=status, media_type="application/xml")


def _verify(keys: Sequence[sas.UserDelegationKey], bucket: str, obj: str,
            query: dict[str, str], now: datetime,
            if_match: str | None = None) -> Response | None:  # fmt: skip
    sig = query.get("X-Amz-Signature", "")
    try:
        exp = int(query.get("X-Sim-Expires", ""))
    except ValueError:
        return _xml_error(403, "AccessDenied", "Request has invalid parameters")
    if now.timestamp() > exp:
        return _xml_error(403, "AccessDenied", "Request has expired")
    version = query.get("versionId")
    signed = if_match if query.get("X-Sim-If-Match") == "1" else None
    if query.get("X-Sim-If-Match") == "1" and if_match is None:
        # A signed header that wasn't sent.
        return _xml_error(403, "SignatureDoesNotMatch", "The request signature does not match")
    if not any(
        hmac.compare_digest(sig, _signature(k, bucket, obj, version, exp, signed)) for k in keys
    ):
        return _xml_error(403, "SignatureDoesNotMatch", "The request signature does not match")
    return None


def _range(header: str | None, size: int) -> tuple[int, int] | None | Response:
    if header is None:
        return None
    spec = header.strip()
    if not spec.startswith("bytes=") or "," in spec:
        return _xml_error(400, "InvalidArgument", "Unsupported Range")
    a, _, b = spec[6:].partition("-")
    try:
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return _xml_error(400, "InvalidArgument", "Unsupported Range")
    if start >= size or end < start:
        return _xml_error(416, "InvalidRange", "The requested range is not satisfiable")
    return start, min(end, size - 1)


async def get_object(request: Request, facade) -> Response:  # type: ignore[no-untyped-def]
    sim = facade.s3
    if sim is None:
        return _xml_error(501, "NotImplemented", "this facade hosts no S3 sim")
    parts = request.path_params.get("path", "").split("/", 1)
    if len(parts) < 2 or not all(parts):
        return _xml_error(400, "InvalidRequest", "bucket and key are required")
    bucket, obj = parts
    query = dict(request.query_params)
    now = sim.clock.now()
    if_match = request.headers.get("if-match")
    denied = _verify(facade.keys, bucket, obj, query, now, if_match)
    if denied is not None:
        return denied
    version = query.get("versionId")
    ref = ObjectRef(sim.backend_id, bucket, obj, version)
    try:
        head = await sim.head(ref)
    except ObjectNotFound:
        code = "NoSuchVersion" if version else "NoSuchKey"
        return _xml_error(404, code, "The specified key does not exist.")
    if if_match is not None and if_match.strip() not in ("*", head.etag or ""):
        # The object was replaced since the URL was minted (S3 checks the
        # precondition before the storage class).
        msg = "At least one of the pre-conditions you specified did not hold"
        return _xml_error(412, "PreconditionFailed", msg)
    if head.restore_state in ARCHIVED:
        return _xml_error(403, "InvalidObjectState",
                          "The operation is not valid for the object's storage class")  # fmt: skip
    rng = _range(request.headers.get("range"), head.size)
    if isinstance(rng, Response):
        return rng
    headers = {
        "x-amz-version-id": head.ref.version_id or "null",
        "accept-ranges": "bytes",
        "etag": head.etag or "",
    }
    start, end = (0, head.size - 1) if rng is None else rng
    length = max(0, end - start + 1)
    headers["content-length"] = str(length)
    status = 200
    if rng is not None:
        status = 206
        headers["content-range"] = f"bytes {start}-{end}/{head.size}"
    elif request.headers.get("x-amz-checksum-mode", "").upper() == "ENABLED" and head.crc64nvme:
        # A whole-object request with checksum mode: the stored full-object
        # CRC64NVME, base64 of the big-endian value, as S3 answers it.
        headers["x-amz-checksum-crc64nvme"] = base64.b64encode(head.crc64nvme).decode()
        headers["x-amz-checksum-type"] = "FULL_OBJECT"
    facade.count_s3("omnifile", bucket, request.method)
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)

    served = f"{bucket}/{obj}"
    chunks = sim.get_range(head.ref, start, length)
    # The first chunk before the status line, so an injected fault is an
    # HTTP error, as S3 answers one, not a stream cut short.
    try:
        first = await anext(chunks) if length else b""
    except StopAsyncIteration:
        first = b""
    except Unavailable:
        return _xml_error(503, "ServiceUnavailable", "Service is unable to handle request.")
    except BackendError:
        return _xml_error(500, "InternalError", "We encountered an internal error.")

    async def body():  # type: ignore[no-untyped-def]
        chunk = first
        while chunk:
            facade.s3_served[served] = facade.s3_served.get(served, 0) + len(chunk)
            yield chunk
            chunk = await anext(chunks, b"")

    return StreamingResponse(body(), status_code=status, headers=headers)


__all__ = ["PREFIX", "get_object", "presign"]
