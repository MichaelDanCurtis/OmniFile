"""Azure Resource Manager with a token someone else holds: the port Connect Azure
(`azure_connect.py`) and the Storage Mover deployment (`cloud_jobs.py`) call.

`Arm` is the port: GET, PUT and DELETE of an ARM path at an API version
(`delete_resource`: a role assignment or definition, never a blob).
`HttpArm` is the real one, given a token source (an admin's delegated token for
one request, or the worker's managed identity). `FakeArm` (`settings/fakes.py`)
answers the tests and dev mode, so nothing reaches Azure.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .ports import ARM, ProbeError, TokenSource


class Arm(Protocol):
    async def get(self, path: str, api_version: str) -> dict[str, Any]: ...

    async def put(self, path: str, api_version: str, body: dict[str, Any]) -> dict[str, Any]: ...

    async def delete_resource(self, path: str, api_version: str) -> None: ...


class StaticToken:
    """A token held for one request, never stored."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token


class HttpArm:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenSource, *, url: str = ARM) -> None:
        self.http, self.tokens, self.url = http, tokens, url.rstrip("/")

    async def _call(
        self, method: str, path: str, api_version: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        sep = "&" if "?" in path else "?"
        try:
            token = await self.tokens.token()
            r = await self.http.request(
                method,
                f"{self.url}{path}{sep}api-version={api_version}",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=20,
            )
        except httpx.HTTPError as e:
            raise ProbeError(f"{type(e).__name__}: {e}") from None
        except Exception as e:  # noqa: BLE001 - a token failure is an answer too
            raise ProbeError(f"no token: {type(e).__name__}") from None
        if r.status_code >= 400:
            code, msg = "", ""
            try:
                err = r.json().get("error") or {}
                code = str(err.get("code") or "")
                msg = str(err.get("message") or "")[:300]
            except ValueError:
                pass
            raise ArmError(r.status_code, code, msg)
        return r.json() if r.content else {}

    async def get(self, path: str, api_version: str) -> dict[str, Any]:
        return await self._call("GET", path, api_version)

    async def put(self, path: str, api_version: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("PUT", path, api_version, body)

    async def delete_resource(self, path: str, api_version: str) -> None:
        await self._call("DELETE", path, api_version)


class ArmError(ProbeError):
    """ARM answered an error: `status`, its `code` (e.g. AuthorizationFailed)."""

    def __init__(self, status: int, code: str = "", message: str = "") -> None:
        super().__init__(f"answered {status} {code} {message}".strip(), status)
        self.code = code
