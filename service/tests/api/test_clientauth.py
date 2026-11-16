"""Client authentication: X-Forwarded-Client-Cert behind ingress, and direct TLS."""

from __future__ import annotations

import base64
import ipaddress
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection

from omnifile_service.api.__main__ import dev_token, forwarded_source
from omnifile_service.api.app import create_app
from omnifile_service.api.clientauth import (
    DirectTls,
    EdgeAuthenticator,
    ForwardedClientCert,
    parse_xfcc,
    verify_client_certificate,
)
from omnifile_service.api.context import ApiSettings, Services
from omnifile_service.api.dataplane import DataPlanes
from omnifile_service.api.db import Catalog
from omnifile_service.api.problems import ApiError
from omnifile_service.api.tlsproto import ClientCertH11Protocol
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.ca import FileCASigner

from .conftest import E, EdgeClient, xfcc


def conn(headers: dict[str, str], client: str = "10.0.0.5", scope_extra: dict | None = None):
    scope = {
        "type": "http",
        "headers": Headers(headers).raw,
        "client": (client, 1234),
        **(scope_extra or {}),
    }
    return HTTPConnection(scope)


def leaf(ca: FileCASigner, *, eku=ExtendedKeyUsageOID.CLIENT_AUTH, days: int = 30, start=None):
    key = ec.generate_private_key(ec.SECP256R1())
    now = start or datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")]))
        .issuer_name(ca.ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .sign(ca.private_key, hashes.SHA256())
    )
    return key, cert


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_xfcc_spellings(ca: FileCASigner) -> None:
    _, cert = leaf(ca)
    text = pem(cert)
    src = ForwardedClientCert(trust_ingress=True, trusted_proxies=("10.0.0.0/16",))
    der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    for value in (
        xfcc(text),  # URL-encoded PEM (Envoy)
        xfcc(text, encode="flat"),  # PEM with the line breaks removed
        f'Hash={xfcc(text).split(";")[0][5:]};Cert="{der_b64}"',  # bare base64 DER
        'Cert="' + text.replace("\n", "\\n") + '";Chain=""',  # literal \n escapes, no Hash
    ):
        got = src.certificate(conn({"x-forwarded-client-cert": value}))
        assert got == cert, value


def test_xfcc_refusals(ca: FileCASigner) -> None:
    _, cert = leaf(ca)
    good = xfcc(pem(cert))
    trusted = ForwardedClientCert(trust_ingress=True, trusted_proxies=("10.0.0.0/16",))
    cases = [
        (
            ForwardedClientCert(trust_ingress=False, trusted_proxies=("10.0.0.0/16",)),
            {"x-forwarded-client-cert": good},
        ),
        (trusted, {}),
        (trusted, {"x-forwarded-client-cert": "Hash=" + "0" * 64 + good[good.index(";") :]}),
        (trusted, {"x-forwarded-client-cert": good + "," + good}),
        (trusted, {"x-forwarded-client-cert": 'Cert="nope"'}),
        (
            ForwardedClientCert(trust_ingress=True, trusted_proxies=("100.64.0.0/16",)),
            {"x-forwarded-client-cert": good},
        ),
    ]
    for src, headers in cases:
        with pytest.raises(ApiError) as e:
            src.certificate(conn(headers))
        assert e.value.code in ("certificate_invalid", "certificate_required"), headers
    ok = ForwardedClientCert(trust_ingress=True, trusted_proxies=("10.0.0.0/16",))
    assert ok.certificate(conn({"x-forwarded-client-cert": good})) == cert


def test_forwarded_mode_needs_trusted_proxies(ca: FileCASigner) -> None:
    """Without the ingress's addresses anyone who reaches the app directly
    could send their own header: refuse to build that, and refuse to start."""
    with pytest.raises(ValueError):
        ForwardedClientCert(trust_ingress=True, trusted_proxies=())
    for trust, proxies in (("true", None), ("true", " , "), (None, "10.0.0.0/16"), ("true", "x")):
        with pytest.raises(SystemExit):
            forwarded_source(trust, proxies)
    src = forwarded_source("true", "100.100.0.0/24, 10.1.0.0/16", "10.60.0.0/26")
    _, cert = leaf(ca)
    header = {"x-forwarded-client-cert": xfcc(pem(cert))}
    assert src.certificate(conn(header, client="10.1.2.3")) == cert
    for peer in ("10.2.0.1", "testclient", ""):
        with pytest.raises(ApiError):
            src.certificate(conn(header, client=peer))


def test_dev_token_needs_dev_mode() -> None:
    assert dev_token(None, None) is None
    assert dev_token("t" * 20, "1") == "t" * 20
    for mode in (None, "0", "true"):
        with pytest.raises(SystemExit):
            dev_token("t" * 20, mode)


def test_parse_xfcc_quoted_separators() -> None:
    fields = parse_xfcc('Hash=ab;Cert="a;b,c";Chain="";')
    assert fields == {"hash": "ab", "cert": "a;b,c", "chain": ""}


def test_direct_tls_refuses_a_forwarded_header(ca: FileCASigner) -> None:
    _, cert = leaf(ca)
    with pytest.raises(ApiError):
        DirectTls().certificate(conn({"x-forwarded-client-cert": xfcc(pem(cert))}))
    with pytest.raises(ApiError) as e:
        DirectTls().certificate(conn({}))
    assert e.value.code == "certificate_required"
    tls = {"extensions": {"tls": {"client_cert_chain": [pem(cert)]}}}
    assert DirectTls().certificate(conn({}, scope_extra=tls)) == cert


def test_certificate_checks(ca: FileCASigner) -> None:
    other = FileCASigner.generate("Some other CA")
    _, foreign = leaf(other)
    _, server_only = leaf(ca, eku=ExtendedKeyUsageOID.SERVER_AUTH)
    _, expired = leaf(ca, days=1, start=datetime.now(UTC) - timedelta(days=3))
    for cert in (foreign, server_only, expired, ca.ca_certificate):
        with pytest.raises(ApiError) as e:
            verify_client_certificate(cert, ca.ca_certificate)
        assert e.value.code == "certificate_invalid"
    # Same subject name as our CA, different key: the signature check catches it.
    impostor = FileCASigner.generate("OmniFile test CA")
    _, forged = leaf(impostor)
    with pytest.raises(ApiError):
        verify_client_certificate(forged, ca.ca_certificate)


def test_unknown_or_foreign_certificate_over_the_api(make_world, ca) -> None:
    w = make_world()
    other = FileCASigner.generate("Some other CA")
    key, cert = leaf(other)
    stranger = EdgeClient(w.edge.client, w.edge.edge_id, key, pem(cert))
    resp = stranger.post(f"{E}/heartbeat", {"version": "0.1.0"})
    assert resp.status_code == 401 and resp.json()["code"] == "certificate_invalid"
    # Signed by our CA but never enrolled.
    key, cert = leaf(ca)
    unknown = EdgeClient(w.edge.client, w.edge.edge_id, key, pem(cert))
    resp = unknown.post(f"{E}/heartbeat", {"version": "0.1.0"})
    assert resp.status_code == 401
    bare = w.edge.client.post(f"/v0/edges/{w.edge.edge_id}/heartbeat", json={"version": "1.0.0"})
    assert bare.status_code == 401 and bare.json()["code"] == "certificate_required"


# --- direct TLS with a real uvicorn ---------------------------------------------


def server_cert(ca: FileCASigner, tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.test")]))
        .issuer_name(ca.ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca.private_key, hashes.SHA256())
    )
    cp, kp = tmp / "server.pem", tmp / "server.key"
    cp.write_text(pem(cert))
    kp.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cp, kp


def test_direct_tls_end_to_end(enrol_edge, ca: FileCASigner, db_url: str, tmp_path: Path) -> None:
    edge = enrol_edge()
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text(pem(ca.ca_certificate))
    cert_path, key_path = server_cert(ca, tmp_path)
    catalog = Catalog(as_app(db_url, "api"), max_size=2)
    services = Services(
        catalog=catalog,
        auth=EdgeAuthenticator(DirectTls(), ca.ca_certificate, catalog),
        planes=DataPlanes(),
        signer=None,
        settings=ApiSettings(),
    )
    config = uvicorn.Config(
        create_app(services),
        host="127.0.0.1",
        port=0,
        http=ClientCertH11Protocol,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        ssl_ca_certs=str(ca_path),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        client_cert = tmp_path / "edge.pem"
        client_key = tmp_path / "edge.key"
        client_cert.write_text(edge.cert_pem)
        client_key.write_bytes(
            edge.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        ctx = ssl.create_default_context(cafile=str(ca_path))
        ctx.load_cert_chain(str(client_cert), str(client_key))
        url = f"https://127.0.0.1:{port}/v0/edges/{edge.edge_id}/heartbeat"
        headers = {"Idempotency-Key": "direct-tls-" + "k" * 10}
        with httpx.Client(verify=ctx) as c:
            resp = c.post(url, json={"version": "0.1.0"}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert "server_time" in resp.json()

        # The forwarded header is refused when nothing forwards it.
        with httpx.Client(verify=ctx) as c:
            resp = c.post(
                url,
                json={"version": "0.1.0"},
                headers={
                    "Idempotency-Key": "direct-tls-" + "j" * 10,
                    "X-Forwarded-Client-Cert": xfcc(edge.cert_pem),
                },
            )
        assert resp.status_code == 401

        # No client certificate: the TLS handshake itself fails.
        plain = ssl.create_default_context(cafile=str(ca_path))
        with pytest.raises(httpx.HTTPError), httpx.Client(verify=plain) as c:
            c.post(url, json={"version": "0.1.0"}, headers=headers)
    finally:
        server.should_exit = True
        thread.join(10)
