"""Point-in-time listings for rewind trees (control-api.md section 4.5)."""

from __future__ import annotations

import time

from omnifile_service.backends.sim.crc64 import crc64nvme

from .conftest import E, create_file
from .test_upload_flow import upload


def _at(sql) -> str:  # type: ignore[no-untyped-def]
    time.sleep(0.01)
    (t,) = sql.execute(
        "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    ).fetchone()
    time.sleep(0.01)
    return t


def _depth(sql, share_id: int, days: int) -> None:  # type: ignore[no-untyped-def]
    with sql.transaction():
        sql.execute("SELECT set_config('omnifile.actor', 'test', true)")
        sql.execute(
            "UPDATE catalog.share SET rewind_depth_max_days = %s, rewind_depth_days = %s"
            " WHERE share_id = %s",
            (days, days, share_id),
        )


def _asof(w, dir_id: int, t: str, **params):  # type: ignore[no-untyped-def]
    return w.edge.get(f"{E}/asof", {"dir_id": str(dir_id), "t": t, **params})


def test_a_directory_as_it_was(make_world, facade_client, sql) -> None:  # type: ignore[no-untyped-def]
    w = make_world()
    _depth(sql, w.share_id, 30)
    a = create_file(w, "a.segy")
    _, v1 = upload(w, facade_client, a, b"first version")
    b = create_file(w, "b.segy")
    upload(w, facade_client, b, b"bee")
    t1 = _at(sql)
    _, v2 = upload(w, facade_client, a, b"second, longer version")
    ren = {
        "op_id": "ren-1",
        "op": "rename",
        "file": {"file_id": str(b)},
        "new_parent": {"file_id": str(w.root_id)},
        "new_name": "c.segy",
        "replace": False,
    }
    r = w.edge.post(f"{E}/changes", {"ops": [ren]})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "applied", r.text
    create_file(w, "d.segy")
    t2 = _at(sql)

    then = _asof(w, w.root_id, t1)
    assert then.status_code == 200, then.text
    rows = {e["name"]: e for e in then.json()["entries"]}
    assert set(rows) == {"a.segy", "b.segy"} and then.json()["more"] is False
    old = rows["a.segy"]
    assert old["version_id"] == v1["version_id"] and old["size"] == len(b"first version")
    assert old["crc64nvme"] == f"{crc64nvme(b'first version'):016x}"
    assert old["state"] == "remote" and old["resident_bytes"] == 0 and old["stub"] is None
    assert old["location"]["backend_kind"] == "azure_blob"
    assert old["location"]["backend_version_id"]  # that version's blob, not the current one

    # Its bytes are read with a credential for that version (section 4.5).
    cred = w.edge.post(f"{E}/credentials", {"access": "read", "file_id": str(a),
                                             "version_id": old["version_id"]})  # fmt: skip
    assert cred.status_code == 200, cred.text
    got = facade_client.get(cred.json()["url"], headers={"x-ms-version": "2026-04-06"})
    assert got.status_code == 200 and got.content == b"first version"

    now = {e["name"]: e for e in _asof(w, w.root_id, t2).json()["entries"]}
    assert set(now) == {"a.segy", "c.segy", "d.segy"}
    assert now["a.segy"]["version_id"] == v2["version_id"]
    assert now["a.segy"]["location"]["backend_version_id"] != old["location"]["backend_version_id"]
    # A rename keeps the bytes: same content version under the new name.
    assert now["c.segy"]["file_id"] == rows["b.segy"]["file_id"]
    assert now["c.segy"]["version_id"] == rows["b.segy"]["version_id"]

    # Paging by name.
    first = _asof(w, w.root_id, t2, limit=2).json()
    assert [e["name"] for e in first["entries"]] == ["a.segy", "c.segy"] and first["more"]
    rest = _asof(w, w.root_id, t2, limit=2, after="c.segy").json()
    assert [e["name"] for e in rest["entries"]] == ["d.segy"] and not rest["more"]


def test_asof_refusals(make_world, sql) -> None:  # type: ignore[no-untyped-def]
    w = make_world()
    other = make_world()
    _depth(sql, w.share_id, 7)
    f = create_file(w, "x.segy")
    assert _asof(w, w.root_id, "2020-01-01T00:00:00Z").status_code == 410
    assert _asof(w, w.root_id, "2020-01-01T00:00:00Z").json()["code"] == "beyond_rewind_depth"
    assert _asof(w, w.root_id, "2999-01-01T00:00:00Z").status_code == 400
    assert _asof(w, w.root_id, "yesterday").status_code == 400
    assert w.edge.get(f"{E}/asof", {"dir_id": str(w.root_id)}).status_code == 400
    # Not a directory, or another edge's.
    t = _at(sql)
    assert _asof(w, f, t).status_code == 404
    assert _asof(w, other.root_id, t).status_code == 404
    # No rewind depth: nothing in the past is offered.
    assert _asof(other, other.root_id, "2026-01-01T00:00:00Z").status_code == 410
