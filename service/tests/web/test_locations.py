"""Locations (portal-api.md section 5.12): request a move, follow it."""

from __future__ import annotations

from .conftest import seed
from .test_sources import BUCKET, _s3_backend


def _migrating(sql, w) -> str:  # type: ignore[no-untyped-def]
    """The dataset migrates from an S3 backend, with a seeded source on raw/."""
    s3 = _s3_backend(sql)
    sql.execute(
        "UPDATE catalog.dataset SET migration_state = 'migrating', source_backend_id = %s"
        " WHERE dataset_id = %s",
        (int(s3), w.dataset_id),
    )
    sql.execute(
        "INSERT INTO catalog.source (dataset_id, backend_id, container, prefix, owners_sid,"
        " state) VALUES (%s, %s, %s, 'raw/', %s, 'seeded')",
        (w.dataset_id, int(s3), BUCKET, w.g_owners),
    )
    return s3


def test_a_location_is_requested_and_followed(web, sql):
    w = seed(sql)
    _migrating(sql, w)
    adopt = web.app_token(["Archive.Adopt"])
    body = {"dataset_id": str(w.dataset_id), "prefix": "raw/2019/",
            "to_backend_id": str(w.backend_id)}  # fmt: skip
    r = web.call("POST", "/v1/locations", token=adopt, json=body)
    assert r.status_code == 202, r.text
    loc = r.json()
    assert (loc["state"], loc["copy"], loc["mode"]) == ("pending", "omnifile", "serve")
    assert loc["target_root"] == f"migrated/{BUCKET}/" and loc["run"] is None
    (queued,) = sql.execute(
        "SELECT count(*) FROM pgmq.q_migrate WHERE message->>'migration_id' = %s",
        (loc["migration_id"],),
    ).fetchone()
    assert queued == 1
    (action,) = sql.execute(
        "SELECT action FROM catalog.audit_log WHERE details->>'migration_id' = %s",
        (loc["migration_id"],),
    ).fetchone()
    assert action == "location.request"
    read = web.app_token(["Archive.Read"])
    one = web.call("GET", "/v1/locations/{migration_id}", token=read,
                   migration_id=loc["migration_id"])  # fmt: skip
    assert one.status_code == 200 and one.json()["files"]["verified"] == 0
    external = web.call("POST", "/v1/locations", token=adopt,
                        json={**body, "prefix": "raw/2020/", "copy": "external"})  # fmt: skip
    assert external.status_code == 202 and external.json()["copy"] == "external"
    listed = web.call("GET", "/v1/locations", token=read,
                      params={"dataset_id": str(w.dataset_id), "limit": 1}).json()  # fmt: skip
    assert [x["prefix"] for x in listed["items"]] == ["raw/2020/"] and listed["next_cursor"]
    admin = web.browser(w.admin)
    assert admin.get("/v1/locations", params={"state": "pending"}).status_code == 200


def test_location_refusals(web, sql):
    w = seed(sql)
    adopt = web.app_token(["Archive.Adopt"])
    body = {"dataset_id": str(w.dataset_id), "prefix": "raw/", "to_backend_id": str(w.backend_id)}

    def post(token=adopt, check=True, **kw):  # type: ignore[no-untyped-def]
        return web.call("POST", "/v1/locations", token=token, json={**body, **kw}, check=check)

    r = post()  # the dataset isn't migrating
    assert r.status_code == 409 and r.json()["code"] == "migration_conflict"
    s3 = _migrating(sql, w)
    assert post(token=web.app_token(["Archive.Read"])).status_code == 403
    assert post(prefix="elsewhere/").json()["code"] == "invalid_request"
    assert post(to_backend_id=s3).json()["code"] == "invalid_request"
    assert post(mode="hand_off").json()["code"] == "invalid_request"
    assert post(check=False, copy="rsync").status_code == 400
    assert post().status_code == 202
    r = post(prefix="raw/deeper/")
    assert r.status_code == 409 and r.json()["code"] == "migration_conflict"
    assert web.browser(w.steward).post("/v1/locations", body).status_code == 403
    r = web.call("GET", "/v1/locations/{migration_id}", token=adopt, migration_id="999999999")
    assert r.status_code == 404
