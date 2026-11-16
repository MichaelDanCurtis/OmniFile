"""The OmniFile CA: edge certificates, issued only by `enrol` (control-api.md section 2).

`CertificateSigner` is the seam between "what an edge certificate looks like"
and "who holds the CA key":

- `FileCASigner` loads the CA key and certificate from PEM files. Dev and
  tests only.
- Production signs with the CA key in Key Vault (HSM, `sign` only). That
  signer builds the TBSCertificate itself and asks Key Vault for the
  signature; it plugs in here without changing the callers (M6).

Every edge certificate is built by `edge_certificate_template`, whatever the
signer: CN = omnifile-edge-<edge_id>, SAN URI urn:omnifile:edge:<edge_id>,
EKU clientAuth, 30 days. The CSR's subject and extensions are ignored; only
its public key (ECDSA P-256) is used, after its self-signature is checked.

The same CA issues each edge's IAM Roles Anywhere certificate (decision 50),
from `roles_anywhere_certificate_template`: a profile of its own, so the
Roles Anywhere trust policy can tell it from an mTLS certificate. Subject
OU = omnifile-roles-anywhere, CN = omnifile-edge-<edge_id>, the same SAN,
EKU clientAuth, digitalSignature, 30 days (renewed with the mTLS one). Its
key is the edge's TPM-sealed AWS key, never its mTLS key. The CA's CRL
(`crl_builder`) lists the unexpired certificates of revoked edges, for
Roles Anywhere (`worker/crl_jobs.py`); once an edge has been revoked it
always lists at least one serial (`CRL_SQL` in `enrol/app.py`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

EDGE_CERT_LIFETIME = timedelta(days=30)
RENEW_AFTER_FRACTION = 2 / 3  # renew from day 20 of 30
BACKDATE = timedelta(minutes=5)  # tolerate small clock skew on the edge


class CsrError(ValueError):
    """The CSR is not a valid, self-signed ECDSA P-256 request."""


@dataclass(frozen=True)
class Issued:
    cert: x509.Certificate
    cert_pem: str
    chain_pem: str  # intermediates between the leaf and ca_pem; "" when the CA signs directly
    ca_pem: str

    @property
    def sha256(self) -> bytes:
        return cert_sha256(self.cert)

    @property
    def not_after(self) -> datetime:
        return self.cert.not_valid_after_utc

    @property
    def serial_hex(self) -> str:
        return format(self.cert.serial_number, "x")


class CertificateSigner(Protocol):
    """Holds (or reaches) the CA key. Only `enrol` ever has one."""

    async def sign_crl(
        self, builder: x509.CertificateRevocationListBuilder
    ) -> x509.CertificateRevocationList:
        """Sign a fully built CRL with the CA key."""
        ...

    @property
    def ca_certificate(self) -> x509.Certificate: ...

    @property
    def chain_pem(self) -> str:
        """Intermediates between issued leaves and the trust anchor (may be "")."""
        ...

    async def sign(self, builder: x509.CertificateBuilder) -> x509.Certificate:
        """Sign a fully built certificate template with the CA key."""
        ...


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def cert_sha256(cert: x509.Certificate) -> bytes:
    """SHA-256 of the DER encoding: the fingerprint the catalog stores."""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()


def edge_common_name(edge_id: int) -> str:
    return f"omnifile-edge-{edge_id}"


def edge_san_uri(edge_id: int) -> str:
    return f"urn:omnifile:edge:{edge_id}"


def load_csr(csr_pem: str) -> ec.EllipticCurvePublicKey:
    """The CSR's public key, after checking its signature and key type."""
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except (ValueError, TypeError):
        raise CsrError("csr_pem is not a PEM PKCS#10 request") from None
    if not csr.is_signature_valid:
        raise CsrError("the CSR's signature does not verify")
    key = csr.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise CsrError("the CSR's key must be ECDSA P-256")
    return key


def edge_certificate_template(
    ca: x509.Certificate,
    edge_id: int,
    public_key: ec.EllipticCurvePublicKey,
    now: datetime,
    lifetime: timedelta = EDGE_CERT_LIFETIME,
    subject: x509.Name | None = None,
) -> x509.CertificateBuilder:
    not_before = now - BACKDATE
    if subject is None:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, edge_common_name(edge_id))])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(now + lifetime)
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(edge_san_uri(edge_id))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
    )


async def issue_edge_certificate(
    signer: CertificateSigner, edge_id: int, csr_pem: str, now: datetime | None = None
) -> Issued:
    key = load_csr(csr_pem)
    now = now or datetime.now(UTC)
    builder = edge_certificate_template(signer.ca_certificate, edge_id, key, now)
    cert = await signer.sign(builder)
    return Issued(cert, pem(cert), signer.chain_pem, pem(signer.ca_certificate))


#: The Roles Anywhere certificate's subject OU: what the trust policy
#: conditions on (`aws:PrincipalTag/x509Subject/OU`), so an mTLS certificate
#: from the same CA can't get AWS credentials.
ROLES_ANYWHERE_OU = "omnifile-roles-anywhere"
ROLES_ANYWHERE_LIFETIME = EDGE_CERT_LIFETIME
#: How long a CRL is good for. It is published again well before (weekly, and
#: on every revocation; `worker/crl_jobs.py`).
CRL_LIFETIME = timedelta(days=30)


def key_sha256(public_key: ec.EllipticCurvePublicKey) -> bytes:
    """SHA-256 of the key's SubjectPublicKeyInfo DER: the pinned AWS key."""
    return hashlib.sha256(
        public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).digest()


def roles_anywhere_subject(edge_id: int) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ROLES_ANYWHERE_OU),
            x509.NameAttribute(NameOID.COMMON_NAME, edge_common_name(edge_id)),
        ]
    )


def roles_anywhere_certificate_template(
    ca: x509.Certificate,
    edge_id: int,
    public_key: ec.EllipticCurvePublicKey,
    now: datetime,
    lifetime: timedelta = ROLES_ANYWHERE_LIFETIME,
) -> x509.CertificateBuilder:
    """The edge's Roles Anywhere certificate: the mTLS template's extensions
    (not a CA, digitalSignature, clientAuth, the edge's SAN) under a subject
    of its own (`roles_anywhere_subject`)."""
    return edge_certificate_template(
        ca, edge_id, public_key, now, lifetime, subject=roles_anywhere_subject(edge_id)
    )


async def issue_roles_anywhere_certificate(
    signer: CertificateSigner,
    edge_id: int,
    key: ec.EllipticCurvePublicKey,
    now: datetime | None = None,
) -> Issued:
    """`key` is the CSR's, already checked by `load_csr`."""
    now = now or datetime.now(UTC)
    builder = roles_anywhere_certificate_template(signer.ca_certificate, edge_id, key, now)
    cert = await signer.sign(builder)
    return Issued(cert, pem(cert), signer.chain_pem, pem(signer.ca_certificate))


def crl_builder(
    ca: x509.Certificate,
    revoked: list[tuple[int, datetime]],
    now: datetime,
    number: int,
    lifetime: timedelta = CRL_LIFETIME,
) -> x509.CertificateRevocationListBuilder:
    """A v2 CRL of `revoked` (serial, revocation time), numbered `number`."""
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca.subject)
        .last_update(now - BACKDATE)
        .next_update(now + lifetime)
        .add_extension(x509.CRLNumber(number), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
    )
    for serial, at in sorted(set(revoked)):
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(min(at, now))
            .build()
        )
    return builder


def renew_after(not_after: datetime, lifetime: timedelta = EDGE_CERT_LIFETIME) -> datetime:
    """Day 20 of a 30-day certificate."""
    return not_after - lifetime * (1 - RENEW_AFTER_FRACTION)


# --- api's own TLS server certificate (threat model T5) -----------------------

#: api's server certificate, with TCP ingress (api terminates mutual TLS itself).
#: Each replica asks for its own at start, for a key only it holds in memory,
#: and renews it from two thirds of its life.
API_SERVER_CERT_LIFETIME = timedelta(days=30)


def _leaf_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _leaf_builder(
    ca: x509.Certificate,
    subject: x509.Name,
    san: list[x509.GeneralName],
    eku: x509.ObjectIdentifier,
    public_key: ec.EllipticCurvePublicKey,
    now: datetime,
    lifetime: timedelta,
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + lifetime)
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_leaf_usage(), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
    )


def api_server_certificate_template(
    ca: x509.Certificate,
    names: tuple[str, ...],
    public_key: ec.EllipticCurvePublicKey,
    now: datetime,
    lifetime: timedelta = API_SERVER_CERT_LIFETIME,
) -> x509.CertificateBuilder:
    """api's server certificate: CN and SAN DNS names from enrol's own
    configuration (never the CSR), EKU serverAuth only, so it can't act as a
    client, and an edge certificate (clientAuth only) can't act as api."""
    if not names:
        raise ValueError("api server certificate needs at least one DNS name")
    return _leaf_builder(
        ca,
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]),
        [x509.DNSName(n) for n in names],
        ExtendedKeyUsageOID.SERVER_AUTH,
        public_key,
        now,
        lifetime,
    )


async def issue_api_server_certificate(
    signer: CertificateSigner, names: tuple[str, ...], csr_pem: str, now: datetime | None = None
) -> Issued:
    key = load_csr(csr_pem)
    now = now or datetime.now(UTC)
    cert = await signer.sign(
        api_server_certificate_template(signer.ca_certificate, names, key, now)
    )
    return Issued(cert, pem(cert), signer.chain_pem, pem(signer.ca_certificate))


# --- the Teams relay's client certificate (teams.md 4.1) ---------------------

RELAY_COMMON_NAME = "omnifile-relay"
RELAY_SAN_URI = "urn:omnifile:relay"
RELAY_CERT_DAYS = 180
#: The longest a relay certificate may be issued for (the CA/B limit for TLS).
RELAY_CERT_MAX_DAYS = 397


def relay_certificate_template(
    ca: x509.Certificate,
    public_key: ec.EllipticCurvePublicKey,
    now: datetime,
    lifetime: timedelta = timedelta(days=RELAY_CERT_DAYS),
) -> x509.CertificateBuilder:
    """The relay's client certificate: CN omnifile-relay, SAN URI
    urn:omnifile:relay and no edge SAN, EKU clientAuth. api accepts it on the
    intents path only, and only while its SHA-256 is pinned
    (OMNIFILE_TEAMS_RELAY_CERT_SHA256)."""
    return _leaf_builder(
        ca,
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, RELAY_COMMON_NAME)]),
        [x509.UniformResourceIdentifier(RELAY_SAN_URI)],
        ExtendedKeyUsageOID.CLIENT_AUTH,
        public_key,
        now,
        lifetime,
    )


async def issue_relay_certificate(
    signer: CertificateSigner,
    csr_pem: str,
    days: int = RELAY_CERT_DAYS,
    now: datetime | None = None,
) -> Issued:
    if not 1 <= days <= RELAY_CERT_MAX_DAYS:
        raise CsrError(f"a relay certificate lasts 1 to {RELAY_CERT_MAX_DAYS} days")
    key = load_csr(csr_pem)
    now = now or datetime.now(UTC)
    builder = relay_certificate_template(signer.ca_certificate, key, now, timedelta(days=days))
    cert = await signer.sign(builder)
    return Issued(cert, pem(cert), signer.chain_pem, pem(signer.ca_certificate))


class FileCASigner:
    """A CA whose key is a PEM file. Dev and tests only; production uses Key Vault."""

    def __init__(
        self, key: ec.EllipticCurvePrivateKey, cert: x509.Certificate, chain_pem: str = ""
    ) -> None:
        self._key = key
        self._cert = cert
        self._chain = chain_pem

    @classmethod
    def from_files(cls, key_path: str | Path, cert_path: str | Path) -> FileCASigner:
        key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("the dev CA key must be an EC private key")
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        return cls(key, cert)

    @classmethod
    def generate(cls, name: str = "OmniFile dev CA", days: int = 3650) -> FileCASigner:
        """A fresh self-signed CA (tests, and `python -m omnifile_service.enrol init-dev-ca`)."""
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
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
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        return cls(key, cert)

    def write(self, key_path: str | Path, cert_path: str | Path) -> None:
        key_bytes = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        kp = Path(key_path)
        kp.touch(mode=0o600, exist_ok=True)
        kp.chmod(0o600)
        kp.write_bytes(key_bytes)
        Path(cert_path).write_text(pem(self._cert))

    @property
    def ca_certificate(self) -> x509.Certificate:
        return self._cert

    @property
    def chain_pem(self) -> str:
        return self._chain

    @property
    def private_key(self) -> ec.EllipticCurvePrivateKey:
        """Tests use it to make server certificates."""
        return self._key

    async def sign(self, builder: x509.CertificateBuilder) -> x509.Certificate:
        return builder.sign(self._key, hashes.SHA256())

    async def sign_crl(
        self, builder: x509.CertificateRevocationListBuilder
    ) -> x509.CertificateRevocationList:
        return builder.sign(self._key, hashes.SHA256())
