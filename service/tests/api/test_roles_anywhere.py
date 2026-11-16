"""The edge's IAM Roles Anywhere certificate from OmniFile's edge CA, and the
CA's CRL (decision 50; control-api.md section 2, threat model T4).

- `POST /v0/edges/{id}/aws-certificate`: proof of possession of both keys
  (the CSR's self-signature for the AWS key, a proof by the mTLS key for the
  request), the key pinned at the first certificate, a revoked edge refused,
  and the certificate's own profile.
- `POST /internal/v0/crl`: only the worker's identity; the revoked edges'
  unexpired certificates, signed by the CA.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.testclient import TestClient

from omnifile_service.api.clientauth import ROLES_ANYWHERE_PROOF_CONTEXT, renewal_proof_message
from omnifile_service.api.db import Catalog
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.app import EnrolServices, FailureLimiter, create_enrol_app
from omnifile_service.enrol.ca import FileCASigner
from omnifile_service.enrol.callers import SharedTokenVerifier

from .conftest import SIGN_TOKEN, EdgeClient, csr_pem, new_key, renewal_body

E = "/v0/edges/{edge_id}"
CRL_TOKEN = "dev-crl-token-" + "w" * 16


def ra_body(
    edge: EdgeClient,
    csr: str,
    *,
    signed_at: str | None = None,
    context: bytes = ROLES_ANYWHERE_PROOF_CONTEXT,
) -> dict:
    """`POST aws-certificate` with the proof signed by the edge's mTLS key."""
    at = signed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    der = x509.load_pem_x509_csr(csr.encode()).public_bytes(serialization.Encoding.DER)
    sig = edge.key.sign(
        renewal_proof_message(edge.edge_id, at, der, context), ec.ECDSA(hashes.SHA256())
    )
    return {"csr_pem": csr, "proof": {"signed_at": at, "signature": base64.b64encode(sig).decode()}}


def spki(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def csr_claiming(signer: ec.EllipticCurvePrivateKey, claimed: ec.EllipticCurvePrivateKey) -> str:
    """A CSR signed by `signer` that names `claimed`'s public key: what someone
    who doesn't hold `claimed` would send."""
    der = x509.load_pem_x509_csr(csr_pem(signer).encode()).public_bytes(serialization.Encoding.DER)
    mine, theirs = spki(signer), spki(claimed)
    assert len(mine) == len(theirs) and der.count(mine) == 1
    forged = der.replace(mine, theirs)
    b64 = base64.encodebytes(forged).decode()
    return f"-----BEGIN CERTIFICATE REQUEST-----\n{b64}-----END CERTIFICATE REQUEST-----\n"


def audits(sql, action: str, edge_id: int) -> list[dict]:
    rows = sql.execute(
        "SELECT decision, details FROM catalog.audit_log WHERE action = %s"
        " AND details->>'edge_id' = %s ORDER BY audit_id",
        (action, str(edge_id)),
    ).fetchall()
    return [{"decision": r[0], **r[1]} for r in rows]


def test_the_certificate_has_its_own_profile_for_the_aws_key(enrol_edge, sql, ca) -> None:
    edge: EdgeClient = enrol_edge()
    before = sql.execute(
        "SELECT cert_sha256 FROM catalog.edge WHERE edge_id = %s", (edge.edge_id,)
    ).fetchone()[0]
    aws_key = new_key()
    body = ra_body(edge, csr_pem(aws_key, cn="omnifile-edge-somehost"))
    key = "ra-" + uuid.uuid4().hex
    resp = edge.post(f"{E}/aws-certificate", body, key=key)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["edge_id"] == str(edge.edge_id)
    cert = x509.load_pem_x509_certificate(out["cert_pem"].encode())

    # For the AWS key, issued by the edge CA.
    assert cert.public_key().public_numbers() == aws_key.public_key().public_numbers()
    cert.verify_directly_issued_by(ca.ca_certificate)
    assert (
        out["ca_pem"].strip()
        == ca.ca_certificate.public_bytes(serialization.Encoding.PEM).decode().strip()
    )
    # Subject: OU=omnifile-roles-anywhere, CN=omnifile-edge-<id> (the CSR's subject is ignored).
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == (
        f"omnifile-edge-{edge.edge_id}"
    )
    assert cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value == (
        "omnifile-roles-anywhere"
    )
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
        f"urn:omnifile:edge:{edge.edge_id}"
    ]
    # What Roles Anywhere requires: not a CA, digitalSignature, SHA-256; and clientAuth.
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is False
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.digital_signature and not ku.key_cert_sign and not ku.crl_sign
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert isinstance(cert.signature_hash_algorithm, hashes.SHA256)
    # 30 days, like the mTLS certificate; renewed from day 20.
    life = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert timedelta(days=30) <= life <= timedelta(days=30, minutes=6)
    renew = datetime.fromisoformat(out["renew_after"].replace("Z", "+00:00"))
    assert abs(renew - (cert.not_valid_after_utc - timedelta(days=10))) < timedelta(seconds=2)

    # A lost response: the same key returns the same certificate.
    again = edge.post(f"{E}/aws-certificate", body, key=key)
    assert again.status_code == 201 and again.json()["cert_pem"] == out["cert_pem"]

    # Recorded and audited; the key is pinned; the mTLS certificate is untouched
    # and still works, and the Roles Anywhere certificate opens no control call.
    serial = format(cert.serial_number, "x")
    row = sql.execute(
        "SELECT edge_id, key_sha256, not_after FROM catalog.edge_ra_certificate WHERE serial = %s",
        (serial,),
    ).fetchone()
    assert row[0] == edge.edge_id and bytes(row[1]) == hashlib.sha256(spki(aws_key)).digest()
    pinned, after = sql.execute(
        "SELECT ra_key_sha256, cert_sha256 FROM catalog.edge WHERE edge_id = %s",
        (edge.edge_id,),
    ).fetchone()
    assert bytes(pinned) == hashlib.sha256(spki(aws_key)).digest()
    assert after == before
    [a] = audits(sql, "edge.roles_anywhere.sign", edge.edge_id)
    assert a["decision"] == "allow" and a["serial"] == serial
    assert a["subject"] == f"CN=omnifile-edge-{edge.edge_id},OU=omnifile-roles-anywhere"
    assert edge.post(f"{E}/heartbeat", {"version": "0.1.0"}).status_code == 200
    as_mtls = EdgeClient(edge.client, edge.edge_id, aws_key, out["cert_pem"])
    assert as_mtls.post(f"{E}/heartbeat", {"version": "0.1.0"}).status_code == 401

    # Renewal with the same CSR: a new certificate for the same key.
    renewed = edge.post(f"{E}/aws-certificate", ra_body(edge, body["csr_pem"]))
    assert renewed.status_code == 201
    second = x509.load_pem_x509_certificate(renewed.json()["cert_pem"].encode())
    assert second.serial_number != cert.serial_number
    assert second.public_key().public_numbers() == aws_key.public_key().public_numbers()


def test_proof_of_possession_of_both_keys(enrol_edge, sql) -> None:
    """The CSR's signature must be by the key it names (the AWS key), and the
    request must be signed by the presented certificate's key, under the
    Roles Anywhere context (a renewal proof doesn't do)."""
    edge: EdgeClient = enrol_edge()
    aws_key = new_key()
    csr = csr_pem(aws_key)

    def refused(body, code: str, status: int) -> None:
        resp = edge.post(f"{E}/aws-certificate", body, check=False)
        assert resp.status_code == status, resp.text
        assert resp.json()["code"] == code, resp.text

    refused({"csr_pem": csr}, "invalid_request", 400)
    # Someone holding only the edge's (public) certificate.
    thief = EdgeClient(edge.client, edge.edge_id, new_key(), edge.cert_pem)
    refused(ra_body(thief, csr), "certificate_invalid", 401)
    # A proof over another CSR, a renewal's proof, a stale one.
    other = ra_body(edge, csr_pem(new_key()))
    refused({"csr_pem": csr, "proof": other["proof"]}, "certificate_invalid", 401)
    refused(renewal_body(edge, csr), "certificate_invalid", 401)
    refused(ra_body(edge, csr, signed_at="2026-01-01T00:00:00Z"), "certificate_invalid", 401)
    # A CSR naming a key its signer doesn't hold (wrong key).
    refused(ra_body(edge, csr_claiming(new_key(), aws_key)), "invalid_request", 400)
    # The mTLS key itself: the AWS key must be a key of its own.
    refused(ra_body(edge, csr_pem(edge.key)), "invalid_request", 400)
    # An unexpected field.
    extra = ra_body(edge, csr)
    extra["purpose"] = "renewal"
    refused(extra, "invalid_request", 400)
    assert audits(sql, "edge.roles_anywhere.sign", edge.edge_id) == []
    assert edge.post(f"{E}/aws-certificate", ra_body(edge, csr)).status_code == 201


def test_the_aws_key_is_pinned(enrol_edge, sql) -> None:
    edge: EdgeClient = enrol_edge()
    first = new_key()
    assert edge.post(f"{E}/aws-certificate", ra_body(edge, csr_pem(first))).status_code == 201
    resp = edge.post(f"{E}/aws-certificate", ra_body(edge, csr_pem(new_key())), check=False)
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "aws_key_mismatch"
    denied = [
        a for a in audits(sql, "edge.roles_anywhere.sign", edge.edge_id) if a["decision"] == "deny"
    ]
    assert len(denied) == 1 and denied[0]["reason"] == "key"
    # The pinned key still works; a renewed mTLS certificate changes nothing.
    renewed = edge.post(f"{E}/certificate", renewal_body(edge, csr_pem(next_key := new_key())))
    assert renewed.status_code == 201
    edge2 = EdgeClient(edge.client, edge.edge_id, next_key, renewed.json()["cert_pem"])
    assert edge2.post(f"{E}/aws-certificate", ra_body(edge2, csr_pem(first))).status_code == 201


def test_a_revoked_edge_gets_none(enrol_edge, enrol_client, sql) -> None:
    edge: EdgeClient = enrol_edge()
    csr = csr_pem(new_key())
    cert = x509.load_pem_x509_certificate(edge.cert_pem.encode())
    presented = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    sql.execute(
        "UPDATE catalog.edge SET state = 'revoked', revoked_at = now() WHERE edge_id = %s",
        (edge.edge_id,),
    )
    resp = edge.post(f"{E}/aws-certificate", ra_body(edge, csr), check=False)
    assert resp.status_code == 403 and resp.json()["code"] == "edge_revoked"
    # And enrol refuses it too, should api ever forward one.
    body = {
        "edge_id": str(edge.edge_id),
        "csr_pem": csr,
        "purpose": "roles_anywhere",
        "current_cert_sha256": presented,
        "request_id": "req-" + uuid.uuid4().hex,
    }
    resp = enrol_client.post(
        "/internal/v0/sign", json=body, headers={"Authorization": f"Bearer {SIGN_TOKEN}"}
    )
    assert resp.status_code == 403 and resp.json()["code"] == "edge_revoked"
    assert (
        sql.execute(
            "SELECT count(*) FROM catalog.edge_ra_certificate WHERE edge_id = %s", (edge.edge_id,)
        ).fetchone()[0]
        == 0
    )


def crl_app(db_url: str, ca: FileCASigner, *, callers: bool = True) -> TestClient:
    services = EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=2),
        signer=ca,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        limiter=FailureLimiter(limit=5),
        crl_callers=SharedTokenVerifier(CRL_TOKEN, caller="test-worker") if callers else None,
    )
    return TestClient(create_enrol_app(services), base_url="https://enrol.test")


def test_the_crl_lists_revoked_edges_certificates(db_url, ca, enrol_edge, sql) -> None:
    kept: EdgeClient = enrol_edge()
    lost: EdgeClient = enrol_edge()
    certs = {}
    for e in (kept, lost):
        r = e.post(f"{E}/aws-certificate", ra_body(e, csr_pem(new_key())))
        assert r.status_code == 201
        certs[e.edge_id] = x509.load_pem_x509_certificate(r.json()["cert_pem"].encode())
    sql.execute(
        "UPDATE catalog.edge SET state = 'revoked', revoked_at = now() WHERE edge_id = %s",
        (lost.edge_id,),
    )
    with crl_app(db_url, ca, callers=False) as c:
        r = c.post("/internal/v0/crl", json={}, headers={"Authorization": f"Bearer {CRL_TOKEN}"})
        assert r.status_code == 403 and r.json()["code"] == "caller_not_allowed"
    with crl_app(db_url, ca) as c:
        # Only the worker's identity: api's token is not enough.
        r = c.post("/internal/v0/crl", json={}, headers={"Authorization": f"Bearer {SIGN_TOKEN}"})
        assert r.status_code == 403
        r = c.post(
            "/internal/v0/crl",
            json={"serials": ["1"]},
            headers={"Authorization": f"Bearer {CRL_TOKEN}"},
        )
        assert r.status_code == 400, "the list comes from the catalog, never the caller"
        r = c.post("/internal/v0/crl", json={}, headers={"Authorization": f"Bearer {CRL_TOKEN}"})
        assert r.status_code == 200, r.text
        out = r.json()
    crl = x509.load_pem_x509_crl(out["crl_pem"].encode())
    assert crl.is_signature_valid(ca.ca_certificate.public_key())  # type: ignore[arg-type]
    assert crl.issuer == ca.ca_certificate.subject
    revoked = {r.serial_number for r in crl}
    lost_mtls = x509.load_pem_x509_certificate(lost.cert_pem.encode())
    assert certs[lost.edge_id].serial_number in revoked
    assert lost_mtls.serial_number in revoked
    assert certs[kept.edge_id].serial_number not in revoked
    number = crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
    assert str(number) == out["crl_number"] and out["revoked"] == len(revoked)
    assert crl.next_update_utc - crl.last_update_utc >= timedelta(days=30)
    assert out["through"] is not None
    assert (
        sql.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'edge_ca.crl.sign'"
            " AND details->>'crl_number' = %s",
            (out["crl_number"],),
        ).fetchone()[0]
        == 1
    )
