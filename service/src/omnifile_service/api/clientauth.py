"""Who is calling: the edge's client certificate, checked against the OmniFile CA.

Two ways the certificate reaches the app (`ClientCertSource`):

- `DirectTls`: uvicorn terminates TLS itself with `CERT_REQUIRED` against the
  OmniFile CA. This is how `api` runs in Azure by default (TCP ingress,
  threat model T5), with a server certificate from the edge CA that enrol
  issues (`EnrolServerCertificates`, below), and in dev and tests.
  `tlsproto.ClientCertH11Protocol` puts the peer certificate into the ASGI
  scope as the TLS extension (`scope["extensions"]["tls"]["client_cert_chain"]`),
  which uvicorn does not do by itself. An `X-Forwarded-Client-Cert` header is
  refused: nothing in front sets it, so a caller did.
- `ForwardedClientCert`: behind an HTTP ingress
  (`clientCertificateMode: require`). Ingress terminates TLS and forwards the
  certificate in `X-Forwarded-Client-Cert`, documented as
  `Hash=<sha256 hex>;Cert="<PEM>";Chain="<PEM...>";` (Microsoft Learn,
  "Configure client certificate authentication in Azure Container Apps",
  read 2026-10-05). Ingress does not validate the chain, so the app does.
  The header is refused unless the app is configured to trust the ingress
  (`trust_ingress`), and then only from the `trusted_proxies` peer addresses,
  which are required: anyone who can reach the app directly could otherwise
  send their own header. They must hold only the proxy: on a shared Container
  Apps environment the ingress shares its subnet with every other app, which
  is why TCP ingress is the default (T5). A forwarded source with no trusted
  proxies cannot be built, so `api` refuses to start that way.
  VERIFY in M1 on a real environment that ingress replaces a client-supplied
  copy of the header (M1 carry-over).

Whatever the source, `verify_client_certificate` checks the certificate the
same way: issued by the OmniFile CA (signature and name), within its validity
dates, a leaf with EKU clientAuth. `EdgeAuthenticator` then maps its SHA-256
to an active edge (control-api.md section 2).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import os
import re
import ssl
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import unquote

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.requests import HTTPConnection

from .problems import ApiError

XFCC = "x-forwarded-client-cert"
MAX_XFCC_BYTES = 32 * 1024


@dataclass(frozen=True)
class ClientCert:
    cert: x509.Certificate
    sha256: bytes


class ClientCertSource(Protocol):
    def certificate(self, conn: HTTPConnection) -> x509.Certificate:
        """The caller's leaf certificate, unverified. Raises `ApiError` if absent."""
        ...


class DirectTls:
    """uvicorn terminated TLS and required a client certificate."""

    def certificate(self, conn: HTTPConnection) -> x509.Certificate:
        if conn.headers.get(XFCC) is not None:
            # Nobody is in front of us to set it; a caller sent it.
            raise ApiError("certificate_invalid", "X-Forwarded-Client-Cert is not accepted here")
        tls = conn.scope.get("extensions", {}).get("tls") or {}
        chain = tls.get("client_cert_chain") or []
        if not chain:
            raise ApiError("certificate_required", "no client certificate")
        try:
            return x509.load_pem_x509_certificate(chain[0].encode())
        except ValueError:
            raise ApiError("certificate_invalid", "unreadable client certificate") from None


class ForwardedClientCert:
    """Container Apps ingress forwarded the client certificate in X-Forwarded-Client-Cert."""

    def __init__(self, *, trust_ingress: bool, trusted_proxies: tuple[str, ...]) -> None:
        self.trust_ingress = trust_ingress
        self.trusted = parse_networks(trusted_proxies)
        if trust_ingress and not self.trusted:
            raise ValueError(
                "forwarded client certificates need trusted proxies (the ingress's addresses)"
            )

    def certificate(self, conn: HTTPConnection) -> x509.Certificate:
        values = conn.headers.getlist(XFCC)
        if not values:
            raise ApiError("certificate_required", "no client certificate")
        if not self.trust_ingress:
            raise ApiError(
                "certificate_invalid", "X-Forwarded-Client-Cert is not trusted by this app"
            )
        if not peer_in(conn, self.trusted):
            raise ApiError("certificate_invalid", "X-Forwarded-Client-Cert from an untrusted peer")
        if len(values) != 1:
            raise ApiError("certificate_invalid", "more than one X-Forwarded-Client-Cert header")
        fields = parse_xfcc(values[0])
        cert_text = fields.get("cert")
        if not cert_text:
            raise ApiError("certificate_invalid", "X-Forwarded-Client-Cert has no Cert")
        cert = load_forwarded_pem(cert_text)
        declared = fields.get("hash")
        if declared is not None:
            actual = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
            if declared.lower() != actual:
                raise ApiError("certificate_invalid", "X-Forwarded-Client-Cert Hash does not match")
        return cert


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip(value: str | None) -> IPAddress | None:
    try:
        return address(value.strip()) if value else None  # IPv4-mapped as IPv4
    except ValueError:
        return None


def forwarded_client_ip(
    peer_host: str | None, forwarded_for: list[str], trusted_ingress: tuple[Network, ...]
) -> IPAddress | None:
    """The caller's address: the TCP peer, unless the peer is the trusted
    ingress, in which case the last X-Forwarded-For entry (the address the
    ingress itself appended; earlier entries are the client's own claims)."""
    peer = _ip(peer_host)
    if peer is None or not any(peer in net for net in trusted_ingress):
        return peer
    if not forwarded_for:
        return peer
    last = forwarded_for[-1].split(",")[-1]
    return _ip(last) or peer


#: The widest range a trust list may name: an ingress or a subnet is never wider, and the
#: limit closes "0.0.0.0/1,128.0.0.0/1", which is every address without a /0 (review of #34).
MIN_PREFIX = {4: 16, 6: 48}

_MAPPED = ipaddress.ip_network("::ffff:0:0/96")


def network(cidr: str) -> Network:
    """One CIDR (or bare address), IPv4-mapped IPv6 (`::ffff:a.b.c.d/p`, p >= 96) written as
    the IPv4 network it is, so ranges and peers compare in one version (review of #34: a
    mapped proxy range skipped the overlap check). An IPv6 range that holds every mapped
    address is refused: it trusts all of IPv4."""
    net = ipaddress.ip_network(cidr.strip(), strict=False)
    if isinstance(net, ipaddress.IPv6Network):
        if net.prefixlen >= 96 and net.subnet_of(_MAPPED):
            mapped = net.network_address.ipv4_mapped
            assert mapped is not None
            return ipaddress.IPv4Network((mapped, net.prefixlen - 96))
        if net.supernet_of(_MAPPED):
            raise ValueError(f"{net} holds every IPv4-mapped address: wider than all of IPv4")
    return net


def address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """A peer's address, an IPv4-mapped one as IPv4 (a dual-stack socket reports
    `::ffff:10.60.0.4` for an IPv4 caller)."""
    addr = ipaddress.ip_address(host)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def parse_networks(cidrs: tuple[str, ...] | list[str]) -> tuple[Network, ...]:
    """CIDRs (or bare addresses) -> networks. Raises ValueError on a bad one, or on a
    range wider than MIN_PREFIX (/16 for IPv4, /48 for IPv6): a trust list that admits
    everyone, or near enough, is no trust list."""
    nets = tuple(network(c) for c in cidrs if c.strip())
    for net in nets:
        if net.prefixlen == 0:
            raise ValueError(f"{net} trusts every address; name the ingress's range")
        if net.prefixlen < MIN_PREFIX[net.version]:
            raise ValueError(
                f"{net} is wider than /{MIN_PREFIX[net.version]}; name the ingress's range"
            )
    return nets


def peer_in(conn: HTTPConnection, networks: tuple[Network, ...]) -> bool:
    """True if the TCP peer (not any forwarded header) is in one of `networks`."""
    if not networks:
        return False
    try:
        addr = address(conn.client.host if conn.client else "")
    except ValueError:
        return False
    return any(addr in net for net in networks)


def parse_xfcc(value: str) -> dict[str, str]:
    """Parse one `Key=value;Key="quoted";...` element into lower-cased keys.

    Quoted values may contain `;` and `,`; a backslash escapes `"` and `\\`
    (Envoy's format, which Container Apps' header follows).
    Repeated keys, or more than one element (a `,` outside quotes), are
    refused: ingress forwards exactly one certificate.
    """
    if len(value) > MAX_XFCC_BYTES:
        raise ApiError("certificate_invalid", "X-Forwarded-Client-Cert is too large")
    fields: dict[str, str] = {}
    i, n = 0, len(value)
    while i < n:
        while i < n and value[i] in " ;":
            i += 1
        if i >= n:
            break
        eq = value.find("=", i)
        if eq < 0:
            raise ApiError("certificate_invalid", "malformed X-Forwarded-Client-Cert")
        key = value[i:eq].strip().lower()
        i = eq + 1
        if i < n and value[i] == '"':
            i += 1
            out: list[str] = []
            while i < n and value[i] != '"':
                if value[i] == "\\" and i + 1 < n and value[i + 1] in '"\\':
                    i += 1
                out.append(value[i])
                i += 1
            if i >= n:
                raise ApiError(
                    "certificate_invalid", "unterminated quote in X-Forwarded-Client-Cert"
                )
            i += 1
            val = "".join(out)
        else:
            end = i
            while end < n and value[end] not in ";,":
                end += 1
            val = value[i:end].strip()
            i = end
        if i < n and value[i] == ",":
            raise ApiError(
                "certificate_invalid", "more than one certificate in X-Forwarded-Client-Cert"
            )
        if not key or key in fields:
            raise ApiError("certificate_invalid", "malformed X-Forwarded-Client-Cert")
        fields[key] = val
    return fields


_PEM_BODY = re.compile(
    r"-----BEGIN CERTIFICATE-----(?P<body>.*?)-----END CERTIFICATE-----", re.DOTALL
)


def load_forwarded_pem(text: str) -> x509.Certificate:
    """A certificate as ingress forwards it.

    Accepted spellings, because the documented example elides the encoding:
    PEM with or without line breaks (a header can't carry raw newlines),
    URL-encoded PEM (Envoy), literal `\\n` escapes, or bare base64 DER.
    Only the first certificate is read.
    """
    if "%" in text:
        text = unquote(text)
    m = _PEM_BODY.search(text)
    body = m["body"] if m else text
    body = re.sub(r"\\n|\s", "", body)
    try:
        der = base64.b64decode(body, validate=True)
        return x509.load_der_x509_certificate(der)
    except (ValueError, binascii.Error):
        raise ApiError(
            "certificate_invalid", "unreadable certificate in X-Forwarded-Client-Cert"
        ) from None


def verify_client_certificate(
    cert: x509.Certificate, ca: x509.Certificate, now: datetime | None = None
) -> ClientCert:
    """Check `cert` was issued by `ca` for client auth and is valid now."""
    now = now or datetime.now(UTC)
    if cert.issuer != ca.subject:
        raise ApiError("certificate_invalid", "not issued by the OmniFile CA")
    pub = ca.public_key()
    try:
        if isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert.signature_hash_algorithm),  # type: ignore[arg-type]
            )
        elif isinstance(pub, rsa.RSAPublicKey):
            pub.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                PKCS1v15(),
                cert.signature_hash_algorithm,  # type: ignore[arg-type]
            )
        else:
            raise ApiError("certificate_invalid", "unsupported CA key type")
    except InvalidSignature:
        raise ApiError("certificate_invalid", "signature does not verify") from None
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ApiError("certificate_invalid", "certificate expired or not yet valid")
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        if bc.ca:
            raise ApiError("certificate_invalid", "a CA certificate is not a client")
    except x509.ExtensionNotFound:
        pass
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        raise ApiError("certificate_invalid", "no clientAuth usage") from None
    if ExtendedKeyUsageOID.CLIENT_AUTH not in eku:
        raise ApiError("certificate_invalid", "no clientAuth usage")
    der = cert.public_bytes(serialization.Encoding.DER)
    return ClientCert(cert, hashlib.sha256(der).digest())


def san_uris(cert: x509.Certificate) -> list[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return san.get_values_for_type(x509.UniformResourceIdentifier)


@dataclass(frozen=True)
class EdgeIdentity:
    edge_id: int
    cert_sha256: bytes
    cert_not_after: datetime | None
    #: The certificate the edge presented (already verified), for proofs of
    #: possession of its key (renewal).
    cert: x509.Certificate | None = None

    @property
    def principal(self) -> str:
        return f"edge:{self.edge_id}"


class EdgeAuthenticator:
    """Certificate -> active edge (control-api.md section 2, "Every other call")."""

    def __init__(self, source: ClientCertSource, ca: x509.Certificate, catalog) -> None:
        self.source = source
        self.ca = ca
        self.catalog = catalog

    async def authenticate(self, conn: HTTPConnection, path_edge_id: int) -> EdgeIdentity:
        verified = verify_client_certificate(self.source.certificate(conn), self.ca)
        if RELAY_SAN in san_uris(verified.cert):
            # The relay may call its one path and nothing else (teams.md section 4.1).
            raise ApiError("caller_not_allowed", "the Teams relay may not call edge paths")
        async with self.catalog.read() as db:
            row = await (
                await db.execute(
                    "SELECT edge_id, state, cert_sha256, cert_not_after,"
                    "       prev_cert_sha256, prev_cert_not_after"
                    "  FROM catalog.edge"
                    " WHERE cert_sha256 = %(h)s"
                    "    OR (prev_cert_sha256 = %(h)s AND prev_cert_not_after > now())",
                    {"h": verified.sha256},
                )
            ).fetchone()
        if row is None:
            raise ApiError("certificate_invalid", "certificate is not an enrolled edge's")
        if row["state"] == "revoked":
            raise ApiError("edge_revoked", "this edge has been revoked")
        if row["state"] != "active":
            raise ApiError("certificate_invalid", "edge is not active")
        # Belt and braces: the certificate names the same edge as the catalog row.
        if f"urn:omnifile:edge:{row['edge_id']}" not in san_uris(verified.cert):
            raise ApiError("certificate_invalid", "certificate SAN does not name this edge")
        if row["edge_id"] != path_edge_id:
            raise ApiError("edge_mismatch", "the certificate is for another edge")
        not_after = (
            row["cert_not_after"]
            if row["cert_sha256"] == verified.sha256
            else row["prev_cert_not_after"]
        )
        return EdgeIdentity(row["edge_id"], verified.sha256, not_after, verified.cert)


# --- the Teams relay (teams.md section 4.1) ----------------------------------

RELAY_SAN = "urn:omnifile:relay"
EDGE_SAN_PREFIX = "urn:omnifile:edge:"


@dataclass(frozen=True)
class RelayIdentity:
    cert_sha256: bytes

    @property
    def principal(self) -> str:
        return "relay"


class RelayAuthenticator:
    """Certificate -> the relay: issued by the OmniFile CA for client auth, with SAN
    `urn:omnifile:relay` and no edge SAN, and (outside dev mode) one of the pinned SHA-256
    fingerprints in `allowed`, so a relay certificate can be revoked by dropping its pin.
    Same certificate sources (direct TLS or the ingress's forwarded header) as edges.

    Every refusal is `403 caller_not_allowed`, never 401: the relay treats 401 as transient
    (teams.md section 4.4)."""

    def __init__(
        self,
        source: ClientCertSource,
        ca: x509.Certificate,
        *,
        allowed: frozenset[str] | None = None,
    ) -> None:
        self.source = source
        self.ca = ca
        self.allowed = allowed

    def authenticate(self, conn: HTTPConnection) -> RelayIdentity:
        try:
            verified = verify_client_certificate(self.source.certificate(conn), self.ca)
        except ApiError as exc:
            raise ApiError(
                "caller_not_allowed", exc.detail or "no valid relay certificate"
            ) from None
        uris = san_uris(verified.cert)
        if RELAY_SAN not in uris or any(u.startswith(EDGE_SAN_PREFIX) for u in uris):
            raise ApiError("caller_not_allowed", "only the Teams relay may call this path")
        if self.allowed is not None and verified.sha256.hex() not in self.allowed:
            raise ApiError("caller_not_allowed", "this relay certificate is not pinned")
        return RelayIdentity(verified.sha256)


# --- renewal proof of possession (control-api.md section 2, "Renewal") -------

RENEWAL_PROOF_CONTEXT = b"omnifile-renewal-v1"
#: The same proof for a Roles Anywhere certificate (decision 50), under its
#: own context so one can never be replayed as the other.
ROLES_ANYWHERE_PROOF_CONTEXT = b"omnifile-roles-anywhere-v1"
RENEWAL_PROOF_SKEW = timedelta(minutes=5)


def renewal_proof_message(
    edge_id: int, signed_at: str, csr_der: bytes, context: bytes = RENEWAL_PROOF_CONTEXT
) -> bytes:
    """What the edge signs with its CURRENT certificate's key to renew:
    `omnifile-renewal-v1\\n<edge_id>\\n<signed_at>\\n<sha256(CSR DER) hex>`,
    where `signed_at` is exactly the string sent in the request. A Roles
    Anywhere request signs the same with `omnifile-roles-anywhere-v1`."""
    digest = hashlib.sha256(csr_der).hexdigest()
    return b"\n".join([context, str(edge_id).encode(), signed_at.encode(), digest.encode()])


def verify_renewal_proof(
    cert: x509.Certificate,
    edge_id: int,
    csr_pem: str,
    signed_at: str,
    signature_b64: str,
    now: datetime,
    context: bytes = RENEWAL_PROOF_CONTEXT,
) -> None:
    """Check the renewal was signed by the presented certificate's private key,
    over this CSR, within `RENEWAL_PROOF_SKEW` of `now`. A forwarded
    certificate alone (public data) is then not enough to obtain a new one."""
    try:
        at = datetime.fromisoformat(signed_at.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("invalid_request", "proof.signed_at must be an RFC 3339 time") from None
    if at.tzinfo is None:
        raise ApiError("invalid_request", "proof.signed_at must carry a time zone")
    if abs(now - at) > RENEWAL_PROOF_SKEW:
        raise ApiError("certificate_invalid", "renewal proof is too old or from the future")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        sig = base64.b64decode(signature_b64, validate=True)
    except (ValueError, binascii.Error):
        raise ApiError("invalid_request", "unreadable CSR or proof.signature") from None
    msg = renewal_proof_message(
        edge_id, signed_at, csr.public_bytes(serialization.Encoding.DER), context
    )
    pub = cert.public_key()
    try:
        if isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(sig, msg, ec.ECDSA(hashes.SHA256()))
        else:
            raise ApiError("certificate_invalid", "the certificate's key type cannot renew")
    except InvalidSignature:
        raise ApiError(
            "certificate_invalid", "renewal proof is not signed by the presented certificate"
        ) from None


# --- api's own server certificate (direct TLS; threat model T5) ---------------
#
# With TCP ingress nothing terminates TLS in front of api: it does, with
# CERT_REQUIRED against the edge CA, so a caller must hold an edge CA-issued
# key to finish the handshake, and `DirectTls` refuses X-Forwarded-Client-Cert.
# Its server certificate comes from the same CA: each replica makes a P-256
# key in memory and has enrol sign it (`/internal/v0/sign`, purpose
# `api_server`; enrol alone holds the CA key, in Key Vault). Edges pin the edge
# CA for api from their enrolment answer (`api_server_ca_sha256`).


def server_tls_context(ca_pem: bytes) -> ssl.SSLContext:
    """TLS 1.2+ server context that requires a client certificate from the edge CA."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=ca_pem.decode())
    return ctx


def load_chain_in_memory(ctx: ssl.SSLContext, chain_pem: str, key_pem: bytes) -> None:
    """`ctx.load_cert_chain` for PEM held in memory (Python's ssl reads only
    files): through a memfd on Linux, so the key never touches a disk;
    elsewhere (dev) a 0600 file in a private temporary directory, removed at
    once. A later call replaces the certificate for new handshakes."""
    data = chain_pem.encode() + b"\n" + key_pem
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("omnifile-api-tls")
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            ctx.load_cert_chain(f"/proc/self/fd/{fd}")
        finally:
            os.close(fd)
        return
    with tempfile.TemporaryDirectory(prefix="omnifile-api-tls-") as tmp:
        path = os.path.join(tmp, "chain.pem")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        ctx.load_cert_chain(path)


@dataclass(frozen=True)
class ServerCertificate:
    """api's server certificate: the chain to present (leaf, then
    intermediates), its key, and when to renew it."""

    chain_pem: str
    key_pem: bytes
    not_after: datetime
    renew_after: datetime
    sha256: bytes


def check_server_certificate(
    cert: x509.Certificate,
    issuer: x509.Certificate,
    public_key: ec.EllipticCurvePublicKey,
    now: datetime | None = None,
) -> None:
    """What api checks before presenting a certificate enrol issued: issued by
    `issuer`, for this replica's key, a valid serverAuth leaf. Raises ValueError."""
    now = now or datetime.now(UTC)
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise ValueError(f"not issued by the edge CA: {type(exc).__name__}") from None
    if cert.public_key().public_numbers() != public_key.public_numbers():  # type: ignore[union-attr]
        raise ValueError("the certificate is not for this replica's key")
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError("the certificate is expired or not yet valid")
    try:
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("a CA certificate is not a server certificate")
    except x509.ExtensionNotFound:
        pass
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        raise ValueError("no serverAuth usage") from None
    if ExtendedKeyUsageOID.SERVER_AUTH not in eku:
        raise ValueError("no serverAuth usage")


class EnrolServerCertificates:
    """api's server certificates, from enrol (`purpose: api_server`).

    `tokens` gives api's managed-identity token for enrol's audience (or the
    dev shared token): the same caller check as renewals. `ca` is the edge CA
    (OMNIFILE_CA_CERT), which every certificate must chain to."""

    def __init__(self, url: str, tokens, ca: x509.Certificate, *, http=None) -> None:
        import httpx

        self.url = url
        self.tokens = tokens
        self.ca = ca
        self.http = http or httpx.AsyncClient(timeout=10.0, trust_env=False, follow_redirects=False)

    async def issue(self) -> ServerCertificate:
        """A new key and its certificate. Raises RuntimeError or ValueError."""
        import httpx

        key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "omnifile-api")]))
            .sign(key, hashes.SHA256())
        )
        body = {
            "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode(),
            "purpose": "api_server",
            "request_id": f"api-server-{uuid.uuid4().hex}",
        }
        try:
            resp = await self.http.post(
                self.url,
                json=body,
                headers={"Authorization": f"Bearer {await self.tokens.token()}"},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"enrol unreachable: {type(exc).__name__}") from None
        if resp.status_code != 201:
            raise RuntimeError(f"enrol refused api's server certificate: {resp.status_code}")
        out = resp.json()
        cert = x509.load_pem_x509_certificate(out["cert_pem"].encode())
        chain = _pem_certs(out.get("chain_pem") or "")
        check_server_certificate(cert, chain[0] if chain else self.ca, key.public_key())
        if chain:
            # Intermediates must lead to the edge CA the edges pin.
            chain[-1].verify_directly_issued_by(self.ca)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        chain_pem = out["cert_pem"].strip() + "\n" + (out.get("chain_pem") or "").strip()
        return ServerCertificate(
            chain_pem=chain_pem.strip() + "\n",
            key_pem=key_pem,
            not_after=cert.not_valid_after_utc,
            renew_after=datetime.fromisoformat(out["renew_after"].replace("Z", "+00:00")),
            sha256=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest(),
        )

    async def aclose(self) -> None:
        await self.http.aclose()


def _pem_certs(text: str) -> list[x509.Certificate]:
    return [
        x509.load_pem_x509_certificate(m.group(0).encode())
        for m in re.finditer(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.DOTALL
        )
    ]
