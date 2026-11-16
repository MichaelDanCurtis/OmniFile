"""What Settings calls out to: DNS, Azure Resource Manager and Microsoft Graph.

`Probes` is the port. `HttpProbes` is the real one: `web`'s own managed
identity for ARM and Graph tokens (no secret), and the resolver of the
container, which is the VNet's (so a private DNS zone answers as the apps
see it). `probes_from_env` builds it; without ``OMNIFILE_SETTINGS_PROBES=1``
`web` makes no outbound call and every live check says it isn't configured.

The tests use `settings.fakes.FakeProbes`.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
ARM_SCOPE = "https://management.azure.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class ProbeError(Exception):
    """A call that didn't answer: `status` is the HTTP status (None: no answer)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Probes(Protocol):
    async def resolve(self, host: str) -> list[str]: ...

    async def arm_get(self, path: str, api_version: str) -> dict[str, Any]: ...

    async def graph_get(self, path: str) -> dict[str, Any]: ...


class TokenSource(Protocol):
    async def token(self) -> str: ...


class HttpProbes:
    def __init__(
        self,
        http: httpx.AsyncClient,
        arm_tokens: TokenSource,
        graph_tokens: TokenSource,
        *,
        arm_url: str = ARM,
        graph_url: str = GRAPH,
    ) -> None:
        self.http = http
        self.arm_tokens = arm_tokens
        self.graph_tokens = graph_tokens
        self.arm_url = arm_url.rstrip("/")
        self.graph_url = graph_url.rstrip("/")

    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout=5
            )
        except (OSError, TimeoutError) as e:
            raise ProbeError(f"{host} does not resolve: {e}") from None
        return sorted({str(i[4][0]) for i in infos})

    async def _get(self, url: str, tokens: TokenSource) -> dict[str, Any]:
        try:
            token = await tokens.token()
            r = await self.http.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        except httpx.HTTPError as e:
            raise ProbeError(f"{type(e).__name__}: {e}") from None
        except Exception as e:  # noqa: BLE001 - a token failure is an answer too
            raise ProbeError(f"no token: {type(e).__name__}") from None
        if r.status_code >= 400:
            msg = ""
            try:
                err = r.json().get("error") or {}
                msg = str(err.get("message") or err.get("code") or "")[:300]
            except ValueError:
                pass
            raise ProbeError(f"answered {r.status_code} {msg}".strip(), r.status_code)
        return r.json() if r.content else {}

    async def arm_get(self, path: str, api_version: str) -> dict[str, Any]:
        sep = "&" if "?" in path else "?"
        return await self._get(
            f"{self.arm_url}{path}{sep}api-version={api_version}", self.arm_tokens
        )

    async def graph_get(self, path: str) -> dict[str, Any]:
        return await self._get(f"{self.graph_url}{path}", self.graph_tokens)


#: What Settings reads from `web`'s environment: names and ids, never a secret.
ENV_NAMES = (
    "OMNIFILE_SETTINGS_PROBES",
    "OMNIFILE_AZURE_SUBSCRIPTION_ID",
    "OMNIFILE_AZURE_RESOURCE_GROUP",
    "OMNIFILE_SERVICE_PRINCIPAL_IDS",
    "OMNIFILE_AWS_ACCOUNTS",
    "OMNIFILE_AWS_ACCOUNTS_FILE",
    "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE",
    "OMNIFILE_AWS_PRIVATE_NETWORK",
    "OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS",
    "OMNIFILE_TENANT_ID",
    "OMNIFILE_TEAMS_APP_ID",
    "OMNIFILE_TEAMS_BOT_APP_ID",
    "OMNIFILE_TEAMS_APP_PACKAGE",
    "OMNIFILE_NOTIFY_SENDER",
    "OMNIFILE_STORAGE_MOVER",
    # Connect AWS's quick-create link: the template's https URL in S3 (cloud-connect.md 2.2).
    "OMNIFILE_AWS_CONNECT_TEMPLATE_URL",
    # The service's VNet: Connect Azure checks a privatelink.blob zone is linked to it, and
    # Connect AWS gives its range to the interface endpoints' security group.
    "OMNIFILE_SERVICE_VNET_ID",
    "OMNIFILE_SERVICE_VNET_CIDR",
)


def settings_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: env[k] for k in ENV_NAMES if env.get(k)}


def probes_from_env(env: Mapping[str, str], http: httpx.AsyncClient | None = None):
    """The real probes when ``OMNIFILE_SETTINGS_PROBES=1``, else None."""
    if env.get("OMNIFILE_SETTINGS_PROBES") != "1":
        return None
    from ..notify.tokens import ManagedIdentity

    client_id = env.get("AZURE_CLIENT_ID") or None
    return HttpProbes(
        http or httpx.AsyncClient(trust_env=False, follow_redirects=False),
        ManagedIdentity(ARM_SCOPE, client_id),
        ManagedIdentity(GRAPH_SCOPE, client_id),
    )
