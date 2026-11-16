"""How every app reaches the catalog (`deploy/azure/README.md`, "The catalog connection").

Two ways, picked by the environment:

- **Azure** (what `deploy/azure/main.bicep` sets): ``OMNIFILE_PG_HOST``,
  ``OMNIFILE_PG_DATABASE``, ``OMNIFILE_PG_USER`` (the app's managed identity, which is its
  PostgreSQL principal) and ``OMNIFILE_PG_SSLMODE`` (``verify-full`` by default; ``require``
  allowed). The server allows Microsoft Entra sign-in only, so the password is an Entra access
  token for the PostgreSQL scope, from the managed identity ``AZURE_CLIENT_ID`` names
  (``DefaultAzureCredential`` without it). Each new connection gets a token with at least five
  minutes left; the server checks it only at sign-in, so a pooled connection outlives its token.
  The session acts as the app's group role (``omnifile_api`` ..., `privileges.as_app`), which
  `migrate` makes the login a member of (``OMNIFILE_DB_MEMBERS``).
- **Dev and tests:** ``OMNIFILE_DATABASE_URL``, a libpq connection string used as it is.

Setting both is refused. Tokens are never logged: they live in this module's cache and in the
connect call's keyword arguments only.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.conninfo import make_conninfo

from .privileges import ROLES, as_app

#: The Entra scope of Azure Database for PostgreSQL.
SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
#: Renew a token this long before it expires.
MARGIN_S = 300
#: Every variable the Azure way reads (tests check main.bicep sets them).
AZURE_ENV = (
    "OMNIFILE_PG_HOST",
    "OMNIFILE_PG_DATABASE",
    "OMNIFILE_PG_USER",
    "OMNIFILE_PG_SSLMODE",
    "AZURE_CLIENT_ID",
)
_SYSTEM_CAS = Path("/etc/ssl/certs/ca-certificates.crt")


class TokenCredential(Protocol):
    """azure-identity's sync credential interface (only what is used here)."""

    def get_token(self, *scopes: str) -> Any: ...


class EntraPassword:
    """An Entra access token for PostgreSQL, used as the password, renewed before it expires.

    Thread-safe; one per process is enough. `repr` never shows the token."""

    def __init__(
        self,
        credential: TokenCredential,
        *,
        margin_s: float = MARGIN_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._margin = margin_s
        self._now = now
        self._token: str | None = None
        self._expires_on = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or self._expires_on - self._margin <= self._now():
                got = self._credential.get_token(SCOPE)
                self._token, self._expires_on = got.token, float(got.expires_on)
            return self._token

    def __repr__(self) -> str:
        return "EntraPassword(<token not shown>)"


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id)
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


@dataclass(frozen=True)
class CatalogDb:
    """The catalog's connection string, and the password source when it signs in with Entra."""

    conninfo: str
    password: Callable[[], str] | None = None

    def kwargs(self) -> dict[str, Any]:
        return {} if self.password is None else {"password": self.password()}

    async def akwargs(self) -> dict[str, Any]:
        if self.password is None:
            return {}
        # azure-identity's sync credential makes a blocking HTTP call when it renews.
        return {"password": await asyncio.to_thread(self.password)}

    def __repr__(self) -> str:
        return f"CatalogDb(entra={self.password is not None})"


#: What the apps pass around: a CatalogDb, or (tests, tools) a plain connection string.
Db = CatalogDb | str


def from_env(
    app: str,
    env: Mapping[str, str] | None = None,
    *,
    credential: TokenCredential | None = None,
) -> CatalogDb:
    """The catalog for `app` (api, enrol, web or worker) from the environment. Raises ValueError."""
    env = os.environ if env is None else env
    if app not in ROLES:
        raise ValueError(f"unknown app {app!r}")
    url = (env.get("OMNIFILE_DATABASE_URL") or "").strip()
    host = (env.get("OMNIFILE_PG_HOST") or "").strip()
    if url and host:
        raise ValueError(
            "set OMNIFILE_DATABASE_URL (dev and tests) or OMNIFILE_PG_HOST (Azure), not both"
        )
    if url:
        return CatalogDb(url)
    if not host:
        raise ValueError(
            "the catalog is not configured: set OMNIFILE_PG_HOST, OMNIFILE_PG_DATABASE and"
            " OMNIFILE_PG_USER (Azure, Entra sign-in), or OMNIFILE_DATABASE_URL (dev)"
        )
    missing = [n for n in ("OMNIFILE_PG_DATABASE", "OMNIFILE_PG_USER") if not env.get(n)]
    if missing:
        raise ValueError(f"{' and '.join(missing)} must be set with OMNIFILE_PG_HOST")
    sslmode = env.get("OMNIFILE_PG_SSLMODE") or "verify-full"
    allowed = {"verify-full", "require"}
    if env.get("OMNIFILE_DEV_MODE") == "1":
        allowed.add("disable")  # a local server without TLS, in tests
    if sslmode not in allowed:
        raise ValueError(f"OMNIFILE_PG_SSLMODE must be one of {sorted(allowed)}, not {sslmode!r}")
    port = env.get("OMNIFILE_PG_PORT") or "5432"
    if not port.isdigit():
        raise ValueError("OMNIFILE_PG_PORT must be a port number")
    params: dict[str, Any] = {
        "host": host,
        "port": port,
        "dbname": env["OMNIFILE_PG_DATABASE"],
        "user": env["OMNIFILE_PG_USER"],
        "sslmode": sslmode,
        "application_name": f"omnifile-{app}",
    }
    if sslmode == "verify-full":
        # libpq's own default (~/.postgresql/root.crt) is never there in a container.
        params["sslrootcert"] = env.get("OMNIFILE_PG_SSLROOTCERT") or (
            str(_SYSTEM_CAS) if _SYSTEM_CAS.exists() else "system"
        )
    conninfo = as_app(make_conninfo(**params), app)
    cred = credential or _credential(env.get("AZURE_CLIENT_ID") or None)
    return CatalogDb(conninfo, EntraPassword(cred))


def from_env_or_exit(app: str) -> CatalogDb:
    """`from_env` for an app's entry point: a configuration error ends the process."""
    try:
        return from_env(app)
    except ValueError as e:
        raise SystemExit(f"catalog: {e}") from None


def connect(db: Db, **kwargs: Any) -> psycopg.Connection:
    """A connection to the catalog (sync)."""
    if isinstance(db, str):
        return psycopg.connect(db, **kwargs)
    return psycopg.connect(db.conninfo, **db.kwargs(), **kwargs)


async def aconnect(db: Db, **kwargs: Any) -> psycopg.AsyncConnection:
    """A connection to the catalog (async)."""
    if isinstance(db, str):
        return await psycopg.AsyncConnection.connect(db, **kwargs)
    return await psycopg.AsyncConnection.connect(db.conninfo, **(await db.akwargs()), **kwargs)


def conninfo_of(db: Db) -> str:
    return db if isinstance(db, str) else db.conninfo


async def pool_kwargs(db: Db, base: Mapping[str, Any]) -> dict[str, Any]:
    """The keyword arguments a pool passes to each new connection: `base` plus a fresh password."""
    extra = {} if isinstance(db, str) else await db.akwargs()
    return {**base, **extra}


#: The most connections one replica of an app holds to the catalog. `deploy/azure/main.bicep`
#: sets it per app at the small size, whose B1ms server has 35 user connections.
POOL_MAX_ENV = "OMNIFILE_PG_POOL_MAX"
POOL_MAX_LIMIT = 100


def pool_max(default: int | None, env: Mapping[str, str] | None = None) -> int | None:
    """``OMNIFILE_PG_POOL_MAX``: a whole number from 1 to 100, or `default` when it is unset or
    empty. Anything else is a configuration error (ValueError)."""
    raw = (os.environ if env is None else env).get(POOL_MAX_ENV, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"{POOL_MAX_ENV} must be a whole number, not {raw!r}") from None
    if not 1 <= n <= POOL_MAX_LIMIT:
        raise ValueError(f"{POOL_MAX_ENV} must be from 1 to {POOL_MAX_LIMIT}, not {n}")
    return n


def pool_max_or_exit(default: int | None) -> int | None:
    """`pool_max` for an app's entry point: a configuration error ends the process."""
    try:
        return pool_max(default)
    except ValueError as e:
        raise SystemExit(f"catalog: {e}") from None
