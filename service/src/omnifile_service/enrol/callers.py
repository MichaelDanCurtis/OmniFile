"""Who may call `POST /internal/v0/sign`: only the `api` app's managed identity.

`CallerVerifier` takes the Authorization header and returns the caller's
object id, or raises `ApiError("caller_not_allowed")`.

- `SharedTokenVerifier`: dev and tests. A shared secret token, compared in
  constant time.
- `EntraCallerVerifier`: production. An Entra access token, RS256-signed by
  the tenant's keys, for enrol's audience, issued to api's object id. Managed
  identity tokens are v1 tokens (issuer `https://sts.windows.net/<tid>/`); v2
  (`https://login.microsoftonline.com/<tid>/v2.0`) is accepted too.
  VERIFY on a real deployment: the audience string api requests
  (deploy/azure/main.bicep sets OMNIFILE_ENROL_AUDIENCE to enrol's client id)
  is the `aud` the token carries.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Protocol

import jwt

from ..api.problems import ApiError


class CallerVerifier(Protocol):
    async def verify(self, authorization: str | None) -> str: ...


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError("caller_not_allowed", "a bearer token is required")
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise ApiError("caller_not_allowed", "a bearer token is required")
    return token


class SharedTokenVerifier:
    def __init__(self, token: str, caller: str = "dev-api") -> None:
        if len(token) < 16:
            raise ValueError("the dev sign token must be at least 16 characters")
        self._token = token.encode()
        self._caller = caller

    async def verify(self, authorization: str | None) -> str:
        if not hmac.compare_digest(bearer(authorization).encode(), self._token):
            raise ApiError("caller_not_allowed", "bad token")
        return self._caller


class EntraCallerVerifier:
    def __init__(
        self,
        tenant_id: str,
        audience: str,
        allowed_object_id: str,
        *,
        jwks_url: str | None = None,
        leeway_seconds: int = 120,
    ) -> None:
        self.tenant_id = tenant_id.lower()
        self.audiences = [audience, f"api://{audience}"]
        self.allowed = allowed_object_id.lower()
        self.issuers = [
            f"https://sts.windows.net/{self.tenant_id}/",
            f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
        ]
        self.leeway = leeway_seconds
        self._jwks = jwt.PyJWKClient(
            jwks_url or f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=6 * 3600,
        )

    async def verify(self, authorization: str | None) -> str:
        token = bearer(authorization)
        try:
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=self.issuers,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "aud", "iss", "oid", "tid"]},
            )
        except jwt.PyJWTError:
            raise ApiError("caller_not_allowed", "invalid Entra token") from None
        if str(claims.get("tid", "")).lower() != self.tenant_id:
            raise ApiError("caller_not_allowed", "wrong tenant")
        oid = str(claims.get("oid", "")).lower()
        if oid != self.allowed:
            raise ApiError("caller_not_allowed", "this identity may not request signatures")
        return oid
