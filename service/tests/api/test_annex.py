"""Annex A1 through the control API (control-api.md section 4.8; annex.md):
the edge reports what it sees on a NAS, uploads through the usual path, and
the service verifies each copy in Azure, keeps deleted files' copies as
tombstones, and records reconciliation drift."""

from __future__ import annotations

import asyncio
import base64
import struct
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from omnifile_service.annex.acl import AclError, acl_body, parse_sd
from omnifile_service.annex.purge import purge_due, purge_file
from omnifile_service.annex.rules import copy_template
from omnifile_service.catalog.privileges import as_app
from omnifile_service.retention.depth import prune_versions

from .conftest import E, World, complete_body, hashes_of, upload_blocks

DOMAIN = "S-1-5-21-111-222-333"
OWNERS = f"{DOMAIN}-512"
GEO = f"{DOMAIN}-2001"
INTERNS = f"{DOMAIN}-2002"
ALICE = f"{DOMAIN}-1105"


# --- NT security descriptors -----------------------------------------------------


def sid_bytes(sid: str) -> bytes:
    parts = sid.split("-")
    auth, subs = int(parts[2]), [int(p) for p in parts[3:]]
    return bytes([1, len(subs)]) + auth.to_bytes(6, "big") + struct.pack(f"<{len(subs)}I", *subs)


def ace(kind: int, mask: int, sid: str, flags: int = 0) -> bytes:
    body = struct.pack("<I", mask) + sid_bytes(sid)
    return struct.pack("<BBH", kind, flags, 4 + len(body)) + body


def sd(owner: str, group: str, aces: list[bytes] | None) -> bytes:
    o, g = sid_bytes(owner), sid_bytes(group)
    head = 20
    if aces is None:
        return struct.pack("<BBHIIII", 1, 0, 0x8000, head, head + len(o), 0, 0) + o + g
    acl = b"".join(aces)
    dacl = struct.pack("<BBHHH", 2, 0, 8 + len(acl), len(aces), 0) + acl
    return (
        struct.pack("<BBHIIII", 1, 0, 0x8004, head, head + len(o), 0, head + len(o) + len(g))
        + o
        + g
        + dacl
    )


READ = 0x0012_0089  # FILE_GENERIC_READ
FULL = 0x001F_01FF
TRAVERSE = 0x0000_0020  # FILE_TRAVERSE: a folder lets them through (decision 63)


def nt_acl(aces: list[bytes] | None) -> dict:
    return {"kind": "ntfs_sd", "raw": base64.b64encode(sd(ALICE, GEO, aces)).decode()}


def test_an_nt_descriptor_reads_deny_first() -> None:
    body = acl_body(
        nt_acl(
            [
                ace(1, READ, INTERNS),  # deny interns
                ace(0, FULL, OWNERS),
                ace(0, READ, GEO),
                ace(0, READ, "S-1-5-11"),  # Authenticated Users -> Everyone
                ace(0, READ, f"{DOMAIN}-9", flags=0x08),  # inherit-only: not on the file
            ]
        )
    )
    assert body["deny_first"] and body["raw_kind"] == "ntfs_sd"
    assert body["owner_sid"] == ALICE and body["group_sid"] == GEO
    assert set(body["read"]) == {OWNERS, GEO, "S-1-1-0"}
    assert body["deny"] == [INTERNS]
    # No DACL at all: everyone may read (an empty DACL: nobody).
    assert acl_body(nt_acl(None))["read"] == ["S-1-1-0"]
    assert acl_body(nt_acl([]))["read"] == []


def test_a_descriptor_the_service_cant_read_is_refused_whole() -> None:
    raw = bytearray(sd(ALICE, GEO, [ace(1, READ, INTERNS)]))
    raw[20 + 2 * 28 + 8] = 0x42  # an unknown ACE type
    with pytest.raises(AclError):
        parse_sd(bytes(raw))
    with pytest.raises(AclError):
        acl_body({"kind": "ntfs_sd", "raw": base64.b64encode(raw[:30]).decode()})
    # An NFSv4 deny of read to a principal the edge couldn't map is refused.
    with pytest.raises(AclError):
        acl_body({"kind": "nfs4", "aces": [{"type": "deny", "sid": None, "mask": 1}]})


def test_deny_first_never_lets_a_group_allow_outvote_a_group_deny(sql) -> None:
    def can(read, deny, user, groups, first):
        return sql.execute(
            "SELECT catalog.acl_can_read(%s::catalog.sid[], %s::catalog.sid[], %s, %s, %s)",
            (read, deny, user, groups, first),
        ).fetchone()[0]

    # POSIX order: a group that grants wins over one that doesn't.
    assert can([GEO], [INTERNS], ALICE, [GEO, INTERNS], False) is True
    # NT order: any matching deny refuses.
    assert can([GEO], [INTERNS], ALICE, [GEO, INTERNS], True) is False
    assert can([GEO], [INTERNS], ALICE, [GEO], True) is True
    assert can(["S-1-1-0"], ["S-1-1-0"], ALICE, [], True) is False


# --- the edge's events -------------------------------------------------------------


@pytest.fixture
def annex(make_world, sql):
    w: World = make_world()
    return (w, *make_annex(sql, w))


def make_annex(sql, w: World, protocol: str = "smb") -> tuple[int, int, int]:
    """(annex_id, dataset_id, filer_id): an annex of an SMB filer on the
    world's edge, copying into its own dataset (no share: an annex's copies
    are never served by the edge)."""
    tag = uuid.uuid4().hex[:8]
    (nas,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'nas', '{}', false) RETURNING backend_id",
        (f"nas-{tag}",),
    ).fetchone()
    (filer,) = sql.execute(
        "INSERT INTO catalog.filer (name, host, protocol, export, edge_id, backend_id, created_by)"
        " VALUES (%s, %s, %s, 'geo', %s, %s, 'test') RETURNING filer_id",
        (f"ontap-{tag}", f"ontap-{tag}", protocol, w.edge.edge_id, nas),
    ).fetchone()
    (ds,) = sql.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING dataset_id",
        (f"annex-{tag}", w.backend_id, w.edge.edge_id),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, NULL, '', 'dir')",
        (ds,),
    )
    (src,) = sql.execute(
        "INSERT INTO catalog.source (dataset_id, path, backend_id, container, prefix, owners_sid,"
        " kind) VALUES (%s, 'from-nas', %s, 'geo', 'long-term-storage/', %s, 'annex')"
        " RETURNING source_id",
        (ds, nas, OWNERS),
    ).fetchone()
    (ident,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind) VALUES (%s, 'user')"
        " ON CONFLICT (sid) DO UPDATE SET kind = 'user' RETURNING identity_id",
        (ALICE,),
    ).fetchone()
    (annex_id,) = sql.execute(
        "INSERT INTO catalog.annex (name, source_id, filer_id, dataset_id, edge_id, path,"
        " owners_sid, ruleset_version, created_by)"
        " VALUES (%s, %s, %s, %s, %s, 'long-term-storage', %s, 1, 'test') RETURNING annex_id",
        (f"lts-{tag}", src, filer, ds, w.edge.edge_id, OWNERS),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.annex_ruleset (annex_id, version, body, state, owner_identity_id,"
        " proposed_via, decided_by_identity_id, decided_at)"
        " VALUES (%s, 1, %s, 'active', %s, 'portal', %s, now())",
        (annex_id, psycopg.types.json.Jsonb(copy_template(ds)), ident, ident),
    )
    return annex_id, ds, filer


def events(w: World, annex_id: int, evs: list[dict], key: str | None = None) -> list[dict]:
    r = w.edge.post(f"{E}/annex-events", {"annex_id": str(annex_id), "events": evs}, key=key)
    assert r.status_code == 200, r.text
    return r.json()["results"]


DATA = b"C 1 CLIENT OmniFile" + bytes(3200 - 19) + bytes(range(256)) * 20  # a SEG-Y-ish header


def arrived(path: str, data: bytes, ino: str = "1001", mtime: int = 1_700_000_000, **kw) -> dict:
    return {
        "kind": "arrived",
        "path": path,
        "nas_file_id": ino,
        "size": len(data),
        "mtime_ns": str(mtime * 10**9),
        "ctime_ns": str(mtime * 10**9 + 5),
        "acl": nt_acl([ace(0, READ, GEO), ace(0, FULL, OWNERS)]),
        **kw,
    }


def copy(w: World, facade_client, file_id: str, data: bytes, base: str | None = None) -> str:
    body = {
        "op_id": f"annex-{uuid.uuid4().hex}",
        "file": {"file_id": file_id},
        "size": len(data),
        "mtime_ns": "1700000000000000000",
        "block_size": 4096,
        **hashes_of(data),
    }
    if base is not None:
        body["base_version_id"] = base
    up = w.edge.post(f"{E}/uploads", body)
    assert up.status_code == 201, up.text
    u = up.json()
    assert u["action"] == "upload"
    upload_blocks(facade_client, u["target"]["url"], data, 4096)
    done = w.edge.post(
        f"{E}/uploads/{{upload_id}}/complete", complete_body(data), upload_id=u["upload_id"]
    )
    assert done.status_code == 200, done.text
    return u["version_id"]


def test_a_verification_racing_the_copied_event_isnt_lost(
    annex, facade_client, sql, db_url
) -> None:
    """A small file is read back about when the edge says it copied it. The worker's
    trigger ran before the event, found no copy to mark, and committed after the event had
    read "not verified": the location verified, the annex file never saying so, for good."""
    import threading

    w, annex_id, ds, _ = annex
    data = b"read back as the edge reports it"
    [r] = events(w, annex_id, [arrived("hidden.txt", data, kind="arrived")])
    fid = r["file_id"]
    vid = copy(w, facade_client, fid, data)
    done: list[list[dict]] = []
    with psycopg.connect(as_app(db_url, "worker")) as worker:
        moved = worker.execute(
            "UPDATE catalog.location SET verified = true, verified_at = now(),"
            " verified_by = 'azure_read_crc' WHERE version_id = %s AND role = 'primary'"
            " AND NOT verified",
            (int(vid),),
        )
        assert moved.rowcount == 1
        t = threading.Thread(
            target=lambda: done.append(
                events(
                    w,
                    annex_id,
                    [
                        {
                            "kind": "copied",
                            "path": "hidden.txt",
                            "file_id": fid,
                            "version_id": vid,
                            "size": len(data),
                            "mtime_ns": "1700000000000000000",
                        }
                    ],
                )
            )
        )
        t.start()
        t.join(1.5)  # the event waits for the worker's commit
        worker.commit()
    t.join(30)
    assert not t.is_alive()
    assert done[0][0]["status"] == "applied"
    (verified,) = sql.execute(
        "SELECT verified_version_id FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert verified == int(vid)


def _copied_event(w, annex_id, path, fid, vid, data):
    return events(w, annex_id, [{"kind": "copied", "path": path, "file_id": fid,
                                 "version_id": vid, "size": len(data),
                                 "mtime_ns": "1700000000000000000"}])  # fmt: skip


def _spoil_crc(sql, vid) -> None:
    """The catalog says other bytes than Azure holds: the read-back refuses the copy."""
    sql.execute(
        "UPDATE catalog.location SET crc64nvme = '\\x0000000000000000'::bytea"
        " WHERE version_id = %s",
        (int(vid),),
    )


def _refusals(sql, annex_id, fid):
    return sql.execute(
        "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s AND file_id = %s"
        " AND kind = 'problem' AND details->>'reason' = 'copy_refused'",
        (annex_id, int(fid)),
    ).fetchone()[0]


def _asked(w, annex_id):
    r = w.edge.get(f"{E}/annexes")
    assert r.status_code == 200, r.text
    [a] = [a for a in r.json()["annexes"] if a["annex_id"] == str(annex_id)]
    return a.get("reconcile_asked_at")


def test_a_refused_read_back_is_said_and_asks_for_a_reconciliation(
    annex, facade_client, sql
) -> None:
    """It stayed `copied` for good, with an audit row and a log line only, until the next
    reconciliation, up to `reconcile_days` away."""
    w, annex_id, ds, _ = annex
    data = b"bytes Azure won't agree with"
    [r] = events(w, annex_id, [arrived("refused.bin", data)])
    fid = r["file_id"]
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, "refused.bin", fid, vid, data)
    assert _asked(w, annex_id) is None
    _spoil_crc(sql, vid)
    assert w.verify()
    assert _refusals(sql, annex_id, fid) == 1
    assert _asked(w, annex_id) is not None
    (verified,) = sql.execute(
        "SELECT verified_version_id FROM catalog.annex_file WHERE file_id = %s", (int(fid),)
    ).fetchone()
    assert verified is None, "still not counted as copied"


def test_a_copy_azure_no_longer_has_is_said_too(annex, facade_client, sql) -> None:
    w, annex_id, ds, _ = annex
    data = b"a copy that went"
    [r] = events(w, annex_id, [arrived("gone.bin", data)])
    fid = r["file_id"]
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, "gone.bin", fid, vid, data)
    sql.execute(
        "UPDATE catalog.location SET object_key = object_key || '-nowhere' WHERE version_id = %s",
        (int(vid),),
    )
    assert w.verify()
    assert _refusals(sql, annex_id, fid) == 1
    (d,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE file_id = %s AND kind = 'problem'",
        (int(fid),),
    ).fetchone()
    assert d["cause"] == "missing" and "didn't match" not in d["detail"]


def test_a_read_back_refused_before_the_copied_event_is_said_by_it(
    annex, facade_client, sql
) -> None:
    """A small file is read back at once: the refusal found no copy to speak of."""
    w, annex_id, ds, _ = annex
    data = b"refused before the edge said it copied it"
    [r] = events(w, annex_id, [arrived("early.bin", data)])
    fid = r["file_id"]
    vid = copy(w, facade_client, fid, data)
    _spoil_crc(sql, vid)
    assert w.verify()
    assert _refusals(sql, annex_id, fid) == 0
    _copied_event(w, annex_id, "early.bin", fid, vid, data)
    assert _refusals(sql, annex_id, fid) == 1
    assert _asked(w, annex_id) is not None


def test_a_refusal_racing_the_copied_event_isnt_lost(annex, facade_client, sql, db_url) -> None:
    """The refusal's own look and the event's, each before the other's commit: both missed."""
    import threading

    from omnifile_service.annex.readback import copy_refused

    w, annex_id, ds, _ = annex
    data = b"refused while the event is on its way"
    [r] = events(w, annex_id, [arrived("race.bin", data)])
    fid = r["file_id"]
    vid = copy(w, facade_client, fid, data)
    (lid,) = sql.execute(
        "SELECT location_id FROM catalog.location WHERE version_id = %s", (int(vid),)
    ).fetchone()
    looked, release, found = threading.Event(), threading.Event(), []

    async def worker_side():
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as c:
            async with c.transaction():
                await c.execute(
                    "INSERT INTO catalog.audit_log (actor, action, decision, file_id, version_id,"
                    " details) VALUES ('system:worker', 'location.verify', 'deny', %s, %s,"
                    ' \'{"reason": "spoilt"}\'::jsonb)',
                    (int(fid), int(vid)),
                )
                found.append(await copy_refused(c, lid, int(fid), int(vid), "spoilt", "mismatch"))
                looked.set()
                await asyncio.get_running_loop().run_in_executor(None, release.wait)

    worker = threading.Thread(target=lambda: asyncio.run(worker_side()))
    worker.start()
    assert looked.wait(10)
    edge = threading.Thread(target=lambda: _copied_event(w, annex_id, "race.bin", fid, vid, data))
    edge.start()
    edge.join(1.5)  # the event waits for the refusal's commit
    release.set()
    worker.join(30)
    edge.join(30)
    assert found == [0], "the refusal found no copy yet"
    assert _refusals(sql, annex_id, fid) == 1, "the event found the refusal"


def _af(sql, fid):
    return sql.execute(
        "SELECT copy_refusals, copy_refused_version_id FROM catalog.annex_file WHERE file_id = %s",
        (int(fid),),
    ).fetchone()


def _ask_in(sql, annex_id):
    """Seconds from now to the annex's ask (negative: due)."""
    (v,) = sql.execute(
        "SELECT extract(epoch FROM reconcile_asked_at - now()) FROM catalog.annex"
        " WHERE annex_id = %s",
        (annex_id,),
    ).fetchone()
    return None if v is None else float(v)


def _refuse(w, facade_client, sql, annex_id, path, fid, data):
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, path, fid, vid, data)
    _spoil_crc(sql, vid)
    assert w.verify()
    return vid


def _records(w, annex_id):
    r = w.edge.get(f"{E}/annexes/{{annex_id}}/records", annex_id=annex_id)
    assert r.status_code == 200, r.text
    return {x["path"]: x for x in r.json()["records"]}


def test_refusals_are_bounded_backed_off_and_end_at_a_verified_copy(
    annex, facade_client, sql, caplog
) -> None:
    """Review of #81: a file always refused was copied again for ever (a new version, a
    full upload and a full read-back each time), with no alert that stayed up."""
    import logging

    from omnifile_service.annex.readback import AT_BOUND_SQL, MAX_REFUSALS

    w, annex_id, ds, _ = annex
    data = b"bytes Azure never agrees with"
    [r] = events(w, annex_id, [arrived("loop.bin", data)])
    fid = r["file_id"]

    def at_bound():
        return dict(sql.execute(AT_BOUND_SQL, (MAX_REFUSALS,)).fetchall())

    # 1: asked at once.
    _refuse(w, facade_client, sql, annex_id, "loop.bin", fid, data)
    assert _af(sql, fid)[0] == 1
    assert _ask_in(sql, annex_id) <= 1 and _asked(w, annex_id) is not None
    assert _records(w, annex_id)["loop.bin"]["refused"] is True
    # 2: asked an hour later, and not shown to the edge until then.
    _refuse(w, facade_client, sql, annex_id, "loop.bin", fid, data)
    assert _af(sql, fid)[0] == 2
    assert 3500 < _ask_in(sql, annex_id) <= 3600
    assert _asked(w, annex_id) is None
    later = _ask_in(sql, annex_id)
    # 3: no more asks, one ALERT, and the gauge's row.
    with caplog.at_level(logging.ERROR, logger="omnifile_service.annex.readback"):
        _refuse(w, facade_client, sql, annex_id, "loop.bin", fid, data)
        assert _af(sql, fid)[0] == 3
        assert abs(_ask_in(sql, annex_id) - later) < 60, "not asked again"
        assert _records(w, annex_id)["loop.bin"]["refused"] is False
        assert at_bound() == {sql.execute("SELECT name FROM catalog.annex WHERE annex_id = %s",
                                          (annex_id,)).fetchone()[0]: 1}  # fmt: skip
        # 4 (the edge's own reconciliation copies it again): no second ALERT.
        _refuse(w, facade_client, sql, annex_id, "loop.bin", fid, data)
        assert _af(sql, fid)[0] == 4
    alerts = [m for m in caplog.messages if m.startswith("ALERT") and "refused" in m]
    assert len(alerts) == 1, alerts
    assert _refusals(sql, annex_id, fid) == 4, "one problem per version"
    # A copy that verifies sets the count back, and the gauge goes.
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, "loop.bin", fid, vid, data)
    assert w.verify()
    assert _af(sql, fid) == (0, None)
    assert at_bound() == {}
    # So the next refusal asks at once again (other bytes, or the upload dedupes).
    _refuse(w, facade_client, sql, annex_id, "loop.bin", fid, data.upper())
    assert _af(sql, fid)[0] == 1
    assert _ask_in(sql, annex_id) <= 1 and _asked(w, annex_id) is not None


def test_a_copy_already_verified_when_copied_ends_the_refusals(annex, facade_client, sql) -> None:
    """A dedupe onto a verified copy: no read-back, so no trigger, sets the count back."""
    w, annex_id, ds, _ = annex
    good, bad = b"bytes another file has, verified", b"bytes Azure won't agree with...."
    assert len(good) == len(bad)
    res = events(w, annex_id, [arrived("other.bin", good, ino="21"),
                               arrived("again.bin", good, ino="22")])  # fmt: skip
    of, af = res[0]["file_id"], res[1]["file_id"]
    ov = copy(w, facade_client, of, good)
    _copied_event(w, annex_id, "other.bin", of, ov, good)
    assert w.verify()
    _refuse(w, facade_client, sql, annex_id, "again.bin", af, bad)
    assert _af(sql, af)[0] == 1
    up = w.edge.post(f"{E}/uploads", {"op_id": f"annex-{uuid.uuid4().hex}",
                                      "file": {"file_id": af}, "size": len(good),
                                      "mtime_ns": "1700000000000000000", "block_size": 4096,
                                      **hashes_of(good)})  # fmt: skip
    assert up.status_code == 201, up.text
    assert up.json()["action"] == "dedupe", up.json()
    vid = up.json()["version_id"]
    [r] = _copied_event(w, annex_id, "again.bin", af, vid, good)
    assert r["verified"] is True
    assert _af(sql, af) == (0, None)


def test_a_version_refused_twice_counts_once(annex, facade_client, sql, db_url) -> None:
    from omnifile_service.annex.readback import copy_refused

    w, annex_id, ds, _ = annex
    data = b"read back twice"
    [r] = events(w, annex_id, [arrived("twice.bin", data)])
    fid = r["file_id"]
    vid = _refuse(w, facade_client, sql, annex_id, "twice.bin", fid, data)
    (lid,) = sql.execute(
        "SELECT location_id FROM catalog.location WHERE version_id = %s", (int(vid),)
    ).fetchone()

    async def again():
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as c:
            async with c.transaction():
                return await copy_refused(c, lid, int(fid), int(vid), "again", "mismatch")

    assert asyncio.run(again()) == 1
    assert _af(sql, fid) == (1, int(vid))
    assert _refusals(sql, annex_id, fid) == 1


def test_records_say_refused_only_for_a_refused_copy(annex, facade_client, sql) -> None:
    """Review of #81: `copy_ok` is false while a read-back is pending too, so a
    reconciliation asked for by one refusal copied the whole backlog again."""
    w, annex_id, ds, _ = annex
    pending, spoilt = b"waiting for its read-back", b"refused by its read-back"
    res = events(w, annex_id, [arrived("pending.bin", pending, ino="11"),
                               arrived("spoilt.bin", spoilt, ino="12")])  # fmt: skip
    pf, sf = res[0]["file_id"], res[1]["file_id"]
    pv = copy(w, facade_client, pf, pending)
    _copied_event(w, annex_id, "pending.bin", pf, pv, pending)
    sv = copy(w, facade_client, sf, spoilt)
    _copied_event(w, annex_id, "spoilt.bin", sf, sv, spoilt)
    # Only the spoilt one's read-back runs.
    _spoil_crc(sql, sv)
    sql.execute(
        "DELETE FROM pgmq.q_verify WHERE (message->>'location_id')::bigint IN"
        " (SELECT location_id FROM catalog.location WHERE version_id = %s)",
        (int(pv),),
    )
    assert w.verify()
    recs = _records(w, annex_id)
    assert (recs["pending.bin"]["copy_ok"], recs["pending.bin"]["refused"]) == (False, False)
    assert (recs["spoilt.bin"]["copy_ok"], recs["spoilt.bin"]["refused"]) == (False, True)


def test_a_copy_mismatch_drift_is_resolved_by_a_verified_copy_not_a_copied_event(
    annex, facade_client, sql
) -> None:
    w, annex_id, ds, _ = annex
    data = b"drifted"
    [r] = events(w, annex_id, [arrived("drift.bin", data)])
    fid = r["file_id"]
    _refuse(w, facade_client, sql, annex_id, "drift.bin", fid, data)
    drift = {"kind": "drift", "path": "drift.bin", "file_id": fid,
             "details": {"drift": "copy_mismatch", "observed": {"copy_ok": False}}}  # fmt: skip
    events(w, annex_id, [drift])
    events(w, annex_id, [drift])  # the next reconciliation's: the same row

    def open_drift():
        return sql.execute(
            "SELECT count(*) FILTER (WHERE resolved_at IS NULL"
            "   AND NOT coalesce(observed, '{}'::jsonb) ? 'resolved_by'), count(*)"
            "  FROM catalog.reconcile_drift WHERE source = 'annex' AND kind = 'copy_mismatch'"
            "   AND object_key LIKE %s",
            ("%drift.bin",),
        ).fetchone()

    assert open_drift() == (1, 1)
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, "drift.bin", fid, vid, data)
    assert open_drift() == (1, 1), "copied, not yet read back"
    assert w.verify()
    assert open_drift() == (0, 1)


def test_a_refusal_takes_the_annex_before_the_location(annex, facade_client, sql, db_url) -> None:
    """Review of #81: an edge's event batch takes the annex, then the location; the
    refusal took them the other way round."""
    import threading

    from omnifile_service.annex.readback import copy_refused

    w, annex_id, ds, _ = annex
    data = b"locks in order"
    [r] = events(w, annex_id, [arrived("locks.bin", data)])
    fid = r["file_id"]
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, "locks.bin", fid, vid, data)
    (lid,) = sql.execute(
        "SELECT location_id FROM catalog.location WHERE version_id = %s", (int(vid),)
    ).fetchone()
    holder = psycopg.connect(db_url)
    holder.execute("SELECT 1 FROM catalog.annex WHERE annex_id = %s FOR UPDATE", (annex_id,))
    done = []

    async def refuse():
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as c:
            async with c.transaction():
                done.append(await copy_refused(c, lid, int(fid), int(vid), "x", "mismatch"))

    t = threading.Thread(target=lambda: asyncio.run(refuse()))
    t.start()
    t.join(1.5)
    assert t.is_alive(), "it waits for the annex"
    # The location is free meanwhile: the batch holding the annex could take it.
    with psycopg.connect(db_url) as probe:
        probe.execute(
            "SELECT 1 FROM catalog.location WHERE location_id = %s FOR UPDATE NOWAIT", (lid,)
        )
    holder.commit()
    holder.close()
    t.join(30)
    assert done == [1]


def test_the_edge_lists_its_annexes_with_the_copy_template(annex) -> None:
    w, annex_id, ds, _ = annex
    r = w.edge.get(f"{E}/annexes")
    assert r.status_code == 200, r.text
    [a] = [a for a in r.json()["annexes"] if a["annex_id"] == str(annex_id)]
    assert a["mode"] == "copy" and a["state"] == "registered"
    assert a["filer"]["protocol"] == "smb" and a["filer"]["export"] == "geo"
    assert a["path"] == "long-term-storage"
    assert a["ruleset"]["body"]["template"] == "copy"
    assert a["settings"]["quiet_minutes"] == 10


def test_copy_verify_change_delete_and_rename(annex, facade_client, sql) -> None:
    w, annex_id, ds, filer = annex
    res = events(
        w,
        annex_id,
        [
            {
                "kind": "mount",
                "details": {
                    "ok": True,
                    "read_only": True,
                    "readable": True,
                    "sec": "krb5",
                    "acls_shown": True,
                },
            },
            {"kind": "baseline", "details": {"files": 1, "bytes": len(DATA)}},
            arrived("dataset/line_1042.segy", DATA, baseline=True),
            arrived("dataset/notes.txt", b"hello", ino="1002"),
        ],
    )
    assert [r["status"] for r in res] == ["applied"] * 4, res
    fid, notes = res[2]["file_id"], res[3]["file_id"]
    assert res[2]["copy"] is True and res[2]["content_version_id"] is None
    row = sql.execute(
        "SELECT f.state, f.origin, f.resident_bytes, a.raw_kind, a.deny_first, a.read_sids::text[],"
        " p.name AS parent, (SELECT name FROM catalog.file WHERE file_id = p.parent_id) AS top"
        " FROM catalog.file f JOIN catalog.acl a ON a.acl_id = f.acl_id"
        " JOIN catalog.file p ON p.file_id = f.parent_id WHERE f.file_id = %s",
        (fid,),
    ).fetchone()
    assert row[:5] == ("remote", "annex", 0, "ntfs_sd", True)
    assert set(row[5]) == {GEO, OWNERS}
    assert row[6:] == ("dataset", "from-nas")  # under the source's folder
    health, state, files = sql.execute(
        "SELECT fl.health, a.state, a.baseline_files FROM catalog.annex a"
        " JOIN catalog.filer fl USING (filer_id) WHERE a.annex_id = %s",
        (annex_id,),
    ).fetchone()
    assert health["read_only"] is True and state == "active" and files == 1

    # The copy goes through the usual upload path and stays `remote`.
    vid = copy(w, facade_client, fid, DATA)
    assert sql.execute(
        "SELECT state, resident_bytes FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone() == ("remote", 0)
    res = events(
        w,
        annex_id,
        [
            {
                "kind": "copied",
                "path": "dataset/line_1042.segy",
                "file_id": fid,
                "version_id": vid,
                "size": len(DATA),
                "mtime_ns": "1700000000000000000",
                "ctime_ns": "1700000000000000005",
            }
        ],
    )
    assert res[0]["status"] == "applied" and res[0]["verified"] is False
    assert w.verify(), "the verify queue reads the copy back in Azure"
    v = sql.execute(
        "SELECT af.verified_version_id, l.verified_by FROM catalog.annex_file af"
        " JOIN catalog.location l ON l.version_id = af.copied_version_id"
        " WHERE af.file_id = %s",
        (fid,),
    ).fetchone()
    assert v == (int(vid), "azure_read_crc")
    kinds = [
        k
        for (k,) in sql.execute(
            "SELECT kind FROM catalog.annex_event WHERE annex_id = %s ORDER BY event_id",
            (annex_id,),
        )
    ]
    assert kinds == ["mount", "baseline", "arrived", "copied", "verified"]  # no baseline arrival

    # Seen again unchanged: nothing to copy.
    res = events(w, annex_id, [{**arrived("dataset/line_1042.segy", DATA), "kind": "changed"}])
    assert res[0]["copy"] is False
    # Changed on the NAS: a new version, the old one kept.
    new = DATA[:-1] + b"!"
    res = events(
        w,
        annex_id,
        [{**arrived("dataset/line_1042.segy", new, mtime=1_700_000_100), "kind": "changed"}],
    )
    assert res[0]["copy"] is True and res[0]["content_version_id"] == vid
    vid2 = copy(w, facade_client, fid, new, base=vid)
    assert vid2 != vid
    n = sql.execute(
        "SELECT count(*) FROM catalog.file_version WHERE file_id = %s"
        " AND kind = 'content' AND state = 'committed'",
        (fid,),
    ).fetchone()[0]
    assert n == 2

    # Renamed on the NAS: same NAS id, the catalog file moves.
    res = events(
        w,
        annex_id,
        [
            {
                "kind": "renamed",
                "path": "dataset/hello.txt",
                "old_path": "dataset/notes.txt",
                "nas_file_id": "1002",
            }
        ],
    )
    assert res[0]["status"] == "applied" and res[0]["file_id"] == notes
    assert (
        sql.execute("SELECT name FROM catalog.file WHERE file_id = %s", (notes,)).fetchone()[0]
        == "hello.txt"
    )

    # Deleted on the NAS: a tombstone; the copy is kept (decision 40, 51).
    res = events(
        w, annex_id, [{"kind": "left", "path": "dataset/line_1042.segy", "nas_file_id": "1001"}]
    )
    assert res[0]["status"] == "applied"
    deleted, gone, locs = sql.execute(
        "SELECT f.deleted_at IS NOT NULL, af.gone_since IS NOT NULL,"
        " (SELECT count(*) FROM catalog.location l WHERE l.file_id = f.file_id"
        "   AND l.state = 'committed')"
        " FROM catalog.file f JOIN catalog.annex_file af USING (file_id) WHERE f.file_id = %s",
        (fid,),
    ).fetchone()
    assert deleted and gone and locs == 2
    # Recreated under the same name: a new catalog file at that path.
    res = events(w, annex_id, [arrived("dataset/line_1042.segy", DATA, ino="1003")])
    assert res[0]["status"] == "applied" and res[0]["file_id"] != fid

    # The edge's reconciliation reads the catalog's record.
    r = w.edge.get(f"{E}/annexes/{{annex_id}}/records", {"limit": 10}, annex_id=annex_id)
    assert r.status_code == 200, r.text
    recs = {x["path"]: x for x in r.json()["records"]}
    assert set(recs) == {"dataset/hello.txt", "dataset/line_1042.segy"}
    assert recs["dataset/line_1042.segy"]["nas_file_id"] == "1003"

    # Drift found by reconciliation, and marked by the event that fixes it.
    res = events(
        w,
        annex_id,
        [
            {
                "kind": "drift",
                "path": "dataset/hello.txt",
                "details": {
                    "drift": "changed_no_event",
                    "expected": {"size": 5},
                    "observed": {"size": 6},
                },
            },
            {**arrived("dataset/hello.txt", b"hello!", ino="1002"), "kind": "changed"},
        ],
    )
    assert [r["status"] for r in res] == ["applied", "applied"]
    d = sql.execute(
        "SELECT kind, object_key, observed->>'resolved_by' FROM catalog.reconcile_drift"
        " WHERE source = 'annex' AND backend_id = (SELECT backend_id FROM catalog.filer"
        " WHERE filer_id = %s)",
        (filer,),
    ).fetchall()
    assert d == [("changed_no_event", "long-term-storage/dataset/hello.txt", "arrived")]


def test_one_bad_event_never_loses_the_others(annex, sql) -> None:
    w, annex_id, *_ = annex
    res = events(
        w,
        annex_id,
        [
            arrived("a.txt", b"a", ino="1"),
            {
                "kind": "arrived",
                "path": "../escape",
                "nas_file_id": "2",
                "size": 1,
                "mtime_ns": "1",
            },
            {"kind": "teleported", "path": "x"},
            {**arrived("b.txt", b"b", ino="3"), "acl": {"kind": "ntfs_sd", "raw": "AAAA"}},
            arrived("a.txt/inside", b"c", ino="4"),  # a.txt is a file in the catalog
        ],
    )
    assert [r["status"] for r in res] == ["applied", "rejected", "rejected", "applied", "rejected"]
    assert res[2]["code"] == "unknown_kind" and res[4]["code"] == "path_conflict"
    # An ACL the service can't read: copied, but unreadable in the portal.
    acl, problem = sql.execute(
        "SELECT f.acl_id, (SELECT count(*) FROM catalog.annex_event e WHERE e.file_id = f.file_id"
        "  AND e.kind = 'problem') FROM catalog.file f WHERE f.file_id = %s",
        (res[3]["file_id"],),
    ).fetchone()
    assert acl is None and problem == 1


def test_a_replayed_batch_is_answered_from_the_store(annex, sql) -> None:
    w, annex_id, *_ = annex
    key = f"annex-batch-{uuid.uuid4().hex}"
    first = events(w, annex_id, [arrived("x.bin", b"x", ino="9")], key=key)
    again = events(w, annex_id, [arrived("x.bin", b"x", ino="9")], key=key)
    assert first == again
    n = sql.execute(
        "SELECT count(*) FROM catalog.annex_file WHERE annex_id = %s", (annex_id,)
    ).fetchone()[0]
    assert n == 1


def test_annex_datasets_are_not_in_the_edge_feed(annex, sql) -> None:
    w, annex_id, ds, _ = annex
    events(w, annex_id, [arrived("in-feed.txt", b"?", ino="7")])
    r = w.edge.get(f"{E}/feed", {"after": "0.0", "limit": 1000})
    assert r.status_code == 200, r.text
    assert all(e.get("dataset_id") != str(ds) for e in r.json()["events"])
    assert all(e.get("file", {}).get("dataset_id") != str(ds) for e in r.json()["events"])


def test_another_edge_cant_report_for_the_annex(annex, enrol_edge) -> None:
    w, annex_id, *_ = annex
    other = enrol_edge()
    r = other.post(f"{E}/annex-events", {"annex_id": str(annex_id), "events": [arrived("x", b"x")]})
    assert r.status_code == 404


def test_purge_prunes_the_copy_only_of_a_deleted_file(annex, facade_client, sql, db_url) -> None:
    w, annex_id, ds, _ = annex
    [res] = events(w, annex_id, [arrived("gone.segy", DATA, ino="50")])
    fid = res["file_id"]
    vid = copy(w, facade_client, fid, DATA)
    events(
        w,
        annex_id,
        [
            {
                "kind": "copied",
                "path": "gone.segy",
                "file_id": fid,
                "version_id": vid,
                "size": len(DATA),
                "mtime_ns": "1700000000000000000",
                "ctime_ns": "1700000000000000005",
            }
        ],
    )

    async def run() -> str:
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            return await purge_file(conn, annex_id, int(fid), datetime.now(UTC))

    # Not requested (and the file is still on the NAS): nothing happens.
    assert asyncio.run(run()) == "noop"
    events(w, annex_id, [{"kind": "left", "path": "gone.segy"}])
    sql.execute(
        "UPDATE catalog.annex_file SET purge_requested_at = now(), purge_requested_by = 'user:1'"
        " WHERE file_id = %s",
        (fid,),
    )
    assert asyncio.run(run()) == "purged"
    states = sql.execute(
        "SELECT v.kind, v.state, l.state FROM catalog.file_version v"
        " LEFT JOIN catalog.location l ON l.version_id = v.version_id"
        " WHERE v.file_id = %s ORDER BY v.version_id",
        (fid,),
    ).fetchall()
    assert ("content", "pruned", "deleting") in states
    assert ("tombstone", "committed", None) in states
    queued = sql.execute(
        "SELECT count(*) FROM pgmq.q_retention WHERE message->>'op' = 'delete_blob'"
    ).fetchone()[0]
    assert queued >= 1
    assert asyncio.run(run()) == "noop"  # once


def test_callback_allows_grant_nothing_and_callback_denies_still_deny() -> None:
    body = acl_body(
        nt_acl(
            [
                ace(0x09, READ, GEO),  # ACCESS_ALLOWED_CALLBACK: a condition we can't test
                ace(0x0A, READ, INTERNS),  # ACCESS_DENIED_CALLBACK
                ace(0, READ, OWNERS),
            ]
        )
    )
    assert body["read"] == [OWNERS]
    assert body["deny"] == [INTERNS]


def test_a_filers_protocol_decides_the_acl_kind_it_may_send() -> None:
    posix = {"kind": "posix", "mode": 0o644, "owner_sid": ALICE, "group_sid": GEO}
    nfs4 = {"kind": "nfs4", "aces": [{"type": "allow", "sid": GEO, "mask": 1}]}
    # Over SMB the mount's owner, group and mode are the client's: refused.
    for bad, proto in ((posix, "smb"), (nfs4, "smb"), (posix, "nfs4"), (nt_acl([]), "nfs3")):
        with pytest.raises(AclError):
            acl_body(bad, proto)
    assert acl_body(nt_acl([ace(0, READ, GEO)]), "smb")["read"] == [GEO]
    assert acl_body(nfs4, "nfs4")["read"] == [GEO]
    assert acl_body(posix, "nfs3")["raw_kind"] == "posix"


def test_an_nfs4_acl_reads_deny_first() -> None:
    body = acl_body(
        {
            "kind": "nfs4",
            "aces": [
                {"type": "allow", "sid": GEO, "mask": 1},
                {"type": "deny", "sid": INTERNS, "mask": 1},
                {"type": "allow", "sid": INTERNS, "mask": 1},
            ],
        },
        "nfs4",
    )
    assert body["deny_first"] is True
    assert body["read"] == [GEO] and body["deny"] == [INTERNS]


def test_a_posix_acl_from_an_smb_annex_leaves_the_file_unreadable(annex, sql) -> None:
    w, annex_id, *_ = annex
    posix = {"kind": "posix", "mode": 0o777, "owner_sid": ALICE, "group_sid": GEO}
    [res] = events(w, annex_id, [{**arrived("open.txt", b"x", ino="61"), "acl": posix}])
    assert res["status"] == "applied"
    acl = sql.execute(
        "SELECT acl_id FROM catalog.file WHERE file_id = %s", (res["file_id"],)
    ).fetchone()[0]
    assert acl is None, "fail closed: nobody reads it in the portal"


def test_a_change_whose_acl_cant_be_read_drops_the_old_acl(annex, sql) -> None:
    w, annex_id, *_ = annex
    [res] = events(w, annex_id, [arrived("wide.txt", b"one", ino="62")])
    fid = res["file_id"]
    assert sql.execute("SELECT acl_id FROM catalog.file WHERE file_id = %s", (fid,)).fetchone()[0]
    bad = {**arrived("wide.txt", b"one!", ino="62", mtime=1_700_000_500), "kind": "changed"}
    bad["acl"] = {"kind": "ntfs_sd", "raw": "AAAA"}
    events(w, annex_id, [bad])
    acl = sql.execute("SELECT acl_id FROM catalog.file WHERE file_id = %s", (fid,)).fetchone()[0]
    assert acl is None, "never the old, perhaps wider, ACL"


def test_an_annexes_copies_are_never_pruned_by_depth(annex, facade_client, sql, db_url) -> None:
    """Decision 51: kept until an ArchiveAdmin purges them, though an annex
    dataset has no shares (rewind depth 0)."""
    w, annex_id, ds, _ = annex

    def copied(path: str, fid: str, vid: str, data: bytes) -> None:
        events(
            w,
            annex_id,
            [
                {
                    "kind": "copied",
                    "path": path,
                    "file_id": fid,
                    "version_id": vid,
                    "size": len(data),
                    "mtime_ns": "1700000000000000000",
                    "ctime_ns": "1700000000000000005",
                }
            ],
        )

    [gone] = events(w, annex_id, [arrived("gone.segy", DATA, ino="71")])
    v_gone = copy(w, facade_client, gone["file_id"], DATA)
    copied("gone.segy", gone["file_id"], v_gone, DATA)
    events(w, annex_id, [{"kind": "left", "path": "gone.segy", "nas_file_id": "71"}])
    [chg] = events(w, annex_id, [arrived("changed.segy", DATA, ino="72")])
    v_old = copy(w, facade_client, chg["file_id"], DATA)
    copied("changed.segy", chg["file_id"], v_old, DATA)
    new = DATA[:-1] + b"?"
    events(
        w,
        annex_id,
        [{**arrived("changed.segy", new, ino="72", mtime=1_700_000_100), "kind": "changed"}],
    )
    copy(w, facade_client, chg["file_id"], new, base=v_old)

    async def prune():
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            return await prune_versions(conn, datetime.now(UTC) + timedelta(days=2), dataset_id=ds)

    result = asyncio.run(prune())
    assert result.versions == [] and result.locations == []
    states = dict(
        sql.execute(
            "SELECT version_id::text, state FROM catalog.file_version WHERE version_id = ANY(%s)",
            ([int(v_gone), int(v_old)],),
        ).fetchall()
    )
    assert states == {v_gone: "committed", v_old: "committed"}


def test_the_edge_never_starts_an_annex_waiting_for_approval(annex, sql) -> None:
    w, annex_id, *_ = annex
    sql.execute(
        "UPDATE catalog.annex_ruleset SET state = 'proposed', decided_by_identity_id = NULL,"
        " decided_at = NULL WHERE annex_id = %s",
        (annex_id,),
    )
    r = w.edge.get(f"{E}/annexes")
    assert r.status_code == 200, r.text
    assert str(annex_id) not in {a["annex_id"] for a in r.json()["annexes"]}


def test_a_retention_purges_the_due_files_whatever_waits_before_them(
    make_world, sql, db_url
) -> None:
    """Two annexes: A keeps deleted files' copies 365 days, and its older,
    not-yet-due rows come first by gone_since and fill the batch; B keeps
    them 1 day, and its row is due. B's must be purged anyway."""
    w: World = make_world()
    a_id, a_ds, _ = make_annex(sql, w)
    b_id, b_ds, _ = make_annex(sql, w)
    sql.execute("UPDATE catalog.annex SET deleted_retention_days = 365 WHERE annex_id = %s",
                (a_id,))  # fmt: skip
    sql.execute("UPDATE catalog.annex SET deleted_retention_days = 1 WHERE annex_id = %s",
                (b_id,))  # fmt: skip

    def gone(annex_id: int, ds: int, name: str, days: int) -> int:
        root = sql.execute(
            "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL", (ds,)
        ).fetchone()[0]
        (fid,) = sql.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, size, state, origin,"
            " deleted_at) VALUES (%s, %s, %s, 'file', 1, 'remote', 'annex', now())"
            " RETURNING file_id",
            (ds, root, name),
        ).fetchone()
        sql.execute(
            "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size,"
            " mtime_ns, first_seen, last_seen, gone_since, nas_state) VALUES (%s, %s, %s, %s,"
            " 1, 1, now(), now(), now() - make_interval(days => %s), 'gone')",
            (annex_id, fid, name, name, days),
        )
        return fid

    # A: gone 40 days ago, not due for another 325; older than B's row.
    for k in range(3):
        gone(a_id, a_ds, f"a-{k}", 40)
    due = gone(b_id, b_ds, "b-due", 2)

    async def run() -> int:
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            return await purge_due(conn, datetime.now(UTC), limit=2)

    asyncio.run(run())
    requested = sql.execute(
        "SELECT annex_id, file_id FROM catalog.annex_file WHERE annex_id = ANY(%s)"
        " AND purge_requested_at IS NOT NULL",
        ([a_id, b_id],),
    ).fetchall()
    assert requested == [(b_id, due)]


def test_the_apps_write_only_the_annex_columns_they_need(db_url) -> None:
    with psycopg.connect(as_app(db_url, "web")) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE catalog.filer SET host = host WHERE false")
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE catalog.annex SET owners_sid = owners_sid WHERE false")
    with psycopg.connect(as_app(db_url, "api")) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE catalog.annex_file SET purge_requested_at = now() WHERE false")


# --- A2.1: rules, dry runs, would_remove ----------------------------------------------------


def test_the_edge_answers_dry_runs_once_and_only_its_own(annex, enrol_edge, sql) -> None:
    w, annex_id, ds, _ = annex
    (run_id,) = sql.execute(
        "INSERT INTO catalog.annex_dry_run (annex_id, body, requested_by)"
        " VALUES (%s, %s, 'user:1') RETURNING run_id",
        (annex_id, psycopg.types.json.Jsonb(copy_template(ds))),
    ).fetchone()
    r = w.edge.get(f"{E}/annex-dry-runs", {"annex_id": str(annex_id)})
    assert r.status_code == 200, r.text
    [run] = r.json()["dry_runs"]
    assert run["run_id"] == str(run_id) and run["body"]["rules"][0]["trigger"] == "arrived"
    # Another edge sees none of it, and can't answer it.
    other = enrol_edge()
    assert other.get(f"{E}/annex-dry-runs").json()["dry_runs"] == []
    r = other.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "done", "report": {}},
                   run_id=run_id)  # fmt: skip
    assert r.status_code == 404
    report = {"files": 3, "rules": [{"id": "copy", "files": 3}]}
    # Taken first, once: a second taker is refused.
    r = w.edge.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "running"}, run_id=run_id)
    assert r.status_code == 200 and r.json()["state"] == "running", r.text
    r = w.edge.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "running"}, run_id=run_id,
                    key=f"annex-dry-run-again-{uuid.uuid4().hex}")  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"
    assert w.edge.get(f"{E}/annex-dry-runs").json()["dry_runs"] == [], "taken: not handed out"
    r = w.edge.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "done", "report": report},
                    run_id=run_id)  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "done", r.text
    # Answered once: a late "failed" doesn't overwrite the report.
    r = w.edge.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "failed", "error": "late"},
                    run_id=run_id, key=f"annex-dry-run-late-{uuid.uuid4().hex}")  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "done"
    state, stored = sql.execute(
        "SELECT state, report FROM catalog.annex_dry_run WHERE run_id = %s", (run_id,)
    ).fetchone()
    assert state == "done" and stored == report
    assert w.edge.get(f"{E}/annex-dry-runs").json()["dry_runs"] == []


def test_would_remove_is_recorded_with_whether_the_copy_is_verified(annex, facade_client, sql):
    w, annex_id, ds, _ = annex
    [res] = events(w, annex_id, [arrived("old.segy", DATA, ino="81", rule="copy")])
    fid = res["file_id"]
    assert sql.execute("SELECT rule FROM catalog.annex_file WHERE file_id = %s",
                       (fid,)).fetchone()[0] == "copy"  # fmt: skip
    vid = copy(w, facade_client, fid, DATA)
    events(w, annex_id, [{"kind": "copied", "path": "old.segy", "file_id": fid,
                          "version_id": vid, "size": len(DATA),
                          "mtime_ns": "1700000000000000000",
                          "ctime_ns": "1700000000000000005"}])  # fmt: skip
    ev = {"kind": "would_remove", "path": "old.segy", "file_id": fid, "version_id": vid,
          "rule": "archive", "reason": "removals_not_enabled"}  # fmt: skip
    [r] = events(w, annex_id, [ev])
    assert r["status"] == "applied", r
    d = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'would_remove'",
        (annex_id,),
    ).fetchone()[0]
    assert d["rule"] == "archive" and d["current"] is True
    # Nothing on the NAS or in the catalog moves: the file is as it was.
    assert sql.execute("SELECT nas_state, gone_since FROM catalog.annex_file WHERE file_id = %s",
                       (fid,)).fetchone() == ("present", None)  # fmt: skip
    [r] = events(w, annex_id, [{**ev, "file_id": "999999999"}])
    assert r["status"] == "rejected" and r["code"] == "not_found"


def test_a_rule_set_problem_for_the_whole_annex_is_recorded(annex, sql) -> None:
    w, annex_id, *_ = annex
    [r] = events(w, annex_id, [{"kind": "skipped", "path": ".", "reason": "rules_unreadable",
                                "detail": "trigger 'x'"}])  # fmt: skip
    assert r["status"] == "applied", r
    path = sql.execute(
        "SELECT path FROM catalog.annex_event WHERE annex_id = %s AND kind = 'skipped'",
        (annex_id,),
    ).fetchone()[0]
    assert path is None


def test_a_dry_run_left_running_expires_and_the_annexes_carry_the_services_clock(
    annex, sql
) -> None:
    w, annex_id, ds, _ = annex
    (run_id,) = sql.execute(
        "INSERT INTO catalog.annex_dry_run (annex_id, body, requested_by, state, taken_at)"
        " VALUES (%s, %s, 'user:1', 'running', now() - interval '16 minutes') RETURNING run_id",
        (annex_id, psycopg.types.json.Jsonb(copy_template(ds))),
    ).fetchone()
    w.edge.get(f"{E}/annex-dry-runs")
    state, error = sql.execute(
        "SELECT state, error FROM catalog.annex_dry_run WHERE run_id = %s", (run_id,)
    ).fetchone()
    assert state == "failed" and "in time" in error
    r = w.edge.post(f"{E}/annex-dry-runs/{{run_id}}", {"state": "running"}, run_id=run_id)
    assert r.status_code == 409
    served = [x.get("served_at") for x in w.edge.get(f"{E}/annexes").json()["annexes"]]
    assert served and all(s and s.endswith("Z") for s in served)


def _stop(sql, annex_id, fid, *, ack: bool = False) -> None:
    """As an ArchiveAdmin's stop (and acknowledgement) would (web/routes_annex.py)."""
    sql.execute(
        "UPDATE catalog.annex_file SET copy_stopped_at = now(), copy_stopped_by = 'user:admin',"
        " copy_refused_ack_at = CASE WHEN %s THEN now() END,"
        " copy_refused_ack_by = CASE WHEN %s THEN 'user:admin' END"
        " WHERE annex_id = %s AND file_id = %s",
        (ack, ack, annex_id, int(fid)),
    )


def _stopped(sql, fid):
    return sql.execute(
        "SELECT copy_stopped_at IS NOT NULL, copy_refused_ack_at IS NOT NULL"
        "  FROM catalog.annex_file WHERE file_id = %s",
        (int(fid),),
    ).fetchone()


def test_a_stopped_copy_isnt_asked_for_and_its_records_say_so(annex, facade_client, sql) -> None:
    """#81's follow-up: the edge copied an always-refused file again for good."""
    w, annex_id, ds, _ = annex
    data = b"bytes Azure won't take"
    [r] = events(w, annex_id, [arrived("stop.bin", data)])
    fid = r["file_id"]
    _refuse(w, facade_client, sql, annex_id, "stop.bin", fid, data)
    rec = _records(w, annex_id)["stop.bin"]
    assert (rec["refused"], rec["copy_stopped"]) == (True, False)
    _stop(sql, annex_id, fid)
    rec = _records(w, annex_id)["stop.bin"]
    assert (rec["refused"], rec["copy_stopped"]) == (False, True)
    # The next refusal (a changed file's copy, say) asks for nothing.
    sql.execute("UPDATE catalog.annex SET reconcile_asked_at = NULL WHERE annex_id = %s",
                (annex_id,))  # fmt: skip
    _refuse(w, facade_client, sql, annex_id, "stop.bin", fid, data)
    assert _af(sql, fid)[0] == 2
    assert _ask_in(sql, annex_id) is None, "not asked"
    (d,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE file_id = %s AND kind = 'problem'"
        " ORDER BY event_id DESC LIMIT 1",
        (int(fid),),
    ).fetchone()
    assert "stopped by an ArchiveAdmin" in d["detail"]


def test_an_acknowledgement_takes_only_that_file_out_of_the_gauge(
    annex, facade_client, sql
) -> None:
    from omnifile_service.annex.readback import AT_BOUND_SQL, MAX_REFUSALS

    w, annex_id, ds, _ = annex
    res = events(w, annex_id, [arrived("one.bin", b"one refused", ino="31"),
                               arrived("two.bin", b"two refused", ino="32")])  # fmt: skip
    one, two = res[0]["file_id"], res[1]["file_id"]
    sql.execute("UPDATE catalog.annex_file SET copy_refusals = %s WHERE annex_id = %s",
                (MAX_REFUSALS, annex_id))  # fmt: skip
    (name,) = sql.execute(
        "SELECT name FROM catalog.annex WHERE annex_id = %s", (annex_id,)
    ).fetchone()

    def at_bound():
        return dict(sql.execute(AT_BOUND_SQL, (MAX_REFUSALS,)).fetchall()).get(name)

    assert at_bound() == 2
    sql.execute(
        "UPDATE catalog.annex_file SET copy_refused_ack_at = now(), copy_refused_ack_by = 'u'"
        " WHERE file_id = %s",
        (int(one),),
    )
    assert at_bound() == 1, "the other still alerts"
    assert _stopped(sql, two) == (False, False)


def test_a_verified_version_ends_the_stop_and_the_acknowledgement(
    annex, facade_client, sql
) -> None:
    w, annex_id, ds, _ = annex
    data = b"refused until the backend is fixed"
    [r] = events(w, annex_id, [arrived("fixed.bin", data)])
    fid = r["file_id"]
    _refuse(w, facade_client, sql, annex_id, "fixed.bin", fid, data)
    _stop(sql, annex_id, fid, ack=True)
    assert _stopped(sql, fid) == (True, True)
    # The backend fixed: a copy that verifies.
    vid = copy(w, facade_client, fid, data)
    _copied_event(w, annex_id, "fixed.bin", fid, vid, data)
    assert _stopped(sql, fid) == (True, True), "copied, not yet read back"
    assert w.verify()
    assert _stopped(sql, fid) == (False, False)
    assert _records(w, annex_id)["fixed.bin"]["copy_stopped"] is False


def test_a_copy_already_verified_when_copied_ends_the_stop_too(annex, facade_client, sql) -> None:
    """A dedupe onto a verified copy: no read-back, so no trigger."""
    w, annex_id, ds, _ = annex
    good, bad = b"bytes another file has, verified", b"bytes Azure won't agree with...."
    res = events(w, annex_id, [arrived("o.bin", good, ino="41"),
                               arrived("d.bin", good, ino="42")])  # fmt: skip
    of, df = res[0]["file_id"], res[1]["file_id"]
    ov = copy(w, facade_client, of, good)
    _copied_event(w, annex_id, "o.bin", of, ov, good)
    assert w.verify()
    _refuse(w, facade_client, sql, annex_id, "d.bin", df, bad)
    _stop(sql, annex_id, df, ack=True)
    up = w.edge.post(f"{E}/uploads", {"op_id": f"annex-{uuid.uuid4().hex}",
                                      "file": {"file_id": df}, "size": len(good),
                                      "mtime_ns": "1700000000000000000", "block_size": 4096,
                                      **hashes_of(good)})  # fmt: skip
    assert up.status_code == 201 and up.json()["action"] == "dedupe", up.text
    [r] = _copied_event(w, annex_id, "d.bin", df, up.json()["version_id"], good)
    assert r["verified"] is True
    assert _stopped(sql, df) == (False, False)


def _gauge(sql, annex_id) -> int:
    from omnifile_service.annex.readback import AT_BOUND_SQL, MAX_REFUSALS

    (name,) = sql.execute(
        "SELECT name FROM catalog.annex WHERE annex_id = %s", (annex_id,)
    ).fetchone()
    return dict(sql.execute(AT_BOUND_SQL, (MAX_REFUSALS,)).fetchall()).get(name, 0)


def _ack(sql, fid) -> None:
    sql.execute(
        "UPDATE catalog.annex_file SET copy_refused_ack_at = now(), copy_refused_ack_by = 'u'"
        " WHERE file_id = %s",
        (int(fid),),
    )


def test_a_file_stopped_below_the_bound_keeps_the_alert_up_until_acknowledged(
    annex, facade_client, sql
) -> None:
    """Review of #93: stopped at its first refusal, it is never asked for again, so its
    count never reaches the bound, and a file with no verified copy alerted nobody."""
    w, annex_id, ds, _ = annex
    data = b"stopped at its first refusal"
    [r] = events(w, annex_id, [arrived("early-stop.bin", data)])
    fid = r["file_id"]
    _refuse(w, facade_client, sql, annex_id, "early-stop.bin", fid, data)
    assert _af(sql, fid)[0] == 1
    assert _gauge(sql, annex_id) == 0, "below the bound, and still asked for"
    _stop(sql, annex_id, fid)
    assert _gauge(sql, annex_id) == 1, "stopped: nothing else would ever say so"
    _ack(sql, fid)
    assert _gauge(sql, annex_id) == 0, "two deliberate acts"


def test_a_new_refusal_ends_an_acknowledgement_unless_copying_is_stopped(
    annex, facade_client, sql
) -> None:
    """Review of #93: each scheduled copy of an acknowledged file could be refused again
    with the alert quiet for good."""
    from omnifile_service.annex.readback import MAX_REFUSALS

    w, annex_id, ds, _ = annex
    one, two = b"acknowledged, still copied", b"acknowledged and stopped.."
    res = events(w, annex_id, [arrived("acked.bin", one, ino="51"),
                               arrived("both.bin", two, ino="52")])  # fmt: skip
    acked, both = res[0]["file_id"], res[1]["file_id"]
    for path, fid, data in (("acked.bin", acked, one), ("both.bin", both, two)):
        for _ in range(MAX_REFUSALS):
            _refuse(w, facade_client, sql, annex_id, path, fid, data)
    assert _gauge(sql, annex_id) == 2
    _ack(sql, acked)
    _stop(sql, annex_id, both, ack=True)
    assert _gauge(sql, annex_id) == 0
    # The edge's own reconciliation copies the first again, and it is refused again.
    _refuse(w, facade_client, sql, annex_id, "acked.bin", acked, one)
    assert _stopped(sql, acked) == (False, False), "its acknowledgement ended"
    assert _gauge(sql, annex_id) == 1
    # A stopped file's (a changed file's copy, say) keeps it.
    _refuse(w, facade_client, sql, annex_id, "both.bin", both, two)
    assert _stopped(sql, both) == (True, True)
    assert _gauge(sql, annex_id) == 1
