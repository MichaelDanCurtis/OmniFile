"""control-api.yaml and the code say the same thing about enrolment and certificates.

`test_openapi_conformance` walks the api's routes through `EdgeClient`, which checks each
request and response against the spec. Two gaps that let drift through, closed here:
- enrol's routes weren't walked at all (its answers were never checked);
- a response may carry a field the spec doesn't declare (response schemas allow extra fields).

So: every enrol route is called with each purpose, its requests and responses checked against
the spec, and every field it answers must be declared (`undeclared`); the same for api's
`/certificate` and `/aws-certificate` answers. The code's request models (`SIGN_FIELDS`,
`API_SERVER_FIELDS`, `SIGN_PURPOSES`) must equal the spec's, and a field the spec requires,
left out, is a 400. A field added on one side only fails here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from cryptography import x509
from starlette.testclient import TestClient

from omnifile_service.api.db import Catalog
from omnifile_service.catalog.privileges import as_app
from omnifile_service.enrol.app import (
    API_SERVER_FIELDS,
    SIGN_FIELDS,
    SIGN_PURPOSES,
    EnrolServices,
    FailureLimiter,
    create_enrol_app,
)
from omnifile_service.enrol.ca import cert_sha256
from omnifile_service.enrol.callers import SharedTokenVerifier

from . import spec
from .conftest import (
    SIGN_TOKEN,
    E,
    csr_pem,
    enrol_body,
    idem,
    new_key,
    pending_edge,
    renewal_body,
)
from .test_roles_anywhere import ra_body

AUTH = {"Authorization": f"Bearer {SIGN_TOKEN}"}
SIGN = "/internal/v0/sign"


def _deref(schema: dict[str, Any]) -> dict[str, Any]:
    return spec._deref(schema)


def declared(schema: dict[str, Any]) -> set[str]:
    """Every top-level property a schema declares, through $ref, allOf and oneOf."""
    schema = _deref(schema)
    names = set(schema.get("properties", {}))
    for key in ("allOf", "oneOf", "anyOf"):
        for part in schema.get(key, []):
            names |= declared(part)
    return names


def request_schema(method: str, template: str) -> dict[str, Any]:
    op = spec.operation(method, template)
    return _deref(op["requestBody"]["content"]["application/json"]["schema"])


def response_schema(method: str, template: str, status: int) -> dict[str, Any]:
    response = _deref(spec.operation(method, template)["responses"][str(status)])
    return _deref(response["content"]["application/json"]["schema"])


def undeclared(method: str, template: str, status: int, body: dict) -> set[str]:
    return set(body) - declared(response_schema(method, template, status))


def checked(client: TestClient, method: str, template: str, path: str, body, **kw):
    """One call, its request and response checked against the spec, every answered field
    declared."""
    spec.check_request(method, template, body)
    resp = client.request(method, path, json=body, **kw)
    spec.check_response(
        method, template, resp.status_code, resp.headers.get("content-type", ""), resp.content
    )
    if resp.status_code < 300 and resp.content:
        extra = undeclared(method, template, resp.status_code, resp.json())
        assert not extra, f"{method} {template} answers fields the spec doesn't declare: {extra}"
    return resp


@pytest.fixture
def enrol(db_url, ca):
    services = EnrolServices(
        catalog=Catalog(as_app(db_url, "enrol"), max_size=2),
        signer=ca,
        callers=SharedTokenVerifier(SIGN_TOKEN),
        api_url="https://omni-api.example.internal:8443",
        limiter=FailureLimiter(limit=50),
        crl_callers=SharedTokenVerifier(SIGN_TOKEN, caller="dev-worker"),
        api_server_names=("omni-api.example.internal",),
    )
    with TestClient(create_enrol_app(services), base_url="https://enrol.test") as c:
        yield c


# --- the request models -------------------------------------------------------------------


def test_the_sign_request_models_are_the_specs() -> None:
    schema = request_schema("POST", SIGN)
    branches = [_deref(b) for b in schema["oneOf"]]
    assert set().union(*(set(b["properties"]) for b in branches)) == SIGN_FIELDS
    (server,) = [b for b in branches if b["properties"]["purpose"].get("const") == "api_server"]
    assert set(server["properties"]) == API_SERVER_FIELDS
    purposes = set()
    for b in branches:
        p = b["properties"]["purpose"]
        purposes |= set(p.get("enum", [])) | ({p["const"]} if "const" in p else set())
    assert purposes == set(SIGN_PURPOSES)
    assert all(b.get("additionalProperties") is False for b in branches)


@pytest.mark.parametrize("purpose", ["renewal", "api_server"])
def test_every_field_the_spec_requires_is_required(enrol, sql, purpose) -> None:
    schema = request_schema("POST", SIGN)
    (branch,) = [
        _deref(b)
        for b in schema["oneOf"]
        if purpose in _deref(b)["properties"]["purpose"].get("enum", [])
        or _deref(b)["properties"]["purpose"].get("const") == purpose
    ]
    body = {
        "edge_id": "1",
        "csr_pem": csr_pem(new_key()),
        "purpose": purpose,
        "current_cert_sha256": "a" * 64,
        "request_id": f"spec-{uuid.uuid4().hex}",
    }
    body = {k: v for k, v in body.items() if k in branch["properties"]}
    for field in branch["required"]:
        partial = {k: v for k, v in body.items() if k != field}
        resp = enrol.post(SIGN, json=partial, headers=AUTH)
        assert resp.status_code == 400, (field, resp.text)


@pytest.mark.parametrize("purpose", ["renewal", "roles_anywhere", "api_server"])
def test_every_field_the_code_requires_is_required_in_the_spec(enrol, sql, purpose) -> None:
    """The converse (review of #34): a spec looser than the code, say one that dropped
    `request_id` from `required`, passed. A field whose absence the code refuses with a 400
    must be in the branch's `required`."""
    schema = request_schema("POST", SIGN)
    (branch,) = [
        _deref(b)
        for b in schema["oneOf"]
        if purpose in _deref(b)["properties"]["purpose"].get("enum", [])
        or _deref(b)["properties"]["purpose"].get("const") == purpose
    ]
    body = {
        "edge_id": "1",
        "csr_pem": csr_pem(new_key()),
        "purpose": purpose,
        "current_cert_sha256": "a" * 64,
        "request_id": f"spec-{uuid.uuid4().hex}",
    }
    body = {k: v for k, v in body.items() if k in branch["properties"]}
    whole = enrol.post(SIGN, json=body, headers=AUTH)
    assert whole.status_code != 400, whole.text
    refused = set()
    for field in body:
        partial = {k: v for k, v in body.items() if k != field}
        if enrol.post(SIGN, json=partial, headers=AUTH).status_code == 400:
            refused.add(field)
    assert refused, "the code requires something"
    assert refused <= set(branch["required"]), sorted(refused - set(branch["required"]))


# --- every answer is declared ---------------------------------------------------------------


def test_every_enrol_route_answers_what_the_spec_declares(enrol, sql) -> None:
    edge_id, token = pending_edge(sql)
    key = new_key()
    answer = checked(
        enrol, "POST", "/v0/enrol", "/v0/enrol", enrol_body(token, key), headers=idem()
    )
    assert answer.status_code == 201, answer.text
    assert "api_server_ca_sha256" in answer.json()
    cert = answer.json()["cert_pem"]
    current = cert_sha256(x509.load_pem_x509_certificate(cert.encode())).hex()
    for purpose in ("renewal", "roles_anywhere"):
        body = {
            "edge_id": str(edge_id),
            "csr_pem": csr_pem(new_key()),
            "purpose": purpose,
            "current_cert_sha256": current,
            "request_id": f"spec-{purpose}-{uuid.uuid4().hex}",
        }
        resp = checked(enrol, "POST", SIGN, SIGN, body, headers=AUTH)
        assert resp.status_code == 201, (purpose, resp.text)
        if purpose == "renewal":
            current = cert_sha256(
                x509.load_pem_x509_certificate(resp.json()["cert_pem"].encode())
            ).hex()
    server = {
        "csr_pem": csr_pem(new_key()),
        "purpose": "api_server",
        "request_id": f"spec-api-server-{uuid.uuid4().hex}",
    }
    resp = checked(enrol, "POST", SIGN, SIGN, server, headers=AUTH)
    assert resp.status_code == 201, resp.text
    sql.execute(
        "UPDATE catalog.edge SET state = 'revoked', revoked_at = now() WHERE edge_id = %s",
        (edge_id,),
    )
    resp = checked(enrol, "POST", "/internal/v0/crl", "/internal/v0/crl", {}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    # A refusal is a documented problem too.
    bad = checked(enrol, "POST", SIGN, SIGN, {**server, "request_id": "x" * 16}, headers={})
    assert bad.status_code == 403


def test_api_certificate_answers_are_declared(enrol_edge) -> None:
    edge = enrol_edge()
    renewed = edge.post(f"{E}/certificate", renewal_body(edge, csr_pem(new_key())))
    assert renewed.status_code == 201, renewed.text
    assert not undeclared("POST", f"{E}/certificate", 201, renewed.json())
    ra = edge.post(f"{E}/aws-certificate", ra_body(edge, csr_pem(new_key())))
    assert ra.status_code == 201, ra.text
    assert not undeclared("POST", f"{E}/aws-certificate", 201, ra.json())


def test_the_check_catches_an_undeclared_field() -> None:
    """The check itself: a field the spec doesn't name is reported."""
    body = {"edge_id": "1", "cert_pem": "", "chain_pem": "", "ca_pem": "", "not_after": "x"}
    assert undeclared("POST", "/v0/enrol", 201, body) == set()
    assert undeclared("POST", "/v0/enrol", 201, {**body, "surprise": 1}) == {"surprise"}
