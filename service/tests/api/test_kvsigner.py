"""The Key Vault CA signer (enrol/kvsigner.py; M7 review: enrol could only sign
with a key file). A fake vault over httpx holds a real P-384 key and answers
`keys/get` and `sign` as Key Vault's REST API does."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

from omnifile_service.api.clientauth import verify_client_certificate
from omnifile_service.enrol.ca import FileCASigner, issue_edge_certificate
from omnifile_service.enrol.kvsigner import (
    KeyVaultCASigner,
    KeyVaultError,
    KeyVaultKey,
    self_signed_ca,
    splice_signature,
)

VAULT = "https://kv-test.vault.azure.net"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeVault:
    def __init__(self, curve: ec.EllipticCurve | None = None, crv: str = "P-384") -> None:
        curve = curve or ec.SECP384R1()
        self.key = ec.generate_private_key(curve)
        self.crv = crv
        self.size = (curve.key_size + 7) // 8
        self.signs = 0
        self.tokens: list[str] = []
        self.tamper = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.tokens.append(req.headers.get("authorization", ""))
        path = req.url.path
        if req.method == "GET" and path == "/keys/omnifile-edge-ca":
            n = self.key.public_key().public_numbers()
            return httpx.Response(200, json={"key": {
                "kid": f"{VAULT}/keys/omnifile-edge-ca/v1", "kty": "EC-HSM", "crv": self.crv,
                "key_ops": ["sign", "verify"],
                "x": b64(n.x.to_bytes(self.size, "big")), "y": b64(n.y.to_bytes(self.size, "big")),
            }})  # fmt: skip
        if req.method == "POST" and path == "/keys/omnifile-edge-ca/v1/sign":
            body = json.loads(req.content)
            digest = base64.urlsafe_b64decode(body["value"] + "==")
            alg = {"ES256": hashes.SHA256(), "ES384": hashes.SHA384()}[body["alg"]]
            der = self.key.sign(digest, ec.ECDSA(Prehashed(alg)))
            r, s = decode_dss_signature(der)
            if self.tamper:
                s ^= 1
            self.signs += 1
            raw = r.to_bytes(self.size, "big") + s.to_bytes(self.size, "big")
            return httpx.Response(200, json={"kid": "x", "value": b64(raw)})
        return httpx.Response(404, json={"error": {"code": "KeyNotFound"}})


def _key(vault: FakeVault) -> KeyVaultKey:
    async def token() -> str:
        return "mi-token"

    http = httpx.AsyncClient(transport=httpx.MockTransport(vault.handler))
    return KeyVaultKey(VAULT, "omnifile-edge-ca", token, http)


def _csr() -> str:
    from cryptography.hazmat.primitives import serialization

    k = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(k, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def test_the_vault_bootstraps_the_ca_and_signs_edge_certificates() -> None:
    vault = FakeVault()

    async def run():
        ca = await self_signed_ca(_key(vault))
        signer = await KeyVaultCASigner.open(_key(vault), ca)
        issued = await issue_edge_certificate(signer, 42, _csr())
        return ca, issued

    ca, issued = asyncio.run(run())
    assert ca.signature_hash_algorithm.name == "sha384"
    ca.verify_directly_issued_by(ca)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    issued.cert.verify_directly_issued_by(ca)
    # The control API accepts it as edge 42's.
    v = verify_client_certificate(issued.cert, ca, now=datetime.now(UTC))
    assert v.sha256 == issued.sha256
    assert vault.signs == 2
    assert all(t == "Bearer mi-token" for t in vault.tokens)


def test_the_vault_signs_roles_anywhere_certificates_and_the_crl() -> None:
    """Decision 50: the same vault key signs the edges' Roles Anywhere
    certificates and the CA's CRL; a bad vault signature never becomes a CRL."""
    from omnifile_service.enrol.ca import (
        crl_builder,
        issue_roles_anywhere_certificate,
        load_csr,
    )

    vault = FakeVault()

    async def run():
        ca = await self_signed_ca(_key(vault))
        signer = await KeyVaultCASigner.open(_key(vault), ca)
        issued = await issue_roles_anywhere_certificate(signer, 42, load_csr(_csr()))
        now = datetime.now(UTC)
        crl = await signer.sign_crl(crl_builder(ca, [(issued.cert.serial_number, now)], now, 7))
        vault.tamper = True
        with pytest.raises(KeyVaultError, match="CRL signature doesn't verify"):
            await signer.sign_crl(crl_builder(ca, [], now, 8))
        return ca, issued, crl

    ca, issued, crl = asyncio.run(run())
    issued.cert.verify_directly_issued_by(ca)
    assert crl.is_signature_valid(ca.public_key())  # type: ignore[arg-type]
    assert crl.signature_hash_algorithm.name == "sha384"  # type: ignore[union-attr]
    assert [r.serial_number for r in crl] == [issued.cert.serial_number]
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 7


def test_a_vault_key_that_is_not_the_ca_certificates_is_refused_at_start() -> None:
    other = FileCASigner.generate().ca_certificate
    with pytest.raises(KeyVaultError, match="not the CA certificate's key"):
        asyncio.run(KeyVaultCASigner.open(_key(FakeVault()), other))


def test_a_bad_signature_from_the_vault_never_becomes_a_certificate() -> None:
    vault = FakeVault()

    async def run():
        ca = await self_signed_ca(_key(vault))
        signer = await KeyVaultCASigner.open(_key(vault), ca)
        vault.tamper = True
        return await issue_edge_certificate(signer, 7, _csr())

    with pytest.raises(KeyVaultError, match="doesn't verify"):
        asyncio.run(run())


def test_vault_errors_and_unusable_keys_are_refused() -> None:
    vault = FakeVault()
    k = _key(vault)
    k._name = "missing"
    with pytest.raises(KeyVaultError, match="404 KeyNotFound"):
        asyncio.run(k.load())
    with pytest.raises(ValueError):
        KeyVaultKey("http://kv.example", "k", None, None)  # type: ignore[arg-type]
    rsa_like = FakeVault(ec.SECP256K1(), "P-256K")
    with pytest.raises(KeyVaultError, match="unsupported curve"):
        asyncio.run(_key(rsa_like).load())


def test_splice_keeps_the_tbs_and_algorithm_and_replaces_only_the_signature() -> None:
    from cryptography.hazmat.primitives import serialization

    ca = FileCASigner.generate()
    der = ca.ca_certificate.public_bytes(serialization.Encoding.DER)
    out = splice_signature(der, b"\x30\x06\x02\x01\x01\x02\x01\x01")
    cert = x509.load_der_x509_certificate(out)
    assert cert.tbs_certificate_bytes == ca.ca_certificate.tbs_certificate_bytes
    assert cert.signature == b"\x30\x06\x02\x01\x01\x02\x01\x01"
    with pytest.raises(ValueError):
        splice_signature(b"\x31\x00", b"")


def test_a_ca_key_in_a_file_is_for_dev_mode_only(monkeypatch, tmp_path) -> None:
    from omnifile_service.enrol import __main__ as enrol_main

    ca = FileCASigner.generate()
    ca.write(tmp_path / "ca.key", tmp_path / "ca.crt")
    monkeypatch.setenv("OMNIFILE_DEV_CA_KEY", str(tmp_path / "ca.key"))
    monkeypatch.setenv("OMNIFILE_DEV_CA_CERT", str(tmp_path / "ca.crt"))
    monkeypatch.delenv("OMNIFILE_DEV_MODE", raising=False)
    with pytest.raises(SystemExit, match="needs OMNIFILE_DEV_MODE=1"):
        enrol_main.build()
    # And with no signer at all, it says what to set.
    monkeypatch.delenv("OMNIFILE_DEV_CA_KEY")
    monkeypatch.delenv("OMNIFILE_KEYVAULT_URI", raising=False)
    with pytest.raises(SystemExit, match="OMNIFILE_KEYVAULT_URI"):
        enrol_main.build()
