"""Signed card intents.

When the service sends a card, it puts one compact token per button in ``Action.Execute``
``data`` under :data:`DATA_KEY`. The token is a JWS (JWT) signed with **Ed25519** (``EdDSA``):

header  ``{"alg": "EdDSA", "typ": "omnifile-intent+jwt", "kid": "<key id>"}``
claims  ``jti``  intent id (random, single use)
        ``act``  ``recall`` | ``keep_online`` | ``track``
        ``fid``  catalog file id
        ``sub``  intended recipient's Entra objectId
        ``tid``  Entra tenant id
        ``iat`` / ``exp``  issued / expires (seconds)
        ``iss`` ``omnifile-service``; ``aud`` ``omnifile-teams-relay``

The service holds the private key; the relay holds only public keys, so it can check an intent
but never create one. ``typ`` and ``aud`` stop a token signed for another purpose from being
accepted here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import jwt

from .config import Settings

DATA_KEY = "omnifile_intent"
ACTIONS = frozenset({"recall", "keep_online", "track"})
_MAX_TOKEN_BYTES = 2048
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class IntentError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Intent:
    intent_id: str
    action: str
    file_id: str
    recipient: str
    tenant: str
    issued_at: int
    expires_at: int
    token: str


class IntentVerifier:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def verify(self, token: object) -> Intent:
        if not isinstance(token, str) or not token:
            raise IntentError("missing_intent")
        if len(token) > _MAX_TOKEN_BYTES:
            raise IntentError("intent_too_large")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise IntentError("malformed_intent") from None
        if header.get("alg") != "EdDSA" or header.get("typ") != "omnifile-intent+jwt":
            raise IntentError("wrong_intent_type")
        kid = header.get("kid")
        key = self._s.intent_public_keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise IntentError("unknown_intent_key")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["EdDSA"],
                audience=self._s.intent_audience,
                issuer=self._s.intent_issuer,
                leeway=self._s.intent_leeway_seconds,
                options={
                    "require": ["jti", "act", "fid", "sub", "tid", "iat", "exp", "iss", "aud"]
                },
            )
        except jwt.ExpiredSignatureError:
            raise IntentError("intent_expired") from None
        except jwt.InvalidSignatureError:
            raise IntentError("intent_bad_signature") from None
        except jwt.PyJWTError:
            raise IntentError("invalid_intent") from None

        jti, act, fid = claims["jti"], claims["act"], claims["fid"]
        sub, tid, iat, exp = claims["sub"], claims["tid"], claims["iat"], claims["exp"]
        if not all(isinstance(v, str) for v in (jti, act, fid, sub, tid)):
            raise IntentError("invalid_intent")
        if not isinstance(iat, int) or not isinstance(exp, int):
            raise IntentError("invalid_intent")
        if act not in ACTIONS:
            raise IntentError("unknown_action")
        if not _ID.match(jti) or not _ID.match(fid):
            raise IntentError("invalid_intent")
        sub, tid = sub.lower(), tid.lower()
        if not _GUID.match(sub) or not _GUID.match(tid):
            raise IntentError("invalid_intent")
        if exp - iat > self._s.intent_max_lifetime_seconds or exp <= iat:
            raise IntentError("intent_lifetime_too_long")
        return Intent(
            intent_id=jti,
            action=act,
            file_id=fid,
            recipient=sub,
            tenant=tid,
            issued_at=iat,
            expires_at=exp,
            token=token,
        )
