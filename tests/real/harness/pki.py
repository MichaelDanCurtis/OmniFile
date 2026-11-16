"""Test certificates for the AWS run, made at run time and kept in OUT/pki (never committed).

The trust anchor's CA is shaped like OmniFile's edge CA (`enrol init-kv-ca`: ECDSA P-384,
self-signed, SHA-384). When the AWS run names an Azure run (--azure-run) whose guide made the
real edge CA, its certificate is used instead, so RW-22 proves the real one.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .core import REAL, Context


def _dir(ctx: Context) -> Path:
    d = ctx.out / "pki"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _ca(ctx: Context, name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    d = _dir(ctx)
    kf, cf = d / f"{name}.key", d / f"{name}.pem"
    if kf.exists():
        key = serialization.load_pem_private_key(kf.read_bytes(), None)
        return key, x509.load_pem_x509_certificate(cf.read_bytes())  # type: ignore[return-value]
    key = ec.generate_private_key(ec.SECP384R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OmniFile real-cloud test"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"{name} {ctx.run_id}"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ctx.now - timedelta(minutes=5))
        .not_valid_after(ctx.now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA384())
    )
    kf.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    kf.chmod(0o600)
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def edge_ca(ctx: Context) -> str:
    if ctx.azure_run:
        real = REAL / "out" / ctx.azure_run / "edge-ca.pem"
        if real.exists():
            return real.read_text()
    _, cert = _ca(ctx, "edge-ca")
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def leaf(ctx: Context, name: str, cn: str, ou: str | None, ca: str = "edge-ca") -> dict[str, str]:
    """A P-256 client certificate as enrol issues them (`OU=omnifile-roles-anywhere`,
    `CN=omnifile-edge-<id>`, clientAuth, 30 days), signed by CA with SHA-384."""
    d = _dir(ctx)
    f = d / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text())
    ca_key, ca_cert = _ca(ctx, ca)
    key = ec.generate_private_key(ec.SECP256R1())
    attrs = ([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, ou)] if ou else []) + [
        x509.NameAttribute(NameOID.COMMON_NAME, cn)
    ]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(attrs))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ctx.now - timedelta(minutes=5))
        .not_valid_after(ctx.now + timedelta(days=30))
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
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA384())
    )
    out = {
        "cert": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "serial": format(cert.serial_number, "x"),
    }
    f.write_text(json.dumps(out))
    f.chmod(0o600)
    return out


def crl(ctx: Context, revoked_serials: list[str]) -> str:
    key, cert = _ca(ctx, "edge-ca")
    b = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(cert.subject)
        .last_update(ctx.now)
        .next_update(ctx.now + timedelta(days=30))
    )
    for s in revoked_serials:
        b = b.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(int(s, 16))
            .revocation_date(ctx.now)
            .build()
        )
    return b.sign(key, hashes.SHA384()).public_bytes(serialization.Encoding.PEM).decode()
