"""Two M4 contract follow-ups (portal-api.md 5.5, 5.6):

- `RecallRequest.priority` is any string; the server accepts only a priority the
  file's recall options offer (an open set, like `RecallOption.priority`);
- `Hold.inherited_from` in `GET /v1/holds?file_id=`: the folder above the file
  that the hold is on.
"""

from __future__ import annotations

from .conftest import add_dir, add_file, seed


def test_recall_priority_is_checked_against_the_offered_options(web, sql):
    w = seed(sql)
    add_file(sql, w, "old.segy", tier="archive", state="stub", size=2 * 10**9)
    fid = str(w.files["old.segy"])
    reader = web.browser(w.reader)
    options = reader.get("/v1/files/{file_id}/recall-options", file_id=fid).json()["options"]
    offered = {o["priority"] for o in options if o["offered"]}
    assert "standard" in offered
    # Not a priority any backend offers: refused, naming the field. The request
    # schema takes any string (checked by spec.py), so this is the server's answer.
    r = reader.post("/v1/recalls", {"file_id": fid, "priority": "bulk"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_request"
    assert any(e.get("field") == "priority" for e in r.json().get("errors", [])), r.json()
    # A listed option that is not offered is refused too.
    for o in options:
        if not o["offered"]:
            r = reader.post("/v1/recalls", {"file_id": fid, "priority": o["priority"]})
            assert r.status_code == 400, r.text
    r = reader.post("/v1/recalls", {"file_id": fid, "priority": "standard"})
    assert r.status_code == 201, r.text
    assert r.json()["priority"] == "standard"


def test_hold_listing_by_file_names_the_folder_it_is_inherited_from(web, sql):
    w = seed(sql)
    d = add_dir(sql, w, "survey")
    a = add_file(sql, w, "a.segy", parent=d)
    owner = web.browser(w.owner)
    on_dir = owner.post("/v1/holds", {"items": [str(d)], "kind": "keep_online",
                                      "reason": "processing"}).json()["holds"][0]  # fmt: skip
    on_file = owner.post("/v1/holds", {"items": [str(a)], "kind": "keep_online",
                                       "reason": "processing"}).json()["holds"][0]  # fmt: skip
    by_file = owner.get("/v1/holds", params={"file_id": str(a)}).json()["items"]
    got = {h["hold_id"]: h["inherited_from"] for h in by_file}
    assert got == {on_dir["hold_id"]: str(d), on_file["hold_id"]: None}
    # Other listings (and the create and release replies) carry null.
    assert on_dir["inherited_from"] is None
    listed = owner.get("/v1/holds").json()["items"]
    assert all(h["inherited_from"] is None for h in listed)
