"""The return path through the control API (phase A3.1): folder ACLs and
free space from the edge, and who may add a file to a NAS folder."""

from __future__ import annotations

import base64

import psycopg

from omnifile_service.annex import rules as R
from omnifile_service.annex.acl import acl_body, folder_write_sets

from .test_annex import (
    ALICE,
    DOMAIN,
    FULL,
    GEO,
    INTERNS,
    OWNERS,
    READ,
    ace,
    annex,  # noqa: F401 - the fixture
    events,
    nt_acl,
    sd,
)

ADD_FILE = 0x0000_0002
INHERIT_ONLY = 0x08


def _write(value: dict, protocol: str) -> tuple:
    return folder_write_sets(value, acl_body(value, protocol))


def test_who_may_add_a_file_to_a_folder_reads_deny_first() -> None:
    # NT: an allow of FILE_ADD_FILE; a deny of it wins; read alone isn't write;
    # an inherit-only entry doesn't apply to the folder itself.
    v = nt_acl([
        ace(0, READ | ADD_FILE, GEO),
        ace(1, ADD_FILE, INTERNS),
        ace(0, FULL, INTERNS),
        ace(0, READ, OWNERS),
        ace(0, FULL, ALICE, INHERIT_ONLY),
    ])  # fmt: skip
    write, deny = _write(v, "smb")
    assert write == [GEO] and deny == [INTERNS]
    # No DACL at all: everyone may.
    assert _write({"kind": "ntfs_sd", "raw": base64.b64encode(sd(ALICE, GEO, None)).decode()},
                  "smb")[0] == ["S-1-1-0"]  # fmt: skip
    # NFSv4: a deny for a principal the edge couldn't map could be the
    # requester's: nobody may place a file there.
    v4 = {"kind": "nfs4", "aces": [{"type": "allow", "sid": GEO, "mask": ADD_FILE | 1},
                                   {"type": "deny", "sid": None, "mask": ADD_FILE}]}  # fmt: skip
    assert _write(v4, "nfs4") == (None, [])
    v4["aces"][1] = {"type": "allow", "sid": None, "mask": ADD_FILE}
    assert _write(v4, "nfs4") == ([GEO], [])
    # POSIX: w and x on the folder, through the mask.
    px = {"kind": "posix", "mode": 0o750, "owner_sid": ALICE, "group_sid": GEO,
          "entries": [{"scope": "access", "tag": "owner", "perms": "rwx"},
                      {"scope": "access", "tag": "owning_group", "perms": "r-x"},
                      {"scope": "access", "tag": "group", "sid": INTERNS, "perms": "rwx"},
                      {"scope": "access", "tag": "mask", "perms": "rwx"},
                      {"scope": "access", "tag": "other", "perms": "---"}]}  # fmt: skip
    write, deny = _write(px, "nfs3")
    assert set(write) == {ALICE, INTERNS} and GEO in deny
    px["entries"][3]["perms"] = "r-x"
    write, _ = _write(px, "nfs3")
    assert write == [ALICE], "the mask limits named entries, never the owner"


def test_folder_acls_from_the_edge_say_who_may_add_a_file(annex, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    allow = nt_acl([ace(0, READ | ADD_FILE, GEO), ace(1, ADD_FILE, INTERNS)])
    res = events(w, annex_id, [
        {"kind": "folder_acl", "path": "long-term-storage", "acl": allow,
         "observed_at": "2026-11-05T10:00:00Z"},
        {"kind": "folder_acl", "path": "long-term-storage/surveys",
         "error": "no NT security descriptor", "observed_at": "2026-11-05T10:00:00Z"},
        {"kind": "target_space", "path": "long-term-storage", "free_bytes": 7, "total_bytes": 9},
        # Outside the annex, and not a return location of its rules.
        {"kind": "folder_acl", "path": "finance", "acl": allow},
        {"kind": "folder_acl", "path": "long-term-storage/../finance", "acl": allow},
    ])  # fmt: skip
    assert [r["status"] for r in res] == ["applied"] * 3 + ["rejected"] * 2, res
    assert res[3]["code"] == "out_of_scope"
    rows = {
        r[0]: tuple(r[1:])
        for r in sql.execute(
            "SELECT n.path, a.write_sids::text[], a.write_deny_sids::text[], n.error"
            "  FROM catalog.nas_folder_acl n LEFT JOIN catalog.acl a ON a.acl_id = n.acl_id"
            " WHERE n.filer_id = %s",
            (filer,),
        ).fetchall()
    }
    assert rows["long-term-storage"] == ([GEO], [INTERNS], None)
    assert rows["long-term-storage/surveys"][2] == "no NT security descriptor"
    (can_geo, can_interns) = sql.execute(
        "SELECT catalog.acl_can_write(a.write_sids, a.write_deny_sids, %s, '{}', a.deny_first),"
        "       catalog.acl_can_write(a.write_sids, a.write_deny_sids, %s, %s, a.deny_first)"
        "  FROM catalog.nas_folder_acl n JOIN catalog.acl a ON a.acl_id = n.acl_id"
        " WHERE n.filer_id = %s AND n.path = 'long-term-storage'",
        (GEO, f"{DOMAIN}-7777", [INTERNS], filer),
    ).fetchone()
    assert can_geo and not can_interns
    assert sql.execute(
        "SELECT free_bytes FROM catalog.nas_target_space WHERE filer_id = %s", (filer,)
    ).fetchone() == (7,)
    # An older report never overwrites a newer one; a gone folder is kept, with when it
    # went (decision 63: the files that were in it are still judged by it).
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage",
                          "error": "stale", "observed_at": "2026-11-05T09:00:00Z"}])  # fmt: skip
    assert sql.execute(
        "SELECT error FROM catalog.nas_folder_acl WHERE filer_id = %s"
        " AND path = 'long-term-storage'",
        (filer,),
    ).fetchone() == (None,)
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/surveys",
                          "gone": True}])  # fmt: skip
    assert sql.execute(
        "SELECT count(*) FILTER (WHERE gone_at IS NULL),"
        " count(*) FILTER (WHERE gone_at IS NOT NULL)"
        "  FROM catalog.nas_folder_acl WHERE filer_id = %s AND path LIKE 'long-term-storage%%'",
        (filer,),
    ).fetchone() == (1, 1)
    # Back again: no longer gone.
    events(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/surveys", "acl": allow}])
    assert sql.execute(
        "SELECT gone_at FROM catalog.nas_folder_acl WHERE filer_id = %s"
        " AND path = 'long-term-storage/surveys'",
        (filer,),
    ).fetchone() == (None,)


def test_a_return_location_on_the_filer_is_the_edges_to_read_too(annex, sql) -> None:  # noqa: F811
    w, annex_id, ds, filer = annex
    body = R.copy_template(ds)
    body["rules"].append({
        "id": "restore", "trigger": "recall_completed", "conditions": {},
        "actions": [{"place_at": {"kind": "nas_folder", "filer_id": str(filer),
                                  "path": "restore"}}]})  # fmt: skip
    sql.execute(
        "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
        (psycopg.types.json.Jsonb(R.normalize(body, ds)), annex_id),
    )
    allow = nt_acl([ace(0, ADD_FILE, GEO)])
    res = events(w, annex_id, [
        {"kind": "folder_acl", "path": "restore", "acl": allow},
        {"kind": "folder_acl", "path": "restore/2026", "acl": allow},
        {"kind": "folder_acl", "path": "restored", "acl": allow},
    ])  # fmt: skip
    assert [r["status"] for r in res] == ["applied", "applied", "rejected"], res
