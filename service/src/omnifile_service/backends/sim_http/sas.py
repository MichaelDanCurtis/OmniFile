"""User-delegation SAS: minting and validation.

From Microsoft's "Create a user delegation SAS"
(https://learn.microsoft.com/en-us/rest/api/storageservices/create-user-delegation-sas,
read 2026-10-05):

- the signature is base64(HMAC-SHA256(base64decode(key.Value), UTF8(StringToSign)));
- the string-to-sign has three layouts: 2020-12-06 and later, 2025-07-05 and
  later (adds skdutid and sduoid), and 2026-04-06 and later (adds the
  canonicalized srh and srq values);
- canonicalizedResource is "/blob/<account>/<container>[/<blob>]", URL-decoded;
- permissions must appear in the order "racwdxltmeop", with no repeats;
- the key is valid for up to seven days.

sim_http supports sr=b, sr=bv and sr=c. For sr=bv the version id is signed in
the signedSnapshotTime field, as the Azure SDKs do, and must equal the
request's `versionid`. Not supported, and refused with 403: sv before
2020-12-06, sr=bs and sr=d, srh/srq, and sduoid (a user-bound SAS also needs a
Bearer token, which sim_http rejects).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from . import errors

DEFAULT_SIGNED_VERSION = "2026-04-06"
MIN_SIGNED_VERSION = "2020-12-06"
V_DELEGATED_USER = "2025-07-05"
V_SIGNED_REQUEST = "2026-04-06"
PERMISSION_ORDER = "racwdxyltmeopi"  # documented "racwdxltmeop", plus y and i from the table
MAX_KEY_LIFETIME = timedelta(days=7)


def fmt_time(t: datetime) -> str:
    """ISO 8601 UTC with seconds, as the SDKs write SAS times."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    """The accepted ISO 8601 UTC forms: date, minutes, seconds, fractional seconds."""
    v = value.strip()
    if len(v) == 10:
        v += "T00:00:00Z"
    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("SAS times must be UTC")
    return t.astimezone(UTC)


@dataclass(frozen=True)
class UserDelegationKey:
    """What Get User Delegation Key returns (fields named as in its XML)."""

    signed_oid: str
    signed_tid: str
    signed_start: datetime
    signed_expiry: datetime
    signed_service: str
    signed_version: str
    value: str  # base64

    @classmethod
    def issue(
        cls, now: datetime, lifetime: timedelta = MAX_KEY_LIFETIME, *, version: str | None = None
    ) -> UserDelegationKey:
        if lifetime > MAX_KEY_LIFETIME:
            raise ValueError("a user delegation key lasts at most seven days")
        return cls(
            signed_oid=str(uuid.uuid4()),
            signed_tid=str(uuid.uuid4()),
            signed_start=now.replace(microsecond=0),
            signed_expiry=(now + lifetime).replace(microsecond=0),
            signed_service="b",
            signed_version=version or DEFAULT_SIGNED_VERSION,
            value=base64.b64encode(secrets.token_bytes(32)).decode(),
        )

    def to_json(self) -> dict[str, str]:
        return {
            "SignedOid": self.signed_oid,
            "SignedTid": self.signed_tid,
            "SignedStart": fmt_time(self.signed_start),
            "SignedExpiry": fmt_time(self.signed_expiry),
            "SignedService": self.signed_service,
            "SignedVersion": self.signed_version,
            "Value": self.value,
        }


def canonical_resource(account: str, container: str, blob: str | None) -> str:
    return f"/blob/{account}/{container}" + (f"/{blob}" if blob else "")


def string_to_sign(params: Mapping[str, str], resource: str) -> str:
    """StringToSign for the SAS's `sv`, from the URL-decoded query values."""
    g = params.get
    sv = g("sv", "")
    head = [
        g("sp", ""),
        g("st", ""),
        g("se", ""),
        resource,
        g("skoid", ""),
        g("sktid", ""),
        g("skt", ""),
        g("ske", ""),
        g("sks", ""),
        g("skv", ""),
        g("saoid", ""),
        g("suoid", ""),
        g("scid", ""),
    ]
    if sv >= V_DELEGATED_USER:
        head += [g("skdutid", ""), g("sduoid", "")]
    head += [g("sip", ""), g("spr", ""), sv, g("sr", ""), g("_sst", ""), g("ses", "")]
    if sv >= V_SIGNED_REQUEST:
        head += ["", ""]  # canonicalized srh and srq; refused when present
    head += [g("rscc", ""), g("rscd", ""), g("rsce", ""), g("rscl", ""), g("rsct", "")]
    return "\n".join(head)


def sign(key_value_b64: str, sts: str) -> str:
    mac = hmac.new(base64.b64decode(key_value_b64), sts.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def mint(
    key: UserDelegationKey,
    account: str,
    container: str,
    blob: str | None,
    permissions: str,
    expiry: datetime,
    *,
    start: datetime | None = None,
    version_id: str | None = None,
    protocol: str = "https,http",
    ip: str | None = None,
    signed_version: str = DEFAULT_SIGNED_VERSION,
) -> str:
    """A user-delegation SAS query string (no leading '?').

    `blob=None` makes a container SAS (sr=c); `version_id` makes sr=bv.
    """
    if blob is None and version_id is not None:
        raise ValueError("a version SAS names a blob")
    sr = "c" if blob is None else ("bv" if version_id else "b")
    params: dict[str, str] = {"sv": signed_version, "sr": sr, "sp": permissions}
    if start is not None:
        params["st"] = fmt_time(start)
    params["se"] = fmt_time(expiry)
    params.update(
        skoid=key.signed_oid,
        sktid=key.signed_tid,
        skt=fmt_time(key.signed_start),
        ske=fmt_time(key.signed_expiry),
        sks=key.signed_service,
        skv=key.signed_version,
    )
    if ip:
        params["sip"] = ip
    if protocol:
        params["spr"] = protocol
    signing = dict(params)
    if version_id:
        signing["_sst"] = version_id
    sts = string_to_sign(signing, canonical_resource(account, container, blob))
    params["sig"] = sign(key.value, sts)
    return urlencode(params, quote_via=quote)


@dataclass(frozen=True)
class Grant:
    permissions: frozenset[str]
    resource: str  # "b", "bv" or "c"
    signed_version: str
    version_id: str | None = None


def _check_permissions(sp: str) -> frozenset[str]:
    if not sp or len(set(sp)) != len(sp) or any(c not in PERMISSION_ORDER for c in sp):
        raise errors.authentication_failed(f"sp {sp!r} is not a valid permission string")
    positions = [PERMISSION_ORDER.index(c) for c in sp]
    if positions != sorted(positions):
        raise errors.authentication_failed(f"sp {sp!r} is not in the order racwdxltmeop")
    return frozenset(sp)


def _in_ip_range(client: str | None, sip: str) -> bool:
    if client is None:
        return False
    try:
        addr = ipaddress.IPv4Address(client)
        lo, _, hi = sip.partition("-")
        low = ipaddress.IPv4Address(lo)
        high = ipaddress.IPv4Address(hi or lo)
    except ValueError:
        return False
    return low <= addr <= high


_REQUIRED = ("sv", "sr", "sp", "se", "skoid", "sktid", "skt", "ske", "sks", "skv", "sig")


def validate(
    keys: list[UserDelegationKey],
    query: Mapping[str, str],
    *,
    account: str,
    container: str,
    blob: str,
    request_version_id: str | None,
    now: datetime,
    scheme: str,
    client_ip: str | None,
) -> Grant:
    """Check a request's SAS. Raise `AzureError` (401/403) unless it is valid
    for this account/container/blob now. Permission checks per operation are
    the caller's (`Grant.permissions`)."""
    if "sig" not in query:
        raise errors.no_authentication()
    if "skoid" not in query:
        raise errors.authentication_failed(
            "sim_http accepts only user delegation SAS (no skoid: service or account SAS)"
        )
    missing = [p for p in _REQUIRED if not query.get(p)]
    if missing:
        raise errors.authentication_failed(f"missing SAS fields: {', '.join(missing)}")
    sv, sr = query["sv"], query["sr"]
    if sv < MIN_SIGNED_VERSION:
        raise errors.authentication_failed(f"sv {sv} is older than {MIN_SIGNED_VERSION}")
    if "srh" in query or "srq" in query:
        raise errors.authentication_failed("srh/srq are not supported by sim_http")
    if query.get("sduoid") or query.get("skdutid"):
        raise errors.authentication_failed("user-bound SAS (sduoid) needs a Bearer token")
    if sr not in ("b", "bv", "c"):
        raise errors.authentication_failed(f"sr={sr} is not supported by sim_http")
    perms = _check_permissions(query["sp"])

    signing = dict(query)
    if sr == "bv":
        if not request_version_id:
            raise errors.authentication_failed("sr=bv needs a versionid on the request")
        signing["_sst"] = request_version_id
    resource = canonical_resource(account, container, None if sr == "c" else blob)
    sts = string_to_sign(signing, resource)
    key = next(
        (
            k
            for k in keys
            if (
                k.signed_oid,
                k.signed_tid,
                fmt_time(k.signed_start),
                fmt_time(k.signed_expiry),
                k.signed_service,
                k.signed_version,
            )
            == (
                query["skoid"],
                query["sktid"],
                _norm(query["skt"]),
                _norm(query["ske"]),
                query["sks"],
                query["skv"],
            )
        ),
        None,
    )
    try:
        given = base64.b64decode(query["sig"], validate=True)
    except ValueError:
        given = b""
    if key is None or not hmac.compare_digest(base64.b64decode(sign(key.value, sts)), given):
        raise errors.authentication_failed(
            f"Signature did not match. String to sign used was {sts}"
        )

    try:
        expiry = parse_time(query["se"])
        start = parse_time(query["st"]) if query.get("st") else None
        kstart, kexpiry = parse_time(query["skt"]), parse_time(query["ske"])
    except ValueError:
        raise errors.authentication_failed("a SAS time is not ISO 8601 UTC") from None
    if start is not None and expiry <= start:
        raise errors.authentication_failed(
            f"Signed expiry time [{query['se']}] must be after signed start time [{query['st']}]"
        )
    if (start is not None and now < start) or now >= expiry:
        raise errors.authentication_failed(
            f"Signature not valid in the specified time frame: Start [{query.get('st', '')}] - "
            f"Expiry [{query['se']}] - Current [{fmt_time(now)}]"
        )
    if not kstart <= now < kexpiry:
        raise errors.authentication_failed("The user delegation key is not valid now")

    spr = query.get("spr", "https,http")
    if spr not in ("https", "https,http"):
        raise errors.authentication_failed(f"spr {spr!r} is invalid")
    if spr == "https" and scheme != "https":
        raise errors.protocol_mismatch()
    if query.get("sip") and not _in_ip_range(client_ip, query["sip"]):
        raise errors.source_ip_mismatch()
    return Grant(perms, sr, sv, request_version_id if sr == "bv" else None)


def _norm(value: str) -> str:
    try:
        return fmt_time(parse_time(value))
    except ValueError:
        return value
