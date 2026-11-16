"""Run the `enrol` app, or its dev helpers.

  python -m omnifile_service.enrol                      serve
  python -m omnifile_service.enrol init-dev-ca KEY CERT make a dev CA (files)
  python -m omnifile_service.enrol init-kv-ca OUT      the production CA certificate, self-
                                                        signed by the Key Vault key (once)
  python -m omnifile_service.enrol add-edge NAME --url URL --server-ca CERT
                                                        dev stand-in for the portal: a pending
                                                        edge and its one-time enrolment bundle
  python -m omnifile_service.enrol issue-relay-cert --csr-b64 B64 [--days N]
                                                        the Teams relay's client certificate,
                                                        signed by the CA key (Key Vault): prints
                                                        the PEM, then "SHA-256: <hex>" for api's
                                                        OMNIFILE_TEAMS_RELAY_CERT_SHA256. B64 is
                                                        the base64 of the CSR's PEM (ECDSA P-256;
                                                        its subject and SANs are ignored)

Without OMNIFILE_CA_CERT (a fresh deployment, before README step 7), `serve`
waits: /healthz answers 200 and every other path 503, so the container stays
up for `az containerapp exec ... init-kv-ca`.

Environment (serve):
  OMNIFILE_PG_HOST / _DATABASE / _USER / _SSLMODE, AZURE_CLIENT_ID
                                        the catalog on Azure, Entra sign-in as the app's
                                        managed identity (catalog/connect.py); or, dev,
  OMNIFILE_DATABASE_URL                 a libpq connection string
  OMNIFILE_KEYVAULT_URI / OMNIFILE_CA_KEY_NAME
                                        production: the CA key in Key Vault (sign only), used
                                        with the app's managed identity (AZURE_CLIENT_ID)
  OMNIFILE_CA_CERT                      the CA certificate (PEM file) that key belongs to,
                                        from init-kv-ca; checked against the key at start
  OMNIFILE_DEV_CA_KEY / OMNIFILE_DEV_CA_CERT
                                        dev CA files, only with OMNIFILE_DEV_MODE=1
  OMNIFILE_ENROL_DEV_TOKEN              dev only, and only with OMNIFILE_DEV_MODE=1: the shared
                                        token api sends to /internal/v0/sign
  OMNIFILE_DEV_MODE                     "1" allows the dev shortcuts; refused otherwise
  OMNIFILE_ENROL_TENANT_ID / _AUDIENCE / _ALLOWED_CALLER_OID
                                        production: the Entra token check
  OMNIFILE_ENROL_CRL_CALLER_OID         the worker's managed identity, the only caller of
                                        /internal/v0/crl (decision 50); unset: nobody
  OMNIFILE_ENROL_TRUSTED_INGRESS        CIDRs of the ingress: only from these peers is
                                        X-Forwarded-For used for the caller's address
  OMNIFILE_ENROL_SIGN_SOURCES           CIDRs /internal/v0/sign may be called from (api's
                                        subnet). Set it in production; ingress should restrict
                                        the path too
  OMNIFILE_API_URL                      returned to edges as api_url
  OMNIFILE_ENROL_API_SERVER_NAMES       comma-separated DNS names of api (TCP ingress, threat
                                        model T5): enrol signs api's server certificate for
                                        them (/internal/v0/sign, purpose api_server), and the
                                        enrolment answer tells edges to pin the edge CA for api
  OMNIFILE_ENROL_HOST / PORT            default 0.0.0.0:8080
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import logging
import os
import sys

import httpx
import uvicorn
from cryptography import x509

from ..api.clientauth import parse_networks
from ..api.db import DEFAULT_POOL_MAX, Catalog
from ..catalog.connect import Db, connect, from_env_or_exit, pool_max_or_exit
from . import bundle
from .app import EnrolServices, create_enrol_app
from .ca import RELAY_CERT_DAYS, CsrError, FileCASigner, issue_relay_certificate, pem
from .callers import EntraCallerVerifier, SharedTokenVerifier
from .kvsigner import (
    DeferredKeyVaultSigner,
    KeyVaultError,
    KeyVaultKey,
    fingerprint,
    managed_identity_token,
    self_signed_ca,
)
from .waiting import create_waiting_app


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


def _need(name: str) -> str:
    value = _env(name)
    if value is None:
        sys.exit(f"{name} is required")
    return value


def kv_key(http: httpx.AsyncClient, token=None) -> KeyVaultKey:
    return KeyVaultKey(
        _need("OMNIFILE_KEYVAULT_URI"),
        _need("OMNIFILE_CA_KEY_NAME"),
        token or managed_identity_token(_env("AZURE_CLIENT_ID")),
        http,
    )


def kv_signer(http_factory=None, token_factory=None) -> DeferredKeyVaultSigner:
    """The Key Vault signer. Its client and credential are made, and the vault
    key checked against the CA certificate, when enrol starts serving, in the
    serving event loop (`DeferredKeyVaultSigner`). The factories are for tests."""
    with open(_need("OMNIFILE_CA_CERT"), "rb") as fh:
        ca = x509.load_pem_x509_certificate(fh.read())

    def open_key() -> KeyVaultKey:
        http = (http_factory or (lambda: httpx.AsyncClient(timeout=15.0, follow_redirects=False)))()
        return kv_key(http, token_factory() if token_factory else None)

    return DeferredKeyVaultSigner(ca, open_key)


def api_server_names(raw: str | None) -> tuple[str, ...]:
    """OMNIFILE_ENROL_API_SERVER_NAMES: DNS names, lower-cased; no wildcards, IPs or URLs."""
    names = tuple(n.strip().lower().rstrip(".") for n in (raw or "").split(",") if n.strip())
    for n in names:
        labels = n.split(".")
        if len(labels) < 2 or not all(
            label and len(label) <= 63 and label.replace("-", "a").isalnum() and label.isascii()
            for label in labels
        ):
            sys.exit(f"OMNIFILE_ENROL_API_SERVER_NAMES: {n!r} is not a DNS name")
        if all(label.isdigit() for label in labels):
            sys.exit(f"OMNIFILE_ENROL_API_SERVER_NAMES: {n!r} is an address, not a name")
    return names


def build():
    if (
        not _env("OMNIFILE_DEV_CA_KEY")
        and _env("OMNIFILE_KEYVAULT_URI")
        and _env("OMNIFILE_CA_KEY_NAME")
        and not _env("OMNIFILE_CA_CERT")
    ):
        # A fresh deployment: the CA certificate is made from inside this container.
        return create_waiting_app(
            "enrol",
            "no CA certificate (OMNIFILE_CA_CERT): run init-kv-ca, then redeploy with"
            " edgeCaCertPem (deploy/azure/README.md, step 7)",
        )
    if _env("OMNIFILE_DEV_CA_KEY"):
        # A CA key in a file is for dev and tests only (M7 review).
        if _env("OMNIFILE_DEV_MODE") != "1":
            sys.exit("OMNIFILE_DEV_CA_KEY is a dev setting: it needs OMNIFILE_DEV_MODE=1")
        signer = FileCASigner.from_files(
            _need("OMNIFILE_DEV_CA_KEY"), _need("OMNIFILE_DEV_CA_CERT")
        )
    elif _env("OMNIFILE_KEYVAULT_URI") and _env("OMNIFILE_CA_KEY_NAME"):
        signer = kv_signer()
    else:
        sys.exit(
            "no CA signer: set OMNIFILE_KEYVAULT_URI, OMNIFILE_CA_KEY_NAME and OMNIFILE_CA_CERT"
            " (or the dev CA files in dev mode)"
        )
    if dev := _env("OMNIFILE_ENROL_DEV_TOKEN"):
        if _env("OMNIFILE_DEV_MODE") != "1":
            sys.exit("OMNIFILE_ENROL_DEV_TOKEN is a dev setting: it needs OMNIFILE_DEV_MODE=1")
        callers = SharedTokenVerifier(dev)
        crl_callers = SharedTokenVerifier(dev, caller="dev-worker")
    else:
        callers = EntraCallerVerifier(
            _need("OMNIFILE_ENROL_TENANT_ID"),
            _need("OMNIFILE_ENROL_AUDIENCE"),
            _need("OMNIFILE_ENROL_ALLOWED_CALLER_OID"),
        )
        crl_callers = None
        if worker := _env("OMNIFILE_ENROL_CRL_CALLER_OID"):
            crl_callers = EntraCallerVerifier(
                _need("OMNIFILE_ENROL_TENANT_ID"), _need("OMNIFILE_ENROL_AUDIENCE"), worker
            )
    services = EnrolServices(
        catalog=Catalog(from_env_or_exit("enrol"), max_size=pool_max_or_exit(DEFAULT_POOL_MAX)),
        signer=signer,
        callers=callers,
        api_url=_env("OMNIFILE_API_URL"),
        trusted_ingress=_cidrs("OMNIFILE_ENROL_TRUSTED_INGRESS"),
        sign_sources=_cidrs("OMNIFILE_ENROL_SIGN_SOURCES"),
        crl_callers=crl_callers,
        api_server_names=api_server_names(_env("OMNIFILE_ENROL_API_SERVER_NAMES")),
    )
    return create_enrol_app(services)


def _cidrs(name: str):
    try:
        return parse_networks((_env(name) or "").split(","))
    except ValueError as exc:
        sys.exit(f"{name}: {exc}")


def add_edge(conninfo: Db, name: str, url: str, server_ca_pem: bytes, hours: int = 24) -> str:
    """Create a pending edge and return its enrolment bundle (shown once)."""
    token, token_sha = bundle.new_token()
    with connect(conninfo) as conn:
        conn.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + make_interval(hours => %s))"
            " ON CONFLICT (name) DO UPDATE"
            "   SET enrolment_token_sha256 = EXCLUDED.enrolment_token_sha256,"
            "   enrolment_expires_at = EXCLUDED.enrolment_expires_at"
            " WHERE catalog.edge.state = 'pending'",
            (name, token_sha, hours),
        )
    return bundle.make(url, bundle.ca_sha256(server_ca_pem), token)


def relay_certificate(open_signer, csr_b64: str, days: int) -> str:
    """The relay's certificate PEM and its SHA-256 line, or exit with why not.
    `open_signer` is an async callable: the Key Vault signer is opened and used
    in one event loop."""
    try:
        csr = base64.b64decode("".join(csr_b64.split()), validate=True).decode()
    except (ValueError, binascii.Error, UnicodeDecodeError):
        sys.exit("issue-relay-cert: --csr-b64 is not base64 of a PEM CSR")

    async def go():
        return await issue_relay_certificate(await open_signer(), csr, days)

    try:
        issued = asyncio.run(go())
    except CsrError as e:
        sys.exit(f"issue-relay-cert: {e}")
    except KeyVaultError as e:
        sys.exit(f"issue-relay-cert: {e}")
    return f"{issued.cert_pem}SHA-256: {issued.sha256.hex()}"


def audit_relay_certificate(conninfo: Db, out: str) -> None:
    """Every relay certificate issued is audited, as edge certificates are."""
    import json

    sha = out.rsplit("SHA-256: ", 1)[1].strip()
    with connect(conninfo) as conn:
        conn.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:enrol', 'relay.certificate.sign', 'allow', %s::jsonb)",
            (json.dumps({"cert_sha256": sha}),),
        )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m omnifile_service.enrol")
    sub = p.add_subparsers(dest="cmd")
    ca = sub.add_parser("init-dev-ca")
    ca.add_argument("key")
    ca.add_argument("cert")
    kv = sub.add_parser("init-kv-ca", help="self-sign the CA certificate with the Key Vault key")
    kv.add_argument("out", help="where to write the CA certificate (PEM)")
    kv.add_argument("--name", default="OmniFile edge CA")
    kv.add_argument("--days", type=int, default=3650)
    ae = sub.add_parser("add-edge")
    ae.add_argument("name")
    ae.add_argument("--url", required=True, help="enrol URL the edge calls")
    ae.add_argument("--server-ca", required=True, help="PEM of the CA that issued enrol's TLS cert")
    ae.add_argument("--hours", type=int, default=24)
    rc = sub.add_parser("issue-relay-cert", help="sign the Teams relay's client certificate")
    rc.add_argument("--csr-b64", required=True, help="base64 of the relay CSR's PEM")
    rc.add_argument("--days", type=int, default=RELAY_CERT_DAYS)
    args = p.parse_args(argv)
    if args.cmd == "init-dev-ca":
        FileCASigner.generate().write(args.key, args.cert)
        print(f"wrote {args.key} (0600) and {args.cert}")
        return
    if args.cmd == "init-kv-ca":

        async def bootstrap() -> x509.Certificate:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
                return await self_signed_ca(kv_key(http), args.name, args.days)

        try:
            cert = asyncio.run(bootstrap())
        except KeyVaultError as e:
            sys.exit(f"init-kv-ca: {e}")
        with open(args.out, "x") as fh:
            fh.write(pem(cert))
        print(f"wrote {args.out}: {cert.subject.rfc4514_string()}, SHA-256 {fingerprint(cert)}")
        print("Give it to api and enrol (deploy/azure/README.md, step 7).")
        return
    if args.cmd == "issue-relay-cert":
        if _env("OMNIFILE_DEV_CA_KEY"):
            if _env("OMNIFILE_DEV_MODE") != "1":
                sys.exit("OMNIFILE_DEV_CA_KEY is a dev setting: it needs OMNIFILE_DEV_MODE=1")
            dev = FileCASigner.from_files(
                _need("OMNIFILE_DEV_CA_KEY"), _need("OMNIFILE_DEV_CA_CERT")
            )

            async def open_signer():
                return dev
        else:
            deferred = kv_signer()

            async def open_signer():
                await deferred.start()
                return deferred

        out = relay_certificate(open_signer, args.csr_b64, args.days)
        audit_relay_certificate(from_env_or_exit("enrol"), out)
        print(out)
        return
    if args.cmd == "add-edge":
        with open(args.server_ca, "rb") as fh:
            print(add_edge(from_env_or_exit("enrol"), args.name, args.url, fh.read(), args.hours))
        return
    logging.basicConfig(level=_env("OMNIFILE_ENROL_LOG_LEVEL") or "INFO")
    uvicorn.run(
        build(),
        host=_env("OMNIFILE_ENROL_HOST") or "0.0.0.0",
        port=int(_env("PORT") or "8080"),
        server_header=False,
    )


if __name__ == "__main__":
    main()
