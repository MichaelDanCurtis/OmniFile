"""Fixtures for the api and enrol tests.

Needs a Postgres 16+ server (OMNIFILE_TEST_DATABASE_URL, as for tests/catalog;
skipped without it). Each test session migrates one throwaway database; tests
make their own edges and datasets, so they don't interfere.

The pieces:
- a test CA (`FileCASigner.generate()`), the `enrol` app, and the `api` app,
  both on that database;
- the sim Azure Blob HTTP facade (`backends/sim_http`) in process; the api
  mints real user-delegation SAS tokens for it and HEADs it over HTTP;
- `EdgeClient`: an enrolled edge that calls the api with its certificate in
  X-Forwarded-Client-Cert, and checks every request and response against
  control-api.yaml (spec.py).
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from starlette.testclient import TestClient

from omnifile_service.api.app import create_app
from omnifile_service.api.clientauth import (
    EdgeAuthenticator,
    ForwardedClientCert,
    renewal_proof_message,
)
from omnifile_service.api.context import ApiSettings, Services
from omnifile_service.api.dataplane import DataPlanes, SimHttpDataPlane
from omnifile_service.api.db import Catalog
from omnifile_service.api.signclient import HttpSignClient, StaticToken
from omnifile_service.backends.base import SystemClock
from omnifile_service.backends.sim import SimBackend
from omnifile_service.backends.sim.crc64 import crc64nvme
from omnifile_service.backends.sim_http import BlobFacade, azure_crc64
from omnifile_service.backends.sim_http import create_app as create_facade
from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.app import EnrolServices, FailureLimiter, create_enrol_app
from omnifile_service.enrol.ca import FileCASigner
from omnifile_service.enrol.callers import SharedTokenVerifier

from . import spec

ENV = "OMNIFILE_TEST_DATABASE_URL"
SIGN_TOKEN = "dev-sign-token-" + "x" * 16
SIM_BASE = "http://sim.test"
#: The TestClient's peer address for the api, inside the trusted ingress range.
INGRESS_PEER = ("10.0.0.10", 50000)
INGRESS_CIDR = "10.0.0.0/24"


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set; api tests need a Postgres 16+ server")
    name = f"omnifile_api_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(server)
    params["dbname"] = name
    url = make_conninfo(**params)
    migrate(url)
    yield url
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def ca() -> FileCASigner:
    return FileCASigner.generate("OmniFile test CA")


@pytest.fixture
def facade() -> BlobFacade:
    return BlobFacade(SimBackend.azure_two_account(SystemClock()))


@pytest.fixture
def facade_client(facade: BlobFacade) -> TestClient:
    return TestClient(create_facade(facade), base_url=SIM_BASE)


def make_enrol_app(db_url: str, ca: FileCASigner):
    services = EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=4),
        signer=ca,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        api_url="https://api.test",
        limiter=FailureLimiter(limit=5),
    )
    return create_enrol_app(services)


@pytest.fixture
def enrol_app(db_url: str, ca: FileCASigner):
    return make_enrol_app(db_url, ca)


@pytest.fixture
def enrol_client(enrol_app) -> Iterator[TestClient]:
    with TestClient(enrol_app, base_url="https://enrol.test") as c:
        yield c


@pytest.fixture
def sql(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        yield conn


@dataclass
class Api:
    client: TestClient
    services: Services
    backend_id: int


@pytest.fixture
def api(db_url: str, ca: FileCASigner, facade: BlobFacade, sql) -> Iterator[Api]:
    (backend_id,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'sim', %s, false) RETURNING backend_id",
        (f"sim-{uuid.uuid4().hex[:8]}", '{"flavour": "azure_blob", "container": "live/data"}'),
    ).fetchone()
    with serve_api(db_url, ca, facade, backend_id) as served:
        yield served


@contextmanager
def serve_api(db_url: str, ca: FileCASigner, facade: BlobFacade, backend_id: int) -> Iterator[Api]:
    """The api app (and its in-process enrol signer) on `db_url`, whose
    `backend_id` row is the facade's sim. The catalog restore drill serves a
    second one on the restored database."""
    catalog = Catalog(as_app(db_url, "api"), max_size=6)
    planes = DataPlanes()
    facade_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_facade(facade)), base_url=SIM_BASE
    )
    planes.add(backend_id, SimHttpDataPlane(SIM_BASE, facade.key, http=facade_http))
    # api calls enrol in process. Its own instance: a pool belongs to one event loop.
    inner_enrol = make_enrol_app(db_url, ca)
    enrol_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=inner_enrol), base_url="https://enrol.test"
    )
    signer = HttpSignClient(
        "https://enrol.test/internal/v0/sign", StaticToken(SIGN_TOKEN), http=enrol_http
    )
    services = Services(
        catalog=catalog,
        auth=EdgeAuthenticator(
            ForwardedClientCert(trust_ingress=True, trusted_proxies=(INGRESS_CIDR,)),
            ca.ca_certificate,
            catalog,
        ),
        planes=planes,
        signer=signer,
        settings=ApiSettings(feed_poll_seconds=0.05, default_resident_bytes=1024),
        closers=[inner_enrol.state.svc.catalog.close, facade_http.aclose],
    )
    app = create_app(services)
    with TestClient(app, base_url="https://api.test", client=INGRESS_PEER) as client:
        yield Api(client, services, backend_id)


# --- PKI helpers -------------------------------------------------------------


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def csr_pem(key: ec.EllipticCurvePrivateKey, cn: str = "whatever") -> str:
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def xfcc(cert_pem: str, *, encode: str = "url") -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    value = quote(cert_pem, safe="") if encode == "url" else cert_pem.replace("\n", "")
    return f'Hash={digest};Cert="{value}";Chain="";'


def pending_edge(sql: psycopg.Connection, *, expires: str = "1 day") -> tuple[int, str]:
    token = secrets.token_urlsafe(32)
    (edge_id,) = sql.execute(
        "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
        " VALUES (%s, %s, now() + %s::interval) RETURNING edge_id",
        (f"edge-{uuid.uuid4().hex[:8]}", hashlib.sha256(token.encode()).digest(), expires),
    ).fetchone()
    return edge_id, token


def enrol_body(token: str, key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    return {
        "token": token,
        "csr_pem": csr_pem(key),
        "facts": {"hostname": "edge1", "os": "ubuntu 26.04", "kernel": "7.0", "version": "0.1.0"},
    }


def idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


# --- the edge as a client ----------------------------------------------------


@dataclass
class EdgeClient:
    client: TestClient
    edge_id: int
    key: ec.EllipticCurvePrivateKey
    cert_pem: str
    log: list[tuple[str, str, int]] = field(default_factory=list)

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"X-Forwarded-Client-Cert": xfcc(self.cert_pem), "OmniFile-Edge-Version": "0.1.0"}
        h.update(extra or {})
        return h

    def call(
        self,
        method: str,
        template: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
        check: bool = True,
        **path_params: Any,
    ) -> httpx.Response:
        path = template.replace("{edge_id}", str(path_params.pop("edge_id", self.edge_id)))
        for name, value in path_params.items():
            path = path.replace("{" + name + "}", str(value))
        h = self.headers(headers)
        if method == "POST":
            h.setdefault("Idempotency-Key", key or str(uuid.uuid4()))
            if check:
                spec.check_request(method, template, json)
        resp = self.client.request(method, path, json=json, params=params, headers=h)
        if check:
            spec.check_response(
                method,
                template,
                resp.status_code,
                resp.headers.get("content-type", ""),
                resp.content,
            )
        self.log.append((method, template, resp.status_code))
        return resp

    def post(self, template: str, json: Any = None, **kw: Any) -> httpx.Response:
        return self.call("POST", template, json=json, **kw)

    def get(self, template: str, params: dict[str, Any] | None = None, **kw: Any) -> httpx.Response:
        return self.call("GET", template, params=params, **kw)


@dataclass
class World:
    edge: EdgeClient
    dataset_id: int
    root_id: int
    share_id: int
    backend_id: int
    #: Runs the worker's `verify` queue (upload read-back) over the facade.
    verify: Callable[[], list[int]] = lambda: []


def run_verify_queue(db_url: str, facade: BlobFacade) -> list[int]:
    """What the worker's `verify` consumer does, for every queued message:
    read the blob back from the facade's sim and set `verified`. Returns the
    location ids handled."""
    import asyncio
    from datetime import UTC, datetime

    from omnifile_service.worker.queue import Message
    from omnifile_service.worker.verify import handle_verify

    class AnyId:  # every backend id is the facade's sim
        def get(self, backend_id: int):
            return facade.sim

    async def go() -> list[int]:
        done: list[int] = []
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            rows = await (
                await conn.execute("SELECT msg_id, message FROM pgmq.q_verify ORDER BY msg_id")
            ).fetchall()
            for msg_id, body in rows:
                now = datetime.now(UTC)
                async with conn.transaction():
                    await handle_verify(
                        Message(msg_id, 1, now, now, body),
                        conn,
                        registry=AnyId(),  # type: ignore[arg-type]
                    )
                    await conn.execute("SELECT pgmq.delete('verify', %s::bigint)", (msg_id,))
                done.append(int(body["location_id"]))
        return done

    return asyncio.run(go())


@pytest.fixture
def enrol_edge(api: Api, enrol_client: TestClient, sql) -> Callable[[], EdgeClient]:
    def make() -> EdgeClient:
        edge_id, token = pending_edge(sql)
        key = new_key()
        resp = enrol_client.post("/v0/enrol", json=enrol_body(token, key), headers=idem())
        assert resp.status_code == 201, resp.text
        return EdgeClient(api.client, edge_id, key, resp.json()["cert_pem"])

    return make


@pytest.fixture
def make_world(api: Api, enrol_edge, sql, db_url: str, facade: BlobFacade) -> Callable[..., World]:
    def make(*, on_open: str = "wait", threshold: int | None = None) -> World:
        edge = enrol_edge()
        tag = uuid.uuid4().hex[:8]
        (dataset_id,) = sql.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id)"
            " VALUES (%s, %s, %s) RETURNING dataset_id",
            (f"ds-{tag}", api.backend_id, edge.edge_id),
        ).fetchone()
        (root_id,) = sql.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
            " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
            (dataset_id,),
        ).fetchone()
        (share_id,) = sql.execute(
            "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, nfs_sec,"
            " owners_group_sid, on_open, wait_threshold_minutes)"
            " VALUES (%s, %s, %s, ARRAY['nfs4'], ARRAY['krb5'], 'S-1-5-21-1-2-3-512', %s, %s)"
            " RETURNING share_id",
            (dataset_id, edge.edge_id, f"share-{tag}", on_open, threshold),
        ).fetchone()
        return World(
            edge,
            dataset_id,
            root_id,
            share_id,
            api.backend_id,
            verify=lambda: run_verify_queue(db_url, facade),
        )

    return make


# --- data helpers --------------------------------------------------------------

E = "/v0/edges/{edge_id}"


def create_file(w: World, name: str, *, parent: int | None = None, kind: str = "file") -> int:
    op = {
        "op_id": f"op-{uuid.uuid4().hex}",
        "op": "create",
        "parent": {"file_id": str(parent or w.root_id)},
        "name": name,
        "kind": kind,
        "attrs": {"mode": 0o644, "uid": 1000, "gid": 1000, "mtime_ns": "1700000000000000000"},
    }
    resp = w.edge.post(f"{E}/changes", {"ops": [op]})
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["status"] == "applied", result
    return int(result["file_id"])


def hashes_of(data: bytes) -> dict[str, Any]:
    """The service stores BLAKE3 and never recomputes it, so a stand-in of the
    right shape will do here."""
    return {"blake3": hashlib.sha256(data).hexdigest(), "crc64nvme": f"{crc64nvme(data):016x}"}


def block_id(i: int) -> str:
    return base64.b64encode(f"{i:08d}".encode()).decode()


def blocks_of(data: bytes, block: int) -> list[dict[str, Any]]:
    """The `blocks` an edge declares at complete for `upload_blocks(data, block)`."""
    out = []
    for i in range(0, max(len(data), 1), block):
        chunk = data[i : i + block]
        out.append(
            {
                "id": block_id(i // block),
                "length": len(chunk),
                "crc64nvme": f"{crc64nvme(chunk):016x}",
            }
        )
    return out


def complete_body(data: bytes, block: int = 4096, **extra: Any) -> dict[str, Any]:
    return {"size": len(data), **hashes_of(data), "blocks": blocks_of(data, block), **extra}


def renewal_body(edge: EdgeClient, csr: str, *, signed_at: str | None = None) -> dict[str, Any]:
    """`POST /certificate` with proof of possession of the edge's current key."""
    at = signed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    der = x509.load_pem_x509_csr(csr.encode()).public_bytes(serialization.Encoding.DER)
    msg = renewal_proof_message(edge.edge_id, at, der)
    sig = edge.key.sign(msg, ec.ECDSA(hashes.SHA256()))
    return {"csr_pem": csr, "proof": {"signed_at": at, "signature": base64.b64encode(sig).decode()}}


def upload_blocks(facade_client: TestClient, url: str, data: bytes, block: int) -> str | None:
    """What the edge's uploader does: Put Block with x-ms-content-crc64, then
    Put Block List with no tier. Returns the new blob version."""
    ids = []
    for i in range(0, max(len(data), 1), block):
        chunk = data[i : i + block]
        bid = block_id(i // block)
        ids.append(bid)
        r = facade_client.put(
            f"{url}&comp=block&blockid={quote(bid)}",
            content=chunk,
            headers={"x-ms-content-crc64": azure_crc64(chunk), "x-ms-version": "2026-04-06"},
        )
        assert r.status_code == 201, r.text
    body = "<?xml version='1.0' encoding='utf-8'?><BlockList>"
    body += "".join(f"<Latest>{b}</Latest>" for b in ids) + "</BlockList>"
    r = facade_client.put(
        f"{url}&comp=blocklist", content=body.encode(), headers={"x-ms-version": "2026-04-06"}
    )
    assert r.status_code == 201, r.text
    return r.headers.get("x-ms-version-id")
