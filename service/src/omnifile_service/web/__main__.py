"""Run the `web` app: python -m omnifile_service.web

Environment:
  OMNIFILE_PG_HOST / _DATABASE / _USER / _SSLMODE, AZURE_CLIENT_ID
                                  the catalog on Azure, with Entra sign-in as the app's
                                  managed identity (catalog/connect.py); or, dev and tests,
  OMNIFILE_DATABASE_URL           a libpq connection string
  OMNIFILE_WEB_ORIGIN             the portal's origin, e.g. https://portal.contoso.com (required)
  OMNIFILE_WEB_TENANT_ID          Entra tenant id (required)
  OMNIFILE_WEB_CLIENT_ID          the app registration's client id (required). Without any of
                                  these three, web starts not ready: /healthz answers 503 with
                                  what is missing, every other path 503, until the deployment's
                                  second phase sets them (deploy/entra/register.sh)
  OMNIFILE_WEB_API_AUDIENCES      comma-separated bearer audiences (application ID URI,
                                  client id); unset: partner tokens are refused
  OMNIFILE_WEB_OIDC               entra (default) | dev. dev mounts the dev issuer at
                                  /dev-idp and needs OMNIFILE_DEV_MODE=1
  OMNIFILE_WEB_CLIENT_SECRET      dev only, and only with OMNIFILE_DEV_MODE=1. Otherwise web
                                  signs in with a client assertion from its managed
                                  identity (AZURE_CLIENT_ID selects a user-assigned one)
  OMNIFILE_WEB_CURSOR_KEY         base64 of at least 32 bytes, shared by all replicas
                                  (listing cursors). Unset: per replica
  OMNIFILE_WEB_STATIC_DIR         the built SPA (portal/dist)
  OMNIFILE_ENROL_PUBLIC_URL       the enrol URL edges reach (enrolment bundles; unset:
                                  the portal can't add edges)
  OMNIFILE_ENROL_SERVER_CA        PEM of the CA that issued enrol's TLS certificate (or
  OMNIFILE_ENROL_SERVER_CA_SHA256 its SHA-256, hex): the edge pins it
  OMNIFILE_APPLIANCE_IMAGES_URL   where the appliance's install images are published (a base
                                  URL; Edges, Add edge links each file under it). Unset: the
                                  portal names the files of the release folder, with no link
  OMNIFILE_DEV_IDP_USERS          dev only: a JSON file of seeded users
                                  ([{"key", "oid", "name", "upn", "onprem_sid", "groups",
                                  "roles", "overage"}]); default: an admin, a steward, a reader
  OMNIFILE_DEV_IDP_APPS           dev only: a JSON file of partner apps
                                  ([{"client_id", "secret", "roles"}])
  OMNIFILE_DEV_CLOCK_FILE         dev only: the shared virtual clock (sim, api, worker).
                                  Catalog times follow it (WORM deadlines, pins, holds);
                                  sessions and sign-in stay on real time. Refused unless
                                  every catalog backend is sim
  OMNIFILE_DEV_MODE               "1" allows the dev settings above; refused otherwise
  OMNIFILE_WEB_HOST / PORT        default 0.0.0.0:8080 (TLS ends at the ingress)
  OMNIFILE_WEB_TRUSTED_INGRESS    comma-separated CIDRs of the ingress: only from these
                                  peers is X-Forwarded-For believed (the sign-in rate
                                  limit per address, audit rows' client_addr)
  Settings (portal-api.md 5.20), names and ids only, never a secret:
  OMNIFILE_SETTINGS_PROBES        "1": live checks call DNS, ARM and Graph with web's managed
                                  identity (it needs Reader on the storage accounts and the
                                  Storage Mover). Unset: no outbound call; checks say so
  OMNIFILE_AZURE_SUBSCRIPTION_ID  the storage accounts' subscription (RBAC and change-feed checks)
  OMNIFILE_AZURE_RESOURCE_GROUP   their resource group (web holds "OmniFile Settings Reader" there)
  OMNIFILE_SERVICE_PRINCIPAL_IDS  worker=<object id>,api=<object id>: who needs the data role
  OMNIFILE_AWS_ACCOUNTS[_FILE], OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE, OMNIFILE_AWS_PRIVATE_NETWORK
                                  the worker's AWS settings, for the S3 checks and trust policy
  OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS  object ids the AWS role must trust (worker, api, verifier)
  OMNIFILE_TENANT_ID, OMNIFILE_TEAMS_APP_ID, OMNIFILE_TEAMS_BOT_APP_ID, OMNIFILE_NOTIFY_SENDER,
  OMNIFILE_STORAGE_MOVER          as the worker has them
  OMNIFILE_TEAMS_APP_PACKAGE      the zip deploy/teams/build.sh made, for its download
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import uvicorn

from .. import metrics
from ..api.db import DEFAULT_POOL_MAX, Catalog
from ..apiclients.keys import install_redaction
from ..backends.sim.clock import DevClockRefused, OffsetClock, dev_clock, require_sim_only
from ..catalog.connect import Db, connect, from_env_or_exit, pool_max_or_exit
from ..settings.ports import probes_from_env, settings_env
from .auth import Authenticator, HttpJwks, ManagedIdentityAssertion, StaticJwks, StaticSecret
from .config import OidcEndpoints, WebSettings
from .context import WebServices
from .devidp import DevClient, DevIdp, DevUser, default_users
from .preview import PolicyPreviewPort

log = logging.getLogger("omnifile_service.web")


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def _need(name: str) -> str:
    value = _env(name)
    if value is None:
        sys.exit(f"{name} is required")
    return value


def cursor_key(value: str | None) -> bytes | None:
    if not value:
        return None
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        sys.exit("OMNIFILE_WEB_CURSOR_KEY must be base64")
    if len(key) < 32:
        sys.exit("OMNIFILE_WEB_CURSOR_KEY must decode to at least 32 bytes")
    return key


def trusted_ingress() -> tuple:
    """`OMNIFILE_WEB_TRUSTED_INGRESS`: the ingress's CIDRs (empty: no proxy)."""
    from ..api.clientauth import parse_networks

    raw = [c for c in (_env("OMNIFILE_WEB_TRUSTED_INGRESS") or "").split(",") if c.strip()]
    try:
        return parse_networks(raw)
    except ValueError as exc:
        raise SystemExit(f"OMNIFILE_WEB_TRUSTED_INGRESS: {exc}") from None


def enrol_ca_sha256() -> str | None:
    """OMNIFILE_ENROL_SERVER_CA_SHA256, or the SHA-256 of OMNIFILE_ENROL_SERVER_CA (a PEM file)."""
    if v := _env("OMNIFILE_ENROL_SERVER_CA_SHA256"):
        return v.lower()
    if path := _env("OMNIFILE_ENROL_SERVER_CA"):
        from ..enrol.bundle import ca_sha256

        return ca_sha256(Path(path).read_bytes())
    return None


def check_dev(oidc: str, secret: str | None, dev_mode: str | None) -> None:
    """The dev issuer and a static client secret need OMNIFILE_DEV_MODE=1."""
    if oidc not in ("entra", "dev"):
        sys.exit("OMNIFILE_WEB_OIDC must be entra or dev")
    if oidc == "dev" and dev_mode != "1":
        sys.exit("OMNIFILE_WEB_OIDC=dev is the dev issuer: it needs OMNIFILE_DEV_MODE=1")
    if secret is not None and dev_mode != "1":
        sys.exit("OMNIFILE_WEB_CLIENT_SECRET is a dev setting: it needs OMNIFILE_DEV_MODE=1")


def service_clock(conninfo: Db):
    """The service clock (`WebSettings.service_clock`): None for real time,
    or, dev only and over sim backends only, the shared virtual clock."""
    try:
        clock = dev_clock()
        if not isinstance(clock, OffsetClock):
            return None

        with connect(conninfo) as conn:
            kinds = dict(conn.execute("SELECT backend_id, kind FROM catalog.backend").fetchall())
        require_sim_only(clock, kinds)
    except DevClockRefused as e:
        sys.exit(str(e))
    log.warning("dev clock %s: catalog times now %s", clock.path, clock.now())
    return clock.now


#: Set by the deployment's second phase (deploy/entra/register.sh, deploy/azure/README.md step 6).
SIGN_IN_SETTINGS = ("OMNIFILE_WEB_ORIGIN", "OMNIFILE_WEB_TENANT_ID", "OMNIFILE_WEB_CLIENT_ID")


def build():
    missing = [name for name in SIGN_IN_SETTINGS if _env(name) is None]
    if missing:
        # Before the app registration exists: start, not ready, rather than exit and be
        # restarted in a loop (enrol/waiting.py).
        from ..enrol.waiting import create_waiting_app

        return create_waiting_app(
            "web",
            f"{', '.join(missing)} not set: run deploy/entra/register.sh and redeploy with"
            " webClientId (deploy/azure/README.md, step 6)",
        )
    conninfo = from_env_or_exit("web")
    origin = _need("OMNIFILE_WEB_ORIGIN").rstrip("/")
    tenant = _need("OMNIFILE_WEB_TENANT_ID")
    client_id = _need("OMNIFILE_WEB_CLIENT_ID")
    oidc = _env("OMNIFILE_WEB_OIDC", "entra") or "entra"
    secret = _env("OMNIFILE_WEB_CLIENT_SECRET")
    dev_mode = _env("OMNIFILE_DEV_MODE")
    check_dev(oidc, secret, dev_mode)
    audiences = tuple(a for a in (_env("OMNIFILE_WEB_API_AUDIENCES") or "").split(",") if a)
    endpoints = OidcEndpoints.dev(origin) if oidc == "dev" else OidcEndpoints.entra(tenant)
    settings = WebSettings(
        origin=origin,
        tenant_id=tenant,
        client_id=client_id,
        oidc=endpoints,
        api_audiences=audiences,
        dev_mode=dev_mode == "1",
        static_dir=Path(p) if (p := _env("OMNIFILE_WEB_STATIC_DIR")) else None,
        service_clock=service_clock(conninfo),
        enrol_url=_env("OMNIFILE_ENROL_PUBLIC_URL"),
        enrol_server_ca_sha256=enrol_ca_sha256(),
        trusted_ingress=trusted_ingress(),
        appliance_images_url=_env("OMNIFILE_APPLIANCE_IMAGES_URL"),
    )
    if key := cursor_key(_env("OMNIFILE_WEB_CURSOR_KEY")):
        settings.cursor_key = key
    else:
        log.warning("OMNIFILE_WEB_CURSOR_KEY is not set: cursors work on this replica only")
    catalog = Catalog(conninfo, max_size=pool_max_or_exit(DEFAULT_POOL_MAX))
    dev_app = None
    closers = []
    arm_for = None
    if oidc == "dev":
        users = default_users()
        if path := _env("OMNIFILE_DEV_IDP_USERS"):
            users = [DevUser.from_mapping(u) for u in json.loads(Path(path).read_text())]
        redirects = [settings.redirect_uri, settings.azure_redirect_uri]
        clients = {client_id: DevClient(client_id, secret or "dev-secret", redirects)}
        if path := _env("OMNIFILE_DEV_IDP_APPS"):
            for a in json.loads(Path(path).read_text()):
                clients[a["client_id"]] = DevClient(a["client_id"], a["secret"], (),
                                                    tuple(a.get("roles", ())))  # fmt: skip
        idp = DevIdp(
            issuer=endpoints.issuer,
            tenant_id=tenant,
            clients=clients,
            users={u.key: u for u in users},
            api_audience=audiences[0] if audiences else f"api://{client_id}",
        )
        dev_app = idp.app()
        # The token endpoint is this process: call it in process.
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.mounted()),
                                 base_url=f"{origin}/dev-idp")  # fmt: skip
        jwks = StaticJwks(idp.public_keys)
        credential = StaticSecret(secret or "dev-secret")
        # Connect Azure in dev mode never reaches Azure: a fake ARM with a demo tenant.
        from ..settings.fakes import dev_arm

        arm_for = dev_arm(tenant)
    else:
        http = httpx.AsyncClient()
        jwks = HttpJwks(endpoints.jwks, http)
        credential = (
            StaticSecret(secret) if secret else ManagedIdentityAssertion(_env("AZURE_CLIENT_ID"))
        )
        if isinstance(credential, ManagedIdentityAssertion):
            closers.append(credential.aclose)
    closers.append(http.aclose)
    services = WebServices(
        catalog=catalog,
        settings=settings,
        auth=Authenticator(settings, catalog, jwks, http, credential),
        preview=PolicyPreviewPort(),
        closers=closers,
        probes=probes_from_env(os.environ),
        settings_env=settings_env(os.environ),
        arm_for=arm_for,
    )
    from .app import create_app

    return create_app(services, dev_idp=dev_app)


def main() -> None:
    logging.basicConfig(level=_env("OMNIFILE_WEB_LOG_LEVEL", "INFO"))
    # No API key reaches a log line, whichever logger writes it (keys.py).
    install_redaction("uvicorn", "uvicorn.access", "uvicorn.error")
    try:
        metrics.configure("web")
    except ValueError as e:
        raise SystemExit(str(e)) from None
    app = build()
    uvicorn.run(
        app,
        host=_env("OMNIFILE_WEB_HOST", "0.0.0.0"),  # type: ignore[arg-type]
        port=int(_env("PORT", "8080")),  # type: ignore[arg-type]
        http="h11",
        proxy_headers=False,
        server_header=False,
        # Our logging (main): the root handler, which redacts API keys, writes uvicorn's too.
        log_config=None,
    )


if __name__ == "__main__":
    main()
