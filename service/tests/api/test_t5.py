"""Threat model T5: a foothold inside the internal environment can't act as an edge.

Before, api ran behind the HTTP ingress in forwarded mode and believed
X-Forwarded-Client-Cert from the whole internal subnet, where web, worker, enrol
and the verifier also run. A certificate is public, so any of them could name
any edge. Now (deploy/azure/main.bicep, `apiIngress = tcp` by default):

- api terminates mutual TLS itself behind a TCP passthrough, with a server
  certificate enrol signs from the edge CA, and refuses the header outright;
- forwarded mode stays for a proxy that is alone in `apiForwardedProxyCidrs`,
  with no fallback to the subnet, and believes the header from that proxy only.

The deployed-configuration tests evaluate the compiled template (`Arm`, from
tests/test_deploy_env.py) and build api's certificate source exactly as
`api/__main__.py` does from that environment. Against the template before this
change they fail: the forged header from inside the subnet authenticated.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import socket
import ssl
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from omnifile_service.api import __main__ as api_main
from omnifile_service.api.app import create_app
from omnifile_service.api.clientauth import (
    DirectTls,
    EdgeAuthenticator,
    EnrolServerCertificates,
    ForwardedClientCert,
    ServerCertificate,
    server_tls_context,
)
from omnifile_service.api.context import ApiSettings, Services
from omnifile_service.api.dataplane import DataPlanes
from omnifile_service.api.db import Catalog
from omnifile_service.api.problems import ApiError
from omnifile_service.api.signclient import StaticToken
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.app import EnrolServices, FailureLimiter, create_enrol_app
from omnifile_service.enrol.ca import FileCASigner, cert_sha256
from omnifile_service.enrol.callers import SharedTokenVerifier

from .conftest import SIGN_TOKEN, EdgeClient, enrol_body, idem, new_key, pending_edge, xfcc
from .test_clientauth import conn


def _deploy_env():
    path = Path(__file__).resolve().parents[1] / "test_deploy_env.py"
    spec = importlib.util.spec_from_file_location("omnifile_test_deploy_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deploy_env = _deploy_env()

#: A replica of web, worker, enrol or the verifier: an address inside the internal
#: environment's subnet (main.bicep caeInternalSubnetPrefix, 10.60.0.0/26).
INSIDE_THE_SUBNET = "10.60.0.37"


@pytest.fixture
def compiled() -> dict:
    t = deploy_env._compiled()
    if t is None:
        pytest.skip("needs the Bicep CLI and deploy/azure")
    return t


def deployed_source(template: dict, **params):
    """api's certificate source as deployed with `params` (exits like api would)."""
    env = deploy_env.deployed(template, **params).env("app-api")
    as_text = {k: str(v) for k, v in env.items() if v is not None}
    return api_main.client_cert_source(as_text)


class _CatalogCtx:
    def __init__(self, url: str) -> None:
        self.catalog = Catalog(as_app(url, "api"), max_size=1)

    async def __aenter__(self):
        await self.catalog.open()
        return self.catalog

    async def __aexit__(self, *exc):
        await self.catalog.close()


def authenticate(db_url, source, ca, request, edge_id):
    async def go():
        async with _CatalogCtx(db_url) as catalog:
            return await EdgeAuthenticator(source, ca, catalog).authenticate(request, edge_id)

    return asyncio.run(go())


# --- the deployed configuration --------------------------------------------------------


def test_the_default_deployment_believes_no_forwarded_certificate(
    compiled, enrol_edge, ca, db_url
) -> None:
    victim = enrol_edge()
    mode, source = deployed_source(compiled)
    assert mode == "direct_tls" and isinstance(source, DirectTls)
    forged = conn({"x-forwarded-client-cert": xfcc(victim.cert_pem)}, client=INSIDE_THE_SUBNET)
    with pytest.raises(ApiError) as refused:
        authenticate(db_url, source, ca.ca_certificate, forged, victim.edge_id)
    assert refused.value.code == "certificate_invalid"


#: The subnets the template's other apps run in (main.bicep: the internal and the relay
#: environments), as api gets them in OMNIFILE_API_APP_SUBNETS.
APP_SUBNETS = "10.60.0.0/26,10.60.0.64/27"


def test_the_template_offers_no_forwarded_mode(compiled) -> None:
    """Review of #34: api always runs in the shared internal environment, whose ingress is in
    the subnet every app runs in, so no apiForwardedProxyCidrs value was safe. `http` is gone
    from apiIngress (fail closed at compile time), and api is told where the apps run."""
    assert compiled["parameters"]["apiIngress"]["allowedValues"] == ["tcp"]
    assert "apiForwardedProxyCidrs" not in compiled["parameters"]
    env = deploy_env.deployed(compiled).env("app-api")
    assert env["OMNIFILE_API_APP_SUBNETS"] == APP_SUBNETS


def test_forwarded_mode_refuses_a_proxy_range_over_the_apps() -> None:
    """The second fail-closed place: api itself. The previous default (forwarded mode,
    proxies = the internal subnet) no longer starts; nor does any range touching it."""
    for proxies in ("10.60.0.0/26", "10.60.0.37/32", "10.60.0.0/16", "10.60.0.64/28"):
        with pytest.raises(SystemExit, match="overlaps"):
            api_main.forwarded_source("true", proxies, APP_SUBNETS)
    with pytest.raises(SystemExit, match="OMNIFILE_API_APP_SUBNETS"):
        api_main.forwarded_source("true", "10.70.0.4/32", None)


def test_forwarded_mode_believes_only_the_configured_proxy(enrol_edge, ca, db_url) -> None:
    edge = enrol_edge()
    source = api_main.forwarded_source("true", "10.70.0.4/32", APP_SUBNETS)
    header = {"x-forwarded-client-cert": xfcc(edge.cert_pem)}
    who = authenticate(db_url, source, ca.ca_certificate, conn(header, "10.70.0.4"), edge.edge_id)
    assert who.edge_id == edge.edge_id
    for peer in (INSIDE_THE_SUBNET, "10.70.0.5", "127.0.0.1"):
        with pytest.raises(ApiError) as refused:
            authenticate(db_url, source, ca.ca_certificate, conn(header, peer), edge.edge_id)
        assert refused.value.code == "certificate_invalid", peer
        assert "untrusted peer" in (refused.value.detail or "")


def test_the_old_default_is_why(enrol_edge, ca, db_url) -> None:
    """Why the overlap is refused: with the subnet trusted (the previous default), a forged
    header from any app's replica in it is any edge."""
    victim = enrol_edge()
    source = ForwardedClientCert(trust_ingress=True, trusted_proxies=("10.60.0.0/26",))
    forged = conn({"x-forwarded-client-cert": xfcc(victim.cert_pem)}, client=INSIDE_THE_SUBNET)
    who = authenticate(db_url, source, ca.ca_certificate, forged, victim.edge_id)
    assert who.edge_id == victim.edge_id


def test_an_ipv4_mapped_proxy_range_is_checked_as_ipv4() -> None:
    """Review of #34: `::ffff:10.60.0.0/122` is the internal subnet written as IPv6, and the
    overlap check skipped it (a different IP version). Mapped ranges are IPv4 now, in the
    check and in the trust list; a range holding all of mapped IPv4 is refused."""
    for proxies in ("::ffff:10.60.0.0/122", "::ffff:10.60.0.37/128", "::ffff:a3c:40/124"):
        with pytest.raises(SystemExit, match="overlaps"):
            api_main.forwarded_source("true", proxies, APP_SUBNETS)
    with pytest.raises(SystemExit, match="overlaps"):
        api_main.forwarded_source("true", "10.60.0.4/32", "::ffff:10.60.0.0/122")
    for proxies in ("::/48", "::ffff:0:0/96", "::ffff:0:0/100"):
        with pytest.raises(SystemExit, match="IPv4-mapped|wider|trusts every"):
            api_main.forwarded_source("true", proxies, APP_SUBNETS)


def test_an_ipv4_mapped_peer_is_matched_as_ipv4(enrol_edge, ca, db_url) -> None:
    """A dual-stack socket reports an IPv4 caller as `::ffff:a.b.c.d`: the configured IPv4
    proxy is still believed through it, and a mapped proxy range covers the IPv4 peer."""
    edge = enrol_edge()
    header = {"x-forwarded-client-cert": xfcc(edge.cert_pem)}
    for proxies, peer in (
        ("10.70.0.4/32", "::ffff:10.70.0.4"),
        ("::ffff:10.70.0.4/128", "10.70.0.4"),
    ):
        source = api_main.forwarded_source("true", proxies, APP_SUBNETS)
        who = authenticate(db_url, source, ca.ca_certificate, conn(header, peer), edge.edge_id)
        assert who.edge_id == edge.edge_id, (proxies, peer)
    source = api_main.forwarded_source("true", "10.70.0.4/32", APP_SUBNETS)
    with pytest.raises(ApiError) as refused:
        authenticate(db_url, source, ca.ca_certificate, conn(header, "::ffff:10.70.0.5"),
                     edge.edge_id)  # fmt: skip
    assert refused.value.code == "certificate_invalid"


def test_an_ipv4_mapped_ingress_peer_is_the_ingress() -> None:
    """The same for enrol's caller address: an IPv4-mapped peer that is the trusted ingress
    is the ingress, so its X-Forwarded-For entry is the caller's."""
    from omnifile_service.api.clientauth import forwarded_client_ip, parse_networks

    ingress = parse_networks(["100.100.0.0/24"])
    got = forwarded_client_ip("::ffff:100.100.0.9", ["10.20.0.5"], ingress)
    assert str(got) == "10.20.0.5"
    assert str(forwarded_client_ip("::ffff:10.20.0.6", ["1.2.3.4"], ingress)) == "10.20.0.6"


def test_a_proxy_range_that_trusts_everyone_is_refused() -> None:
    for proxies in ("0.0.0.0/0", "0.0.0.0/1,128.0.0.0/1"):
        with pytest.raises(SystemExit, match="trusts|wider"):
            api_main.forwarded_source("true", proxies, APP_SUBNETS)


# --- the real thing: api's TLS listener behind a TCP passthrough ---------------------


class TcpPassthrough:
    """Bytes in, bytes out, as a Container Apps TCP ingress does: no TLS, no headers."""

    def __init__(self, target: tuple[str, int]) -> None:
        self.target = target
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            upstream = socket.create_connection(self.target)
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        self.sock.close()


def _free_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def _serve_in_thread(run) -> uvicorn.Server:
    holder: list[uvicorn.Server] = []
    thread = threading.Thread(target=lambda: asyncio.run(run(holder.append)), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not (holder and holder[0].started):
        assert thread.is_alive(), "the server stopped"
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.05)
    return holder[0]


NAME = "localhost"  # api's DNS name in this test; enrol issues its certificate for it


@pytest.fixture
def tcp_api(db_url: str, ca: FileCASigner, tmp_path: Path) -> Iterator[dict]:
    """enrol (HTTP) and api (direct TLS, its server certificate from that enrol, through
    api/__main__.serve) on loopback, api behind a TCP passthrough."""
    enrol_services = EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=2),
        signer=ca,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        api_url="https://placeholder",
        limiter=FailureLimiter(limit=50),
        api_server_names=(NAME,),
    )
    enrol_port = _free_port()
    enrol = _serve_in_thread(
        lambda hook: _run_enrol(create_enrol_app(enrol_services), enrol_port, hook)
    )
    catalog = Catalog(as_app(db_url, "api"), max_size=2)
    services = Services(
        catalog=catalog,
        auth=EdgeAuthenticator(DirectTls(), ca.ca_certificate, catalog),
        planes=DataPlanes(),
        signer=None,
        settings=ApiSettings(),
    )
    ca_pem = ca.ca_certificate.public_bytes(serialization.Encoding.PEM)
    api_port, health_port = _free_port(), _free_port()
    certificates = EnrolServerCertificates(
        f"http://127.0.0.1:{enrol_port}/internal/v0/sign",
        StaticToken(SIGN_TOKEN),
        ca.ca_certificate,
    )
    api = _serve_in_thread(
        lambda hook: api_main.serve(
            create_app(services),
            host="127.0.0.1",
            port=api_port,
            ctx=server_tls_context(ca_pem),
            certificates=certificates,
            health_port=health_port,
            on_server=hook,
        )
    )
    proxy = TcpPassthrough(("127.0.0.1", api_port))
    ca_path = tmp_path / "edge-ca.pem"
    ca_path.write_bytes(ca_pem)
    try:
        yield {
            "url": f"https://{NAME}:{proxy.port}",
            "enrol": f"http://127.0.0.1:{enrol_port}",
            "ca_path": ca_path,
            "proxy": proxy,
            "health": f"http://127.0.0.1:{health_port}/healthz",
            "api_port": api_port,
        }
    finally:
        proxy.close()
        api.should_exit = True
        enrol.should_exit = True
        time.sleep(0.2)


async def _run_enrol(app, port: int, hook) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    hook(server)
    await server.serve()


def _client_files(tmp: Path, edge: EdgeClient) -> tuple[str, str]:
    cert, key = tmp / f"edge-{edge.edge_id}.pem", tmp / f"edge-{edge.edge_id}.key"
    cert.write_text(edge.cert_pem)
    key.write_bytes(
        edge.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert), str(key)


def _edge(enrol_url: str, sql) -> EdgeClient:
    """An edge enrolled through the running enrol; returns it and its enrolment answer."""
    edge_id, token = pending_edge(sql)
    key = new_key()
    resp = httpx.post(f"{enrol_url}/v0/enrol", json=enrol_body(token, key), headers=idem())
    assert resp.status_code == 201, resp.text
    edge = EdgeClient(None, edge_id, key, resp.json()["cert_pem"])  # type: ignore[arg-type]
    edge.answer = resp.json()  # type: ignore[attr-defined]
    return edge


def _heartbeat(url: str, edge_id: int, ctx: ssl.SSLContext, headers=None) -> httpx.Response:
    h = {"Idempotency-Key": f"t5-{time.monotonic_ns()}-{edge_id}xxxxxxxx", **(headers or {})}
    with httpx.Client(verify=ctx, timeout=10) as c:
        return c.post(f"{url}/v0/edges/{edge_id}/heartbeat", json={"version": "0.1.0"}, headers=h)


def test_a_forged_header_through_a_tcp_passthrough_authenticates_nobody(
    tcp_api, sql, tmp_path
) -> None:
    victim, attacker = _edge(tcp_api["enrol"], sql), _edge(tcp_api["enrol"], sql)

    def ctx_for(edge: EdgeClient | None) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=str(tcp_api["ca_path"]))
        if edge is not None:
            ctx.load_cert_chain(*_client_files(tmp_path, edge))
        return ctx

    pin = victim.answer["api_server_ca_sha256"]  # type: ignore[attr-defined]
    edge_ca = x509.load_pem_x509_certificate(tcp_api["ca_path"].read_bytes())
    assert hashlib.sha256(edge_ca.public_bytes(serialization.Encoding.DER)).hexdigest() == pin
    # The edge's view: through the passthrough, api presents a serverAuth certificate for
    # its name from the CA the edge pinned at enrolment.
    raw = socket.create_connection(("127.0.0.1", tcp_api["proxy"].port))
    with ctx_for(victim).wrap_socket(raw, server_hostname=NAME) as tls:
        served = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
    served.verify_directly_issued_by(edge_ca)
    eku = served.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]

    # The victim itself gets in (the positive control: pinning the edge CA works).
    ok = _heartbeat(tcp_api["url"], victim.edge_id, ctx_for(victim))
    assert ok.status_code == 200, ok.text

    forged = {"X-Forwarded-Client-Cert": xfcc(victim.cert_pem)}
    # Another edge's key, with the victim's certificate in the header: refused, and not
    # even as itself.
    as_victim = _heartbeat(tcp_api["url"], victim.edge_id, ctx_for(attacker), forged)
    assert as_victim.status_code == 401, as_victim.text
    assert as_victim.json()["code"] == "certificate_invalid"
    as_itself = _heartbeat(tcp_api["url"], attacker.edge_id, ctx_for(attacker), forged)
    assert as_itself.status_code == 401
    # No client certificate at all: the handshake fails, nothing reaches the app.
    with pytest.raises(httpx.HTTPError):
        _heartbeat(tcp_api["url"], victim.edge_id, ctx_for(None), forged)
    assert tcp_api["proxy"].connections >= 5


def test_api_serves_health_on_loopback_beside_its_tls_listener(tcp_api) -> None:
    resp = httpx.get(tcp_api["health"], timeout=5)
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    # The TLS port speaks only TLS.
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{tcp_api['api_port']}/healthz", timeout=5)


# --- renewal of api's server certificate --------------------------------------------------


def _material(sha: bytes, renew: datetime, until: datetime) -> ServerCertificate:
    return ServerCertificate("chain", b"key", until, renew, sha)


def test_the_server_certificate_is_renewed_and_swapped_in(monkeypatch) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    first = _material(b"1", now + timedelta(days=20), now + timedelta(days=30))
    second = _material(b"2", now + timedelta(days=40), now + timedelta(days=50))
    loaded: list[str] = []
    monkeypatch.setattr(api_main, "load_chain_in_memory", lambda ctx, c, k: loaded.append(c))
    issued = iter([RuntimeError("enrol down"), second])
    slept: list[float] = []

    class Certs:
        async def issue(self):
            item = next(issued)
            if isinstance(item, Exception):
                raise item
            return item

    clock = [now]

    async def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += timedelta(seconds=s)
        if len(slept) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            api_main.keep_renewed(None, Certs(), first, sleep=sleep, now=lambda: clock[0])  # type: ignore[arg-type]
        )
    assert slept[0] == pytest.approx(timedelta(days=20).total_seconds())
    assert slept[1] == pytest.approx(api_main.RENEW_RETRY_S)  # after the failure
    assert loaded == ["chain"]  # the new one, after the retry


def test_the_first_certificate_is_awaited_with_backoff() -> None:
    attempts = iter(
        [RuntimeError("starting"), ValueError("bad"), _material(b"x", *[datetime.now(UTC)] * 2)]
    )
    slept: list[float] = []

    class Certs:
        async def issue(self):
            item = next(attempts)
            if isinstance(item, Exception):
                raise item
            return item

    async def sleep(s: float) -> None:
        slept.append(s)

    got = asyncio.run(api_main.first_certificate(Certs(), sleep=sleep))  # type: ignore[arg-type]
    assert got.sha256 == b"x" and slept == [1.0, 2.0]


def test_api_waits_without_the_ca_certificate(monkeypatch) -> None:
    from starlette.testclient import TestClient

    monkeypatch.delenv("OMNIFILE_CA_CERT", raising=False)
    mode, app = api_main.build()
    assert mode == "waiting"
    with TestClient(app) as c:
        health = c.get("/healthz")
        assert health.status_code == 503 and health.json()["status"] == "waiting"
        assert c.post("/v0/edges/1/heartbeat", json={}).status_code == 503


def test_a_server_certificate_not_from_the_edge_ca_is_not_presented(ca, db_url) -> None:
    """api checks what enrol returns before serving it."""
    other = FileCASigner.generate("Not the edge CA")
    services = EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=1),
        signer=other,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        api_server_names=(NAME,),
    )
    app = create_enrol_app(services)

    async def go():
        async with app.router.lifespan_context(app):
            http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://e")
            certs = EnrolServerCertificates(
                "http://e/internal/v0/sign", StaticToken(SIGN_TOKEN), ca.ca_certificate, http=http
            )
            return await certs.issue()

    with pytest.raises(ValueError, match="edge CA"):
        asyncio.run(go())
    assert cert_sha256(other.ca_certificate) != cert_sha256(ca.ca_certificate)
