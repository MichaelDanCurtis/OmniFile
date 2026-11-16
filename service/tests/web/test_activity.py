"""Activity: the recall and upload dashboards (portal-api.md section 5.13)."""

from __future__ import annotations

from .conftest import add_file, seed


def _recall(sql, w, fid: int, state: str, *, ago_h: float, took_h: float | None) -> None:  # type: ignore[no-untyped-def]
    vid = sql.execute(
        "SELECT content_version_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()[0]
    done = None if took_h is None else f"now() - interval '{ago_h - took_h} hours'"
    sql.execute(
        "INSERT INTO catalog.recall (file_id, version_id, state, requested_at, completed_at,"
        f" updated_at) VALUES (%s, %s, %s, now() - interval '{ago_h} hours', {done or 'NULL'},"
        f" {done or 'now()'})",
        (fid, vid, state),
    )


def test_activity_sums_uploads_and_recalls(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    a = add_file(sql, w, "a.segy", size=100)
    b = add_file(sql, w, "b.segy", size=200, ago_days=1)
    add_file(sql, w, "seeded.segy", size=999)  # no edge: seeded, not uploaded
    sql.execute(
        "UPDATE catalog.file_version SET edge_id = %s WHERE file_id IN (%s, %s)", (w.edge_id, a, b)
    )
    sql.execute(
        "UPDATE catalog.location SET verified = true, verified_at = now(),"
        " verified_by = 'azure_read_crc', crc64nvme = '\\x0000000000000001'::bytea"
        " WHERE file_id = %s",
        (a,),
    )
    landing = add_file(sql, w, "new.segy", size=5, state="landing")
    assert landing
    _recall(sql, w, a, "available", ago_h=5, took_h=2)
    _recall(sql, w, b, "available", ago_h=30, took_h=4)
    c = add_file(sql, w, "c.segy", size=7, tier="archive", state="stub")
    _recall(sql, w, c, "failed", ago_h=3, took_h=1)
    d = add_file(sql, w, "d.segy", size=7, tier="archive", state="stub")
    _recall(sql, w, d, "rehydrating", ago_h=1, took_h=None)

    read = web.app_token(["Archive.Read"])
    r = web.call("GET", "/v1/activity", token=read,
                 params={"days": "7", "dataset_id": str(w.dataset_id)})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["days"]) == 7 and body["dataset_id"] == str(w.dataset_id)
    assert body["uploads"]["files"] == 2 and body["uploads"]["bytes"] == 300
    # Not yet read back: b.segy's copy. a.segy's is verified, and the
    # others weren't uploaded by an edge (a seeded S3 primary is never
    # read back, and isn't waiting for it).
    assert body["uploads"]["unverified"] == 1
    assert body["uploads"]["landing"] == 1
    rec = body["recalls"]
    assert (rec["available"], rec["failed"], rec["open"], rec["bytes"]) == (2, 1, 1, 300)
    assert rec["time_to_available_s"] == {"p50": 3 * 3600, "p90": round(3.8 * 3600)}
    today, yesterday = body["days"][-1], body["days"][-2]
    assert sum(d["recalls"]["started"] for d in body["days"]) == 4
    assert today["uploads"]["files"] + yesterday["uploads"]["files"] == 2
    assert sum(d["recalls"]["failed"] for d in body["days"]) == 1

    # Another dataset sees none of it.
    other = seed(sql)
    r = web.call("GET", "/v1/activity", token=read, params={"dataset_id": str(other.dataset_id)})
    z = r.json()
    assert len(z["days"]) == 30
    assert (z["uploads"]["files"], z["recalls"]["available"], z["recalls"]["open"]) == (0, 0, 0)
    assert z["recalls"]["time_to_available_s"] == {"p50": None, "p90": None}


def test_activity_is_for_admins_and_readers(web, sql) -> None:  # type: ignore[no-untyped-def]
    seed(sql)
    r = web.call("GET", "/v1/activity", token=web.app_token(["Archive.Adopt"]))
    assert r.status_code == 200
    r = web.call("GET", "/v1/activity", token=web.app_token([]))
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    for bad in ("0", "91", "x"):
        r = web.call("GET", "/v1/activity", token=web.app_token(["Archive.Read"]),
                     params={"days": bad})  # fmt: skip
        assert r.status_code == 400, bad
