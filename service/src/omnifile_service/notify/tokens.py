"""Access tokens for Graph and the Bot Connector (teams.md section 6.1).

- `ClientCredentials`: the OAuth 2.0 client-credentials grant at Entra's token endpoint,
  with a secret (or, in dev and tests, the fakes' endpoint).
- `ManagedIdentity`: the worker's managed identity, through `azure-identity`.

Both cache a token until 5 minutes before it expires. Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol

import httpx

REFRESH_MARGIN_S = 300
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
BOT_SCOPE = "https://api.botframework.com/.default"


class TokenError(Exception):
    """No token: the code is safe to log (never a response body)."""


class TokenSource(Protocol):
    async def token(self) -> str: ...


class ClientCredentials:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._url = token_url
        self._id = client_id
        self._secret = client_secret
        self._scope = scope
        self._clock = clock
        self._token: str | None = None
        self._until = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if self._token is not None and self._clock() < self._until:
            return self._token
        async with self._lock:
            if self._token is not None and self._clock() < self._until:
                return self._token
            try:
                resp = await self._http.post(
                    self._url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._id,
                        "client_secret": self._secret,
                        "scope": self._scope,
                    },
                    timeout=10.0,
                )
            except httpx.HTTPError as exc:
                raise TokenError(f"token_endpoint_{type(exc).__name__}") from None
            if resp.status_code != 200:
                raise TokenError(f"token_endpoint_status_{resp.status_code}")
            try:
                doc = resp.json()
                token = doc["access_token"]
                expires_in = int(doc.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError):
                raise TokenError("token_endpoint_bad_reply") from None
            self._token = token
            self._until = self._clock() + max(0, expires_in - REFRESH_MARGIN_S)
            return token


class ManagedIdentity:
    """The worker's managed identity (Container Apps). `azure-identity` caches and refreshes."""

    def __init__(self, scope: str, client_id: str | None = None) -> None:
        from azure.identity.aio import ManagedIdentityCredential

        self._cred = ManagedIdentityCredential(client_id=client_id)
        self._scope = scope

    async def token(self) -> str:
        try:
            return (await self._cred.get_token(self._scope)).token
        except Exception as exc:  # noqa: BLE001 - azure-identity raises several types
            raise TokenError(f"managed_identity_{type(exc).__name__}") from None

    async def aclose(self) -> None:
        await self._cred.close()


class StaticToken:
    """Tests only."""

    def __init__(self, value: str) -> None:
        self.value = value

    async def token(self) -> str:
        return self.value
