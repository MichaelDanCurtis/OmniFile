"""api -> enrol: certificate renewal, and an edge's Roles Anywhere certificate
(decision 50), go to enrol's `POST /internal/v0/sign`.

`api` never holds the CA key (control-api.md section 2). It authenticates the
edge, then forwards the CSR here with an Entra token for its own managed
identity. In dev and tests the token is a shared test token.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .problems import CODES, ApiError


class TokenProvider(Protocol):
    async def token(self) -> str: ...


class StaticToken:
    """Dev and tests: a shared token that enrol is configured to accept."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token


class ManagedIdentityToken:
    """An Entra access token for api's managed identity, audience = enrol's app.

    VERIFY on a real deployment: that the user-assigned identity's client id
    works as the audience (deploy/azure/main.bicep, OMNIFILE_ENROL_AUDIENCE).
    """

    def __init__(self, audience: str, client_id: str | None = None) -> None:
        from azure.identity.aio import ManagedIdentityCredential

        self._scope = audience.rstrip("/") + "/.default"
        self._credential = ManagedIdentityCredential(client_id=client_id)

    async def token(self) -> str:
        return (await self._credential.get_token(self._scope)).token

    async def aclose(self) -> None:
        await self._credential.close()


class SignClient(Protocol):
    async def sign(
        self,
        edge_id: int,
        csr_pem: str,
        current_cert_sha256: str,
        request_id: str,
        purpose: str = "renewal",
    ) -> dict[str, Any]:
        """enrol's 201 body: cert_pem, chain_pem, ca_pem, not_after (and,
        for `roles_anywhere`, renew_after)."""
        ...


class HttpSignClient:
    def __init__(
        self, url: str, tokens: TokenProvider, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self.url = url
        self.tokens = tokens
        self.http = http or httpx.AsyncClient(timeout=5.0, trust_env=False)

    async def sign(
        self,
        edge_id: int,
        csr_pem: str,
        current_cert_sha256: str,
        request_id: str,
        purpose: str = "renewal",
    ) -> dict[str, Any]:
        body = {
            "edge_id": str(edge_id),
            "csr_pem": csr_pem,
            "purpose": purpose,
            "current_cert_sha256": current_cert_sha256,
            "request_id": request_id,
        }
        try:
            resp = await self.http.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {await self.tokens.token()}"},
            )
        except httpx.HTTPError as exc:
            raise ApiError("unavailable", "enrol unreachable", retry_after_s=5) from exc
        if resp.status_code == 201:
            return resp.json()
        try:
            problem = resp.json()
        except ValueError:
            problem = {}
        code = problem.get("code") if isinstance(problem, dict) else None
        if resp.status_code < 500 and code in CODES and code != "caller_not_allowed":
            raise ApiError(code, problem.get("detail"))
        # A refused token or a 5xx is our problem, not the edge's: retry later.
        raise ApiError("unavailable", f"enrol answered {resp.status_code}", retry_after_s=5)

    async def aclose(self) -> None:
        await self.http.aclose()
