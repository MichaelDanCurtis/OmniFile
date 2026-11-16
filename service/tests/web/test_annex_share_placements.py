"""Recalls of annexed files into an OmniFile share (annex.md 4.6; phase A3.2):
a server-side copy, read back in Azure, with the file's own read set."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime

from psycopg.types.json import Jsonb
from watched import Clock, FakeWorker, Watched, drain_job

from omnifile_service.annex import rules as R
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.portal_jobs import portal_jobs
from omnifile_service.worker.share_placements import handle_share_placement

from .conftest import add_file, seed
from .test_annex import _annex, _dataset, _filer
from .test_annex_returns import _acl
from .test_rewind_recalls import _queue, run_job

DATA = b"seismic trace " * 300


async def _one(data: bytes):
    yield data


def _setup(web, sql, *, hold: int = 3):
    w = seed(sql)
    admin = web.browser(w.admin)
    f = _filer(admin, w)
    ds = _dataset(sql, w)
    a = _annex(admin, w, f, ds)
    loc = {"kind": "share_folder", "share_id": str(w.share_id), "path": "recalls",
           "keep_relative_path": False}  # fmt: skip
    body = R.copy_template(ds)
    body["rules"].append({"id": "to_share", "trigger": "recall_completed", "conditions": {},
                          "actions": [{"place_at": loc},
                                      {"on_conflict": "overwrite_keep_version"},
                                      {"hold_days": hold}]})  # fmt: skip
    owner = web.browser(w.owner)
    r = owner.post("/v1/annexes/{annex_id}/rulesets", {"body": body}, annex_id=a["annex_id"])
    assert r.status_code == 201, r.text
    etag = admin.get("/v1/annexes/{annex_id}", annex_id=a["annex_id"]).headers["ETag"]
    r = admin.post("/v1/annexes/{annex_id}/rulesets/{version}/approve", {},
                   headers={"If-Match": etag}, annex_id=a["annex_id"],
                   version=r.json()["version"])  # fmt: skip
    assert r.status_code == 200, r.text
    # The share's root lets its Owners and Writers add files (decision 28).
    sql.execute(
        "UPDATE catalog.acl SET entries = %s WHERE acl_id = %s",
        (Jsonb([{"scope": "access", "tag": "group", "sid": w.g_owners, "perms": "rwx"},
                {"scope": "access", "tag": "group", "sid": w.g_writers, "perms": "rwx"},
                {"scope": "access", "tag": "group", "sid": w.g_readers, "perms": "r-x"},
                {"scope": "access", "tag": "mask", "perms": "rwx"}]), w.acl_open),
    )  # fmt: skip
    (root,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL", (ds,)
    ).fetchone()
    dw = dataclasses.replace(w, dataset_id=ds, root_id=root, files={}, versions={})
    sim = SimBackend.azure_two_account(VirtualClock(datetime.now(UTC)), backend_id=w.backend_id)
    return w, a, ds, dw, sim


def _annexed(sql, a, dw, sim, path: str, read: list[str], *, data: bytes = DATA,
             deny: list[str] | None = None) -> int:  # fmt: skip
    """A removed annex file whose bytes are a real sim object."""
    ref = asyncio.run(sim.put("live/data", f"obj/{path}", _one(data), None, {}))
    acl = _acl(sql, dw.dataset_id, read)
    sql.execute(
        "UPDATE catalog.acl SET raw_kind = 'ntfs_sd', deny_sids = %s WHERE acl_id = %s",
        (deny or [], acl),
    )
    fid = add_file(sql, dw, path.replace("/", "_"), acl=acl, size=len(data), key=ref.key,
                   backend_version=ref.version_id, crc=to_bytes(crc64nvme(data)),
                   state="remote")  # fmt: skip
    sql.execute(
        "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size, mtime_ns,"
        " first_seen, last_seen, nas_state, copied_size) VALUES (%s, %s, %s, %s, %s, 1, now(),"
        " now(), 'removed', %s)",
        (int(a["annex_id"]), fid, path, str(fid), len(data), len(data)),
    )
    return fid


def _place(web, sql, db_url, who, sim, w, fid: int, **body) -> dict:
    b = web.browser(who)
    r = b.post("/v1/placements", {"file_id": str(fid), "place_at": "default", **body})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["state"] == "ready"
    assert {"kind": "annex_placement", "placement_id": p["placement_id"]} in _queue(sql, "portal")
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    now = datetime.now(UTC)
    state = run_job(db_url, lambda c: handle_share_placement(c, reg, int(p["placement_id"]), now))
    p = b.get("/v1/placements/{placement_id}", placement_id=p["placement_id"]).json()
    assert p["state"] == state, p
    return p


def _row(sql, q: str, *args):
    cur = sql.execute(q, args)
    names = [d.name for d in cur.description]
    r = cur.fetchone()
    return None if r is None else dict(zip(names, r, strict=True))


def test_a_recall_into_a_share_is_a_verified_copy_only_its_readers_can_open(
    web, sql, db_url
) -> None:
    w, a, ds, dw, sim = _setup(web, sql)
    fid = _annexed(sql, a, dw, sim, "surveys/a.segy", [w.g_owners, w.g_writers])
    p = _place(web, sql, db_url, w.owner, sim, w, fid, note="QC")
    assert (p["state"], p["outcome"], p["final_path"]) == ("placed", "placed", "recalls/a.segy")
    pl = _row(sql, "SELECT * FROM catalog.annex_placement WHERE placement_id = %s",
              int(p["placement_id"]))  # fmt: skip
    copy = _row(sql, "SELECT * FROM catalog.file WHERE file_id = %s", pl["placed_file_id"])
    assert copy["dataset_id"] == w.dataset_id and copy["origin"] == "annex_restore"
    assert copy["name"] == "a.segy"
    # Its owner SID has no id on the share's edge: root's, never the NAS's
    # numeric ids (#68 review: they may be someone else's there).
    assert (copy["uid"], copy["gid"]) == (0, 0)
    loc = _row(sql, "SELECT * FROM catalog.location WHERE version_id = %s",
               pl["placed_version_id"])  # fmt: skip
    assert loc["verified"] and loc["verified_by"] == "azure_read_crc"
    assert bytes(loc["crc64nvme"]) == to_bytes(crc64nvme(DATA))
    assert loc["object_key"].endswith(f"annex-restores/{p['placement_id']}")
    # The copy's read set is the annexed file's: the share's readers, who may
    # read its folder, can't open it (decision 51); its writers can.
    (read,) = sql.execute(
        "SELECT read_sids::text[] FROM catalog.acl WHERE acl_id = %s", (copy["acl_id"],)
    ).fetchone()
    assert sorted(read) == sorted([w.g_owners, w.g_writers])
    cid = copy["file_id"]
    assert web.browser(w.writer).get("/v1/files/{file_id}", file_id=cid).status_code == 200
    assert web.browser(w.reader).get("/v1/files/{file_id}", file_id=cid).status_code == 404
    # Held online for the rule's hold, and filled at once for the requester.
    hold = _row(sql, "SELECT * FROM catalog.hold WHERE file_id = %s", cid)
    assert hold["kind"] == "keep_online" and hold["until"] is not None
    rc = _row(sql, "SELECT * FROM catalog.recall WHERE file_id = %s", cid)
    assert rc is not None
    assert {"recall_id": str(rc["recall_id"])} in _queue(sql, "restores")
    # Audited, with an annex event.
    assert _row(sql, "SELECT 1 AS x FROM catalog.audit_log WHERE action = 'annex.placement.placed'"
                " AND file_id = %s", cid) is not None  # fmt: skip
    ev = _row(sql, "SELECT details FROM catalog.annex_event WHERE kind = 'placed'"
              " AND annex_id = %s", int(a["annex_id"]))  # fmt: skip
    assert ev["details"]["path"] == "recalls/a.segy"


def test_a_copy_isnt_made_for_readers_a_folder_above_the_file_keeps_out(web, sql, db_url) -> None:
    """Decision 63: the file's own ACL lets the Owners and Writers read it, but its folder
    on the NAS lets only the Owners through, so a copy readable by the Writers isn't made."""
    w, a, ds, dw, sim = _setup(web, sql)
    fid = _annexed(sql, a, dw, sim, "surveys/b.segy", [w.g_owners, w.g_writers])
    (acl,) = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, search_sids,"
        " search_deny_sids) VALUES (%s, %s, 448, '[]', '{}', %s, '{}') RETURNING acl_id",
        (ds, b"s" * 32, [w.g_owners]),
    ).fetchone()
    sql.execute(
        "UPDATE catalog.nas_folder_acl SET acl_id = %s, observed_at = now()"
        " WHERE annex_id = %s AND path LIKE %s",
        (acl, int(a["annex_id"]), "%surveys"),
    )
    p = _place(web, sql, db_url, w.owner, sim, w, fid)
    assert (p["state"], p.get("fail_reason")) == ("failed", "acl_mismatch"), p


def test_a_flagged_filer_makes_no_group_readable_share_copy_until_acknowledged(
    web, sql, db_url
) -> None:
    """Decision 64: its NAS shows no ACLs, so the read set a copy would grant may be wider
    than the NAS's; only once an ArchiveAdmin acknowledges it."""
    w, a, ds, dw, sim = _setup(web, sql)
    fid = _annexed(sql, a, dw, sim, "surveys/c.segy", [w.g_owners, w.g_writers])
    sql.execute(
        "UPDATE catalog.filer SET acls_hidden_since = now()"
        " WHERE filer_id = (SELECT filer_id FROM catalog.annex WHERE annex_id = %s)",
        (int(a["annex_id"]),),
    )
    p = _place(web, sql, db_url, w.owner, sim, w, fid)
    assert (p["state"], p.get("fail_reason")) == ("failed", "acls_hidden_filer"), p
    sql.execute(
        "UPDATE catalog.filer SET acls_hidden_ack_by = 'x', acls_hidden_ack_at = now()"
        " WHERE filer_id = (SELECT filer_id FROM catalog.annex WHERE annex_id = %s)",
        (int(a["annex_id"]),),
    )
    fid = _annexed(sql, a, dw, sim, "surveys/d.segy", [w.g_owners, w.g_writers])
    p = _place(web, sql, db_url, w.owner, sim, w, fid)
    assert p["state"] == "placed", p


def test_a_name_taken_in_the_share(web, sql, db_url) -> None:
    w, a, ds, dw, sim = _setup(web, sql)
    first = _annexed(sql, a, dw, sim, "one/a.segy", [w.g_owners, w.g_writers])
    p1 = _place(web, sql, db_url, w.owner, sim, w, first)
    row = _row(sql, "SELECT placed_file_id FROM catalog.annex_placement"
               " WHERE placement_id = %s", int(p1["placement_id"]))  # fmt: skip
    copy = int(row["placed_file_id"])
    # A file of someone else's that the requester can't write: never replaced
    # (#68 review), whoever may add files to the folder.
    wider = _annexed(sql, a, dw, sim, "two/a.segy", [w.g_owners, w.g_writers, w.g_readers],
                     data=DATA + b"2")  # fmt: skip
    p2 = _place(web, sql, db_url, w.owner, sim, w, wider)
    assert (p2["outcome"], p2["final_path"]) == ("overwrite_refused_acl",
                                                 "recalls/a-restored.segy")  # fmt: skip
    # The requester may write it: a new version of it, the old one kept.
    rw = _acl(sql, w.dataset_id, [w.g_owners, w.g_writers])
    sql.execute(
        "UPDATE catalog.acl SET entries = %s WHERE acl_id = %s",
        (Jsonb([{"scope": "access", "tag": "group", "sid": w.g_owners, "perms": "rw-"},
                {"scope": "access", "tag": "group", "sid": w.g_writers, "perms": "r--"},
                {"scope": "access", "tag": "mask", "perms": "rw-"}]), rw),
    )  # fmt: skip
    sql.execute("UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (rw, copy))
    p2 = _place(web, sql, db_url, w.owner, sim, w, wider)
    assert (p2["outcome"], p2["final_path"]) == ("placed", "recalls/a.segy")
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.file_version WHERE file_id = %s", (copy,)
    ).fetchone()
    assert n == 2
    # From a file fewer people may read: replacing the bytes would show them
    # to the copy's wider readers. It takes a -restored name instead.
    narrow = _annexed(sql, a, dw, sim, "three/a.segy", [w.g_owners], data=DATA + b"3")
    p3 = _place(web, sql, db_url, w.owner, sim, w, narrow)
    assert p3["outcome"] == "overwrite_refused_acl"
    assert p3["final_path"].startswith("recalls/a-restored-")
    # The same content already there: left alone.
    p5 = _place(web, sql, db_url, w.owner, sim, w, wider, on_conflict="same_content")
    assert (p5["outcome"], p5["final_path"]) == ("already_there", "recalls/a.segy")


def test_the_gates_hold_again_when_the_copy_is_made(web, sql, db_url) -> None:
    w, a, ds, dw, sim = _setup(web, sql)
    fid = _annexed(sql, a, dw, sim, "a.segy", [w.g_owners, w.g_writers])
    owner = web.browser(w.owner)
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"})
    assert r.status_code == 201
    pid = int(r.json()["placement_id"])
    reg = BackendRegistry.from_mapping({w.backend_id: sim})
    now = datetime.now(UTC)
    # The bytes in Azure no longer match the catalog's CRC: nothing is placed.
    sql.execute("UPDATE catalog.file_version SET crc64nvme = %s WHERE file_id = %s",
                (to_bytes(crc64nvme(b"other")), fid))  # fmt: skip
    assert run_job(db_url, lambda c: handle_share_placement(c, reg, pid, now)) == "failed"
    p = _row(sql, "SELECT * FROM catalog.annex_placement WHERE placement_id = %s", pid)
    assert p["fail_reason"] == "crc_mismatch" and p["placed_file_id"] is None
    (n,) = sql.execute(
        "SELECT count(*) FROM catalog.file WHERE origin = 'annex_restore' AND kind = 'file'"
        " AND dataset_id = %s",
        (w.dataset_id,),
    ).fetchone()
    assert n == 0
    # The share's Writers group lost: the requester can't add a file there now.
    sql.execute("UPDATE catalog.file_version SET crc64nvme = %s WHERE file_id = %s",
                (to_bytes(crc64nvme(DATA)), fid))  # fmt: skip
    r = owner.post("/v1/placements", {"file_id": str(fid), "place_at": "default"})
    pid = int(r.json()["placement_id"])
    sql.execute(
        "UPDATE catalog.share SET owners_group_sid = %s, writers_group_sid = NULL"
        " WHERE share_id = %s",
        (w.g_other, w.share_id),
    )
    assert run_job(db_url, lambda c: handle_share_placement(c, reg, pid, now)) == "failed"
    p = _row(sql, "SELECT * FROM catalog.annex_placement WHERE placement_id = %s", pid)
    assert p["fail_reason"] == "access_denied"


def test_a_placement_holds_nothing_across_its_copy_and_read_back(web, sql, db_url) -> None:
    """Run: its row lock took a transaction id across the copy and a read of the whole
    file, hours for a big one, and every edge's feed waited for them."""
    w, a, _ds, dw, sim = _setup(web, sql)
    fid = _annexed(sql, a, dw, sim, "surveys/a.segy", [w.g_owners, w.g_writers])
    provider = Watched(sim, db_url)
    reg = BackendRegistry.from_mapping({w.backend_id: provider})
    jobs = portal_jobs(FakeWorker(reg, Clock(datetime.now(UTC))))
    sql.execute("DELETE FROM pgmq.q_portal")
    r = web.browser(w.owner).post("/v1/placements", {"file_id": str(fid), "place_at": "default"})
    assert r.status_code == 201, r.text
    stats = drain_job(db_url, jobs, "portal")
    assert stats.dead == 0, stats.errors
    got = web.browser(w.owner).get("/v1/placements/{placement_id}",
                                   placement_id=r.json()["placement_id"]).json()  # fmt: skip
    assert got["state"] == "placed", got
    assert {"copy_object", "get_range", "head"} <= set(provider.calls())
    assert provider.with_ids() == [] and provider.with_locks() == []
