"""Partner sources (portal-api.md section 5.10): register, read, retry."""

from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from .conftest import seed

BUCKET = "acme-seismic"


def _s3_backend(sql) -> str:  # type: ignore[no-untyped-def]
    (bid,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, false) RETURNING backend_id",
        (f"s3-{uuid.uuid4().hex[:8]}", Jsonb({"region": "eu-west-1", "buckets": [BUCKET]})),
    ).fetchone()
    return str(bid)


def _body(w, backend: str, **kw) -> dict:  # type: ignore[no-untyped-def]
    return {
        "dataset_id": str(w.dataset_id),
        "backend_id": backend,
        "container": BUCKET,
        "prefix": "raw/",
        "path": "seismic",
        "groups": {"owners": w.g_owners, "readers": w.g_readers},
        **kw,
    }


def test_an_app_registers_a_source_and_follows_it(web, sql):
    w = seed(sql)
    backend = _s3_backend(sql)
    adopt = web.app_token(["Archive.Adopt"])
    inventory = {"kind": "s3_inventory", "bucket": "acme-inventory",
                 "manifest_key": "acme-seismic/daily/x/manifest.json"}  # fmt: skip
    r = web.call("POST", "/v1/sources", token=adopt, json=_body(w, backend, inventory=inventory))
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["state"], s["snapshot_at"], s["inventory"]) == ("registered", None, inventory)
    assert s["groups"] == {"owners": w.g_owners, "writers": None, "readers": w.g_readers}
    # The dataset now migrates from the S3 backend, and the seed is queued.
    state, src = sql.execute(
        "SELECT migration_state, source_backend_id FROM catalog.dataset WHERE dataset_id = %s",
        (w.dataset_id,),
    ).fetchone()
    assert (state, str(src)) == ("migrating", backend)
    (queued,) = sql.execute(
        "SELECT count(*) FROM pgmq.q_seed WHERE message->>'source_id' = %s", (s["source_id"],)
    ).fetchone()
    assert queued == 1
    (actor, details) = sql.execute(
        "SELECT actor, details FROM catalog.audit_log WHERE action = 'source.register'"
        " AND details->>'source_id' = %s",
        (s["source_id"],),
    ).fetchone()
    assert actor == "app:prizmanyx-app" and details["prefix"] == "raw/"
    # Read it back with Archive.Read.
    read = web.app_token(["Archive.Read"])
    one = web.call("GET", "/v1/sources/{source_id}", token=read, source_id=s["source_id"])
    assert one.status_code == 200 and one.json()["counts"]["objects_seen"] == 0
    second = web.call("POST", "/v1/sources", token=adopt,
                      json=_body(w, backend, prefix="cooked/", path="seismic/cooked"))  # fmt: skip
    assert second.status_code == 201, second.text
    page = web.call("GET", "/v1/sources", token=read,
                    params={"dataset_id": str(w.dataset_id), "limit": 1})  # fmt: skip
    assert page.status_code == 200
    assert [x["source_id"] for x in page.json()["items"]] == [second.json()["source_id"]]
    after = {"dataset_id": str(w.dataset_id), "cursor": page.json()["next_cursor"]}
    rest = web.call("GET", "/v1/sources", token=read, params=after)
    assert [x["source_id"] for x in rest.json()["items"]] == [s["source_id"]]
    seeded = web.call("GET", "/v1/sources", token=read, params={"state": "seeded",
                      "dataset_id": str(w.dataset_id)})  # fmt: skip
    assert seeded.json()["items"] == []


def test_refusals(web, sql):
    w = seed(sql)
    backend = _s3_backend(sql)
    adopt = web.app_token(["Archive.Adopt"])

    def post(token=adopt, check=True, **kw):  # type: ignore[no-untyped-def]
        return web.call("POST", "/v1/sources", token=token, json=_body(w, backend, **kw),
                        check=check)  # fmt: skip

    r = post(token=web.app_token(["Archive.Read"]))
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    assert post(container="not-ours").json()["code"] == "invalid_request"
    assert post(prefix="noslash").json()["code"] == "invalid_request"
    r = post(check=False, inventory={"kind": "parquet", "bucket": "b", "manifest_key": "k"})
    assert r.status_code == 400
    r = post(check=False, groups={"owners": "not-a-sid"})
    assert r.status_code == 400
    assert post().status_code == 201
    r = post(prefix="raw/deeper/")
    assert r.status_code == 409 and r.json()["code"] == "source_conflict"
    # A second source backend for the same dataset.
    other = _s3_backend(sql)
    r = web.call("POST", "/v1/sources", token=adopt, json=_body(w, other, prefix="x/"))
    assert r.status_code == 409 and r.json()["code"] == "source_conflict"
    r = web.call("GET", "/v1/sources/{source_id}", token=adopt, source_id="999999999")
    assert r.status_code == 404


def test_only_archive_admins_register_in_the_portal(web, sql):
    w = seed(sql)
    backend = _s3_backend(sql)
    r = web.browser(w.steward).post("/v1/sources", _body(w, backend))
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    admin = web.browser(w.admin)
    r = admin.post("/v1/sources", _body(w, backend))
    assert r.status_code == 201, r.text
    assert admin.get("/v1/sources/{source_id}", source_id=r.json()["source_id"]).status_code == 200


def test_a_failed_source_is_retried(web, sql):
    w = seed(sql)
    backend = _s3_backend(sql)
    adopt = web.app_token(["Archive.Adopt"])
    sid = web.call("POST", "/v1/sources", token=adopt, json=_body(w, backend)).json()["source_id"]
    r = web.call("POST", "/v1/sources/{source_id}/retry", token=adopt, json={}, source_id=sid)
    assert r.status_code == 409 and r.json()["code"] == "source_conflict"
    sql.execute(
        "UPDATE catalog.source SET state = 'failed', last_error = 'the inventory is old'"
        " WHERE source_id = %s",
        (int(sid),),
    )
    r = web.call("POST", "/v1/sources/{source_id}/retry", token=adopt, json={}, source_id=sid)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["last_error"]) == ("seeding", None)


def test_adopt_takes_data_already_in_the_datasets_backend(web, sql):
    w = seed(sql)
    adopt = web.app_token(["Archive.Adopt"])
    body = {"dataset_id": str(w.dataset_id), "backend_id": str(w.backend_id),
            "container": "live/data", "prefix": "moved/", "path": "from-s3",
            "groups": {"owners": w.g_owners}}  # fmt: skip
    r = web.call("POST", "/v1/adopt", token=adopt, json=body)
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["kind"], s["state"], s["path"]) == ("adopt", "registered", "from-s3")
    # Nothing migrates.
    (state,) = sql.execute(
        "SELECT migration_state FROM catalog.dataset WHERE dataset_id = %s", (w.dataset_id,)
    ).fetchone()
    assert state == "steady"
    (action,) = sql.execute(
        "SELECT action FROM catalog.audit_log WHERE details->>'source_id' = %s", (s["source_id"],)
    ).fetchone()
    assert action == "source.adopt"
    # Another backend's data is a source to migrate, not an adoption.
    other = _s3_backend(sql)
    r = web.call("POST", "/v1/adopt", token=adopt,
                 json={**body, "backend_id": other, "container": BUCKET})  # fmt: skip
    assert r.status_code == 400 and "register a source" in r.json()["detail"]
    listed = web.call("GET", "/v1/sources", token=adopt,
                      params={"dataset_id": str(w.dataset_id)}).json()["items"]  # fmt: skip
    assert [x["kind"] for x in listed] == ["adopt"]


def test_only_s3_to_azure_migrates_in_v1(web, sql):
    w = seed(sql)
    adopt = web.app_token(["Archive.Adopt"])
    (azure,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'azure_blob', %s, true) RETURNING backend_id",
        (f"az-{uuid.uuid4().hex[:8]}", Jsonb({"account": "x", "buckets": ["c"]})),
    ).fetchone()
    body = {"dataset_id": str(w.dataset_id), "backend_id": str(azure), "container": "c",
            "groups": {"owners": w.g_owners}}  # fmt: skip
    r = web.call("POST", "/v1/sources", token=adopt, json=body)
    assert r.status_code == 400 and "decision 23" in r.json()["detail"]
    # A dataset that lives on S3 can't take a migration source either.
    s3 = _s3_backend(sql)
    sql.execute("UPDATE catalog.dataset SET backend_id = %s WHERE dataset_id = %s",
                (int(s3), w.dataset_id))  # fmt: skip
    other = _s3_backend(sql)
    r = web.call("POST", "/v1/sources", token=adopt,
                 json={**body, "backend_id": other, "container": BUCKET})  # fmt: skip
    assert r.status_code == 400 and "decision 23" in r.json()["detail"]


def test_s3_metadata_tables_and_an_explicit_listing(web, sql):
    w = seed(sql)
    backend = _s3_backend(sql)
    adopt = web.app_token(["Archive.Adopt"])
    arn = "arn:aws:s3tables:eu-west-1:123456789012:bucket/aws-s3"
    tables = {"kind": "s3_metadata", "table_bucket_arn": arn, "namespace": "b_acme_seismic"}
    r = web.call("POST", "/v1/sources", token=adopt, json=_body(w, backend, inventory=tables))
    assert r.status_code == 201, r.text
    assert r.json()["inventory"] == tables
    listing = web.call("POST", "/v1/sources", token=adopt,
                       json=_body(w, backend, prefix="other/", path="seismic/other",
                                  inventory={"kind": "listing"}))  # fmt: skip
    assert listing.status_code == 201, listing.text
    assert listing.json()["inventory"] == {"kind": "listing"}

    def refused(inventory):  # type: ignore[no-untyped-def]
        r = web.call("POST", "/v1/sources", token=adopt, check=False,
                     json=_body(w, backend, prefix="x/", inventory=inventory))  # fmt: skip
        assert r.status_code == 400, r.text

    # Endpoints are the backend's: a request can't name one.
    refused({**tables, "uri": "https://elsewhere.example/iceberg"})
    refused({"kind": "s3_metadata"})
    refused({**tables, "table_bucket_arn": arn.replace("eu-west-1", "us-east-1")})
    refused({"kind": "listing", "bucket": "b"})


def test_the_portal_lists_s3_backends_and_their_buckets(web, sql):
    """`GET /v1/source-backends` (the New migration wizard): each S3 backend
    an admin configured, with its region and buckets, and nothing that
    steers credentials or the network. ArchiveAdmin sessions only."""
    w = seed(sql)
    listed = _s3_backend(sql)
    (any_bucket,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, true) RETURNING backend_id",
        (f"s3-any-{uuid.uuid4().hex[:8]}", Jsonb({"region": "us-east-1"})),
    ).fetchone()
    r = web.browser(w.admin).get("/v1/source-backends")
    assert r.status_code == 200, r.text
    by_id = {b["backend_id"]: b for b in r.json()["backends"]}
    assert str(w.backend_id) not in by_id  # a dataset's home is not a source
    one = by_id[listed]
    assert set(one) == {"backend_id", "name", "kind", "region", "private_network", "buckets",
                        "metadata_tables"}  # fmt: skip
    assert (one["kind"], one["region"], one["buckets"], one["private_network"]) == (
        "s3", "eu-west-1", [BUCKET], False)  # fmt: skip
    assert by_id[str(any_bucket)]["buckets"] is None  # takes any bucket
    assert by_id[str(any_bucket)]["private_network"] is True
    r = web.browser(w.steward).get("/v1/source-backends")
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    r = web.call("GET", "/v1/source-backends", token=web.app_token(["Archive.Adopt"]),
                 check=False)  # fmt: skip
    assert r.status_code == 401
