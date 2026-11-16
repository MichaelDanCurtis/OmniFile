"""Getting an annexed file back (portal-api.md 5.25; annex.md 4.4-4.6;
phase A3.1, which writes nothing to any NAS or share)."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from psycopg.types.json import Jsonb

from omnifile_service.annex import rules as R

from .conftest import World, add_file, seed
from .test_annex import _annex, _dataset, _filer

ORIGIN = {"kind": "nas_folder", "origin": True}


def _acl(sql, ds: int, read: list[str], *, write: list[str] | None = None,
         write_deny: list[str] | None = None,
         search: list[str] | None = ("S-1-1-0",)) -> int:  # fmt: skip
    """`search`: who a folder with it lets through (decision 63), Everyone unless said."""
    (acl_id,) = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first, write_sids, write_deny_sids, search_sids, search_deny_sids)"
        " VALUES (%s, %s, 488, '[]', %s, '{}', true, %s, %s, %s, '{}') RETURNING acl_id",
        (
            ds,
            uuid.uuid4().bytes * 2,
            read,
            write,
            write_deny or [],
            list(search) if search is not None else None,
        ),
    ).fetchone()
    return acl_id


@dataclasses.dataclass
class Setup:
    w: World
    filer: dict
    annex: dict
    ds: int
    dw: World

    @property
    def aid(self) -> str:
        return self.annex["annex_id"]


def _setup(web, sql, *, rules: list[dict] | None = None) -> Setup:
    """An annex over `long-term-storage` whose running rule set is Copy plus
    the given return rules (approved by an ArchiveAdmin)."""
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    (root,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL", (ds,)
    ).fetchone()
    dw = dataclasses.replace(w, dataset_id=ds, root_id=root)
    body = R.copy_template(ds)
    body["rules"] += rules if rules is not None else R.return_rules("origin")
    owner = web.browser(w.owner)
    r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": body}, annex_id=a["annex_id"])
    assert r.status_code == 201, r.text
    etag = admin.get("/v1/annexes/{annex_id}", annex_id=a["annex_id"]).headers["ETag"]
    r = admin.post(
        "/v1/annexes/{annex_id}/rulesets/{version}/approve",
        {},
        headers={"If-Match": etag},
        annex_id=a["annex_id"],
        version=r.json()["version"],
    )
    assert r.status_code == 200, r.text
    return Setup(w, f, a, ds, dw)


def _removed(sql, s: Setup, path: str, *, acl: int, tier: str = "hot",
             state: str = "removed", size: int = 4096, versions: int = 1) -> int:  # fmt: skip
    fid = add_file(sql, s.dw, path.replace("/", "_"), acl=acl, tier=tier, size=size,
                   state="remote", versions=versions)  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size, mtime_ns,"
        " first_seen, last_seen, nas_state, copied_size) VALUES (%s, %s, %s, %s, %s, 1, now(),"
        " now(), %s, %s)",
        (int(s.aid), fid, path, str(fid), size, state, size),
    )
    return fid


def _folder(sql, s: Setup, path: str, *, write: list[str] | None, error: str | None = None,
            write_deny: list[str] | None = None,
            search: list[str] | None = None) -> None:  # fmt: skip
    """`search`: who it lets through (decision 63), Everyone unless said."""
    acl = None if error else _acl(sql, s.ds, [], write=write, write_deny=write_deny,
                                  search=search or ["S-1-1-0"])  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.nas_folder_acl (filer_id, path, acl_id, error, annex_id,"
        " observed_at) VALUES (%s, %s, %s, %s, %s, now())"
        " ON CONFLICT (filer_id, path) DO UPDATE SET acl_id = EXCLUDED.acl_id,"
        " error = EXCLUDED.error",
        (int(s.filer["filer_id"]), path, acl, error, int(s.aid)),
    )


def test_return_targets_offer_only_folders_the_requester_could_add_a_file_to(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "surveys/line_1042.segy", acl=readable)
    # The original folder lets the Owners add files, not the readers.
    _folder(sql, s, "long-term-storage/surveys", write=[w.g_owners])
    owner, reader = web.browser(w.owner), web.browser(w.reader)
    r = owner.get("/v1/files/{file_id}/return-targets", file_id=fid)
    assert r.status_code == 200, r.text
    [t] = r.json()["items"]
    assert t["default"] and t["allowed"] and t["on_conflict"] == "restored_name"
    assert t["hold_days"] == 14 and t["target"]["origin"]
    assert t["target"]["folder"] == "long-term-storage/surveys"
    assert t["display"].endswith("\\geo\\long-term-storage\\surveys\\line_1042.segy")
    [t] = reader.get("/v1/files/{file_id}/return-targets", file_id=fid).json()["items"]
    assert not t["allowed"] and "can't add files" in t["why_not"]
    # Someone the file's ACL doesn't let read it: no such file.
    out = web.browser(w.outsider).get("/v1/files/{file_id}/return-targets", file_id=fid)
    assert out.status_code == 404
    # A deny wins over the Owners' allow, an ACL the edge couldn't read lets
    # nobody in, and a folder whose ACL isn't known yet isn't offered.
    _folder(sql, s, "long-term-storage/surveys", write=[w.g_owners], write_deny=[w.g_owners])
    [t] = owner.get("/v1/files/{file_id}/return-targets", file_id=fid).json()["items"]
    assert not t["allowed"]
    # Decision 63: a folder whose ACL can't be read hides the file below it from
    # everyone, so it isn't even there to place.
    _folder(sql, s, "long-term-storage/surveys", write=None, error="no NT security descriptor")
    r = owner.get("/v1/files/{file_id}/return-targets", file_id=fid)
    assert r.status_code == 404, r.text
    # A folder gone from the NAS still judges the file (its last ACL is kept), but
    # nothing is placed there.
    _folder(sql, s, "long-term-storage/surveys", write=[w.g_owners])
    sql.execute(
        "UPDATE catalog.nas_folder_acl SET gone_at = now() WHERE path = 'long-term-storage/surveys'"
    )
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    [t] = owner.get("/v1/files/{file_id}/return-targets", file_id=fid).json()["items"]
    assert not t["allowed"] and "no longer on the NAS" in t["why_not"]


def test_a_placement_waits_ready_and_a_second_request_joins_it(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner = web.browser(w.owner)
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default",
                                      "note": "reprocessing line 1042"})  # fmt: skip
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["state"] == "ready" and p["recall_id"] is None and not p["joined"]
    assert p["target"] == {"kind": "nas", "folder": "long-term-storage", "name": "a.segy",
                           "filer_id": s.filer["filer_id"], "origin": True,
                           "display": p["target"]["display"]}  # fmt: skip
    assert p["on_conflict"] == "restored_name" and p["hold_days"] == 14
    again = owner.post("/v1/placements", {"file_id": str(fid), "place_at": ORIGIN})
    assert again.status_code == 200 and again.json()["joined"]
    assert again.json()["placement_id"] == p["placement_id"]
    # Seen by the requester and by an ArchiveAdmin; to anyone else, no such thing.
    got = owner.get("/v1/placements/{placement_id}", placement_id=p["placement_id"])
    assert got.status_code == 200 and got.json()["note"] == "reprocessing line 1042"
    assert (
        web.browser(w.admin)
        .get("/v1/placements/{placement_id}", placement_id=p["placement_id"])
        .status_code
        == 200
    )
    assert (
        web.browser(w.reader)
        .get("/v1/placements/{placement_id}", placement_id=p["placement_id"])
        .status_code
        == 404
    )
    # Audited, with an annex event.
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.request'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    (k,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s AND kind ="
        " 'recall_requested'",
        (int(s.aid),),
    ).fetchone()
    assert k == 1


def test_placements_the_rules_or_the_nas_dont_allow_are_refused(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    on_nas = _removed(sql, s, "b.segy", acl=readable, state="present")
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner, reader = web.browser(w.owner), web.browser(w.reader)

    def code(b, body: dict) -> str:
        r = b.post("/v1/placements", body)
        assert r.status_code >= 400, r.text
        return r.json()["code"]

    # The reader may read the file but not add one to its folder.
    assert code(reader, {"file_id": str(fid), "place_at": "default"}) == "target_not_allowed"
    # A target the rules don't list, and a harsher conflict choice than theirs.
    other = {"kind": "nas_folder", "path": "elsewhere"}
    assert code(owner, {"file_id": str(fid), "place_at": other}) == "target_not_allowed"
    harsher = {"file_id": str(fid), "place_at": "default", "on_conflict": "overwrite_keep_version"}
    assert code(owner, harsher) == "target_not_allowed"
    # The origin of a file still on the NAS.
    assert code(owner, {"file_id": str(on_nas), "place_at": "default"}) == "file_present"
    # An online file needs no recall first.
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default",
                                      "priority": "standard"})  # fmt: skip
    assert r.status_code == 400
    # A gentler conflict choice is the requester's to make.
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default",
                                      "on_conflict": "same_content"})  # fmt: skip
    assert r.status_code == 201 and r.json()["on_conflict"] == "same_content"


def test_a_file_in_archive_is_recalled_first(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners])
    fid = _removed(sql, s, "cold.segy", acl=readable, tier="archive")
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner = web.browser(w.owner)
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"})
    assert r.status_code == 400 and "priority" in r.text
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default",
                                      "priority": "standard"})  # fmt: skip
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["state"] == "rehydrating" and p["recall_id"] is not None
    recall = lambda: sql.execute(  # noqa: E731
        "SELECT state, superseded_reason, held_by_jobs FROM catalog.recall WHERE recall_id = %s",
        (int(p["recall_id"]),),
    ).fetchone()
    # A placement is of this version: while it waits, the catalog counts it on its
    # recall, so a newer copy of the file from the NAS does not end the recall
    # (migration 0059).
    assert recall() == ("requested", None, 1)

    def newer_copy() -> None:
        sql.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
            " state, committed_at, size) SELECT file_id, dataset_id, parent_id, name,"
            " 'content', 'committed', now(), 9 FROM catalog.file WHERE file_id = %s",
            (fid,),
        )

    newer_copy()
    assert recall() == ("requested", None, 1)
    placement = owner.get("/v1/placements/{placement_id}", placement_id=p["placement_id"])
    assert placement.json()["state"] == "rehydrating"
    # A step between two waiting states leaves the count alone; an end takes it off.
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'requested' WHERE placement_id = %s",
        (int(p["placement_id"]),),
    )
    assert recall() == ("requested", None, 1)
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'rehydrating' WHERE placement_id = %s",
        (int(p["placement_id"]),),
    )
    # Its requester cancels it. The file has a newer copy by now, so nothing will
    # bring this version to an edge, and no one else waits for it: the recall ends
    # with the placement, as replaced, not left to run for no one (#96, second review).
    r = owner.post("/v1/placements/{placement_id}/cancel", {}, placement_id=p["placement_id"])
    assert r.status_code == 200 and r.json()["state"] == "cancelled", r.text
    assert recall() == ("superseded", "replaced", 0)
    assert {"recall_id": p["recall_id"]} in [
        m for (m,) in sql.execute("SELECT message FROM pgmq.q_notify")
    ]


def test_a_cancelled_placement_leaves_the_recall_of_a_file_that_is_as_it_was(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners])
    fid = _removed(sql, s, "still.segy", acl=readable, tier="archive")
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner = web.browser(w.owner)
    p = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default",
                                      "priority": "standard"}).json()  # fmt: skip
    r = owner.post("/v1/placements/{placement_id}/cancel", {}, placement_id=p["placement_id"])
    assert r.status_code == 200, r.text
    recall = lambda: sql.execute(  # noqa: E731
        "SELECT state, superseded_reason, held_by_jobs FROM catalog.recall WHERE recall_id = %s",
        (int(p["recall_id"]),),
    ).fetchone()
    assert recall() == ("requested", None, 0)
    # A waiting placement that is deleted (nothing does that today) lets go as well.
    p = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default",
                                      "priority": "standard"}).json()  # fmt: skip
    assert recall() == ("requested", None, 1)
    sql.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
        " state, committed_at, size) SELECT file_id, dataset_id, parent_id, name,"
        " 'tombstone', 'committed', now(), NULL FROM catalog.file WHERE file_id = %s",
        (fid,),
    )
    assert recall() == ("requested", None, 1)
    sql.execute(
        "DELETE FROM catalog.annex_placement WHERE placement_id = %s", (int(p["placement_id"]),)
    )
    assert recall() == ("superseded", "deleted", 0)


def test_an_api_client_places_a_file_for_a_person_it_names(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    token = web.app_token(["Recall.Request"])
    r = web.call("POST", "/v1/placements", token=token,
                 json={"file_id": str(fid), "place_at": "default"})  # fmt: skip
    assert r.status_code == 422 and r.json()["code"] == "requester_required"
    r = web.call("GET", "/v1/files/{file_id}/return-targets", token=token, file_id=fid)
    assert r.status_code == 422 and r.json()["code"] == "requester_required"
    person = f"{w.domain}-4545"
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)",
        (person, [w.g_readers]),
    )
    # The person can't read the file: no such file.
    r = web.call("POST", "/v1/placements", token=token,
                 json={"file_id": str(fid), "place_at": "default",
                       "requester": person})  # fmt: skip
    assert r.status_code == 404
    sql.execute(
        "UPDATE catalog.identity SET group_sids = %s WHERE sid = %s", ([w.g_owners], person)
    )
    r = web.call("GET", "/v1/files/{file_id}/return-targets", token=token, file_id=fid,
                 params={"requester": person})  # fmt: skip
    assert r.status_code == 200 and r.json()["items"][0]["allowed"]
    r = web.call("POST", "/v1/placements", token=token,
                 json={"file_id": str(fid), "place_at": "default",
                       "requester": person})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["request_via"] == "api"
    got = web.call("GET", "/v1/placements/{placement_id}", token=token,
                   placement_id=r.json()["placement_id"])  # fmt: skip
    assert got.status_code == 200
    # Without the role, nothing.
    r = web.call("POST", "/v1/placements", token=web.app_token(["Archive.Read"]),
                 json={"file_id": str(fid), "place_at": "default",
                       "requester": person})  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "role_required"


def test_a_share_target_is_offered_to_its_writers(web, sql) -> None:
    # The share must exist before a rule names it.
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    body = R.copy_template(ds)
    body["rules"] += R.return_rules(
        "share", {"kind": "share_folder", "share_id": str(w.share_id), "path": "recalls"}
    )
    # The Owners group is the share's Owners: they can write it.
    owner = web.browser(w.owner)
    r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": body}, annex_id=a["annex_id"])
    assert r.status_code == 201, r.text
    assert r.json()["needs_archive_admin"], "a target outside the annex's own folders"
    etag = admin.get("/v1/annexes/{annex_id}", annex_id=a["annex_id"]).headers["ETag"]
    assert admin.post("/v1/annexes/{annex_id}/rulesets/{version}/approve", {},
                      headers={"If-Match": etag}, annex_id=a["annex_id"],
                      version=r.json()["version"]).status_code == 200  # fmt: skip
    (root,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL", (ds,)
    ).fetchone()
    s = Setup(w, f, a, ds, dataclasses.replace(w, dataset_id=ds, root_id=root))
    readable = _acl(sql, ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    # The share's root folder lets its Owners and Writers add files.
    sql.execute(
        "UPDATE catalog.acl SET entries = %s WHERE acl_id = %s",
        (Jsonb([{"scope": "access", "tag": "group", "sid": w.g_owners, "perms": "rwx"},
                {"scope": "access", "tag": "group", "sid": w.g_writers, "perms": "rwx"},
                {"scope": "access", "tag": "mask", "perms": "rwx"}]), w.acl_open),
    )  # fmt: skip
    [t] = owner.get("/v1/files/{file_id}/return-targets", file_id=fid).json()["items"]
    assert t["allowed"] and t["target"]["kind"] == "share", t["why_not"]
    assert t["target"]["folder"] == "recalls" and t["on_conflict"] == "overwrite_keep_version"
    # A reader of the file who isn't a Writer of the share.
    [t] = (
        web.browser(w.reader).get("/v1/files/{file_id}/return-targets", file_id=fid).json()["items"]
    )
    assert not t["allowed"] and "Owner or Writer" in t["why_not"]
    # An NT ACL with a deny can't be carried into a share's POSIX ACL: a
    # group's deny can't win over another group's allow there. Not offered.
    denying = _acl(sql, ds, [w.g_owners, w.g_readers])
    sql.execute(
        "UPDATE catalog.acl SET raw_kind = 'ntfs_sd', deny_sids = %s WHERE acl_id = %s",
        ([w.g_other], denying),
    )
    fid2 = _removed(sql, s, "b.segy", acl=denying)
    [t] = owner.get("/v1/files/{file_id}/return-targets", file_id=fid2).json()["items"]
    assert not t["allowed"] and "denies some readers" in t["why_not"]
    r = owner.post("/v1/placements", {"file_id": str(fid2), "place_at": "default"})
    assert r.status_code == 403 and r.json()["code"] == "target_not_allowed"


def test_return_rules_are_checked_where_they_place_files(web, sql) -> None:
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    owner = web.browser(w.owner)

    def checks(body: dict) -> set[str]:
        r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": body}, annex_id=a["annex_id"])
        if r.status_code == 201:
            return set()
        assert r.status_code == 422, r.text
        return {c["check"] for c in r.json()["rule_checks"]}

    # Move now's folder as the return target: it would go straight out again.
    move = R.template("move_now", ds)
    move["rules"] += R.return_rules("origin")
    assert "return_into_drop_folder" in checks(move)
    # An age removal over the target needs a hold.
    archive = R.template("archive_after", ds, 30)
    archive["rules"] = R.outbound(archive["rules"]) + [
        {"id": "back", "trigger": "recall_completed", "conditions": {},
         "actions": [{"place_at": ORIGIN}]}]  # fmt: skip
    assert "return_without_hold" in checks(archive)
    assert checks(R.template("archive_after", ds, 30)) == set(), "the template holds 14 days"

    # Overwrites only inside the annex; locations that don't exist or can't be reached.
    def back(loc: dict, **extra) -> dict:
        b = R.copy_template(ds)
        b["rules"].append({"id": "back", "trigger": "recall_completed", "conditions": {},
                           "actions": [{"place_at": loc}, *extra.get("more", [])]})  # fmt: skip
        return b

    assert "overwrite_outside_annex" in checks(back(
        {"kind": "nas_folder", "filer_id": f["filer_id"], "path": "restore"},
        more=[{"on_conflict": "overwrite_keep_version"}]))  # fmt: skip
    assert "unknown_location" in checks(back({"kind": "share_folder", "share_id": "999999999"}))
    assert "unknown_location" in checks(back({"kind": "nas_folder", "annex_id": "999999999"}))
    # A share the proposer can't write to (an outsider's share).
    other = seed(sql)
    assert "location_unreachable" in checks(back(
        {"kind": "share_folder", "share_id": str(other.share_id)}))  # fmt: skip
    # A filer folder outside every annex is fine; the edge reads its ACLs.
    assert (
        checks(back({"kind": "nas_folder", "filer_id": f["filer_id"], "path": "restore"})) == set()
    )


def test_the_return_preview_is_an_archive_admins(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners])
    for i in range(3):
        _removed(sql, s, f"r{i}.segy", acl=readable, size=100)
    _removed(sql, s, "still.segy", acl=readable, state="present")
    sql.execute(
        "INSERT INTO catalog.nas_target_space (filer_id, path, free_bytes, total_bytes,"
        " annex_id, observed_at) VALUES (%s, 'long-term-storage', 5000, 9000, %s, now())",
        (int(s.filer["filer_id"]), int(s.aid)),
    )
    r = web.browser(w.owner).post("/v1/annexes/{annex_id}/return-preview", {}, annex_id=s.aid)
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    r = web.browser(w.admin).post("/v1/annexes/{annex_id}/return-preview", {}, annex_id=s.aid)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["files"] == 3 and not p["truncated"]
    [rule] = p["rules"]
    assert (rule["id"], rule["files"], rule["bytes"]) == ("back", 3, 300)
    [t] = rule["targets"]
    assert t["location"] == ORIGIN and t["free_bytes"] == 5000 and t["names_taken"] == 0
    # A draft whose rule names requesters: what it may take.
    draft = R.copy_template(s.ds)
    draft["rules"] += [
        {"id": "proc", "trigger": "recall_completed",
         "conditions": {"requester_in": [w.g_writers]},
         "actions": [{"exclude": True}]},
        *R.return_rules("origin")]  # fmt: skip
    p = (
        web.browser(w.admin)
        .post("/v1/annexes/{annex_id}/return-preview", {"body": draft}, annex_id=s.aid)
        .json()
    )
    assert [(x["id"], x["files"], x["may_take"]) for x in p["rules"]] == [
        ("proc", 0, 3), ("back", 3, 0)]  # fmt: skip


def test_an_api_client_whose_owner_lost_access_gets_nothing(web, sql) -> None:
    """#67 review: every route checks the client's owner first."""
    from .conftest import APP_OWNER_SID

    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    person = f"{w.domain}-4646"
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)",
        (person, [w.g_owners]),
    )
    token = web.app_token(["Recall.Request", "Archive.Read"])
    sql.execute(
        "UPDATE catalog.identity SET app_roles_seen_at = now() - interval '60 days' WHERE sid = %s",
        (APP_OWNER_SID,),
    )
    try:
        r = web.call("GET", "/v1/files/{file_id}/return-targets", token=token, file_id=fid,
                     params={"requester": person})  # fmt: skip
        assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
        r = web.call("POST", "/v1/placements", token=token,
                     json={"file_id": str(fid), "place_at": "default",
                           "requester": person})  # fmt: skip
        assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
        r = web.call("POST", "/v1/annexes/{annex_id}/return-preview", token=token, json={},
                     annex_id=s.aid)  # fmt: skip
        assert r.status_code == 403 and r.json()["code"] == "owner_lacks_access"
        (n,) = sql.execute(
            "SELECT count(*) FROM catalog.annex_placement WHERE file_id = %s", (fid,)
        ).fetchone()
        assert n == 0
    finally:
        sql.execute(
            "UPDATE catalog.identity SET app_roles_seen_at = now() WHERE sid = %s",
            (APP_OWNER_SID,),
        )


def test_a_placement_that_cant_go_on_can_be_cancelled_before_its_go(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner = web.browser(w.owner)
    p = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"}).json()
    pid = p["placement_id"]
    # Not someone else's to cancel, or even to know about.
    r = web.browser(w.reader).post("/v1/placements/{placement_id}/cancel", {}, placement_id=pid)
    assert r.status_code == 404
    # Once it has its `go` the file may be under its name.
    sql.execute("UPDATE catalog.annex_placement SET state = 'placing' WHERE placement_id = %s",
                (int(pid),))  # fmt: skip
    r = owner.post("/v1/placements/{placement_id}/cancel", {}, placement_id=pid)
    assert r.status_code == 409 and r.json()["code"] == "placement_not_cancellable"
    sql.execute("UPDATE catalog.annex_placement SET state = 'stuck', before_stuck = 'placing'"
                " WHERE placement_id = %s", (int(pid),))  # fmt: skip
    r = owner.post("/v1/placements/{placement_id}/cancel", {}, placement_id=pid)
    assert r.status_code == 409
    # Stuck before its `go`: this is the way out.
    sql.execute("UPDATE catalog.annex_placement SET state = 'stuck', before_stuck = 'writing'"
                " WHERE placement_id = %s", (int(pid),))  # fmt: skip
    r = owner.post("/v1/placements/{placement_id}/cancel", {}, placement_id=pid)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "cancelled"
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.cancelled'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    r = owner.post("/v1/placements/{placement_id}/cancel", {}, placement_id=pid)
    assert r.status_code == 409


def test_an_owner_clears_the_breakers_pin_and_it_is_audited(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable, state="present")
    sql.execute("UPDATE catalog.annex_file SET pinned_by_breaker = true WHERE file_id = %s", (fid,))
    path = "/v1/annexes/{annex_id}/files/{file_id}/clear-breaker"
    r = web.browser(w.reader).post(path, {}, annex_id=s.aid, file_id=fid)
    assert r.status_code in (403, 404), r.text
    (pinned,) = sql.execute(
        "SELECT pinned_by_breaker FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert pinned
    r = web.browser(w.admin).post(path, {}, annex_id=s.aid, file_id=fid)
    assert r.status_code == 200, r.text
    row = sql.execute(
        "SELECT pinned_by_breaker, breaker_cleared_at IS NOT NULL FROM catalog.annex_file"
        " WHERE file_id = %s",
        (fid,),
    ).fetchone()
    assert row == (False, True)
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.breaker.cleared'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1


def _placed_on(sql, p: dict, **cols) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    sql.execute(f"UPDATE catalog.annex_placement SET {sets} WHERE placement_id = %s",
                (*cols.values(), int(p["placement_id"])))  # fmt: skip


def test_an_archive_admin_cancels_someone_elses_placement(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    p = web.browser(w.owner).post("/v1/placements", {"file_id": str(fid),
                                                     "place_at": "default"}).json()  # fmt: skip
    r = web.browser(w.admin).post("/v1/placements/{placement_id}/cancel", {},
                                  placement_id=p["placement_id"])  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "cancelled"


def test_a_second_request_joins_a_stuck_placement(web, sql) -> None:
    """N4: it was a 500 from the unique index."""
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner = web.browser(w.owner)
    body = {"file_id": str(fid), "place_at": "default"}
    p = owner.post("/v1/placements", body).json()
    _placed_on(sql, p, state="stuck", before_stuck="writing")
    again = owner.post("/v1/placements", body)
    assert again.status_code == 200, again.text
    assert again.json()["joined"] and again.json()["placement_id"] == p["placement_id"]


def test_abandon_only_after_a_day_of_silence_and_an_archive_admins(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable)
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    p = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"}).json()
    path, pid = "/v1/placements/{placement_id}/abandon", p["placement_id"]

    def abandon(b):
        return b.post(path, {}, placement_id=pid)

    # Before its `go`, cancel is the way.
    r = abandon(admin)
    assert r.status_code == 409 and r.json()["code"] == "placement_not_abandonable"
    _placed_on(sql, p, state="placing")
    assert abandon(owner).status_code == 403, "not even its requester"
    # The edge reported just now.
    sql.execute("UPDATE catalog.annex_placement SET last_report_at = now()"
                " WHERE placement_id = %s", (int(pid),))  # fmt: skip
    r = abandon(admin)
    assert r.status_code == 409 and "can be abandoned" in r.json()["detail"]
    # A day of silence.
    sql.execute("UPDATE catalog.annex_placement SET last_report_at = now() - interval '25 hours'"
                " WHERE placement_id = %s", (int(pid),))  # fmt: skip
    r = abandon(admin)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["fail_reason"]) == ("failed", "abandoned")
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.placement.abandoned'"
        " AND file_id = %s",
        (fid,),
    ).fetchone()
    assert n == 1
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'problem' AND file_id = %s"
        " AND details->>'reason' = 'placement_abandoned'",
        (fid,),
    ).fetchone()
    assert n == 1
    # A stuck one too, the same way.
    assert abandon(admin).status_code == 409, "ended"


def test_a_file_on_a_retired_annex_ends_its_placements_and_says_so(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    a, b = (_removed(sql, s, f"{n}.segy", acl=readable) for n in "ab")
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    pa = owner.post("/v1/placements", {"file_id": str(a), "place_at": "default"}).json()
    pb = owner.post("/v1/placements", {"file_id": str(b), "place_at": "default"}).json()
    _placed_on(sql, pa, state="writing")
    etag = admin.get("/v1/annexes/{annex_id}", annex_id=s.aid).headers["ETag"]
    r = admin.call("PATCH", "/v1/annexes/{annex_id}", json={"state": "retired"},
                   headers={"If-Match": etag}, annex_id=s.aid)  # fmt: skip
    assert r.status_code == 200, r.text
    rows = dict(sql.execute(
        "SELECT placement_id, fail_reason FROM catalog.annex_placement WHERE annex_id = %s",
        (int(s.aid),),
    ).fetchall())  # fmt: skip
    assert rows == {int(pa["placement_id"]): "annex_retired",
                    int(pb["placement_id"]): "annex_retired"}  # fmt: skip
    details = {x[0]: x[1] for x in sql.execute(
        "SELECT file_id, details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
        " AND details->>'reason' = 'placement_annex_retired'",
        (int(s.aid),),
    ).fetchall()}  # fmt: skip
    assert ".omnifile-staging-" in details[a]["detail"], "under way: a staged copy may remain"
    assert "nothing on the NAS" in details[b]["detail"]


def test_the_kill_switch_is_an_archive_admins_and_audited(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    path = "/v1/annexes/{annex_id}"

    def patch(b, body):
        etag = admin.get(path, annex_id=s.aid).headers["ETag"]
        return b.call("PATCH", path, json=body, headers={"If-Match": etag}, annex_id=s.aid)

    assert patch(owner, {"placements_paused": True}).status_code == 403
    r = patch(admin, {"placements_paused": True})
    assert r.status_code == 200, r.text
    assert r.json()["placements_paused"] is True
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'annex.settings'"
        " AND details->>'placements_paused' = 'true'"
    ).fetchone()
    assert n == 1
    # Settings', for every annex.
    r = admin.get("/v1/settings/annex-defaults")
    assert r.json()["placements_paused"] is False
    r = admin.call("PUT", "/v1/settings/annex-defaults", json={"placements_paused": True},
                   headers={"If-Match": r.headers["ETag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["placements_paused"] is True
    r = web.browser(w.owner).call("PUT", "/v1/settings/annex-defaults",
                                  json={"placements_paused": False},
                                  headers={"If-Match": r.headers["ETag"]})  # fmt: skip
    assert r.status_code == 403
    sql.execute("UPDATE catalog.annex_defaults SET placements_paused = false")


PLACER = "S-1-5-21-1-2-3-1112"


def _reports(sql, s: Setup, report: dict | None, *, ago: str = "0 seconds") -> None:
    """What the filer's edge last said (`tests/api` sends it the real way)."""
    sql.execute(
        "UPDATE catalog.filer SET placement_account = %s,"
        " placement_account_at = now() - %s::interval WHERE filer_id = %s",
        (Jsonb(report) if report is not None else None, ago, int(s.filer["filer_id"])),
    )


GOOD_REPORT = {"sid": PLACER, "kind": "user", "name": "restorer@OMNI.TEST"}


def _patch_filer(admin, s: Setup, sid, etag: str | None = None):
    path, fid = "/v1/filers/{filer_id}", s.filer["filer_id"]
    h = {"If-Match": etag or admin.get(path, filer_id=fid).headers["ETag"]}
    body = {"placement_sid": sid}
    return admin.call("PATCH", path, json=body, headers=h, filer_id=fid, check=False)


def test_the_placement_account_is_registered_by_an_archive_admin(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    _reports(sql, s, GOOD_REPORT)
    path = "/v1/filers/{filer_id}"
    fid = s.filer["filer_id"]
    admin = web.browser(w.admin)
    assert _patch_filer(web.browser(w.owner), s, PLACER, etag='"1"').status_code == 403
    assert _patch_filer(admin, s, "nope").status_code in (400, 422)
    # If-Match, as for any change that decides who counts as whom.
    r = admin.call("PATCH", path, json={"placement_sid": PLACER}, filer_id=fid, check=False)
    assert r.status_code == 428, r.text
    assert _patch_filer(admin, s, PLACER, etag='"0"').status_code == 412
    r = _patch_filer(admin, s, PLACER)
    assert r.status_code == 200, r.text
    assert r.json()["placement_sid"] == PLACER
    # `{}` says nothing, so it clears nothing. Clearing is an explicit null.
    etag = admin.get(path, filer_id=fid).headers["ETag"]
    r = admin.call("PATCH", path, json={}, headers={"If-Match": etag}, filer_id=fid, check=False)
    assert r.status_code in (400, 422)
    assert admin.get(path, filer_id=fid).json()["placement_sid"] == PLACER
    r = _patch_filer(admin, s, None)
    assert r.status_code == 200 and r.json()["placement_sid"] is None
    (details,) = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'filer.placement_account'"
        " ORDER BY at DESC LIMIT 1"
    ).fetchone()
    assert details["was"] == PLACER and details["placement_sid"] is None
    assert details["reported"]["sid"] == PLACER


def test_a_placement_account_is_only_what_the_filers_edge_reports_and_a_refusal_says_why(
    web, sql
) -> None:
    s = _setup(web, sql)
    admin = web.browser(s.w.admin)

    def refused(sid, code):
        r = _patch_filer(admin, s, sid)
        assert r.status_code == 400, r.text
        assert r.json()["errors"][0]["message"].startswith(code), r.text
        got = admin.get("/v1/filers/{filer_id}", filer_id=s.filer["filer_id"]).json()
        assert got["placement_sid"] is None

    refused(PLACER, "no_report")
    # Winbind away, or no such account: the edge says so, and that is no report.
    _reports(sql, s, {"error": "winbind unavailable"})
    refused(PLACER, "no_report")
    # A report over 6 hours old isn't what it currently says.
    _reports(sql, s, GOOD_REPORT, ago="6 hours 1 minute")
    refused(PLACER, "stale_report")
    _reports(sql, s, GOOD_REPORT, ago="5 hours 59 minutes")
    assert _patch_filer(admin, s, PLACER).status_code == 200
    assert _patch_filer(admin, s, None).status_code == 200
    # Another account than the one it reports.
    _reports(sql, s, GOOD_REPORT)
    refused("S-1-5-21-1-2-3-9999", "different_sid")
    # What it reports isn't one account.
    for kind in ("group", "alias", "well_known", "other_7"):
        _reports(sql, s, {**GOOD_REPORT, "kind": kind})
        refused(PLACER, "not_an_account")
    # A machine account is one, by either name.
    _reports(sql, s, {**GOOD_REPORT, "kind": "computer"})
    assert _patch_filer(admin, s, PLACER).status_code == 200
    assert _patch_filer(admin, s, None).status_code == 200
    # Well-known, whatever the edge calls it.
    for sid in ("S-1-1-0", "S-1-5-11", "S-1-5-32-544"):
        _reports(sql, s, {"sid": sid, "kind": "user", "name": "x"})
        refused(sid, "well_known")
    # The catalog knows it as a group: it may know better.
    _reports(sql, s, GOOD_REPORT)
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind) VALUES (%s, 'group')"
        " ON CONFLICT (sid) DO UPDATE SET kind = 'group'",
        (PLACER,),
    )
    refused(PLACER, "not_an_account")


def test_only_an_smb_filer_has_a_placement_account(web, sql) -> None:
    s = _setup(web, sql)
    admin = web.browser(s.w.admin)
    sql.execute(
        "UPDATE catalog.filer SET protocol = 'nfs3', event_source = 'scan' WHERE filer_id = %s",
        (int(s.filer["filer_id"]),),
    )
    _reports(sql, s, GOOD_REPORT)
    r = _patch_filer(admin, s, PLACER)
    assert r.status_code == 400 and r.json()["errors"][0]["message"].startswith("not_smb")
    got = admin.get("/v1/filers/{filer_id}", filer_id=s.filer["filer_id"]).json()
    assert got["placement_account_report"] is None


def test_a_person_sees_what_the_edge_reports_before_registering_it(web, sql) -> None:
    s = _setup(web, sql)
    admin = web.browser(s.w.admin)

    def shown():
        got = admin.get("/v1/filers/{filer_id}", filer_id=s.filer["filer_id"]).json()
        return got["placement_account_report"]

    r = shown()
    assert (r["sid"], r["can_register"], r["why_not"]) == (None, False, "no_report")
    # An ArchiveAdmin's, who alone can act on it.
    _reports(sql, s, GOOD_REPORT)
    path, fid = "/v1/filers/{filer_id}", s.filer["filer_id"]
    for b in (web.browser(s.w.owner), web.browser(s.w.steward)):
        got = b.get(path, filer_id=fid)
        if got.status_code == 200:
            assert got.json()["placement_account_report"] is None
    got = web.browser(s.w.owner).get("/v1/annexes/{annex_id}", annex_id=s.aid).json()
    assert got["filer"]["placement_account_report"] is None
    assert (
        admin.get("/v1/annexes/{annex_id}", annex_id=s.aid).json()["filer"][
            "placement_account_report"
        ]["sid"]
        == PLACER
    )
    _reports(sql, s, None)
    _reports(sql, s, GOOD_REPORT)
    r = shown()
    assert r["sid"] == PLACER and r["kind"] == "user" and r["name"] == "restorer@OMNI.TEST"
    assert r["can_register"] is True and r["why_not"] is None and r["reported_at"]
    # It registers nothing by itself.
    got = admin.get("/v1/filers/{filer_id}", filer_id=s.filer["filer_id"]).json()
    assert got["placement_sid"] is None
    _reports(sql, s, {"error": "winbind unavailable"})
    r = shown()
    assert r["error"] == "winbind unavailable" and r["why_not"] == "no_report"
    _reports(sql, s, GOOD_REPORT, ago="7 hours")
    assert shown()["why_not"] == "stale_report"
    _reports(sql, s, {**GOOD_REPORT, "kind": "group"})
    assert shown()["why_not"] == "not_an_account"


def test_registering_a_placement_account_needs_a_fresh_sign_in(web, sql) -> None:
    s = _setup(web, sql)
    _reports(sql, s, GOOD_REPORT)
    admin = web.browser(s.w.admin)
    sql.execute("UPDATE catalog.web_session SET auth_time = now() - interval '11 minutes'")
    r = _patch_filer(admin, s, PLACER)
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"


def test_a_new_filer_cant_name_a_placement_account(web, sql) -> None:
    """It has no report yet, so there is nothing it could be checked against."""
    w = seed(sql)
    admin = web.browser(w.admin)
    body = {"name": "with-placer", "host": "nas2.contoso.test", "protocol": "smb",
            "export": "geo", "edge_id": str(w.edge_id)}  # fmt: skip
    r = admin.call("POST", "/v1/filers", json={**body, "placement_sid": PLACER}, check=False)
    assert r.status_code == 400, r.text
    r = admin.post("/v1/filers", body)
    assert r.status_code == 201, r.text
    assert r.json()["placement_sid"] is None


def test_clear_breaker_needs_an_owner_and_shows_the_pin(web, sql) -> None:
    """The earlier test only tried a reader, whom `_annex` stops first."""
    s = _setup(web, sql)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "a.segy", acl=readable, state="present")
    sql.execute("UPDATE catalog.annex_file SET pinned_by_breaker = true WHERE file_id = %s", (fid,))
    path = "/v1/annexes/{annex_id}/files/{file_id}/clear-breaker"
    # Sees the annex, but doesn't own it.
    sql.execute("UPDATE catalog.annex SET owners_sid = %s WHERE annex_id = %s",
                ("S-1-5-21-9-9-9-9", int(s.aid)))  # fmt: skip
    r = web.browser(w.steward).post(path, {}, annex_id=s.aid, file_id=fid)
    assert r.status_code == 403, r.text
    (pinned,) = sql.execute(
        "SELECT pinned_by_breaker FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert pinned
    admin = web.browser(w.admin)
    r = admin.post(path, {}, annex_id=s.aid, file_id=fid)
    assert r.status_code == 200, r.text
    assert r.json()["pinned_by_breaker"] is False
    # Again, on a file that isn't pinned: it did nothing, and the row says so.
    r = admin.post(path, {}, annex_id=s.aid, file_id=fid)
    assert r.status_code == 200
    rows = sql.execute(
        "SELECT details->>'was_pinned' FROM catalog.audit_log WHERE file_id = %s"
        " AND action = 'annex.breaker.cleared' ORDER BY audit_id",
        (fid,),
    ).fetchall()
    assert [x[0] for x in rows] == ["true", "false"]


def test_abandon_takes_a_stuck_placement_after_its_go(web, sql) -> None:
    """The earlier test's comment said so and had no such case."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "a.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    p = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"}).json()
    pid = p["placement_id"]
    _placed_on(sql, p, state="stuck", before_stuck="placing")
    sql.execute("UPDATE catalog.annex_placement SET last_report_at = now() - interval '25 hours'"
                " WHERE placement_id = %s", (int(pid),))  # fmt: skip
    r = admin.post("/v1/placements/{placement_id}/abandon", {}, placement_id=pid)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["fail_reason"]) == ("failed", "abandoned")
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE kind = 'problem' AND file_id = %s"
        " AND details->>'reason' = 'placement_abandoned' ORDER BY event_id DESC LIMIT 1",
        (fid,),
    ).fetchone()
    assert "next scan" in details["detail"], "the edge never said where it was"
    # Stuck before its `go`: cancel is the way.
    q = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"}).json()
    _placed_on(sql, q, state="stuck", before_stuck="writing")
    r = admin.post("/v1/placements/{placement_id}/abandon", {}, placement_id=q["placement_id"])
    assert r.status_code == 409


def test_annex_file_shows_its_hold(web, sql) -> None:
    """Declared in the review: `hold_until` had no test."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "a.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    sql.execute("UPDATE catalog.annex_file SET hold_until = '2099-01-02T03:04:05Z'"
                " WHERE file_id = %s", (fid,))  # fmt: skip
    r = web.browser(w.owner).get("/v1/annexes/{annex_id}/files", annex_id=s.aid)
    row = next(x for x in r.json()["items"] if x["file_id"] == str(fid))
    assert row["hold_until"].startswith("2099-01-02T03:04:05")


def test_a_purge_asked_for_refuses_a_request(web, sql) -> None:
    """B2, at the request: a legal hold leaves `purge_requested_at` set for good."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "a.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    sql.execute("UPDATE catalog.annex_file SET gone_since = now(), purge_requested_at = now(),"
                " purge_requested_by = 'test' WHERE file_id = %s", (fid,))  # fmt: skip
    r = web.browser(w.owner).call(
        "POST", "/v1/placements", json={"file_id": str(fid), "place_at": "default"}, check=False
    )
    assert r.status_code == 404, r.text


def _second_annex_placement(web, sql, s) -> dict:
    """A placement of another annex's file, targeting `s`'s filer."""
    w = s.w
    fid = _removed(sql, s, "t.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    return (
        web.browser(w.owner)
        .post("/v1/placements", {"file_id": str(fid), "place_at": "default"})
        .json()
    )


def test_retiring_the_last_annex_on_a_filer_ends_other_annexes_placements_for_it(web, sql) -> None:
    """Nothing is left there to place them. If another annex is, it goes on."""
    s = _setup(web, sql)
    w = s.w
    admin = web.browser(w.admin)
    p = _second_annex_placement(web, sql, s)
    pid = int(p["placement_id"])
    other = _annex(admin, w, _filer(admin, w), _dataset(sql, w))
    sql.execute("UPDATE catalog.annex_placement SET annex_id = %s, state = 'placing',"
                " before_stuck = NULL WHERE placement_id = %s",
                (int(other["annex_id"]), pid))  # fmt: skip
    # Another annex on the target's filer: it stays that annex's work.
    third = _annex(admin, w, s.filer, _dataset(sql, w), path="third")

    def retire(aid):
        etag = admin.get("/v1/annexes/{annex_id}", annex_id=aid).headers["ETag"]
        r = admin.call("PATCH", "/v1/annexes/{annex_id}", json={"state": "retired"},
                       headers={"If-Match": etag}, annex_id=aid)  # fmt: skip
        assert r.status_code == 200, r.text

    def state():
        return sql.execute("SELECT state, fail_reason FROM catalog.annex_placement"
                           " WHERE placement_id = %s", (pid,)).fetchone()  # fmt: skip

    retire(s.aid)
    assert state()[0] == "placing"
    retire(third["annex_id"])
    assert state() == ("failed", "annex_retired")
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE kind = 'problem'"
        " AND details->>'placement_id' = %s AND details->>'reason' = 'placement_annex_retired'",
        (str(pid),),
    ).fetchone()
    assert f".omnifile-staging-{third['annex_id']}-" in details["detail"], details
    assert "may already be under its name" in details["detail"], details


def test_an_abandon_says_what_the_file_was_left_like(web, sql) -> None:
    """The placement account stays its owner for good, and nothing else says so."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "a.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    for at, words in (("linked", "the placement account may still own it"),
                      ("linking", "may be under"), ("filled", "staged copy")):  # fmt: skip
        p = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"}).json()
        _placed_on(sql, p, state="stuck", before_stuck="placing")
        sql.execute(
            "UPDATE catalog.annex_placement SET last_report_at = now() - interval '25 hours',"
            " last_edge_report = %s WHERE placement_id = %s",
            (Jsonb({"at": at, "name": "a.segy", "reason": "x"}), int(p["placement_id"])),
        )
        r = admin.post("/v1/placements/{placement_id}/abandon", {},
                       placement_id=p["placement_id"])  # fmt: skip
        assert r.status_code == 200, r.text
        (details,) = sql.execute(
            "SELECT details FROM catalog.annex_event WHERE kind = 'problem'"
            " AND details->>'placement_id' = %s AND details->>'reason' = 'placement_abandoned'",
            (p["placement_id"],),
        ).fetchone()
        assert words in details["detail"], at
        assert details["edge_said"]["at"] == at
        (audited,) = sql.execute(
            "SELECT details FROM catalog.audit_log WHERE action = 'annex.placement.abandoned'"
            " AND details->>'placement_id' = %s",
            (p["placement_id"],),
        ).fetchone()
        assert words in audited["left"], at
        assert "a.segy" in details["detail"] or at == "filled"


def test_a_refused_purge_can_be_asked_again_and_a_person_sees_it(web, sql) -> None:
    """SB2: asking again answered 202 with nothing queued."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "a.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    sql.execute("UPDATE catalog.annex_file SET gone_since = now(), purge_refused_at = now(),"
                " nas_state = 'gone' WHERE file_id = %s", (fid,))  # fmt: skip
    owner, admin = web.browser(w.owner), web.browser(w.admin)
    got = owner.get("/v1/annexes/{annex_id}/files", params={"state": "gone"}, annex_id=s.aid).json()
    row = next(x for x in got["items"] if x["file_id"] == str(fid))
    assert row["purge_refused_at"] and row["purge_requested_at"] is None
    # A request goes on.
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"})
    assert r.status_code == 201, r.text
    # And a purge can be asked again, and is queued.
    before = sql.execute("SELECT count(*) FROM pgmq.q_retention").fetchone()[0]
    r = admin.post("/v1/annexes/{annex_id}/files/{file_id}/purge", {}, annex_id=s.aid, file_id=fid)
    assert r.status_code == 202, r.text
    assert sql.execute("SELECT count(*) FROM pgmq.q_retention").fetchone()[0] == before + 1
    # The old refusal is over: it isn't shown beside the new request.
    (refused,) = sql.execute(
        "SELECT purge_refused_at FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert refused is None


def test_asking_again_for_a_purge_with_no_message_queues_one(web, sql) -> None:
    """A request whose message went with a crash answered 202 and queued nothing."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "a.segy", acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))
    sql.execute("UPDATE catalog.annex_file SET gone_since = now(), nas_state = 'gone',"
                " purge_requested_at = now(), purge_requested_by = 'u' WHERE file_id = %s",
                (fid,))  # fmt: skip
    admin = web.browser(w.admin)
    before = sql.execute("SELECT count(*) FROM pgmq.q_retention").fetchone()[0]
    r = admin.post("/v1/annexes/{annex_id}/files/{file_id}/purge", {}, annex_id=s.aid, file_id=fid)
    assert r.status_code == 202, r.text
    assert sql.execute("SELECT count(*) FROM pgmq.q_retention").fetchone()[0] == before + 1


def test_a_purge_asked_for_leaves_the_return_preview(web, sql) -> None:
    s = _setup(web, sql)
    w = s.w
    acl = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    a, b = _removed(sql, s, "a.segy", acl=acl), _removed(sql, s, "b.segy", acl=acl)
    assert a != b
    admin = web.browser(w.admin)

    def files():
        r = admin.post("/v1/annexes/{annex_id}/return-preview", {}, annex_id=s.aid)
        assert r.status_code == 200, r.text
        return sum(x["files"] for x in r.json()["rules"])

    assert files() == 2
    sql.execute("UPDATE catalog.annex_file SET gone_since = now(), purge_requested_at = now(),"
                " purge_requested_by = 'u' WHERE file_id = %s", (a,))  # fmt: skip
    assert files() == 1


def _other_annex_placement(web, sql, s, *, state: str = "placing") -> tuple[dict, int]:
    """A file of another annex's, placed on `s`'s filer, past its `go`."""
    w, admin = s.w, web.browser(s.w.admin)
    p = _second_annex_placement(web, sql, s)
    other = _annex(admin, w, _filer(admin, w), _dataset(sql, w))
    sql.execute(
        "UPDATE catalog.annex_placement SET annex_id = %s, state = %s WHERE placement_id = %s",
        (int(other["annex_id"]), state, int(p["placement_id"])),
    )
    return other, int(p["placement_id"])


def _retire(admin, aid) -> None:
    etag = admin.get("/v1/annexes/{annex_id}", annex_id=aid).headers["ETag"]
    r = admin.call("PATCH", "/v1/annexes/{annex_id}", json={"state": "retired"},
                   headers={"If-Match": etag}, annex_id=aid)  # fmt: skip
    assert r.status_code == 200, r.text


def test_a_placement_past_go_whose_placer_retired_while_another_annex_lives_says_so(
    web, sql
) -> None:
    """Nothing would finish it, and nothing said so. It stays open: abandon is the way."""
    s = _setup(web, sql)
    w, admin = s.w, web.browser(s.w.admin)
    other, pid = _other_annex_placement(web, sql, s)
    third = _annex(admin, w, s.filer, _dataset(sql, w), path="third")
    assert int(third["annex_id"]) > int(s.aid)
    _retire(admin, s.aid)
    (state,) = sql.execute("SELECT state FROM catalog.annex_placement WHERE placement_id = %s",
                           (pid,)).fetchone()  # fmt: skip
    assert state == "placing"
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE kind = 'problem'"
        " AND details->>'placement_id' = %s AND details->>'reason' = 'placement_placer_retired'",
        (str(pid),),
    ).fetchone()
    assert "may already be under its name" in details["detail"]
    assert "abandon" in details["detail"]


@pytest.mark.parametrize("ended", ["placed", "failed", "cancelled"])
def test_retiring_an_annex_leaves_placements_that_have_ended(web, sql, ended) -> None:
    s = _setup(web, sql)
    admin = web.browser(s.w.admin)
    p = _second_annex_placement(web, sql, s)
    pid = int(p["placement_id"])
    sql.execute(
        "UPDATE catalog.annex_placement SET annex_id = %s, state = %s, done_at = now(),"
        " fail_reason = CASE WHEN %s = 'failed' THEN 'x' END,"
        " outcome = CASE WHEN %s = 'placed' THEN 'placed' END WHERE placement_id = %s",
        (int(s.aid), ended, ended, ended, pid),
    )
    _retire(admin, s.aid)
    (state, reason) = sql.execute(
        "SELECT state, fail_reason FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone()
    assert state == ended
    assert reason != "annex_retired"


def test_retiring_an_annex_doesnt_alarm_for_another_annexs_own_placement(web, sql) -> None:
    """The other annex on the filer places its own: nothing is left without a placer."""
    s = _setup(web, sql)
    w, admin = s.w, web.browser(s.w.admin)
    p = _second_annex_placement(web, sql, s)
    pid = int(p["placement_id"])
    same = _annex(admin, w, s.filer, _dataset(sql, w), path="same")
    assert int(same["annex_id"]) > int(s.aid)
    sql.execute(
        "UPDATE catalog.annex_placement SET annex_id = %s, state = 'placing'"
        " WHERE placement_id = %s",
        (int(same["annex_id"]), pid),
    )
    _retire(admin, s.aid)
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE kind = 'problem'"
        " AND details->>'placement_id' = %s AND details->>'reason' = 'placement_placer_retired'",
        (str(pid),),
    ).fetchone()
    assert n == 0


def test_retiring_an_annex_whose_placement_is_past_go_elsewhere_says_it_may_be_there(
    web, sql
) -> None:
    s = _setup(web, sql)
    w, admin = s.w, web.browser(s.w.admin)
    # This annex's file, placed on another filer with an annex of its own.
    p = _second_annex_placement(web, sql, s)
    pid = int(p["placement_id"])
    f2 = _filer(admin, w)
    placer = _annex(admin, w, f2, _dataset(sql, w))
    sql.execute(
        "UPDATE catalog.annex_placement SET state = 'placing',"
        " target = jsonb_set(target, '{filer_id}', to_jsonb(%s::text)) WHERE placement_id = %s",
        (f2["filer_id"], pid),
    )
    _retire(admin, s.aid)
    (details,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE kind = 'problem'"
        " AND details->>'placement_id' = %s AND details->>'reason' = 'placement_annex_retired'",
        (str(pid),),
    ).fetchone()
    assert f"annex {placer['annex_id']} was placing it" in details["detail"]
    assert "may already be under its name" in details["detail"]


def test_problems_say_whether_the_files_folders_are_read(web, sql) -> None:
    """Decision 63: until the edge's first complete folder pass the annex's files are
    hidden, and a folder whose ACL can't be read hides what is below it."""
    s = _setup(web, sql)
    w = s.w
    _removed(sql, s, "surveys/line_1042.segy", acl=_acl(sql, s.ds, [w.g_owners]))
    owner = web.browser(w.owner)

    def read_set() -> dict:
        r = owner.get("/v1/annexes/{annex_id}/problems", annex_id=s.aid)
        assert r.status_code == 200, r.text
        return r.json()["read_set"]

    rs = read_set()
    assert (rs["ready"], rs["read_at"], rs["folders"], rs["unreadable"]) == (False, None, 3, 0)
    sql.execute("UPDATE catalog.annex SET folders_read_at = now() WHERE annex_id = %s",
                (int(s.aid),))  # fmt: skip
    _folder(sql, s, "long-term-storage/surveys", write=None, error="EACCES")
    rs = read_set()
    assert rs["ready"] and rs["read_at"] and (rs["folders"], rs["unreadable"]) == (3, 1)


def test_the_files_list_says_which_folders_narrow_who_may_read_a_file(web, sql) -> None:
    """Decision 63, in the annex's file view: beside the file, the folders above it
    that don't let everyone through."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "surveys/line_1042.segy", acl=_acl(sql, s.ds, [w.g_owners]))
    owner = web.browser(w.owner)

    def narrow() -> list[str]:
        r = owner.get("/v1/annexes/{annex_id}/files", annex_id=s.aid)
        assert r.status_code == 200, r.text
        [f] = [x for x in r.json()["items"] if x["file_id"] == str(fid)]
        return f["folders_narrow"]

    assert narrow() == []
    _folder(sql, s, "long-term-storage/surveys", write=[w.g_owners], search=[w.g_owners])
    assert narrow() == ["long-term-storage/surveys"]


def test_a_refused_copy_is_put_right_once_a_later_version_verifies(web, sql) -> None:
    """Review of #81: a copy_refused problem stayed on the list as if it still stood."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "surveys/refused.segy",
                   acl=_acl(sql, s.ds, [w.g_owners, w.g_readers]))  # fmt: skip
    for v in (11, 12):
        sql.execute(
            "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
            " VALUES (%s, 'problem', 'surveys/refused.segy', %s, %s,"
            ' \'{"reason": "copy_refused", "detail": "x"}\')',
            (int(s.aid), fid, v),
        )
    owner = web.browser(w.owner)

    def resolved() -> dict[str, bool]:
        r = owner.get("/v1/annexes/{annex_id}/problems", annex_id=s.aid)
        assert r.status_code == 200, r.text
        return {e["version_id"]: e.get("resolved", False) for e in r.json()["events"]}

    assert resolved() == {"11": False, "12": False}
    sql.execute("UPDATE catalog.annex_file SET verified_version_id = 12 WHERE file_id = %s", (fid,))
    assert resolved() == {"11": True, "12": True}
    sql.execute("UPDATE catalog.annex_file SET verified_version_id = 11 WHERE file_id = %s", (fid,))
    assert resolved() == {"11": True, "12": False}


def _answer_filer(b, s: Setup, access):
    path, fid = "/v1/filers/{filer_id}", s.filer["filer_id"]
    h = {"If-Match": b.get(path, filer_id=fid).headers.get("ETag", '"0"')}
    return b.call("PATCH", path, json={"access": access}, headers=h, filer_id=fid, check=False)


def test_an_archive_admin_answers_whether_the_share_limits_access(web, sql) -> None:
    """Decision 65: SMB share permissions and NFS export rules can't be read through a
    mount, so filer setup asks; the answer is kept with who gave it, and audited, and
    an annexed file's readers are only those the share lets in."""
    s = _setup(web, sql)
    w = s.w
    fid = _removed(sql, s, "surveys/line_1042.segy", acl=_acl(sql, s.ds, [w.g_owners]))
    admin, owner = web.browser(w.admin), web.browser(w.owner)
    path = "/v1/filers/{filer_id}"

    def visible() -> bool:
        return owner.get("/v1/files/{file_id}/return-targets", file_id=fid).status_code == 200

    assert visible(), "answered 'not limited' (the tests' stand-in for filer setup)"
    assert _answer_filer(owner, s, {"limited": False}).status_code == 403
    for bad in ({"limited": True}, {"limited": True, "groups": ["S-1-1-0"]},
                {"limited": False, "groups": [w.g_owners]}, {"limited": "yes"},
                {"limited": True, "groups": ["nope"]}):  # fmt: skip
        r = _answer_filer(admin, s, bad)
        assert r.status_code in (400, 422), (bad, r.text)
    # The share lets in only the readers' group: the Owners are out.
    r = _answer_filer(admin, s, {"limited": True, "groups": [w.g_readers]})
    assert r.status_code == 200, r.text
    a = r.json()["access"]
    assert (a["answered"], a["limited"], a["groups"]) == (True, True, [w.g_readers])
    assert a["confirmed_by"] and a["confirmed_at"]
    assert not visible()
    r = _answer_filer(admin, s, {"limited": True, "groups": [w.g_owners, w.g_readers]})
    assert r.status_code == 200 and visible()
    r = _answer_filer(admin, s, {"limited": False})
    assert r.status_code == 200 and r.json()["access"]["groups"] == [] and visible()
    actor, details = sql.execute(
        "SELECT actor, details FROM catalog.audit_log WHERE action = 'filer.access'"
        " ORDER BY at DESC LIMIT 1"
    ).fetchone()
    assert details["limited"] is False and details["was"]["limited"] is True
    # Kept with who answered: the signed-in admin, as the audit names them.
    assert r.json()["access"]["confirmed_by"] == actor
    assert admin.get(path, filer_id=s.filer["filer_id"]).json()["access"]["limited"] is False
    # Unanswered (a filer from before the question): hidden from everyone.
    sql.execute(
        "UPDATE catalog.filer SET access_limited = NULL, access_groups = '{}',"
        " access_confirmed_by = NULL, access_confirmed_at = NULL WHERE filer_id = %s",
        (int(s.filer["filer_id"]),),
    )
    assert not visible()
    rs = owner.get("/v1/annexes/{annex_id}/problems", annex_id=s.aid).json()["read_set"]
    assert rs["filer_answered"] is False


def test_an_archive_admin_acknowledges_a_nas_that_shows_no_acls(web, sql) -> None:
    """Decision 64: audited, with their name; only while the filer is flagged."""
    s = _setup(web, sql)
    w = s.w
    admin, owner = web.browser(w.admin), web.browser(w.owner)
    path, fid = "/v1/filers/{filer_id}", s.filer["filer_id"]

    def ack(b):
        h = {"If-Match": b.get(path, filer_id=fid).headers.get("ETag", '"0"')}
        return b.call("PATCH", path, json={"acls_hidden_acknowledged": True}, headers=h,
                      filer_id=fid, check=False)  # fmt: skip

    r = ack(admin)
    assert r.status_code in (400, 422), "not flagged: nothing to acknowledge"
    sql.execute("UPDATE catalog.filer SET acls_hidden_since = now() WHERE filer_id = %s",
                (int(fid),))  # fmt: skip
    acls = admin.get(path, filer_id=fid).json()["acls"]
    assert acls["hidden_since"] and acls["blocks_grants"] and acls["acknowledged_at"] is None
    assert ack(owner).status_code == 403
    r = ack(admin)
    assert r.status_code == 200, r.text
    acls = r.json()["acls"]
    assert not acls["blocks_grants"] and acls["acknowledged_at"]
    (actor,) = sql.execute(
        "SELECT actor FROM catalog.audit_log WHERE action = 'filer.acls_hidden'"
        " ORDER BY at DESC LIMIT 1"
    ).fetchone()
    assert acls["acknowledged_by"] == actor


OVERWRITE = [{"id": "back", "trigger": "recall_completed", "conditions": {},
              "actions": [{"place_at": ORIGIN}, {"on_conflict": "overwrite_keep_version"},
                          {"hold_days": 14}]}]  # fmt: skip


def test_an_older_version_may_replace_a_file_still_on_the_nas(web, sql) -> None:
    """A3.4, decision 83: with overwrite, the origin is offered for an older version
    of a file still there; never for the version already there, and never without
    overwrite. The approval page sees the rule as one that replaces files (85)."""
    s = _setup(web, sql, rules=OVERWRITE)
    w = s.w
    readable = _acl(sql, s.ds, [w.g_owners, w.g_readers])
    fid = _removed(sql, s, "c.segy", acl=readable, state="present", versions=2)
    old, cur = w.versions["c.segy"]
    _folder(sql, s, "long-term-storage", write=[w.g_owners])
    owner = web.browser(w.owner)

    def post(**body):
        return owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default", **body})

    r = post()
    assert r.status_code == 409 and r.json()["code"] == "file_present"
    r = post(version_id=str(cur))
    assert r.status_code == 409 and r.json()["code"] == "file_present"
    # A gentler choice is no overwrite, so no replace: the file is there.
    r = post(version_id=str(old), on_conflict="restored_name")
    assert r.status_code == 409 and r.json()["code"] == "file_present"
    r = post(version_id=str(old))
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["on_conflict"] == "overwrite_keep_version" and p["version_id"] == str(old)
    assert p["target"]["origin"] and p["replace"] is None
    [t] = owner.get("/v1/files/{file_id}/return-targets", file_id=fid).json()["items"]
    assert not t["allowed"] and t["replace_older"] and "older version" in t["why_not"]
    listed = owner.get("/v1/annexes/{annex_id}/rulesets", annex_id=s.aid).json()["items"]
    running = [x for x in listed if x["state"] == "active"]
    assert running and running[0]["replaces_on_nas"] == ["back"]


def test_a_rule_that_never_overwrites_replaces_nothing(web, sql) -> None:
    s = _setup(web, sql)
    owner = web.browser(s.w.owner)
    listed = owner.get("/v1/annexes/{annex_id}/rulesets", annex_id=s.aid).json()["items"]
    assert all(x["replaces_on_nas"] == [] for x in listed)
