"""Enrolment, the internal signing endpoint, and renewal through api (section 2)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.testclient import TestClient

from omnifile_service.api.clientauth import parse_networks
from omnifile_service.api.db import Catalog
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.app import EnrolServices, FailureLimiter, create_enrol_app
from omnifile_service.enrol.callers import SharedTokenVerifier

from . import spec
from .conftest import (
    SIGN_TOKEN,
    EdgeClient,
    csr_pem,
    enrol_body,
    idem,
    new_key,
    pending_edge,
    renewal_body,
)

E = "/v0/edges/{edge_id}"


def check(resp, method: str, template: str) -> None:
    spec.check_response(
        method, template, resp.status_code, resp.headers.get("content-type", ""), resp.content
    )


def test_enrol_issues_the_documented_certificate(enrol_client, sql, ca) -> None:
    edge_id, token = pending_edge(sql)
    key = new_key()
    body = enrol_body(token, key)
    spec.check_request("POST", "/v0/enrol", body)
    resp = enrol_client.post("/v0/enrol", json=body, headers=idem())
    check(resp, "POST", "/v0/enrol")
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["edge_id"] == str(edge_id)
    assert out["api_url"] == "https://api.test"
    assert out["chain_pem"] == ""
    cert = x509.load_pem_x509_certificate(out["cert_pem"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        f"omnifile-edge-{edge_id}"
    )
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
        f"urn:omnifile:edge:{edge_id}"
    ]
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    life = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert timedelta(days=30) <= life <= timedelta(days=30, minutes=6)
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert cert.issuer == ca.ca_certificate.subject
    # The CSR's subject is ignored.
    assert "whatever" not in cert.subject.rfc4514_string()

    row = sql.execute(
        "SELECT state, cert_sha256, enrolment_token_sha256, software_version"
        " FROM catalog.edge WHERE edge_id = %s",
        (edge_id,),
    ).fetchone()
    der = cert.public_bytes(serialization.Encoding.DER)
    assert row == ("active", hashlib.sha256(der).digest(), None, "0.1.0")
    audit = sql.execute(
        "SELECT decision FROM catalog.audit_log WHERE action = 'edge.enrol'"
        " AND details->>'edge_id' = %s",
        (str(edge_id),),
    ).fetchall()
    assert audit == [("allow",)]


def test_a_token_works_once_but_a_lost_response_can_be_replayed(enrol_client, sql) -> None:
    _, token = pending_edge(sql)
    body = enrol_body(token, new_key())
    key = idem()
    first = enrol_client.post("/v0/enrol", json=body, headers=key)
    assert first.status_code == 201
    again = enrol_client.post("/v0/enrol", json=body, headers=key)
    assert again.status_code == 201
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    other = enrol_client.post("/v0/enrol", json=enrol_body(token, new_key()), headers=idem())
    check(other, "POST", "/v0/enrol")
    assert other.status_code == 401
    assert other.json()["code"] == "enrolment_token_invalid"


def test_expired_token_and_bad_csr_are_refused(enrol_client, sql) -> None:
    _, token = pending_edge(sql, expires="-1 minute")
    resp = enrol_client.post("/v0/enrol", json=enrol_body(token, new_key()), headers=idem())
    assert resp.status_code == 401
    _, token = pending_edge(sql)
    body = enrol_body(token, new_key())
    body["csr_pem"] = "-----BEGIN CERTIFICATE REQUEST-----\nnope\n-----END CERTIFICATE REQUEST-----"
    resp = enrol_client.post("/v0/enrol", json=body, headers=idem())
    check(resp, "POST", "/v0/enrol")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_request"


def test_rsa_csr_is_refused(enrol_client, sql) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa

    _, token = pending_edge(sql)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    body = {"token": token, "csr_pem": csr, "facts": {"hostname": "h", "version": "1.0.0"}}
    resp = enrol_client.post("/v0/enrol", json=body, headers=idem())
    assert resp.status_code == 400
    assert "P-256" in resp.json()["detail"]


def test_failed_attempts_are_rate_limited_and_audited(enrol_client, sql) -> None:
    for _ in range(5):
        bogus = enrol_body("z" * 43, new_key())
        assert enrol_client.post("/v0/enrol", json=bogus, headers=idem()).status_code == 401
    resp = enrol_client.post("/v0/enrol", json=enrol_body("z" * 43, new_key()), headers=idem())
    check(resp, "POST", "/v0/enrol")
    assert resp.status_code == 429
    assert resp.json()["code"] == "throttled"
    assert resp.headers["Retry-After"] == "60"
    denies = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'edge.enrol' AND decision = 'deny'"
        " AND at > now() - interval '1 minute'"
    ).fetchone()[0]
    assert denies >= 5


def enrol_behind_ingress(db_url, ca, **kw) -> EnrolServices:
    return EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=2),
        signer=ca,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        limiter=FailureLimiter(limit=3),
        trusted_ingress=parse_networks(["100.100.0.0/24"]),
        **kw,
    )


def with_peer(app):
    """Sets the ASGI peer address from the test-only `x-test-peer` header, so
    one client (one pool) can play several TCP peers."""

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            for k, v in scope["headers"]:
                if k == b"x-test-peer":
                    scope = {**scope, "client": (v.decode(), 1234)}
        await app(scope, receive, send)

    return wrapped


def test_rate_limit_keys_on_forwarded_for_only_behind_the_ingress(db_url, ca, sql) -> None:
    svc = enrol_behind_ingress(db_url, ca)
    bad_token = enrol_body("z" * 43, new_key())
    client = TestClient(with_peer(create_enrol_app(svc)))

    def post(peer: str, xff: str | None, body=bad_token) -> int:
        h = idem() | {"x-test-peer": peer} | ({"X-Forwarded-For": xff} if xff else {})
        return client.post("/v0/enrol", json=body, headers=h).status_code

    with client:
        check_limits(post, bad_token)


def check_limits(post, bad_token) -> None:
    # Through the ingress: each client address has its own budget, and the
    # address is the one the ingress appended (the last), not the client's claim.
    for _ in range(3):
        assert post("100.100.0.7", "1.2.3.4, 198.51.100.1") == 401
    assert post("100.100.0.7", "9.9.9.9, 198.51.100.1") == 429
    assert post("100.100.0.8", "198.51.100.2") == 401
    # Not from the ingress: X-Forwarded-For is ignored, so a scanner can't
    # rotate it to dodge the limit.
    for i in range(3):
        assert post("203.0.113.5", f"192.0.2.{i}") == 401
    assert post("203.0.113.5", "192.0.2.99") == 429
    # Malformed requests can't guess a token, so they don't use up the budget.
    short = {**bad_token, "token": "short"}
    for _ in range(5):
        assert post("203.0.113.6", None, short) == 400
    assert post("203.0.113.6", None) == 401


def test_sign_only_from_the_internal_network(db_url, ca, enrol_edge) -> None:
    edge = enrol_edge()
    svc = enrol_behind_ingress(db_url, ca, sign_sources=parse_networks(["10.20.0.0/23"]))
    body = {
        "edge_id": str(edge.edge_id),
        "csr_pem": csr_pem(new_key()),
        "purpose": "renewal",
        "current_cert_sha256": "ab" * 32,
        "request_id": "r" * 20,
    }
    auth = {"Authorization": f"Bearer {SIGN_TOKEN}"}

    def code(c: TestClient, peer: str, xff: str | None = None) -> str:
        h = auth | {"x-test-peer": peer} | ({"X-Forwarded-For": xff} if xff else {})
        return c.post("/internal/v0/sign", json=body, headers=h).json()["code"]

    with TestClient(with_peer(create_enrol_app(svc))) as c:
        # Outside api's subnet, a valid token is not enough.
        assert code(c, "100.100.0.7", "10.1.2.3") == "caller_not_allowed"  # an edge via ingress
        assert code(c, "192.0.2.1") == "caller_not_allowed"
        # A spoofed X-Forwarded-For from outside the ingress doesn't help.
        assert code(c, "192.0.2.1", "10.20.1.9") == "caller_not_allowed"
        # From api's subnet (directly, or through the ingress) the call gets
        # past the check; the fingerprint, wrong here, is then refused.
        assert code(c, "10.20.0.5") == "certificate_invalid"
        assert code(c, "100.100.0.7", "10.20.1.9") == "certificate_invalid"


def test_sign_needs_the_api_identity(enrol_client, sql) -> None:
    body = {
        "edge_id": "1",
        "csr_pem": csr_pem(new_key()),
        "purpose": "renewal",
        "current_cert_sha256": "0" * 64,
        "request_id": "r" * 20,
    }
    spec.check_request("POST", "/internal/v0/sign", body)
    for headers in ({}, {"Authorization": "Bearer wrong-token-wrong-token"}):
        resp = enrol_client.post("/internal/v0/sign", json=body, headers=headers)
        check(resp, "POST", "/internal/v0/sign")
        assert resp.status_code == 403
        assert resp.json()["code"] == "caller_not_allowed"


def test_sign_refuses_a_certificate_that_is_not_the_edges(enrol_client, enrol_edge) -> None:
    edge = enrol_edge()
    body = {
        "edge_id": str(edge.edge_id),
        "csr_pem": csr_pem(new_key()),
        "purpose": "renewal",
        "current_cert_sha256": "ab" * 32,
        "request_id": "r" * 20,
    }
    resp = enrol_client.post(
        "/internal/v0/sign", json=body, headers={"Authorization": f"Bearer {SIGN_TOKEN}"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "certificate_invalid"
    body["extra"] = "x"
    resp = enrol_client.post(
        "/internal/v0/sign", json=body, headers={"Authorization": f"Bearer {SIGN_TOKEN}"}
    )
    assert resp.status_code == 400


def test_renewal_through_api_keeps_the_old_certificate_working(enrol_edge, sql) -> None:
    edge: EdgeClient = enrol_edge()
    old_pem = edge.cert_pem
    new_key_ = new_key()
    body = renewal_body(edge, csr_pem(new_key_))
    key = "renew-" + "k" * 20
    resp = edge.post(f"{E}/certificate", body, key=key)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["edge_id"] == str(edge.edge_id)
    new = x509.load_pem_x509_certificate(out["cert_pem"].encode())
    assert new.public_key().public_numbers() == new_key_.public_key().public_numbers()

    # A lost response: the same key returns the same certificate.
    again = edge.post(f"{E}/certificate", body, key=key)
    assert again.json()["cert_pem"] == out["cert_pem"]

    row = sql.execute(
        "SELECT cert_sha256, prev_cert_sha256 FROM catalog.edge WHERE edge_id = %s",
        (edge.edge_id,),
    ).fetchone()
    old = x509.load_pem_x509_certificate(old_pem.encode())
    assert row[1] == hashlib.sha256(old.public_bytes(serialization.Encoding.DER)).digest()
    assert row[0] == hashlib.sha256(new.public_bytes(serialization.Encoding.DER)).digest()

    # Both certificates authenticate until the old one expires.
    hb = {"version": "0.1.0"}
    assert edge.post(f"{E}/heartbeat", hb).status_code == 200
    renewed = EdgeClient(edge.client, edge.edge_id, new_key_, out["cert_pem"])
    assert renewed.post(f"{E}/heartbeat", hb).status_code == 200

    # A second renewal with the new certificate retires the first one.
    resp = renewed.post(f"{E}/certificate", renewal_body(renewed, csr_pem(new_key())))
    assert resp.status_code == 201
    stale = edge.post(f"{E}/heartbeat", hb)
    assert stale.status_code == 401
    assert stale.json()["code"] == "certificate_invalid"
    audit = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'edge.certificate.sign'"
        " AND details->>'edge_id' = %s",
        (str(edge.edge_id),),
    ).fetchone()[0]
    assert audit == 2


def test_renewal_needs_proof_of_possession_of_the_current_key(enrol_edge, sql) -> None:
    """The forwarded certificate is public: renewing needs a signature by its
    private key over the CSR and a recent time (control-api.md section 2)."""
    edge: EdgeClient = enrol_edge()
    csr = csr_pem(new_key())

    def refused(body, code: str) -> None:
        resp = edge.post(f"{E}/certificate", body, check=False)
        assert resp.status_code in (400, 401), resp.text
        assert resp.json()["code"] == code, resp.text

    refused({"csr_pem": csr}, "invalid_request")  # no proof at all
    # Signed by another key (someone holding only the certificate).
    thief = EdgeClient(edge.client, edge.edge_id, new_key(), edge.cert_pem)
    refused(renewal_body(thief, csr), "certificate_invalid")
    # Signed over another CSR.
    other = renewal_body(edge, csr_pem(new_key()))
    refused({"csr_pem": csr, "proof": other["proof"]}, "certificate_invalid")
    # Too old, or from the future.
    for at in ("2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"):
        refused(renewal_body(edge, csr, signed_at=at), "certificate_invalid")
    garbled = renewal_body(edge, csr)
    garbled["proof"]["signature"] = "not base64!"
    refused(garbled, "invalid_request")
    # Nothing was signed.
    signs = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'edge.certificate.sign'"
        " AND details->>'edge_id' = %s",
        (str(edge.edge_id),),
    ).fetchone()[0]
    assert signs == 0
    assert edge.post(f"{E}/certificate", renewal_body(edge, csr)).status_code == 201


def test_revoked_edge_is_refused(enrol_edge, sql) -> None:
    edge = enrol_edge()
    sql.execute(
        "UPDATE catalog.edge SET state = 'revoked', revoked_at = now() WHERE edge_id = %s",
        (edge.edge_id,),
    )
    resp = edge.post(f"{E}/heartbeat", {"version": "0.1.0"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "edge_revoked"
