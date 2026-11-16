"""Bot Framework (Bot Connector -> bot) JWT validation.

Implements every check in Microsoft Learn, "Authenticate requests with the Bot Connector API",
section "Authenticate requests from the Bot Connector service to your bot" (read 2026-10-05):

1. token in the ``Authorization`` header with the ``Bearer`` scheme;
2. a well-formed JWT;
3. ``iss`` == ``https://api.botframework.com``;
4. ``aud`` == our bot's Microsoft App ID;
5. within its validity period, with 5 minutes of clock skew;
6. signed by a key from the OpenID keys document, with an algorithm listed in
   ``id_token_signing_alg_values_supported`` of the OpenID metadata;
7. a ``serviceUrl`` claim equal to the activity's ``serviceUrl``;

plus channel endorsements: the signing key's ``endorsements`` must include the activity's
``channelId`` (403 otherwise). The key list is cached, refreshed well inside Microsoft's
"at least once every 24 hours", and refreshed early (rate-limited) when an unknown ``kid``
arrives, so key rotation is picked up without a restart.

The Emulator path (MSA issuers) is deliberately not implemented: production only.
There is no switch to disable validation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from .config import Settings

log = logging.getLogger("omnifile_relay.botauth")

# Algorithms we are willing to accept at all; the metadata may narrow this further.
_ACCEPTABLE_ALGS = frozenset({"RS256", "RS384", "RS512"})
_MAX_TOKEN_BYTES = 16 * 1024
_MAX_DOC_BYTES = 256 * 1024


class AuthError(Exception):
    """The request is not from the Bot Connector service for our bot."""

    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class SigningKey:
    key: RSAPublicKey
    endorsements: frozenset[str]


class BotKeyCache:
    """OpenID metadata + JWKS cache for the Bot Connector service."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._http = http
        self._clock = clock
        self._keys: dict[str, SigningKey] = {}
        self._algs: frozenset[str] = frozenset()
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._fetched_at is not None

    @property
    def algorithms(self) -> frozenset[str]:
        return self._algs

    def _retry_floor(self) -> float:
        # With no keys at all, retry quickly; otherwise an attacker sending random kids must not
        # be able to make us hammer Microsoft.
        return 10.0 if self._fetched_at is None else float(self._s.jwks_min_refresh_seconds)

    def _due(self, now: float, *, unknown_kid: bool) -> bool:
        if self._last_attempt is not None and now - self._last_attempt < self._retry_floor():
            return False
        if self._fetched_at is None:
            return True
        if now - self._fetched_at >= self._s.jwks_refresh_seconds:
            return True
        return unknown_kid

    async def get_key(self, kid: str) -> SigningKey:
        now = self._clock()
        if self._due(now, unknown_kid=kid not in self._keys):
            async with self._lock:
                now = self._clock()
                if self._due(now, unknown_kid=kid not in self._keys):
                    await self._refresh(now)
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at > self._s.jwks_max_stale_seconds:
            raise AuthError("signing_keys_unavailable", status=503)
        try:
            return self._keys[kid]
        except KeyError:
            raise AuthError("unknown_signing_key") from None

    async def _get_json(self, url: str) -> dict:
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "") not in self._s.jwks_hosts:
            raise ValueError("OpenID document points outside the allowed hosts")
        if parts.port not in (None, 443) or parts.username or parts.password:
            raise ValueError("OpenID URL has a port or credentials")
        resp = await self._http.get(url, follow_redirects=False, timeout=5.0)
        resp.raise_for_status()
        if len(resp.content) > _MAX_DOC_BYTES:
            raise ValueError("OpenID document too large")
        doc = resp.json()
        if not isinstance(doc, dict):
            raise ValueError("OpenID document is not an object")
        return doc

    async def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            meta = await self._get_json(self._s.openid_metadata_url)
            jwks_uri = meta.get("jwks_uri")
            if not isinstance(jwks_uri, str):
                raise ValueError("metadata has no jwks_uri")
            algs = meta.get("id_token_signing_alg_values_supported") or []
            allowed = frozenset(a for a in algs if a in _ACCEPTABLE_ALGS)
            if not allowed:
                raise ValueError("metadata lists no acceptable signing algorithm")
            doc = await self._get_json(jwks_uri)
            keys: dict[str, SigningKey] = {}
            for jwk in doc.get("keys", []):
                if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
                    continue
                if jwk.get("use", "sig") != "sig" or not isinstance(jwk.get("kid"), str):
                    continue
                key = RSAAlgorithm.from_jwk(json.dumps(jwk))
                if not isinstance(key, RSAPublicKey):
                    continue
                endorsements = jwk.get("endorsements") or []
                keys[jwk["kid"]] = SigningKey(
                    key=key,
                    endorsements=frozenset(e for e in endorsements if isinstance(e, str)),
                )
            if not keys:
                raise ValueError("JWKS has no usable keys")
        except (httpx.HTTPError, ValueError, jwt.InvalidKeyError) as exc:
            log.warning(
                "jwks refresh failed",
                extra={"event": "jwks_refresh", "outcome": "error", "reason": type(exc).__name__},
            )
            return
        self._keys = keys
        self._algs = allowed
        self._fetched_at = now
        log.info(
            "jwks refreshed",
            extra={"event": "jwks_refresh", "outcome": "ok", "count": len(keys)},
        )


class BotTokenValidator:
    def __init__(self, settings: Settings, keys: BotKeyCache) -> None:
        self._s = settings
        self._keys = keys

    async def validate(self, authorization: str | None, activity: dict) -> dict:
        """Return the verified claims, or raise :class:`AuthError`."""
        if not authorization:
            raise AuthError("missing_authorization")
        scheme, _, token = authorization.partition(" ")
        if scheme != "Bearer" or not token or " " in token.strip():
            raise AuthError("not_bearer")
        token = token.strip()
        if len(token) > _MAX_TOKEN_BYTES:
            raise AuthError("token_too_large")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("malformed_token") from None
        alg = header.get("alg")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("missing_kid")
        if alg not in _ACCEPTABLE_ALGS:
            raise AuthError("algorithm_not_allowed")
        signing = await self._keys.get_key(kid)
        if alg not in self._keys.algorithms:
            raise AuthError("algorithm_not_allowed")
        try:
            claims = jwt.decode(
                token,
                key=signing.key,
                algorithms=[alg],
                audience=self._s.bot_app_id,
                issuer=self._s.bot_issuer,
                leeway=self._s.clock_skew_seconds,
                options={"require": ["exp", "iss", "aud"], "verify_signature": True},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("token_expired") from None
        except jwt.ImmatureSignatureError:
            raise AuthError("token_not_yet_valid") from None
        except jwt.InvalidAudienceError:
            raise AuthError("wrong_audience") from None
        except jwt.InvalidIssuerError:
            raise AuthError("wrong_issuer") from None
        except jwt.InvalidSignatureError:
            raise AuthError("bad_signature") from None
        except jwt.PyJWTError:
            raise AuthError("invalid_token") from None

        service_url = activity.get("serviceUrl")
        if claims.get("serviceUrl") != service_url or not isinstance(service_url, str):
            raise AuthError("service_url_claim_mismatch")

        channel = activity.get("channelId")
        if channel != self._s.channel_id:
            raise AuthError("wrong_channel", status=403)
        if channel not in signing.endorsements:
            raise AuthError("channel_not_endorsed", status=403)

        tid = claims.get("tid")
        if tid is not None and str(tid).lower() != self._s.tenant_id:
            raise AuthError("wrong_tenant", status=403)
        return claims


def service_url_allowed(url: object, hosts: tuple[str, ...]) -> bool:
    """True only for ``https://<allowlisted host>[:443]/...`` with no credentials.

    The relay never calls ``serviceUrl`` itself; this check stops an attacker-chosen host from
    being forwarded inward or logged as if it were Microsoft's.
    """
    if not isinstance(url, str) or len(url) > 512:
        return False
    if any(c.isspace() or c == "\\" for c in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    return (parts.hostname or "") in hosts
