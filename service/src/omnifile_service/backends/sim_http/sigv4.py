"""SigV4 header authentication, checked the way AWS documents it.

Source: "Create a signed AWS API request" and "Elements of an AWS API
request signature" in the IAM user guide (read 2026-10-12). Only the
``Authorization: AWS4-HMAC-SHA256 ...`` header form is accepted; presigned
query auth is not (presigned URLs are served by `s3get.py`, not here).

- The canonical URI is the request path exactly as sent (S3 does not
  normalise or double-encode it).
- The canonical query string re-encodes each name and value (RFC 3986
  unreserved characters kept) and sorts by name, then value.
- Canonical headers are the signed headers, lower-cased, values trimmed
  with inner runs of spaces collapsed, multiple values joined by commas.
- The payload hash is ``x-amz-content-sha256`` when sent (S3), checked
  against the body unless it is ``UNSIGNED-PAYLOAD`` or a ``STREAMING-``
  value; otherwise the SHA-256 of the body.
- Session credentials need ``x-amz-security-token``; expired sessions get
  ``ExpiredToken``.
- With `check_skew`, ``x-amz-date`` must be within 15 minutes of the
  server's clock (``RequestTimeTooSkewed``). Off on a virtual clock.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_SKEW = timedelta(minutes=15)

_AUTH = re.compile(
    r"^AWS4-HMAC-SHA256\s+Credential=(?P<cred>[^,\s]+),\s*SignedHeaders=(?P<sh>[^,\s]+),"
    r"\s*Signature=(?P<sig>[0-9a-f]{64})$"
)
_SPACES = re.compile(r"\s+")


class SigV4Error(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Credential:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    expires_at: datetime | None = None
    #: Who it is, for tests ("edge", "service", "sts:<session name>").
    principal: str = ""


@dataclass(frozen=True)
class Verified:
    credential: Credential
    region: str
    service: str
    payload_hash: str


def _encode(s: str) -> str:
    return quote(s, safe="-_.~")


def canonical_query(raw_query: str) -> str:
    pairs = []
    for part in raw_query.split("&") if raw_query else []:
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((_encode(unquote(k)), _encode(unquote(v))))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = hmac.new(f"AWS4{secret}".encode(), date.encode(), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return k


def verify(
    *,
    method: str,
    raw_path: str,
    raw_query: str,
    headers: Mapping[str, Sequence[str]],
    body: bytes | None,
    lookup: Mapping[str, Credential],
    now: datetime,
    check_skew: bool,
) -> Verified:
    """Check one request's signature. `headers` maps lower-case names to
    their values; `body` None means "not read" (streamed, unsigned payload
    only). Raises `SigV4Error` with S3's status and code."""
    auth = (headers.get("authorization") or [""])[0].strip()
    if not auth:
        raise SigV4Error(403, "AccessDenied", "Anonymous access is not allowed")
    m = _AUTH.match(auth)
    if m is None:
        raise SigV4Error(400, "AuthorizationHeaderMalformed", "not an AWS4-HMAC-SHA256 header")
    cred_parts = m.group("cred").split("/")
    if len(cred_parts) != 5 or cred_parts[4] != "aws4_request":
        raise SigV4Error(400, "AuthorizationHeaderMalformed", "bad credential scope")
    akid, date, region, service, _ = cred_parts
    cred = lookup.get(akid)
    if cred is None:
        raise SigV4Error(403, "InvalidAccessKeyId", f"unknown access key {akid}")
    amz_date = (headers.get("x-amz-date") or [""])[0]
    try:
        when = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise SigV4Error(403, "AccessDenied", "missing or bad x-amz-date") from None
    if amz_date[:8] != date:
        raise SigV4Error(403, "SignatureDoesNotMatch", "credential date differs from x-amz-date")
    if check_skew and abs(when - now) > MAX_SKEW:
        raise SigV4Error(403, "RequestTimeTooSkewed", "request time too far from server time")
    signed = m.group("sh").split(";")
    if "host" not in signed:
        raise SigV4Error(403, "AccessDenied", "host must be signed")
    payload_hash = (headers.get("x-amz-content-sha256") or [""])[0]
    if payload_hash:
        if payload_hash != UNSIGNED and not payload_hash.startswith("STREAMING-"):
            if body is None:
                raise SigV4Error(400, "InvalidRequest", "signed payload not read")
            if hashlib.sha256(body).hexdigest() != payload_hash:
                raise SigV4Error(400, "XAmzContentSHA256Mismatch", "body does not match its hash")
    else:
        if body is None:
            raise SigV4Error(400, "InvalidRequest", "no payload hash")
        payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = ""
    for name in signed:
        values = headers.get(name)
        if values is None:
            raise SigV4Error(403, "SignatureDoesNotMatch", f"signed header {name} missing")
        canonical_headers += f"{name}:{','.join(_SPACES.sub(' ', v.strip()) for v in values)}\n"
    creq = "\n".join(
        [
            method,
            raw_path or "/",
            canonical_query(raw_query),
            canonical_headers,
            ";".join(signed),
            payload_hash,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    key = signing_key(cred.secret_access_key, date, region, service)
    expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, m.group("sig")):
        raise SigV4Error(403, "SignatureDoesNotMatch", "signature does not match")
    if cred.session_token is not None:
        token = (headers.get("x-amz-security-token") or [""])[0]
        if token != cred.session_token:
            raise SigV4Error(403, "InvalidToken", "missing or wrong x-amz-security-token")
        if cred.expires_at is not None and now >= cred.expires_at:
            raise SigV4Error(400, "ExpiredToken", "the session token has expired")
    return Verified(cred, region, service, payload_hash)


def sign_headers(
    *,
    method: str,
    url_path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
    credential: Credential,
    region: str,
    service: str,
    now: datetime,
) -> dict[str, str]:
    """Sign a request (tests and non-SDK harnesses): returns `headers` plus
    x-amz-date, x-amz-content-sha256, x-amz-security-token and Authorization.
    `headers` must include ``host``."""
    out = {k.lower(): v for k, v in headers.items()}
    amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    out["x-amz-date"] = amz_date
    out.setdefault("x-amz-content-sha256", hashlib.sha256(body).hexdigest())
    if credential.session_token:
        out["x-amz-security-token"] = credential.session_token
    signed = sorted(out)
    canonical_headers = "".join(f"{n}:{_SPACES.sub(' ', out[n].strip())}\n" for n in signed)
    creq = "\n".join(
        [method, url_path, canonical_query(query), canonical_headers, ";".join(signed),
         out["x-amz-content-sha256"]]
    )  # fmt: skip
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    sig = hmac.new(
        signing_key(credential.secret_access_key, date, region, service),
        to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    out["authorization"] = (
        f"{ALGORITHM} Credential={credential.access_key_id}/{scope}, "
        f"SignedHeaders={';'.join(signed)}, Signature={sig}"
    )
    return out
