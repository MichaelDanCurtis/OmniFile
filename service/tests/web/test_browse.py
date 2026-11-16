"""Browse, detail, versions and as-of listings, with decision 28's access rules."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from omnifile_service.web.context import make_cursor

from .conftest import _acl, add_dir, add_file, seed


def _names(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [i["name"] for i in resp.json()["items"]]


def world(sql):
    w = seed(sql)
    add_dir(sql, w, "seismic")
    add_file(sql, w, "line_1042.segy", parent=w.files["seismic"], versions=2)
    add_file(sql, w, "secret.segy", acl=w.acl_secret)
    add_file(sql, w, "old.segy", tier="archive", state="stub")
    add_file(sql, w, "a.txt")
    return w


def test_reader_sees_only_what_their_groups_can_read(web, sql):
    w = world(sql)
    r = web.browser(w.reader).get("/v1/dirs/{dir_id}/children", dir_id=w.root_id)
    assert _names(r) == ["a.txt", "old.segy", "seismic"]
    body = r.json()
    assert body["dir"]["share_id"] == str(w.share_id)
    assert body["dir"]["rewind_depth_days"] == 30
    old = next(i for i in body["items"] if i["name"] == "old.segy")
    assert old["tier"]["logical"] == "archive" and old["tier"]["archived"] is True
    assert old["state"] == "stub"
    owner = web.browser(w.owner).get("/v1/dirs/{dir_id}/children", dir_id=w.root_id)
    assert "secret.segy" in _names(owner)


def test_unreadable_items_are_404_never_403(web, sql):
    w = world(sql)
    reader = web.browser(w.reader)
    secret = w.files["secret.segy"]
    vid = w.versions["secret.segy"][0]
    for template, kw in (
        ("/v1/files/{file_id}", {"file_id": secret}),
        ("/v1/files/{file_id}/versions", {"file_id": secret}),
        ("/v1/files/{file_id}/recall-options", {"file_id": secret}),
        ("/v1/dirs/{dir_id}/children", {"dir_id": secret}),
        ("/v1/files/{file_id}", {"file_id": 999_999_999}),
    ):
        r = reader.get(template, **kw)
        assert r.status_code == 404 and r.json()["code"] == "not_found", (template, r.text)
    for template in ("/v1/files/{file_id}/versions/{version_id}/restore",
                     "/v1/files/{file_id}/versions/{version_id}/downloads"):  # fmt: skip
        body = {"target": "restored"} if template.endswith("restore") else {}
        r = reader.post(template, body, file_id=secret, version_id=vid)
        assert r.status_code == 404, r.text
    # A readable file's id with another file's version id: no IDOR.
    r = reader.post("/v1/files/{file_id}/versions/{version_id}/downloads", {},
                    file_id=w.files["a.txt"], version_id=vid)  # fmt: skip
    assert r.status_code == 404
    r = reader.post("/v1/recalls", {"file_id": str(secret), "priority": "standard"})
    assert r.status_code == 404


def test_outsiders_and_cloud_only_users_see_nothing(web, sql):
    w = world(sql)
    for user in (w.outsider, {**w.user("cloud", sid_rid=3020), "sid": None},
                 w.admin):  # roles never grant read  # fmt: skip
        b = web.browser(user)
        assert b.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id).status_code == 404
        assert b.get("/v1/files/{file_id}", file_id=w.files["a.txt"]).status_code == 404


def test_deny_entries_win_over_group_grants(web, sql):
    w = world(sql)
    denied = _acl(sql, w.dataset_id, [w.g_readers], deny=[w.reader["sid"]])
    add_file(sql, w, "denied.txt", acl=denied)
    other = web.browser(w.user("r2", groups=[w.g_readers], sid_rid=3030))
    assert "denied.txt" in _names(other.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id))
    reader = web.browser(w.reader)
    assert "denied.txt" not in _names(reader.get("/v1/dirs/{dir_id}/children", dir_id=w.root_id))
    assert reader.get("/v1/files/{file_id}", file_id=w.files["denied.txt"]).status_code == 404


def test_file_detail_hides_unreadable_ancestors(web, sql):
    w = world(sql)
    private = add_dir(sql, w, "private", acl=w.acl_secret)
    add_file(sql, w, "shared.txt", parent=private)
    r = web.browser(w.reader).get("/v1/files/{file_id}", file_id=w.files["shared.txt"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["path"][0] == {"file_id": str(w.root_id), "name": ""}
    assert d["path"][1] == {"file_id": None, "name": None}
    assert d["path"][2]["name"] == "shared.txt"
    assert d["versions_count"] == 1 and d["mode"] == "0644"
    assert d["dataset_id"] == str(w.dataset_id)
    # But the folder itself can't be listed.
    r = web.browser(w.reader).get("/v1/dirs/{dir_id}/children", dir_id=private)
    assert r.status_code == 404


def test_pins_and_holds_are_inherited_from_folders(web, sql):
    w = world(sql)
    folder = w.files["seismic"]
    owner = sql.execute(
        "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
        (w.owner["sid"],),
    ).fetchone()[0]
    sql.execute(
        "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, set_by_identity_id)"
        " VALUES (%s, %s, 'cold', %s)",
        (w.dataset_id, folder, owner),
    )
    sql.execute(
        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id)"
        " VALUES (%s, %s, 'keep_online', 'x', %s)",
        (w.dataset_id, folder, owner),
    )
    r = web.browser(w.reader).get("/v1/dirs/{dir_id}/children", dir_id=folder)
    [item] = r.json()["items"]
    assert item["pin"]["tier"] == "cold" and item["pin"]["inherited_from"] == str(folder)
    assert item["holds"]["keep_online"] is True and item["holds"]["keep_online_until"] is None


def test_pagination_and_cursors(web, sql):
    w = seed(sql)
    for i in range(5):
        add_file(sql, w, f"f{i}.txt")
    b = web.browser(w.reader)
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        r = b.get("/v1/dirs/{dir_id}/children", params=params, dir_id=w.root_id)
        seen += _names(r)
        cursor = r.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"f{i}.txt" for i in range(5)]
    # Tampered and expired cursors.
    r = b.get("/v1/dirs/{dir_id}/children", params={"cursor": "abc.def"}, dir_id=w.root_id)
    assert r.status_code == 400
    old = make_cursor(web.services.settings.cursor_key, "children", ["f0.txt"],
                      datetime.now(UTC) - timedelta(hours=2))  # fmt: skip
    r = b.get("/v1/dirs/{dir_id}/children", params={"cursor": old}, dir_id=w.root_id)
    assert r.status_code == 410 and r.json()["code"] == "cursor_expired"
    # A cursor from another listing is refused.
    other = make_cursor(web.services.settings.cursor_key, "shares", [1], datetime.now(UTC))
    r = b.get("/v1/dirs/{dir_id}/children", params={"cursor": other}, dir_id=w.root_id)
    assert r.status_code == 400
    assert b.get("/v1/dirs/{dir_id}/children", params={"limit": 1001},
                 dir_id=w.root_id).status_code == 400  # fmt: skip


def test_versions_and_as_of(web, sql):
    w = world(sql)
    fid = w.files["line_1042.segy"]
    b = web.browser(w.reader)
    r = b.get("/v1/files/{file_id}/versions", file_id=fid)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [v["seq"] for v in items] == [2, 1]
    assert items[0]["superseded_at"] is None and items[1]["superseded_at"] is not None
    assert all(v["restorable"] for v in items)
    # The folder as it was 12 hours ago: the first version.
    at = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    r = b.get("/v1/dirs/{dir_id}/children", params={"as_of": at}, dir_id=w.files["seismic"])
    assert r.status_code == 200, r.text
    [entry] = r.json()["items"]
    assert entry["version_id"] == str(w.versions["line_1042.segy"][0])
    assert entry["current"] is False and entry["live"] is True
    assert entry["tier"]["logical"] == "hot"
    # Beyond the share's 30-day depth, and in the future.
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    r = b.get("/v1/dirs/{dir_id}/children", params={"as_of": old}, dir_id=w.root_id)
    assert r.status_code == 410 and r.json()["code"] == "beyond_rewind_depth"
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = b.get("/v1/dirs/{dir_id}/children", params={"as_of": future}, dir_id=w.root_id)
    assert r.status_code == 400


def test_old_versions_need_the_acl_they_had(web, sql):
    """Decision 30: a version is shown only if its own ACL allowed the user."""
    w = seed(sql)
    fid = add_file(sql, w, "report.pdf", versions=2)
    sql.execute(
        "UPDATE catalog.file_version SET acl_id = %s WHERE version_id = %s",
        (w.acl_secret, w.versions["report.pdf"][0]),
    )
    r = web.browser(w.reader).get("/v1/files/{file_id}/versions", file_id=fid)
    assert [v["seq"] for v in r.json()["items"]] == [2]
    r = web.browser(w.owner).get("/v1/files/{file_id}/versions", file_id=fid)
    assert [v["seq"] for v in r.json()["items"]] == [2, 1]


def test_cursors_live_on_wall_clock_time_not_the_service_clock(web, sql):
    """In dev the service clock can run far from real time (WebSettings.
    service_clock); cursors are stamped and checked on the wall clock, so
    they last an hour either way."""
    w = seed(sql)
    for i in range(3):
        add_file(sql, w, f"c{i}.txt")
    b = web.browser(w.reader)
    for offset in (timedelta(hours=16), -timedelta(hours=2)):
        web.services.settings.service_clock = lambda o=offset: datetime.now(UTC) + o
        r = b.get("/v1/dirs/{dir_id}/children", params={"limit": 2}, dir_id=w.root_id)
        cursor = r.json()["next_cursor"]
        stamped = json.loads(base64.urlsafe_b64decode(cursor.split(".")[0] + "=="))["t"]
        assert abs(stamped - datetime.now(UTC).timestamp()) < 60
        r = b.get("/v1/dirs/{dir_id}/children", params={"limit": 2, "cursor": cursor},
                  dir_id=w.root_id)  # fmt: skip
        assert r.status_code == 200, r.text
        assert _names(r) == ["c2.txt"]
