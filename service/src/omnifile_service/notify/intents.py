"""Signed card intents (teams.md section 2).

The format is the relay's (`relay/src/omnifile_relay/intent.py`), byte for byte:

header  ``{"alg": "EdDSA", "typ": "omnifile-intent+jwt", "kid": "<key id>"}``
claims  ``jti`` (UUID), ``act``, ``fid`` (decimal file id), ``sub`` (recipient objectId),
        ``tid`` (tenant), ``iat``, ``exp``, ``iss`` ``omnifile-service``,
        ``aud`` ``omnifile-teams-relay``

Only the worker holds the private key (`IntentSigner`). `api` re-verifies with public keys
only (`IntentVerifier`), exactly as the relay does, so a compromised relay can't invent an
intent.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)

DATA_KEY = "omnifile_intent"
TYP = "omnifile-intent+jwt"
ISSUER = "omnifile-service"
AUDIENCE = "omnifile-teams-relay"
ACTIONS = frozenset({"recall", "keep_online", "track"})
LIFETIME_SECONDS = 7 * 86400  # the service's choice; the relay allows up to 14 days
MAX_LIFETIME_SECONDS = 14 * 86400
LEEWAY_SECONDS = 60
MAX_TOKEN_BYTES = 2048
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class IntentKeyError(ValueError):
    """A key or key set in configuration is unusable."""


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    """A PKCS#8 PEM Ed25519 private key (mounted from a Key Vault secret)."""
    key = load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise IntentKeyError("the intent signing key must be Ed25519")
    return key


def load_public_key(text: str) -> Ed25519PublicKey:
    """A PEM SubjectPublicKeyInfo, or base64url of the raw 32 bytes (the relay's rule)."""
    text = text.strip()
    try:
        if text.startswith("-----BEGIN"):
            key = load_pem_public_key(text.encode())
        else:
            raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            key = Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise IntentKeyError(f"unreadable intent public key: {exc}") from None
    if not isinstance(key, Ed25519PublicKey):
        raise IntentKeyError("intent keys must be Ed25519")
    return key


def load_public_keys(raw: str) -> dict[str, Ed25519PublicKey]:
    """`{kid: key}` JSON, the format of OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS (and the relay's)."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        raise IntentKeyError("intent public keys must be JSON {kid: key}") from None
    if not isinstance(doc, dict) or not doc:
        raise IntentKeyError("intent public keys must be a non-empty JSON object")
    return {str(kid): load_public_key(str(value)) for kid, value in doc.items()}


def public_key_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    """base64url raw public key, for `{kid: key}` configuration."""
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = pub.public_bytes_raw()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class Minted:
    intent_id: str
    token: str
    issued_at: datetime
    expires_at: datetime


class IntentSigner:
    """Mints one intent per button. Held by the worker only."""

    def __init__(
        self, key: Ed25519PrivateKey, kid: str, *, lifetime_seconds: int = LIFETIME_SECONDS
    ) -> None:
        if not kid or not _ID.match(kid):
            raise IntentKeyError("the intent kid must be a short label")
        if not 0 < lifetime_seconds <= MAX_LIFETIME_SECONDS:
            raise IntentKeyError("intent lifetime must be within 14 days")
        self._key = key
        self.kid = kid
        self.lifetime_seconds = lifetime_seconds

    def mint(
        self, *, action: str, file_id: int, recipient: str, tenant: str, now: datetime
    ) -> Minted:
        if action not in ACTIONS:
            raise ValueError(f"unknown intent action {action!r}")
        recipient, tenant = recipient.lower(), tenant.lower()
        if not _GUID.match(recipient) or not _GUID.match(tenant):
            raise ValueError("recipient and tenant must be lowercase GUIDs")
        jti = str(uuid.uuid4())
        iat = int(now.timestamp())
        exp = iat + self.lifetime_seconds
        token = jwt.encode(
            {
                "jti": jti,
                "act": action,
                "fid": str(file_id),
                "sub": recipient,
                "tid": tenant,
                "iat": iat,
                "exp": exp,
                "iss": ISSUER,
                "aud": AUDIENCE,
            },
            self._key,
            algorithm="EdDSA",
            headers={"kid": self.kid, "typ": TYP},
        )
        if len(token) > MAX_TOKEN_BYTES:  # pragma: no cover - ~560 bytes in practice
            raise ValueError("intent token too large")
        return Minted(
            jti,
            token,
            datetime.fromtimestamp(iat, now.tzinfo),
            datetime.fromtimestamp(exp, now.tzinfo),
        )


@dataclass(frozen=True)
class Intent:
    intent_id: str
    action: str
    file_id: int
    recipient: str
    tenant: str
    issued_at: int
    expires_at: int


class IntentRefused(Exception):  # noqa: N818 - a refusal with an API code
    """`code` is the API error code (teams.md section 4.4); `reason` is for audit and logs."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class IntentVerifier:
    """The relay's checks (`IntentVerifier` there), with the service's error codes."""

    def __init__(
        self, public_keys: dict[str, Ed25519PublicKey], *, leeway_seconds: int = LEEWAY_SECONDS
    ) -> None:
        if not public_keys:
            raise IntentKeyError("at least one intent public key is required")
        self._keys = dict(public_keys)
        self._leeway = leeway_seconds

    def verify(self, token: object, now: datetime) -> Intent:
        bad = "intent_invalid"
        if not isinstance(token, str) or not token:
            raise IntentRefused(bad, "missing_intent")
        if len(token) > MAX_TOKEN_BYTES:
            raise IntentRefused(bad, "intent_too_large")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise IntentRefused(bad, "malformed_intent") from None
        if header.get("alg") != "EdDSA" or header.get("typ") != TYP:
            raise IntentRefused(bad, "wrong_intent_type")
        kid = header.get("kid")
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise IntentRefused(bad, "unknown_intent_key")
        try:
            # The signature first, without time checks, so a forged token is never
            # reported as merely expired.
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["EdDSA"],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={
                    "require": ["jti", "act", "fid", "sub", "tid", "iat", "exp", "iss", "aud"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidSignatureError:
            raise IntentRefused(bad, "intent_bad_signature") from None
        except jwt.PyJWTError:
            raise IntentRefused(bad, "invalid_intent") from None
        jti, act, fid = claims["jti"], claims["act"], claims["fid"]
        sub, tid, iat, exp = claims["sub"], claims["tid"], claims["iat"], claims["exp"]
        if not all(isinstance(v, str) for v in (jti, act, fid, sub, tid)):
            raise IntentRefused(bad, "invalid_intent")
        if isinstance(iat, bool) or isinstance(exp, bool):
            raise IntentRefused(bad, "invalid_intent")
        if not isinstance(iat, int) or not isinstance(exp, int):
            raise IntentRefused(bad, "invalid_intent")
        if act not in ACTIONS:
            raise IntentRefused(bad, "unknown_action")
        if not _ID.match(jti) or not _ID.match(fid) or not fid.isdigit():
            raise IntentRefused(bad, "invalid_intent")
        try:
            if str(uuid.UUID(jti)) != jti.lower():
                raise ValueError
        except ValueError:
            raise IntentRefused(bad, "invalid_intent") from None
        sub, tid = sub.lower(), tid.lower()
        if not _GUID.match(sub) or not _GUID.match(tid):
            raise IntentRefused(bad, "invalid_intent")
        if exp <= iat or exp - iat > MAX_LIFETIME_SECONDS:
            raise IntentRefused(bad, "intent_lifetime_too_long")
        t = int(now.timestamp())
        if iat > t + self._leeway:
            raise IntentRefused(bad, "intent_issued_in_future")
        if t > exp + self._leeway:
            raise IntentRefused("intent_expired", "intent_expired")
        return Intent(jti.lower(), act, int(fid), sub, tid, iat, exp)
