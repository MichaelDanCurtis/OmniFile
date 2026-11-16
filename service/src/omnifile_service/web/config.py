"""Settings for the `web` app (read from the environment by `__main__`)."""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ENTRA = "https://login.microsoftonline.com"

#: Entra app roles of signed-in users (portal-api.md section 3.1).
USER_ROLES = ("ArchiveAdmin", "DataSteward", "User")
#: Entra app roles of partner applications (section 2.2).
APP_ROLES = ("Policy.Propose", "Recall.Request", "Archive.Read", "Archive.Adopt")


@dataclass
class OidcEndpoints:
    issuer: str
    authorize: str
    token: str
    jwks: str
    end_session: str

    @classmethod
    def entra(cls, tenant_id: str) -> OidcEndpoints:
        base = f"{ENTRA}/{tenant_id}"
        return cls(
            issuer=f"{base}/v2.0",
            authorize=f"{base}/oauth2/v2.0/authorize",
            token=f"{base}/oauth2/v2.0/token",
            jwks=f"{ENTRA}/{tenant_id}/discovery/v2.0/keys",
            end_session=f"{base}/oauth2/v2.0/logout",
        )

    @classmethod
    def dev(cls, origin: str) -> OidcEndpoints:
        base = f"{origin}/dev-idp"
        return cls(
            issuer=base,
            authorize=f"{base}/authorize",
            token=f"{base}/token",
            jwks=f"{base}/jwks",
            end_session=f"{base}/logout",
        )


@dataclass
class WebSettings:
    #: The portal's origin, scheme://host[:port], no trailing slash. The
    #: Origin header of every unsafe session request must equal it.
    origin: str
    tenant_id: str
    #: The app registration's client id: the ID token audience.
    client_id: str
    oidc: OidcEndpoints
    #: Bearer token audiences: the API's application ID URI and client id.
    api_audiences: tuple[str, ...] = ()
    dev_mode: bool = False
    #: HMAC key for opaque cursors; every replica must share it.
    cursor_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    static_dir: Path | None = None
    session_idle: timedelta = timedelta(minutes=60)
    session_absolute: timedelta = timedelta(hours=10)
    login_ttl: timedelta = timedelta(minutes=10)
    reauth_max_age: timedelta = timedelta(minutes=10)
    #: identity.group_sids are used for an overage user while younger than this.
    groups_cache_max_age: timedelta = timedelta(hours=24)
    #: A signed-in user's groups older than this are refreshed from Graph (the
    #: worker's identity_resolve), and a newer resolution replaces the groups
    #: the session started with: someone removed from an AD group loses portal
    #: reads within about this long, not at their next sign-in (M7 review).
    groups_refresh_every: timedelta = timedelta(minutes=15)
    #: Bearer token clock leeway (section 2.2).
    leeway: timedelta = timedelta(minutes=5)
    rate_per_s: float = 20.0
    rate_burst: float = 40.0
    preview_rate_per_s: float = 5.0
    #: GET /auth/login per client address: each call writes an oidc_login row.
    login_rate_per_s: float = 1.0
    login_burst: float = 20.0
    #: Sessions, sign-in and token checks: always wall-clock time.
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Catalog times the worker later compares on its own clock (WORM lock
    #: deadlines, pin and hold expiries, rewind dates); None means `clock`.
    #: Only dev sets it, to the shared virtual clock the worker and api run
    #: on, so the two sides of a deadline can't disagree.
    service_clock: Callable[[], datetime] | None = None
    #: Enrolment bundles for new edges (portal-api.md section 5.15): the enrol
    #: URL edges reach, and the SHA-256 of the CA that issued its TLS certificate.
    #: Unset: the portal can't add edges.
    enrol_url: str | None = None
    enrol_server_ca_sha256: str | None = None
    #: How long a bundle stays usable.
    enrol_bundle_ttl: timedelta = timedelta(hours=24)
    #: The live Activity feed (portal-api.md 5.13): how many long-polls a
    #: replica holds open at once (more are answered at once, and told to come
    #: back later), and how often a held poll looks for something new.
    live_max_waiting: int = 32
    live_probe_s: float = 2.0
    #: Where the appliance's install images are published (Edges, Add edge):
    #: a base URL the file names are appended to. Unset: the wizard says they
    #: aren't published and names the files the release folder holds.
    appliance_images_url: str | None = None
    #: The ingress's addresses: only from these peers is X-Forwarded-For
    #: believed, for the sign-in rate limit and audit rows' client_addr.
    trusted_ingress: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @property
    def redirect_uri(self) -> str:
        return f"{self.origin}/auth/callback"

    @property
    def azure_redirect_uri(self) -> str:
        """Connect Azure's delegated sign-in (docs/design/cloud-connect.md 1.2)."""
        return f"{self.origin}/auth/azure/callback"
