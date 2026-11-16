"""`GET /v0/edges/{id}/digest` (control-api.md §6): the reconciliation digest."""

from __future__ import annotations

import hashlib

from .conftest import E, create_file


def expected(sql, dataset_id, parent=None):
    q = "SELECT file_id, entity_version FROM catalog.file WHERE dataset_id = %s"
    q += " AND deleted_at IS NULL"
    args = [dataset_id]
    if parent is not None:
        q += " AND parent_id = %s"
        args.append(parent)
    x, n = 0, 0
    for fid, ev in sql.execute(q, args).fetchall():
        x ^= int.from_bytes(hashlib.sha256(f"{fid}:{ev}".encode()).digest()[:8], "big")
        n += 1
    return n, f"{x:016x}"


def digest(w, **q):
    resp = w.edge.get(f"{E}/digest", {"dataset_id": str(w.dataset_id), **q})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_digest_is_the_xor_of_the_live_rows(make_world, sql) -> None:
    w = make_world()
    sub = create_file(w, "sub", kind="dir")
    ids = [create_file(w, f"f{i}") for i in range(3)] + [create_file(w, "g", parent=sub)]
    d = digest(w)
    assert d["as_of"]
    assert (d["live_rows"], d["digest"]) == expected(sql, w.dataset_id) and d["live_rows"] == 6
    # A directory's direct children only.
    s = digest(w, dir_id=str(sub))
    assert (s["live_rows"], s["digest"]) == expected(sql, w.dataset_id, sub) == (1, s["digest"])
    # A rename bumps the row's entity_version: the digest moves.
    op = {"op_id": "op-ren", "op": "rename", "file": {"file_id": str(ids[0])},
          "new_parent": {"file_id": str(w.root_id)}, "new_name": "f0b"}  # fmt: skip
    assert w.edge.post(f"{E}/changes", {"ops": [op]}).status_code == 200
    moved = digest(w)
    assert moved["digest"] != d["digest"] and moved["live_rows"] == 6
    assert (moved["live_rows"], moved["digest"]) == expected(sql, w.dataset_id)
    # A deleted row leaves it.
    op = {"op_id": "op-del", "op": "unlink", "file": {"file_id": str(ids[1])}}
    assert w.edge.post(f"{E}/changes", {"ops": [op]}).status_code == 200
    gone = digest(w)
    assert gone["live_rows"] == 5
    assert (gone["live_rows"], gone["digest"]) == expected(sql, w.dataset_id)


def test_an_empty_directory_and_another_edges_dataset(make_world) -> None:
    w, other = make_world(), make_world()
    empty = create_file(w, "empty", kind="dir")
    assert digest(w, dir_id=str(empty)) | {"as_of": None} == {
        "as_of": None,
        "live_rows": 0,
        "digest": "0000000000000000",
    }
    nope = w.edge.get(f"{E}/digest", {"dataset_id": str(other.dataset_id)}, check=False)
    assert nope.status_code == 404
