"""A source's inline `backend: {kind, config}` (portal-api.md section 5.10):
it only finds a backend an ArchiveAdmin configured, and anything that could
steer the service's credentials or network is `422 config_invalid` naming
the field (line B's 88c525b)."""

from __future__ import annotations

import uuid

import pytest
from psycopg.types.json import Jsonb

from .conftest import seed

ACCOUNT = "123456789012"


def _backend(sql, bucket: str, *, region: str = "eu-west-1", private: bool = True,
             account: str | None = ACCOUNT, buckets: list[str] | None = None) -> str:  # fmt: skip
    config = {"region": region, "buckets": [bucket] if buckets is None else buckets}
    if account is not None:
        # The row names its account; the rest of it (roles, Batch, endpoints)
        # is the operator's (backends/s3/config.py).
        config["account_id"] = account
    (bid,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, %s) RETURNING backend_id",
        (f"s3-{uuid.uuid4().hex[:8]}", Jsonb(config), private),
    ).fetchone()
    return str(bid)


def _body(w, config: dict, **kw) -> dict:  # type: ignore[no-untyped-def]
    return {"dataset_id": str(w.dataset_id), "backend": {"kind": "s3", "config": config},
            "prefix": "raw/", "path": "seismic", "groups": {"owners": w.g_owners},
            **kw}  # fmt: skip


def _bucket() -> str:
    return f"acme-{uuid.uuid4().hex[:10]}"


def test_an_inline_backend_finds_the_configured_one(web, sql):
    w = seed(sql)
    bucket = _bucket()
    _backend(sql, bucket, private=False)  # found second: private networking first
    private = _backend(sql, bucket)
    adopt = web.app_token(["Archive.Adopt"])
    cfg = {"bucket": bucket, "region": "eu-west-1", "account_id": ACCOUNT,
           "requester_pays": False, "private_network": True}  # fmt: skip
    r = web.call("POST", "/v1/sources", token=adopt, json=_body(w, cfg))
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["backend_id"], s["container"]) == (private, bucket)
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'source.register'"
        " AND details->>'source_id' = %s", (s["source_id"],)).fetchone()  # fmt: skip
    assert details["inline_backend"]["bucket"] == bucket
    # No new backend was made.
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.backend WHERE config->'buckets' ? %s", (bucket,)
    ).fetchone()
    assert n == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint_url", "https://attacker.example"),
        ("sqs_endpoint_url", "https://attacker.example"),
        ("role_arn", "arn:aws:iam::999999999999:role/steal"),
        ("audience", "api://attacker"),
        ("web_identity", {"audience": "api://attacker"}),
        ("ca_bundle", "-----BEGIN CERTIFICATE-----"),
        ("credentials", "default"),
        ("roles_anywhere", {"role_arn": "x"}),
        ("event_queues", {"*": "https://sqs.attacker"}),
        ("batch", {"account_id": "1"}),
        ("buckets", []),
        ("private_network", False),
        ("secret_access_key", "x" * 40),
        ("anything_else", 1),
    ],
)
def test_only_the_allow_list_is_taken(web, sql, field, value):
    w = seed(sql)
    bucket = _bucket()
    _backend(sql, bucket)
    cfg = {"bucket": bucket, "region": "eu-west-1", field: value}
    r = web.call("POST", "/v1/sources", token=web.app_token(["Archive.Adopt"]),
                 json=_body(w, cfg), check=False)  # fmt: skip
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "config_invalid"
    assert f"backend.config.{field}" in r.json()["detail"]
    # The value is never echoed back.
    assert str(value) not in r.json()["detail"] or field == "buckets"
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.source WHERE dataset_id = %s", (w.dataset_id,)
    ).fetchone()
    assert n == 0


def test_inline_refusals(web, sql):
    w = seed(sql)
    bucket = _bucket()
    backend = _backend(sql, bucket)
    _backend(sql, f"{bucket}-open", buckets=[])  # open to any bucket: never matched
    adopt = web.app_token(["Archive.Adopt"])

    def post(config=None, check=False, path="/v1/sources", **kw):  # type: ignore[no-untyped-def]
        body = _body(w, config or {"bucket": bucket, "region": "eu-west-1"}, **kw)
        return web.call("POST", path, token=adopt, json=body, check=check)

    def code(r) -> tuple[int, str, str]:  # type: ignore[no-untyped-def]
        return r.status_code, r.json()["code"], r.json()["detail"].split(":")[0]

    assert code(post({"bucket": "unknown-bucket-1", "region": "eu-west-1"})) == (
        422, "config_invalid", "backend.config.bucket")  # fmt: skip
    assert code(post({"bucket": bucket, "region": "us-east-1"})) == (
        422, "config_invalid", "backend.config.bucket")  # fmt: skip
    assert code(post({"bucket": f"{bucket}-open", "region": "eu-west-1"})) == (
        422, "config_invalid", "backend.config.bucket")  # fmt: skip
    assert code(post({"bucket": bucket, "region": "eu-west-1", "account_id": "999999999999"})) == (
        422, "config_invalid", "backend.config.account_id")  # fmt: skip
    assert code(post({"bucket": bucket, "region": "eu-west-1", "requester_pays": True})) == (
        422, "config_invalid", "backend.config.requester_pays")  # fmt: skip
    assert code(post({"bucket": "Bad_Bucket", "region": "eu-west-1"})) == (
        422, "config_invalid", "backend.config.bucket")  # fmt: skip
    assert code(post({"region": "eu-west-1"})) == (422, "config_invalid", "backend.config.bucket")
    bad = [({"kind": "azure_blob", "config": {}}, "backend.kind"),
           ({"kind": "s3", "config": {}, "role": "x"}, "backend.role")]  # fmt: skip
    for backend, field in bad:
        r = web.call("POST", "/v1/sources", token=adopt, check=False,
                     json={**_body(w, {}), "backend": backend})  # fmt: skip
        assert code(r) == (422, "config_invalid", field)
    # Both ways at once, and inline on adopt.
    r = post(backend_id=backend, container=bucket)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    assert code(post(path="/v1/adopt")) == (422, "config_invalid", "backend")
    # `container` names the bucket too.
    r = post({"container": bucket, "region": "eu-west-1"}, check=True)
    assert r.status_code == 201, r.text
