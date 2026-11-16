"""The production CA signer: the CA key never leaves Key Vault (`ca.py`).

The key (`omnifile-edge-ca`, EC P-384, non-exportable, HSM-backed on the
premium SKU; `deploy/azure/modules/keyvault.bicep`) can only `sign`. A
certificate is built as usual, signed once with a throwaway key of the same
curve so that `cryptography` lays out its TBSCertificate with the right
signature algorithm, and the throwaway signature is replaced by the one Key
Vault makes over that TBSCertificate. Every certificate is verified against
the CA certificate before it is returned, so a wrong key, curve or splice
never yields a certificate an edge would be handed.

Key Vault is reached over its REST API (`sign`, `keys/get`), with the app's
managed identity: no SDK beyond `azure-identity`, which the service already
uses. Over private networking the vault URI resolves to its private endpoint.

Bootstrap (once, `python -m omnifile_service.enrol init-kv-ca`): the CA
certificate is self-signed the same way, then given to `api` (its trust
anchor) and `enrol` (`deploy/azure/README.md`, step 7).
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID

from .ca import BACKDATE

API_VERSION = "7.4"
SCOPE = "https://vault.azure.net/.default"

#: Key Vault curve name -> (cryptography curve, JWS algorithm, hash, coordinate bytes).
CURVES: dict[str, tuple[ec.EllipticCurve, str, hashes.HashAlgorithm, int]] = {
    "P-256": (ec.SECP256R1(), "ES256", hashes.SHA256(), 32),
    "P-384": (ec.SECP384R1(), "ES384", hashes.SHA384(), 48),
    "P-521": (ec.SECP521R1(), "ES512", hashes.SHA512(), 66),
}

#: Returns a bearer token for Key Vault (the managed identity, or a test stub).
TokenSource = Callable[[], Awaitable[str]]


class KeyVaultError(RuntimeError):
    """Key Vault refused, or answered something that isn't a usable signature."""


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- DER, just enough to take a certificate apart and put it back together ---


def _read_tlv(der: bytes, at: int) -> tuple[int, int]:
    """(start of value, end of element) of the DER element at `at`."""
    if at + 2 > len(der):
        raise ValueError("truncated DER")
    first = der[at + 1]
    if first < 0x80:
        return at + 2, at + 2 + first
    n = first & 0x7F
    if n == 0 or n > 4 or at + 2 + n > len(der):
        raise ValueError("bad DER length")
    length = int.from_bytes(der[at + 2 : at + 2 + n], "big")
    return at + 2 + n, at + 2 + n + length


def _tlv(tag: int, value: bytes) -> bytes:
    n = len(value)
    if n < 0x80:
        return bytes([tag, n]) + value
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(raw)]) + raw + value


def splice_signature(template_der: bytes, signature_der: bytes) -> bytes:
    """`template_der` (a certificate, or a CRL: both are SEQUENCE { tbs,
    signatureAlgorithm, signatureValue }) with its signatureValue replaced."""
    body, end = _read_tlv(template_der, 0)
    if template_der[0] != 0x30 or end != len(template_der):
        raise ValueError("not a DER certificate or CRL")
    _, tbs_end = _read_tlv(template_der, body)
    _, alg_end = _read_tlv(template_der, tbs_end)
    tbs_and_alg = template_der[body:alg_end]
    bit_string = _tlv(0x03, b"\x00" + signature_der)
    return _tlv(0x30, tbs_and_alg + bit_string)


# --- the vault ---


class KeyVaultKey:
    """One EC key in Key Vault, used for `sign` only."""

    def __init__(
        self,
        vault_uri: str,
        key_name: str,
        token: TokenSource,
        http: httpx.AsyncClient,
        key_version: str = "",
    ) -> None:
        if not vault_uri.startswith("https://"):
            raise ValueError("the Key Vault URI must be https://")
        self._base = vault_uri.rstrip("/")
        self._name = key_name
        self._version = key_version
        self._token = token
        self._http = http
        self.curve_name = ""
        self.public_key: ec.EllipticCurvePublicKey | None = None
        self.kid = ""

    def _url(self, op: str = "") -> str:
        path = f"{self._base}/keys/{quote(self._name, safe='')}"
        if self._version:
            path += f"/{quote(self._version, safe='')}"
        return f"{path}{op}?api-version={API_VERSION}"

    async def _call(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        try:
            r = await self._http.request(method, url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise KeyVaultError(f"Key Vault unreachable: {type(e).__name__}") from None
        if r.status_code != 200:
            code = ""
            try:
                code = r.json().get("error", {}).get("code", "")
            except ValueError:
                pass
            raise KeyVaultError(f"Key Vault answered {r.status_code} {code}".strip())
        data = r.json()
        if not isinstance(data, dict):
            raise KeyVaultError("Key Vault answered a non-object")
        return data

    async def load(self) -> ec.EllipticCurvePublicKey:
        """Reads the key's public half (it must be an EC key that can sign)."""
        data = await self._call("GET", self._url())
        jwk = data.get("key") or {}
        if jwk.get("kty") not in ("EC", "EC-HSM"):
            raise KeyVaultError(f"{self._name} is not an EC key")
        if "sign" not in (jwk.get("key_ops") or []):
            raise KeyVaultError(f"{self._name} may not sign")
        crv = jwk.get("crv")
        if crv not in CURVES:
            raise KeyVaultError(f"{self._name} has an unsupported curve {crv!r}")
        curve, _, _, size = CURVES[crv]
        x, y = _unb64url(jwk["x"]), _unb64url(jwk["y"])
        if len(x) != size or len(y) != size:
            raise KeyVaultError("the key's coordinates have the wrong size")
        nums = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), curve
        )
        self.public_key = nums.public_key()
        self.curve_name = crv
        self.kid = str(jwk.get("kid", ""))
        return self.public_key

    async def sign_digest(self, digest: bytes) -> bytes:
        """A DER ECDSA signature over `digest`, made by the vault."""
        if self.public_key is None:
            await self.load()
        _, alg, _, size = CURVES[self.curve_name]
        base = (self.kid or self._url().split("?")[0]).rstrip("/")
        url = f"{base}/sign?api-version={API_VERSION}"
        if not url.startswith(self._base + "/keys/"):
            raise KeyVaultError("the key id points outside the configured vault")
        data = await self._call("POST", url, {"alg": alg, "value": _b64url(digest)})
        raw = _unb64url(str(data.get("value", "")))
        if len(raw) != 2 * size:
            raise KeyVaultError("Key Vault's signature has the wrong length")
        r, s = int.from_bytes(raw[:size], "big"), int.from_bytes(raw[size:], "big")
        return encode_dss_signature(r, s)

    async def sign_builder(self, builder: x509.CertificateBuilder) -> x509.Certificate:
        """`builder` signed by the vault key (see the module docstring)."""
        if self.public_key is None:
            await self.load()
        curve, _, hash_alg, _ = CURVES[self.curve_name]
        template = builder.sign(ec.generate_private_key(curve), hash_alg)
        h = hashes.Hash(hash_alg)
        h.update(template.tbs_certificate_bytes)
        sig = await self.sign_digest(h.finalize())
        der = splice_signature(template.public_bytes(serialization.Encoding.DER), sig)
        return x509.load_der_x509_certificate(der)

    async def sign_crl_builder(
        self, builder: x509.CertificateRevocationListBuilder
    ) -> x509.CertificateRevocationList:
        """`builder` signed by the vault key, as `sign_builder` does a certificate."""
        if self.public_key is None:
            await self.load()
        curve, _, hash_alg, _ = CURVES[self.curve_name]
        template = builder.sign(ec.generate_private_key(curve), hash_alg)
        h = hashes.Hash(hash_alg)
        h.update(template.tbs_certlist_bytes)
        sig = await self.sign_digest(h.finalize())
        der = splice_signature(template.public_bytes(serialization.Encoding.DER), sig)
        return x509.load_der_x509_crl(der)


class KeyVaultCASigner:
    """`CertificateSigner` over a Key Vault key and its CA certificate."""

    def __init__(self, key: KeyVaultKey, ca_cert: x509.Certificate, chain_pem: str = "") -> None:
        self._key = key
        self._cert = ca_cert
        self._chain = chain_pem

    @classmethod
    async def open(
        cls, key: KeyVaultKey, ca_cert: x509.Certificate, chain_pem: str = ""
    ) -> KeyVaultCASigner:
        """Checks, before serving, that the vault key is the CA certificate's."""
        public = await key.load()
        if public.public_numbers() != ca_cert.public_key().public_numbers():  # type: ignore[union-attr]
            raise KeyVaultError(
                "the Key Vault key is not the CA certificate's key: check OMNIFILE_CA_KEY_NAME"
                " and the CA certificate"
            )
        return cls(key, ca_cert, chain_pem)

    @property
    def ca_certificate(self) -> x509.Certificate:
        return self._cert

    @property
    def chain_pem(self) -> str:
        return self._chain

    async def sign(self, builder: x509.CertificateBuilder) -> x509.Certificate:
        cert = await self._key.sign_builder(builder)
        try:
            cert.verify_directly_issued_by(self._cert)
        except (ValueError, TypeError) as e:
            raise KeyVaultError(f"the vault's certificate doesn't verify: {e}") from None
        except Exception as e:  # InvalidSignature
            why = type(e).__name__
            raise KeyVaultError(f"the vault's signature doesn't verify: {why}") from None
        return cert

    async def sign_crl(
        self, builder: x509.CertificateRevocationListBuilder
    ) -> x509.CertificateRevocationList:
        crl = await self._key.sign_crl_builder(builder)
        if not crl.is_signature_valid(self._cert.public_key()):  # type: ignore[arg-type]
            raise KeyVaultError("the vault's CRL signature doesn't verify")
        return crl


class DeferredKeyVaultSigner:
    """`KeyVaultCASigner`, opened in the event loop that serves (enrol's lifespan).

    Its HTTP client and managed-identity credential belong to the loop that
    first uses them: a signer opened in a setup loop (`asyncio.run` before
    `uvicorn.run`) failed every sign afterwards with "Event loop is closed".
    `start` makes them, checks the vault key against the CA certificate (so a
    wrong key still stops enrol at start), and keeps the signer.
    """

    def __init__(
        self, ca_cert: x509.Certificate, open_key: Callable[[], KeyVaultKey], chain_pem: str = ""
    ) -> None:
        self._cert = ca_cert
        self._open_key = open_key
        self._chain = chain_pem
        self._signer: KeyVaultCASigner | None = None

    async def start(self) -> None:
        if self._signer is None:
            self._signer = await KeyVaultCASigner.open(self._open_key(), self._cert, self._chain)

    def _ready(self) -> KeyVaultCASigner:
        if self._signer is None:
            raise KeyVaultError("the Key Vault signer was not started")
        return self._signer

    @property
    def ca_certificate(self) -> x509.Certificate:
        return self._cert

    @property
    def chain_pem(self) -> str:
        return self._chain

    async def sign(self, builder: x509.CertificateBuilder) -> x509.Certificate:
        return await self._ready().sign(builder)

    async def sign_crl(
        self, builder: x509.CertificateRevocationListBuilder
    ) -> x509.CertificateRevocationList:
        return await self._ready().sign_crl(builder)


async def self_signed_ca(
    key: KeyVaultKey, name: str = "OmniFile edge CA", days: int = 3650
) -> x509.Certificate:
    """The root certificate, signed by the vault key (bootstrap, once)."""
    public = await key.load()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
    )
    cert = await key.sign_builder(builder)
    cert.verify_directly_issued_by(cert)
    return cert


def managed_identity_token(client_id: str | None) -> TokenSource:
    """Key Vault tokens from the app's managed identity (cached by azure-identity)."""
    from azure.identity.aio import ManagedIdentityCredential

    cred = ManagedIdentityCredential(client_id=client_id)

    async def token() -> str:
        return (await cred.get_token(SCOPE)).token

    return token


def fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
