"""Run the `api` app: python -m omnifile_service.api

Environment:
  OMNIFILE_PG_HOST / _DATABASE / _USER / _SSLMODE, AZURE_CLIENT_ID
                                   the catalog on Azure, with Entra sign-in as the app's
                                   managed identity (catalog/connect.py); or, dev and tests,
  OMNIFILE_DATABASE_URL            a libpq connection string
  OMNIFILE_PG_POOL_MAX             the most catalog connections a replica holds (default 10)
  OMNIFILE_CA_CERT                OmniFile edge CA certificate, PEM: the trust anchor for
                                   client certificates. Unset: api waits (/healthz answers
                                   200, every other path 503) until the deployment is given
                                   the CA (deploy/azure/README.md, step 7)
  OMNIFILE_API_CLIENT_AUTH         direct_tls (default): api terminates mutual TLS itself and
                                   requires a client certificate from the edge CA. Azure runs
                                   it this way behind a TCP ingress (threat model T5), and dev.
                                   | forwarded: an HTTP ingress terminates TLS and forwards the
                                   certificate in X-Forwarded-Client-Cert
  OMNIFILE_API_SERVER_CERT_URL     direct_tls in Azure: enrol's /internal/v0/sign. Each replica
                                   makes a key in memory and has enrol sign its server
                                   certificate (purpose api_server, the edge CA, Key Vault), and
                                   renews it from two thirds of its life. The token is the same
                                   as for renewals (OMNIFILE_ENROL_AUDIENCE, or the dev token)
  OMNIFILE_API_TLS_CERT / _KEY     direct_tls in dev: the server certificate and key, as files.
                                   The cert file holds the full chain, leaf then CA
  OMNIFILE_API_HEALTH_PORT         direct_tls: a loopback plain-HTTP /healthz for the image's
                                   HEALTHCHECK (default 8080, on 127.0.0.1)
  OMNIFILE_API_TRUST_FORWARDED_CLIENT_CERT
                                   must be "true" for forwarded: X-Forwarded-Client-Cert is
                                   refused otherwise
  OMNIFILE_API_TRUSTED_PROXIES     required for forwarded: comma-separated CIDRs of the proxy.
                                   The header is honoured only from these peers; api refuses to
                                   start in forwarded mode without them. They must hold only
                                   the proxy (never a subnet other apps share; T5)
  OMNIFILE_API_APP_SUBNETS         required for forwarded: the subnets the other apps run in
                                   (the template sets them). api refuses to start when a proxy
                                   range overlaps one
  OMNIFILE_ENROL_SIGN_URL          enrol's /internal/v0/sign (certificate renewal)
  OMNIFILE_ENROL_DEV_TOKEN         dev only, and only with OMNIFILE_DEV_MODE=1: shared token for
                                   the sign call. Otherwise the managed identity token for
                                   OMNIFILE_ENROL_AUDIENCE (AZURE_CLIENT_ID)
  OMNIFILE_DEV_MODE                "1" allows the dev shortcuts above, and catalog backends of kind
                                   'sim'; without it, api refuses to start when the catalog has one
  OMNIFILE_API_MIN_EDGE_VERSION    default 0.0.0
  OMNIFILE_API_URL                 returned to edges as api_url
  OMNIFILE_API_LEASE_KEY           base64 of at least 32 bytes, shared by all replicas (stub leases)
  OMNIFILE_API_HOST / PORT         default 0.0.0.0:8443 (direct_tls) or :8080 (forwarded)
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

import uvicorn
from cryptography import x509

from .. import metrics
from ..backends.sim.clock import DevClockRefused, OffsetClock, dev_clock, require_sim_only
from ..catalog.connect import Db, connect, from_env_or_exit, pool_max_or_exit
from ..enrol.waiting import create_waiting_app
from .app import create_app
from .clientauth import (
    ClientCertSource,
    DirectTls,
    EdgeAuthenticator,
    EnrolServerCertificates,
    ForwardedClientCert,
    ServerCertificate,
    load_chain_in_memory,
    network,
    server_tls_context,
)
from .context import ApiSettings, Services, lease_key_from_env
from .dataplane import DataPlanes
from .db import DEFAULT_POOL_MAX, Catalog
from .signclient import HttpSignClient, ManagedIdentityToken, StaticToken, TokenProvider
from .tlsproto import ClientCertH11Protocol

log = logging.getLogger("omnifile_service.api")

#: Retry getting the first server certificate from enrol this often at most.
FIRST_CERT_BACKOFF_MAX_S = 30.0
#: After a failed renewal, try again after this, doubling up to RENEW_RETRY_MAX_S.
RENEW_RETRY_S = 60.0
RENEW_RETRY_MAX_S = 15 * 60.0
#: Unrenewed this close to its end, the server certificate stops api (`keep_renewed`).
EXPIRY_MARGIN = timedelta(days=2)


def _env(name: str, default: str | None = None, env: Mapping[str, str] | None = None) -> str | None:
    value = (os.environ if env is None else env).get(name)
    return value if value else default


def _need(name: str, env: Mapping[str, str] | None = None) -> str:
    value = _env(name, env=env)
    if value is None:
        sys.exit(f"{name} is required")
    return value


def backend_registry(conninfo: Db):
    """The worker track's backend selection, built from catalog.backend rows."""
    try:
        from ..worker.backends import BackendRegistry, BackendRow
    except ImportError:
        log.warning("no backend registry available; only sim_http backends will work")
        return None
    with connect(conninfo) as conn:
        rows = conn.execute(
            "SELECT backend_id, kind, config, private_network FROM catalog.backend"
        ).fetchall()
    return BackendRegistry(BackendRow(r[0], r[1], r[2] or {}, r[3]) for r in rows)


def backend_kinds(conninfo: Db) -> dict[int, str]:
    """backend_id -> kind of every catalog backend (the dev clock's guard)."""

    with connect(conninfo) as conn:
        rows = conn.execute("SELECT backend_id, kind FROM catalog.backend").fetchall()
    return {r[0]: r[1] for r in rows}


def service_clock(conninfo: Db):
    """The real clock, or (dev only, every backend sim) the shared virtual one.
    Exits on a dev clock in production or over a real backend."""
    try:
        clock = dev_clock()
        if isinstance(clock, OffsetClock):
            require_sim_only(clock, backend_kinds(conninfo))
    except DevClockRefused as e:
        sys.exit(str(e))
    return clock


def forwarded_source(
    trust: str | None, proxies: str | None, app_subnets: str | None = None
) -> ForwardedClientCert:
    """Forwarded mode: the proxy's addresses are required, so a caller that reaches the app
    directly cannot present its own X-Forwarded-Client-Cert. And they must not overlap the
    subnets other apps run in (`app_subnets`, required): a proxy range that admits them
    lets any of their replicas name any edge (threat model T5)."""
    if trust != "true":
        sys.exit("forwarded mode needs OMNIFILE_API_TRUST_FORWARDED_CLIENT_CERT=true")
    cidrs = [p for p in (proxies or "").split(",") if p.strip()]
    if not cidrs:
        sys.exit(
            "forwarded mode needs OMNIFILE_API_TRUSTED_PROXIES (the proxy's CIDRs);"
            " refusing to trust X-Forwarded-Client-Cert from any peer"
        )
    subnets = [s for s in (app_subnets or "").split(",") if s.strip()]
    if not subnets:
        sys.exit(
            "forwarded mode needs OMNIFILE_API_APP_SUBNETS (the subnets the other apps run"
            " in), to check the proxy range against"
        )
    try:
        source = ForwardedClientCert(trust_ingress=True, trusted_proxies=tuple(cidrs))
    except ValueError as exc:
        sys.exit(f"OMNIFILE_API_TRUSTED_PROXIES: {exc}")
    try:
        apps = [network(s) for s in subnets]
    except ValueError as exc:
        sys.exit(f"OMNIFILE_API_APP_SUBNETS: {exc}")
    for proxy in source.trusted:
        for net in apps:
            if proxy.version == net.version and proxy.overlaps(net):
                sys.exit(
                    f"OMNIFILE_API_TRUSTED_PROXIES: {proxy} overlaps {net}, where other apps"
                    " run: any of their replicas could forge X-Forwarded-Client-Cert (threat"
                    " model T5). Name a range that holds only the proxy"
                )
    return source


def client_cert_source(env: Mapping[str, str]) -> tuple[str, ClientCertSource]:
    """(mode, source) from the environment: how api learns who is calling (T5).
    Exits on a setting that would trust a forged header."""
    mode = _env("OMNIFILE_API_CLIENT_AUTH", "direct_tls", env)
    if mode == "direct_tls":
        return mode, DirectTls()
    if mode == "forwarded":
        return mode, forwarded_source(
            _env("OMNIFILE_API_TRUST_FORWARDED_CLIENT_CERT", env=env),
            _env("OMNIFILE_API_TRUSTED_PROXIES", env=env),
            _env("OMNIFILE_API_APP_SUBNETS", env=env),
        )
    sys.exit("OMNIFILE_API_CLIENT_AUTH must be direct_tls or forwarded")


def dev_token(token: str | None, dev_mode: str | None) -> str | None:
    """The dev sign token, only when OMNIFILE_DEV_MODE=1 says this is a dev install."""
    if token is None:
        return None
    if dev_mode != "1":
        sys.exit("OMNIFILE_ENROL_DEV_TOKEN is a dev setting: it needs OMNIFILE_DEV_MODE=1")
    return token


def enrol_tokens(env: Mapping[str, str]) -> TokenProvider:
    """api's token for enrol: the dev shared token, or its managed identity's."""
    dev = dev_token(_env("OMNIFILE_ENROL_DEV_TOKEN", env=env), _env("OMNIFILE_DEV_MODE", env=env))
    if dev:
        return StaticToken(dev)
    return ManagedIdentityToken(
        _need("OMNIFILE_ENROL_AUDIENCE", env), _env("AZURE_CLIENT_ID", env=env)
    )


def build():
    """(mode, app). mode is direct_tls, forwarded, or waiting (no CA certificate yet)."""
    if not _env("OMNIFILE_CA_CERT"):
        return "waiting", create_waiting_app(
            "api",
            "no edge CA certificate (OMNIFILE_CA_CERT): redeploy with edgeCaCertPem"
            " (deploy/azure/README.md, step 7)",
        )
    conninfo = from_env_or_exit("api")
    with open(_need("OMNIFILE_CA_CERT"), "rb") as fh:
        ca = x509.load_pem_x509_certificate(fh.read())
    mode, source = client_cert_source(os.environ)

    catalog = Catalog(conninfo, max_size=pool_max_or_exit(DEFAULT_POOL_MAX))
    signer = None
    if sign_url := _env("OMNIFILE_ENROL_SIGN_URL"):
        signer = HttpSignClient(sign_url, enrol_tokens(os.environ))
    settings = ApiSettings(
        min_edge_version=_env("OMNIFILE_API_MIN_EDGE_VERSION", "0.0.0") or "0.0.0",
        api_url=_env("OMNIFILE_API_URL"),
    )
    try:
        key = lease_key_from_env(_env("OMNIFILE_API_LEASE_KEY"))
    except ValueError as exc:
        sys.exit(f"OMNIFILE_API_LEASE_KEY: {exc}")
    if key:
        settings.lease_key = key
    else:
        log.warning("OMNIFILE_API_LEASE_KEY is not set: stub leases work on this replica only")
    # Dev and e2e: the shared virtual clock (OMNIFILE_DEV_CLOCK_FILE, dev
    # mode only), so leases, SAS expiries and recall ETAs follow sim_http's
    # /_sim/advance like the worker's and the facade's.
    clock = service_clock(conninfo)
    from ..worker.backends import sim_refusal

    dev_mode = os.environ.get("OMNIFILE_DEV_MODE") == "1"
    if (why := sim_refusal(backend_kinds(conninfo), os.environ)) is not None:
        sys.exit(why)
    registry = backend_registry(conninfo)
    extra: dict = {}
    if isinstance(clock, OffsetClock):
        log.warning("dev clock %s: now %s", clock.path, clock.now())
        extra["clock"] = clock.now
    services = Services(
        catalog=catalog,
        auth=EdgeAuthenticator(source, ca, catalog),
        planes=DataPlanes(registry, allow_sim=dev_mode, **extra),
        signer=signer,
        settings=settings,
        **extra,
    )
    return mode, create_app(services)


# --- serving -------------------------------------------------------------------


class CertificateExpiring(RuntimeError):
    """Renewal kept failing until the certificate is within EXPIRY_MARGIN of its end:
    api exits, so its restart asks enrol for a fresh one before edges see an expired one."""


async def first_certificate(
    certificates: EnrolServerCertificates, sleep: Callable = asyncio.sleep
) -> ServerCertificate:
    """api's first server certificate. Without one it can't serve, so it keeps asking enrol
    (which may still be starting), whatever the failure (a token, the network, a certificate
    that doesn't check), backing off to 30 s."""
    delay = 1.0
    while True:
        try:
            cert = await certificates.issue()
        except Exception as exc:  # noqa: BLE001 - any failure here is retried, never fatal
            log.warning(
                "no server certificate from enrol yet: %s: %s; retrying in %.0f s",
                type(exc).__name__,
                exc,
                delay,
            )
            await sleep(delay)
            delay = min(delay * 2, FIRST_CERT_BACKOFF_MAX_S)
            continue
        log.info("server certificate %s, until %s", cert.sha256.hex(), cert.not_after)
        return cert


async def keep_renewed(
    ctx: ssl.SSLContext,
    certificates: EnrolServerCertificates,
    current: ServerCertificate,
    *,
    sleep: Callable = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Renew the server certificate from its renew_after; new handshakes get the new one at
    once. Any failure (a managed-identity token, enrol, a certificate that doesn't check, the
    TLS context) is retried with backoff, from 1 to 15 minutes. If the certificate gets within
    EXPIRY_MARGIN of its end still unrenewed, raises CertificateExpiring: `serve` then stops
    api, and its restart starts over with `first_certificate`."""
    failures = 0
    while True:
        wait = (current.renew_after - now()).total_seconds()
        await sleep(max(wait, 1.0))
        if current.not_after - now() <= EXPIRY_MARGIN:
            raise CertificateExpiring(
                f"server certificate {current.sha256.hex()} ends {current.not_after} and"
                f" {failures} renewals failed"
            )
        try:
            fresh = await certificates.issue()
            load_chain_in_memory(ctx, fresh.chain_pem, fresh.key_pem)
        except Exception as exc:  # noqa: BLE001 - renewal must never stop on its own
            failures += 1
            delay = min(RENEW_RETRY_S * 2 ** (failures - 1), RENEW_RETRY_MAX_S)
            log.error(
                "server certificate renewal failed (%s: %s); %s left; retrying in %.0f s",
                type(exc).__name__,
                exc,
                current.not_after - now(),
                delay,
            )
            current = ServerCertificate(
                current.chain_pem,
                current.key_pem,
                current.not_after,
                now() + timedelta(seconds=delay),
                current.sha256,
            )
            continue
        failures = 0
        log.info("server certificate renewed: %s, until %s", fresh.sha256.hex(), fresh.not_after)
        current = fresh


async def start_health(host: str, port: int, ready: Callable[[], bool]) -> asyncio.Server:
    """A plain-HTTP `GET /healthz` beside the TLS listener, for the image's
    HEALTHCHECK (direct TLS needs a client certificate, which a probe lacks)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            ok = line.startswith(b"GET /healthz ") and ready()
            body = b'{"status":"ok"}' if ok else b'{"status":"unavailable"}'
            status = b"200 OK" if ok else b"503 Service Unavailable"
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                b"Content-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)


async def serve(
    app,
    *,
    host: str,
    port: int,
    ctx: ssl.SSLContext | None,
    certificates: EnrolServerCertificates | None = None,
    health_port: int | None = None,
    waiting: bool = False,
    on_server: Callable[[uvicorn.Server], None] | None = None,
) -> None:
    """Serve `app`; with `ctx`, over mutual TLS (direct_tls). With
    `certificates`, the server certificate comes from enrol first.
    `on_server` gets the uvicorn server once made (tests stop it). `waiting`: the app is
    the waiting one, so the loopback health listener says not ready too."""
    renew = None
    renewal_died: list[BaseException] = []
    if ctx is not None and certificates is not None:
        current = await first_certificate(certificates)
        load_chain_in_memory(ctx, current.chain_pem, current.key_pem)
        renew = asyncio.create_task(keep_renewed(ctx, certificates, current))
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        http=ClientCertH11Protocol if ctx is not None else "h11",
        proxy_headers=False,  # the peer address is the TCP peer; trusted_proxies checks it
        server_header=False,
    )
    config.load()
    config.ssl = ctx  # our own context: the certificate is replaced in place on renewal
    server = uvicorn.Server(config)
    if on_server is not None:
        on_server(server)

    def renewal_done(task: asyncio.Task) -> None:
        # The renewal task never ends while api serves. If it does (CertificateExpiring, or
        # anything else), api stops, and exits non-zero, rather than serve on to a dead end.
        if task.cancelled():
            return
        exc = task.exception() or RuntimeError("the renewal task returned")
        log.critical(
            "server certificate renewal stopped: %s: %s; stopping api", type(exc).__name__, exc
        )
        renewal_died.append(exc)
        server.should_exit = True

    if renew is not None:
        renew.add_done_callback(renewal_done)
    health = None
    if health_port and health_port != port:
        health = await start_health(
            "127.0.0.1",
            health_port,
            lambda: (
                not waiting
                and server.started
                and not server.should_exit
                and (renew is None or not renew.done())
            ),
        )
    try:
        await server.serve()
    finally:
        if renew is not None:
            renew.cancel()
        if health is not None:
            health.close()
        if certificates is not None:
            await certificates.aclose()
    if renewal_died:
        raise SystemExit(1)


def main() -> None:
    logging.basicConfig(level=_env("OMNIFILE_API_LOG_LEVEL", "INFO"))
    try:
        metrics.configure("api")
    except ValueError as e:
        sys.exit(str(e))
    mode, app = build()
    host = _env("OMNIFILE_API_HOST", "0.0.0.0") or "0.0.0.0"
    health_port = int(_env("OMNIFILE_API_HEALTH_PORT", "8080") or "8080")
    if mode == "waiting":
        port = int(_env("PORT", "8080") or "8080")
        asyncio.run(
            serve(app, host=host, port=port, ctx=None, health_port=health_port, waiting=True)
        )
        return
    if mode == "direct_tls":
        with open(_need("OMNIFILE_CA_CERT"), "rb") as fh:
            ca_pem = fh.read()
        ctx = server_tls_context(ca_pem)
        certificates = None
        if url := _env("OMNIFILE_API_SERVER_CERT_URL"):
            certificates = EnrolServerCertificates(
                url, enrol_tokens(os.environ), x509.load_pem_x509_certificate(ca_pem)
            )
        else:
            ctx.load_cert_chain(_need("OMNIFILE_API_TLS_CERT"), _need("OMNIFILE_API_TLS_KEY"))
        port = int(_env("PORT", "8443") or "8443")
        asyncio.run(
            serve(
                app,
                host=host,
                port=port,
                ctx=ctx,
                certificates=certificates,
                health_port=health_port,
            )
        )
        return
    port = int(_env("PORT", "8080") or "8080")
    asyncio.run(serve(app, host=host, port=port, ctx=None))


if __name__ == "__main__":
    main()
