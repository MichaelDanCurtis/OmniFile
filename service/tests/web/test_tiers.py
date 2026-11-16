"""Tier pins and holds (decision 29; keep_online and legal holds)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .conftest import add_dir, add_file, seed


def world(sql):
    w = seed(sql)
    add_dir(sql, w, "survey")
    add_file(sql, w, "a.segy", parent=w.files["survey"], tier="smart", size=10**9)
    add_file(sql, w, "b.segy", parent=w.files["survey"], tier="smart", size=10**9)
    add_file(sql, w, "secret.segy", acl=w.acl_secret)
    return w


def test_pin_preview_of_a_shared_blob_says_it_gets_its_own_copy(web, sql):
    """Decision 66. A row of the same blob, primary and committed, is what counts: not
    one on its way out, and not a retained secondary, which has no plan of its own."""
    w = world(sql)
    owner = web.browser(w.owner)

    def codes(tier="hot"):
        r = owner.post("/v1/pins/preview", {"items": [str(w.files["a.segy"])], "tier": tier})
        assert r.status_code == 200, r.text
        return {c["code"] for c in r.json()["items"][0]["checks"]}

    assert "pin_own_copy" not in codes()
    (loc,) = sql.execute(
        "SELECT primary_location_id FROM catalog.file WHERE file_id = %s", (w.files["a.segy"],)
    ).fetchone()
    (other,) = sql.execute(
        "SELECT primary_location_id FROM catalog.file WHERE file_id = %s", (w.files["b.segy"],)
    ).fetchone()
    sql.execute(
        "UPDATE catalog.location o SET container = l.container, object_key = l.object_key,"
        " backend_version_id = l.backend_version_id, backend_id = l.backend_id"
        " FROM catalog.location l WHERE o.location_id = %s AND l.location_id = %s",
        (other, loc),
    )
    assert "pin_own_copy" in codes()
    sql.execute(
        "UPDATE catalog.location SET state = 'deleting', verified = false WHERE location_id = %s",
        (other,),
    )
    assert "pin_own_copy" not in codes()


def test_pin_preview_warns_and_saves_nothing(web, sql):
    w = world(sql)
    owner = web.browser(w.owner)
    r = owner.post(
        "/v1/pins/preview",
        {"items": [str(w.files["a.segy"]), str(w.files["survey"])], "tier": "archive"},
    )
    assert r.status_code == 200, r.text
    p = r.json()
    assert [i["file_id"] for i in p["items"]] == [str(w.files["a.segy"]), str(w.files["survey"])]
    codes = {c["code"] for c in p["items"][0]["checks"]}
    assert "pin_leaves_smart" in codes, p["items"][0]["checks"]
    assert p["totals"]["files"] == 3
    assert sql.execute("SELECT count(*) FROM catalog.tier_pin WHERE dataset_id = %s",
                       (w.dataset_id,)).fetchone() == (0,)  # fmt: skip
    # Unreadable items aren't previewed (or revealed).
    r = web.browser(w.reader).post("/v1/pins/preview", {"items": [str(w.files["secret.segy"])],
                                                        "tier": "hot"})  # fmt: skip
    assert r.json()["items"] == []
    # A folder's totals count only what the caller can read (M7 review): a
    # DataSteward who can't read hidden.segy doesn't learn it is there.
    add_file(sql, w, "hidden.segy", parent=w.files["survey"], acl=w.acl_secret)
    survey = {"items": [str(w.files["survey"])], "tier": "archive"}
    assert owner.post("/v1/pins/preview", survey).json()["totals"]["files"] == 3
    assert web.browser(w.steward).post("/v1/pins/preview", survey).json()["totals"]["files"] == 2
    # Who can't pin it can't preview it either.
    assert web.browser(w.reader).post("/v1/pins/preview", survey).json()["items"] == []


def test_pins_by_role(web, sql):
    w = world(sql)
    a, survey, secret = w.files["a.segy"], w.files["survey"], w.files["secret.segy"]
    until = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    # A plain reader: skipped.
    r = web.browser(w.reader).post("/v1/pins", {"items": [str(a)], "tier": "hot",
                                                "reason": "need it"})  # fmt: skip
    assert r.status_code == 201 and r.json()["pins"] == []
    assert r.json()["skipped"][0]["code"] == "forbidden"
    # The share's Owner: pinned; an unreadable item is skipped as unknown.
    owner = web.browser(w.owner)
    r = owner.post("/v1/pins", {"items": [str(survey), "999999999"], "tier": "cold",
                                "until": until, "reason": "project paused"})  # fmt: skip
    assert r.status_code == 201, r.text
    assert [p["file_id"] for p in r.json()["pins"]] == [str(survey)]
    assert r.json()["skipped"][0] == {"file_id": "999999999", "code": "not_found",
                                      "message": "no such file or folder"}  # fmt: skip
    first = r.json()["pins"][0]["pin_id"]
    # A DataSteward may pin, but not past the archive date.
    steward = web.browser(w.steward)
    r = steward.post("/v1/pins", {"items": [str(survey)], "tier": "hot", "reason": "x",
                                  "override_archive": True})  # fmt: skip
    assert r.json()["skipped"][0]["code"] == "override_archive_owner_only"
    r = steward.post("/v1/pins", {"items": [str(survey)], "tier": "hot", "reason": "rerun"})
    assert r.status_code == 201 and len(r.json()["pins"]) == 1
    second = r.json()["pins"][0]["pin_id"]
    # The new pin replaced the old one.
    pins = owner.get("/v1/pins", params={"share_id": str(w.share_id)}).json()["items"]
    assert [p["pin_id"] for p in pins] == [second]
    everything = owner.get("/v1/pins", params={"active": "false"}).json()["items"]
    assert {p["pin_id"] for p in everything} >= {first, second}
    # The file browser shows the inherited pin.
    item = owner.get("/v1/files/{file_id}", file_id=a).json()
    assert item["pin"]["tier"] == "hot" and item["pin"]["inherited_from"] == str(survey)
    # Release: a reader can't; the owner can.
    r = web.browser(w.reader).post("/v1/pins/{pin_id}/release", pin_id=second)
    assert r.status_code == 403
    r = owner.post("/v1/pins/{pin_id}/release", pin_id=second)
    assert r.status_code == 200 and r.json()["released_at"] is not None
    assert owner.get("/v1/files/{file_id}", file_id=a).json()["pin"] is None
    # Audited by the catalog trigger, with the person.
    actions = [x for (x,) in sql.execute(
        "SELECT action FROM catalog.audit_log WHERE dataset_id = %s AND action LIKE 'tier_pin.%%'",
        (w.dataset_id,))]  # fmt: skip
    assert actions.count("tier_pin.set") == 2 and "tier_pin.release" in actions
    # Secret items never show up in pin listings for readers who can't read them.
    assert str(secret) not in [p["file_id"] for p in pins]


def test_bad_pin_requests(web, sql):
    w = world(sql)
    owner = web.browser(w.owner)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    for body in (
        {"items": [str(w.root_id)], "tier": "smart", "reason": "x"},
        {"items": [str(w.root_id)], "tier": "hot", "reason": "x", "until": past},
        {"items": [str(w.root_id)], "tier": "hot"},
    ):
        assert owner.post("/v1/pins", body, check=False).status_code == 400, body


def test_keep_online_holds(web, sql):
    w = world(sql)
    a = w.files["a.segy"]
    owner = web.browser(w.owner)
    until = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    r = owner.post("/v1/holds", {"items": [str(a)], "kind": "keep_online", "until": until,
                                 "reason": "processing run"})  # fmt: skip
    assert r.status_code == 201, r.text
    hold = r.json()["holds"][0]
    assert hold["kind"] == "keep_online" and hold["set_by"]["kind"] == "identity"
    detail = owner.get("/v1/files/{file_id}", file_id=a).json()
    assert detail["holds"]["keep_online"] and detail["holds"]["keep_online_until"]
    r = web.browser(w.reader).post("/v1/holds", {"items": [str(a)], "kind": "keep_online",
                                                 "reason": "x"})  # fmt: skip
    assert r.json()["skipped"][0]["code"] == "forbidden"
    listed = owner.get("/v1/holds", params={"kind": "keep_online"}).json()["items"]
    assert hold["hold_id"] in [h["hold_id"] for h in listed]
    assert web.browser(w.reader).get("/v1/holds").json()["items"] == []
    r = web.browser(w.reader).post("/v1/holds/{hold_id}/release", hold_id=hold["hold_id"])
    assert r.status_code == 403
    r = owner.post("/v1/holds/{hold_id}/release", hold_id=hold["hold_id"])
    assert r.status_code == 200 and r.json()["released_at"]


def test_legal_holds_are_archive_admin_with_reauth(web, sql):
    w = world(sql)
    a = w.files["a.segy"]
    r = web.browser(w.owner).post("/v1/holds", {"items": [str(a)], "kind": "legal",
                                                "reason": "litigation"})  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    # An ArchiveAdmin who can read it (a member of the readers group).
    admin = web.browser(w.user("legal", roles=["ArchiveAdmin"], groups=[w.g_readers],
                               sid_rid=3040))  # fmt: skip
    r = admin.post("/v1/holds", {"items": [str(a)], "kind": "legal", "reason": "litigation"})
    assert r.status_code == 201, r.text
    hold = r.json()["holds"][0]
    held = sql.execute("SELECT bool_and(legal_hold) FROM catalog.file_version WHERE file_id = %s",
                       (a,)).fetchone()  # fmt: skip
    assert held == (True,)
    assert admin.get("/v1/files/{file_id}", file_id=a).json()["legal_hold"] is True
    sql.execute("UPDATE catalog.web_session SET auth_time = now() - interval '1 hour'"
                " WHERE entra_object_id = %s", (admin.user["oid"],))  # fmt: skip
    r = admin.post("/v1/holds/{hold_id}/release", hold_id=hold["hold_id"])
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"
    admin.sign_in(admin.user, reauth=True)
    r = admin.post("/v1/holds/{hold_id}/release", hold_id=hold["hold_id"])
    assert r.status_code == 200, r.text
    held = sql.execute("SELECT bool_or(legal_hold) FROM catalog.file_version WHERE file_id = %s",
                       (a,)).fetchone()  # fmt: skip
    assert held == (False,)
