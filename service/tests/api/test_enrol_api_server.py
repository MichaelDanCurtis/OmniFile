"""enrol's part in T5 and the deployment: api's server certificate, the pin in the
enrolment answer, the relay's certificate, waiting for the CA, and the Key Vault
signer opened in the loop that serves."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.testclient import TestClient

from omnifile_service.api.clientauth import (
    RelayAuthenticator,
    check_server_certificate,
    verify_client_certificate,
)
from omnifile_service.api.db import Catalog
from omnifile_service.api.problems import ApiError
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol import __main__ as enrol_main
from omnifile_service.enrol.app import EnrolServices, FailureLimiter, create_enrol_app
from omnifile_service.enrol.ca import cert_sha256, issue_edge_certificate
from omnifile_service.enrol.callers import SharedTokenVerifier
from omnifile_service.enrol.kvsigner import KeyVaultCASigner, KeyVaultKey, self_signed_ca

from .conftest import SIGN_TOKEN, csr_pem, enrol_body, idem, new_key, pending_edge
from .test_clientauth import conn
from .test_kvsigner import VAULT, FakeVault

NAMES = ("omni-api.example.internal",)
AUTH = {"Authorization": f"Bearer {SIGN_TOKEN}"}


def enrol_with(db_url: str, signer, **kw) -> TestClient:
    services = EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=2),
        signer=signer,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        api_url="https://omni-api.example.internal:8443",
        limiter=FailureLimiter(limit=5),
        **kw,
    )
    return TestClient(create_enrol_app(services), base_url="https://enrol.test")


def api_server_body(key=None, **extra) -> dict:
    body = {
        "csr_pem": csr_pem(key or new_key(), cn="ignored.example.com"),
        "purpose": "api_server",
        "request_id": f"api-server-{uuid.uuid4().hex}",
    }
    body.update(extra)
    return body


def test_api_server_certificate_is_for_the_configured_names_only(db_url, ca, sql) -> None:
    key = new_key()
    with enrol_with(db_url, ca, api_server_names=NAMES) as c:
        resp = c.post("/internal/v0/sign", json=api_server_body(key), headers=AUTH)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    cert = x509.load_pem_x509_certificate(out["cert_pem"].encode())
    # The names come from enrol's settings, never the CSR.
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == NAMES[0]
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == list(NAMES)
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == []
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    check_server_certificate(cert, ca.ca_certificate, key.public_key())
    assert out["ca_pem"] == ca.ca_certificate.public_bytes(serialization.Encoding.PEM).decode()
    assert out["renew_after"] < out["not_after"]
    # It can never act as a client: no edge, no relay.
    with pytest.raises(ApiError, match="clientAuth"):
        verify_client_certificate(cert, ca.ca_certificate)
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'api.server_certificate.sign'"
        " AND decision = 'allow' AND details->>'cert_sha256' = %s",
        (cert_sha256(cert).hex(),),
    ).fetchone()
    assert n == 1


def test_an_edge_certificate_is_never_a_server_certificate(ca) -> None:
    key = new_key()
    issued = asyncio.run(issue_edge_certificate(ca, 7, csr_pem(key)))
    with pytest.raises(ValueError, match="serverAuth"):
        check_server_certificate(issued.cert, ca.ca_certificate, key.public_key())


def test_api_server_requests_are_idempotent(db_url, ca) -> None:
    body = api_server_body()
    with enrol_with(db_url, ca, api_server_names=NAMES) as c:
        first = c.post("/internal/v0/sign", json=body, headers=AUTH)
        again = c.post("/internal/v0/sign", json=body, headers=AUTH)
    assert first.status_code == again.status_code == 201
    assert first.json()["cert_pem"] == again.json()["cert_pem"]


@pytest.mark.parametrize(
    ("names", "headers", "body", "status", "code"),
    [
        ((), AUTH, {}, 403, "caller_not_allowed"),  # enrol issues none (HTTP ingress)
        (NAMES, {"Authorization": "Bearer nope"}, {}, 403, "caller_not_allowed"),
        (NAMES, {}, {}, 403, "caller_not_allowed"),
        (NAMES, AUTH, {"edge_id": "1"}, 400, "invalid_request"),  # names no edge
        (NAMES, AUTH, {"current_cert_sha256": "a" * 64}, 400, "invalid_request"),
        (NAMES, AUTH, {"request_id": "short"}, 400, "invalid_request"),
    ],
)
def test_api_server_refusals(db_url, ca, names, headers, body, status, code) -> None:
    with enrol_with(db_url, ca, api_server_names=names) as c:
        resp = c.post("/internal/v0/sign", json=api_server_body(**body), headers=headers)
    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code


def test_api_server_only_from_the_internal_network(db_url, ca) -> None:
    from omnifile_service.api.clientauth import parse_networks

    services_kw = {"api_server_names": NAMES, "sign_sources": parse_networks(["10.60.0.0/26"])}
    with enrol_with(db_url, ca, **services_kw) as c:
        resp = c.post("/internal/v0/sign", json=api_server_body(), headers=AUTH)
    assert resp.status_code == 403  # the TestClient's peer is "testclient", outside it


def test_an_rsa_or_unsigned_csr_is_refused(db_url, ca) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(rsa_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    with enrol_with(db_url, ca, api_server_names=NAMES) as c:
        resp = c.post("/internal/v0/sign", json=api_server_body(csr_pem=rsa_csr), headers=AUTH)
    assert resp.status_code == 400


# --- the pin in the enrolment answer ---------------------------------------------


@pytest.mark.parametrize("names", [NAMES, ()])
def test_the_enrolment_answer_pins_the_edge_ca_for_api(db_url, ca, sql, names) -> None:
    edge_id, token = pending_edge(sql)
    with enrol_with(db_url, ca, api_server_names=names) as c:
        resp = c.post("/v0/enrol", json=enrol_body(token, new_key()), headers=idem())
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["api_url"] == "https://omni-api.example.internal:8443"
    if names:
        assert out["api_server_ca_sha256"] == cert_sha256(ca.ca_certificate).hex()
        assert (
            out["api_server_ca_sha256"]
            == hashlib.sha256(
                x509.load_pem_x509_certificate(out["ca_pem"].encode()).public_bytes(
                    serialization.Encoding.DER
                )
            ).hexdigest()
        )
    else:
        # HTTP ingress: api presents the ingress's certificate, the one edges pinned for enrol.
        assert "api_server_ca_sha256" not in out


def test_server_names_are_checked(monkeypatch) -> None:
    assert enrol_main.api_server_names("Omni-API.example.internal.,b.example") == (
        "omni-api.example.internal",
        "b.example",
    )
    for bad in ("*.example.com", "10.0.0.1", "https://x.example", "single", "a..b"):
        with pytest.raises(SystemExit):
            enrol_main.api_server_names(bad)


# --- the relay's certificate ------------------------------------------------------


def test_issue_relay_cert(db_url, ca, sql, tmp_path, monkeypatch, capsys) -> None:
    key_path, cert_path = tmp_path / "ca.key", tmp_path / "ca.pem"
    ca.write(key_path, cert_path)
    monkeypatch.setenv("OMNIFILE_DEV_MODE", "1")
    monkeypatch.setenv("OMNIFILE_DEV_CA_KEY", str(key_path))
    monkeypatch.setenv("OMNIFILE_DEV_CA_CERT", str(cert_path))
    monkeypatch.setenv("OMNIFILE_DATABASE_URL", db_url)
    relay_key = new_key()
    b64 = base64.b64encode(csr_pem(relay_key, cn="omnifile-relay").encode()).decode()
    enrol_main.main(["issue-relay-cert", "--csr-b64", b64])
    out = capsys.readouterr().out
    pem, _, line = out.rpartition("SHA-256: ")
    cert = x509.load_pem_x509_certificate(pem.encode())
    sha = line.strip()
    assert sha == cert_sha256(cert).hex()
    # api takes it on the intents path when pinned, and as nothing else.
    auth = RelayAuthenticator(_PresentedCert(cert), ca.ca_certificate, allowed=frozenset([sha]))
    assert auth.authenticate(conn({})).cert_sha256.hex() == sha
    unpinned = RelayAuthenticator(_PresentedCert(cert), ca.ca_certificate, allowed=frozenset())
    with pytest.raises(ApiError):
        unpinned.authenticate(conn({}))
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'relay.certificate.sign'"
        " AND details->>'cert_sha256' = %s",
        (sha,),
    ).fetchone()
    assert n == 1
    # Bad input ends the command with a reason.
    with pytest.raises(SystemExit, match="base64"):
        enrol_main.main(["issue-relay-cert", "--csr-b64", "not base64!"])
    with pytest.raises(SystemExit, match="days"):
        enrol_main.main(["issue-relay-cert", "--csr-b64", b64, "--days", "400"])


class _PresentedCert:
    def __init__(self, cert: x509.Certificate) -> None:
        self.cert = cert

    def certificate(self, conn):
        return self.cert


# --- before the CA exists ------------------------------------------------------------


def test_enrol_waits_for_the_ca_certificate(monkeypatch) -> None:
    for name in ("OMNIFILE_DEV_CA_KEY", "OMNIFILE_CA_CERT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIFILE_KEYVAULT_URI", VAULT)
    monkeypatch.setenv("OMNIFILE_CA_KEY_NAME", "omnifile-edge-ca")
    with TestClient(enrol_main.build()) as c:
        health = c.get("/healthz")
        enrol = c.post("/v0/enrol", json={})
    assert health.status_code == 503 and health.json()["status"] == "waiting"
    assert "init-kv-ca" in health.json()["reason"]
    assert enrol.status_code == 503 and enrol.json()["code"] == "unavailable"
    assert enrol.headers["retry-after"] == "60"


# --- the Key Vault signer belongs to the loop that serves -----------------------------


class LoopBoundTransport(httpx.AsyncBaseTransport):
    """A transport that, like a real pooled connection, works only in the event
    loop that first used it."""

    def __init__(self, vault: FakeVault) -> None:
        self.vault = vault
        self.loop: asyncio.AbstractEventLoop | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        loop = asyncio.get_running_loop()
        if self.loop is None:
            self.loop = loop
        elif self.loop is not loop:
            raise RuntimeError("Event loop is closed")
        await request.aread()
        return self.vault.handler(request)


def test_the_key_vault_signer_signs_in_the_serving_loop(db_url, sql, tmp_path, monkeypatch) -> None:
    """enrol opened its vault signer in a throwaway loop (asyncio.run) and then served in
    another, so its first sign failed with "Event loop is closed". kv_signer now opens it
    when enrol starts serving."""
    vault = FakeVault()

    async def bootstrap() -> x509.Certificate:
        async def token() -> str:
            return "mi-token"

        key = KeyVaultKey(VAULT, "omnifile-edge-ca", token, httpx.AsyncClient(
            transport=httpx.MockTransport(vault.handler)))  # fmt: skip
        return await self_signed_ca(key)

    ca_cert = asyncio.run(bootstrap())
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("OMNIFILE_KEYVAULT_URI", VAULT)
    monkeypatch.setenv("OMNIFILE_CA_KEY_NAME", "omnifile-edge-ca")
    monkeypatch.setenv("OMNIFILE_CA_CERT", str(ca_path))
    transport = LoopBoundTransport(vault)

    async def token() -> str:
        return "mi-token"

    signer = enrol_main.kv_signer(
        http_factory=lambda: httpx.AsyncClient(transport=transport), token_factory=lambda: token
    )
    edge_id, enrol_token = pending_edge(sql)
    with enrol_with(db_url, signer) as c:
        resp = c.post("/v0/enrol", json=enrol_body(enrol_token, new_key()), headers=idem())
    assert resp.status_code == 201, resp.text
    cert = x509.load_pem_x509_certificate(resp.json()["cert_pem"].encode())
    cert.verify_directly_issued_by(ca_cert)
    assert vault.signs == 2  # the CA bootstrap and this edge


def test_the_old_way_fails_against_a_loop_bound_vault(tmp_path) -> None:
    """The transport stands in for the real failure: open in one loop, sign in another."""
    vault = FakeVault()
    transport = LoopBoundTransport(vault)

    async def token() -> str:
        return "mi-token"

    def key() -> KeyVaultKey:
        return KeyVaultKey(VAULT, "omnifile-edge-ca", token, httpx.AsyncClient(transport=transport))

    async def make_ca():
        return await self_signed_ca(
            KeyVaultKey(
                VAULT,
                "omnifile-edge-ca",
                token,
                httpx.AsyncClient(transport=httpx.MockTransport(vault.handler)),
            )  # fmt: skip
        )

    ca_cert = asyncio.run(make_ca())
    signer = asyncio.run(KeyVaultCASigner.open(key(), ca_cert))
    with pytest.raises(Exception, match="Event loop is closed"):
        asyncio.run(issue_edge_certificate(signer, 1, csr_pem(new_key())))
