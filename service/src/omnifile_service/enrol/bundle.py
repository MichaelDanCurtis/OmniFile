"""The one-time enrolment bundle an edge's setup takes (control-api.md section 2).

`ofe1.<base64url(JSON{"url", "server_ca_sha256", "token"})>`: where to enrol,
which CA's certificate the enrol endpoint presents, and a 256-bit token. The
catalog keeps only the token's SHA-256 and an expiry; the bundle is shown once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

PREFIX = "ofe1."


def new_token() -> tuple[str, bytes]:
    """A fresh token and the SHA-256 the catalog keeps."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).digest()


def ca_sha256(pem: bytes) -> str:
    """The SHA-256 (hex) of a PEM certificate's DER encoding."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(pem)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def make(url: str, server_ca_sha256: str, token: str) -> str:
    payload = json.dumps({"url": url, "server_ca_sha256": server_ca_sha256, "token": token})
    return PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def read(bundle: str) -> dict[str, str]:
    """The bundle's fields (tests, and a check that a bundle is well formed)."""
    if not bundle.startswith(PREFIX):
        raise ValueError("not an enrolment bundle")
    raw = bundle[len(PREFIX) :]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
