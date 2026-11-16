"""Host-side helpers for deploy/images/smoke.sh. Run with the service's Python:

uv --directory service run python deploy/images/smoke_probe.py pki OUT
    a dev CA (ca.key, ca.pem), the relay's client certificate from it (relay.pem,
    relay.key) and an intent public key (intent-keys.json), all generated now and
    readable by the containers' user (test material only, never a real key)
uv ... smoke_probe.py db-create URL     a fresh database on that server; prints its URL
uv ... smoke_probe.py db-drop URL       drop the database URL names
uv ... smoke_probe.py t5 --db URL --enrol URL --api HOST:PORT --name NAME --ca FILE
    the T5 path against the running containers: enrol an edge, check the answer pins
    the edge CA for api, call api over mutual TLS with the server name NAME, and send a
    forged X-Forwarded-Client-Cert, which must be refused. Prints one line per check;
    exits 1 on the first failure.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import socket
import ssl
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from omnifile_service.enrol.ca import FileCASigner, issue_relay_certificate
from omnifile_service.notify.intents import public_key_b64


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _csr(key) -> str:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )


def pki(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ca = FileCASigner.generate("OmniFile smoke CA")
    ca.write(out / "ca.key", out / "ca.pem")
    relay_key = ec.generate_private_key(ec.SECP256R1())
    relay = asyncio.run(issue_relay_certificate(ca, _csr(relay_key), days=1))
    (out / "relay.pem").write_text(relay.cert_pem)
    (out / "relay.key").write_bytes(_key_pem(relay_key))
    intent = Ed25519PrivateKey.generate()
    (out / "intent-keys.json").write_text(json.dumps({"smoke": public_key_b64(intent)}))
    for p in out.iterdir():
        p.chmod(0o644)  # the containers run as uid 10001; this is throwaway test material
    print(f"pki: {out}")


def db_create(url: str) -> None:
    name = f"omnifile_smoke_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(url)
    params["dbname"] = name
    print(make_conninfo(**params))


def db_drop(url: str) -> None:
    params = conninfo_to_dict(url)
    name = params.pop("dbname")
    if not name.startswith("omnifile_smoke_"):
        sys.exit(f"db-drop: {name} is not a smoke database")
    params["dbname"] = "postgres"
    with psycopg.connect(make_conninfo(**params), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _ok(what: str) -> None:
    print(f"ok   {what}")


def _fail(what: str) -> None:
    print(f"FAIL {what}")
    sys.exit(1)


def _request(host: str, port: int, name: str, ctx: ssl.SSLContext, edge_id: int, extra: dict):
    """One POST over TLS with SNI and verification for `name`, connected to host:port."""
    body = json.dumps({"version": "0.0.0"}).encode()
    headers = {
        "Host": name,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Idempotency-Key": f"smoke-{uuid.uuid4().hex}",
        "Connection": "close",
        **extra,
    }
    head = f"POST /v0/edges/{edge_id}/heartbeat HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in headers.items()
    )
    with socket.create_connection((host, port), timeout=20) as raw:
        with ctx.wrap_socket(raw, server_hostname=name) as tls:
            served = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
            tls.sendall(head.encode() + b"\r\n" + body)
            data = b""
            while chunk := tls.recv(65536):
                data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, served, data.split(b"\r\n\r\n", 1)[-1]


def t5(args) -> None:
    ca_pem = Path(args.ca).read_bytes()
    ca = x509.load_pem_x509_certificate(ca_pem)
    token = secrets.token_urlsafe(32)
    with psycopg.connect(args.db, autocommit=True) as conn:
        (edge_id,) = conn.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + interval '1 hour') RETURNING edge_id",
            (f"smoke-{uuid.uuid4().hex[:8]}", hashlib.sha256(token.encode()).digest()),
        ).fetchone()
    key = ec.generate_private_key(ec.SECP256R1())
    body = {
        "token": token,
        "csr_pem": _csr(key),
        "facts": {"hostname": "smoke", "version": "0.0.0"},
    }
    resp = httpx.post(
        f"{args.enrol}/v0/enrol", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    if resp.status_code != 201:
        _fail(f"enrol answered {resp.status_code}: {resp.text[:300]}")
    answer = resp.json()
    _ok(f"enrol: edge {answer['edge_id']} enrolled")
    pin = hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest()
    if answer.get("api_server_ca_sha256") != pin:
        _fail(f"the answer does not pin the edge CA for api: {answer.get('api_server_ca_sha256')}")
    _ok("enrol: the answer pins the edge CA for api (api_server_ca_sha256)")

    work = Path(args.ca).parent
    cert_path, key_path = work / "edge.pem", work / "edge.key"
    cert_path.write_text(answer["cert_pem"])
    key_path.write_bytes(_key_pem(key))
    ctx = ssl.create_default_context(cadata=ca_pem.decode())
    ctx.load_cert_chain(str(cert_path), str(key_path))
    host, port = args.api.rsplit(":", 1)
    status, served, text = _request(host, int(port), args.name, ctx, edge_id, {})
    served.verify_directly_issued_by(ca)
    eku = served.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    names = served.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if status != 200:
        _fail(f"api heartbeat over mutual TLS answered {status}: {text[:300]!r}")
    _ok(f"api: served a certificate from enrol for {names.get_values_for_type(x509.DNSName)}")
    _ok(f"api: EKU {[e._name for e in eku]}; heartbeat over mutual TLS is 200")
    cert_der = x509.load_pem_x509_certificate(answer["cert_pem"].encode()).public_bytes(
        serialization.Encoding.DER
    )
    forged = (
        f"Hash={hashlib.sha256(cert_der).hexdigest()};"
        f'Cert="{quote(answer["cert_pem"], safe="")}";Chain="";'
    )
    status, _, text = _request(
        host, int(port), args.name, ctx, edge_id, {"X-Forwarded-Client-Cert": forged}
    )
    if status != 401:
        _fail(f"api took a forged X-Forwarded-Client-Cert: {status}")
    _ok("api: a forged X-Forwarded-Client-Cert is refused (401)")
    plain = ssl.create_default_context(cadata=ca_pem.decode())
    try:
        _request(host, int(port), args.name, plain, edge_id, {"X-Forwarded-Client-Cert": forged})
    except (ssl.SSLError, ConnectionError, IndexError, ValueError):
        _ok("api: no client certificate, no request")
    else:
        _fail("api answered a caller with no client certificate")


def main() -> None:
    p = argparse.ArgumentParser(prog="smoke_probe.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pki").add_argument("out")
    sub.add_parser("db-create").add_argument("url")
    sub.add_parser("db-drop").add_argument("url")
    t = sub.add_parser("t5")
    for flag in ("--db", "--enrol", "--api", "--name", "--ca"):
        t.add_argument(flag, required=True)
    args = p.parse_args()
    if args.cmd == "pki":
        pki(Path(args.out))
    elif args.cmd == "db-create":
        db_create(args.url)
    elif args.cmd == "db-drop":
        db_drop(args.url)
    else:
        t5(args)


if __name__ == "__main__":
    main()
