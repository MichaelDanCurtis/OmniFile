"""Fake edge credentials: SAS-like and presigned-like URLs that tests can check.

A token is bound to one object (container + key, and version for reads), a
permission set and an expiry, and signed with HMAC-SHA256 under a per-backend
random secret. It proves nothing about Azure's or AWS's real signing; it lets
tests assert that the service minted the right scope and lifetime, and that
the edge rejects a tampered, expired or wrong-object token.

Hosts end in `.invalid` (RFC 2606) so a fake URL can never reach a network.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from ..base import BackendError, EdgeAccess

PERMISSIONS = {EdgeAccess.READ: "r", EdgeAccess.WRITE: "cw"}  # Azure SAS letters [A]


class CredentialRejected(BackendError):
    """A fake SAS / presigned URL failed validation."""


def _canonical(host: str, container: str, key: str, perms: str, expiry: str, version: str) -> bytes:
    return "\n".join([host, container, key, perms, expiry, version]).encode()


def _sign(
    secret: bytes, host: str, container: str, key: str, perms: str, expiry: str, version: str
) -> str:
    msg = _canonical(host, container, key, perms, expiry, version)
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def make_url(
    secret: bytes,
    host: str,
    container: str,
    key: str,
    access: EdgeAccess,
    expiry: datetime,
    version: str | None,
) -> str:
    perms = PERMISSIONS[access]
    exp = expiry.isoformat()
    sig = _sign(secret, host, container, key, perms, exp, version or "")
    query = {"sv": "sim", "sp": perms, "se": exp, "sig": sig}
    if version:
        query["versionid"] = version
    return f"https://{host}/{quote(container)}/{quote(key, safe='/')}?{urlencode(query)}"


def validate_url(
    secret: bytes,
    url: str,
    host: str,
    container: str,
    key: str,
    access: EdgeAccess,
    now: datetime,
    version: str | None = None,
) -> None:
    """Raise `CredentialRejected` unless `url` grants `access` to exactly this object now."""
    parts = urlsplit(url)
    if not parts.hostname or not parts.hostname.endswith(".invalid"):
        raise CredentialRejected("not a sim credential")
    if parts.hostname != host:
        raise CredentialRejected(f"credential is for host {parts.hostname}")
    path = unquote(parts.path).lstrip("/")
    q = dict(parse_qsl(parts.query))
    try:
        perms, exp, sig = q["sp"], q["se"], q["sig"]
    except KeyError as e:
        raise CredentialRejected(f"missing {e.args[0]}") from None
    url_version = q.get("versionid", "")
    url_container, _, url_key = path.partition("/")
    expected = _sign(secret, parts.hostname, url_container, url_key, perms, exp, url_version)
    if not hmac.compare_digest(expected, sig):
        raise CredentialRejected("bad signature")
    if (url_container, url_key) != (container, key):
        raise CredentialRejected(f"credential is for {url_container}/{url_key}")
    if version is not None and url_version and url_version != version:
        raise CredentialRejected(f"credential is for version {url_version}")
    if datetime.fromisoformat(exp) <= now:
        raise CredentialRejected("expired")
    if not set(PERMISSIONS[access]) <= set(perms):
        raise CredentialRejected(f"permission {perms!r} does not allow {access}")
