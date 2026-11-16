"""Cutover of retained and held files (ported from #10, B 0b8dabf; migration
0027).

0003's location guard freezes the role of every copy of a retained or held
version, so the flip (S3 object -> secondary, verified copy -> primary)
failed for a file on legal hold, and for every file of a WORM dataset, and
the whole prefix's cutover with it. A swap destroys nothing, so 0027 allows
exactly a verified swap; everything else the guard refused is still
refused. Cutover also waits until the copy carries the version's retention
and hold. The S3 object stays a retained secondary that OmniFile never
writes or deletes (decision 40).
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sources.support import BUCKET, put

from omnifile_service.backends.base import ObjectRef
from omnifile_service.backends.sim import SimBackend
from omnifile_service.migrate import SimMover
from omnifile_service.retention.common import TAG_HOLD, TAG_WORM
from omnifile_service.retention.worm import set_legal_hold
from omnifile_service.retention.worm_sync import stamp_worm
from omnifile_service.worker.backends import BackendRegistry

from .test_engine import Rig, _with, run

HELD = "retained or on legal hold"
WAITING = "copies still wait for their retention or legal hold"


def _actor(c: psycopg.Connection) -> None:
    c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")


def _file_id(r: Rig, name: str) -> int:
    (fid,) = r.db.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND name = %s"
        "   AND deleted_at IS NULL",
        (r.w.dataset_id, name),
    ).fetchone()
    return fid


def _hold(r: Rig, name: str) -> None:
    fid = _file_id(r, name)
    run(_with(r.connect, lambda c: set_legal_hold(
        c, fid, reason="litigation", actor="system:test", app="prizmanyx")))  # fmt: skip


def _stamp(r: Rig) -> Any:
    return run(_with(r.connect, lambda c: stamp_worm(c, r.registry, r.w.clock.now())))


def _locs(r: Rig, name: str) -> list[dict[str, Any]]:
    cur = r.db.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT l.location_id, l.version_id, l.backend_id, l.container, l.object_key,"
        "       l.backend_version_id, l.role, l.state, l.verified, l.worm_sync_needed"
        "  FROM catalog.file f JOIN catalog.location l ON l.version_id = f.content_version_id"
        " WHERE f.file_id = %s AND l.state = 'committed' ORDER BY l.location_id",
        (_file_id(r, name),),
    )
    return cur.fetchall()


def _src_dest(r: Rig, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    locs = _locs(r, name)
    (src,) = [x for x in locs if x["backend_id"] == r.w.s3_id]
    (dest,) = [x for x in locs if x["backend_id"] == r.w.home_id]
    return src, dest


def _s3_writes(r: Rig) -> int:
    """Mutating calls the S3 fake has seen on the source bucket."""
    return sum(
        1
        for op, params in r.w.fake.calls
        if params.get("Bucket") == BUCKET and not op.startswith(("Get", "Head", "List"))
    )


def test_cutover_flips_a_legally_held_file(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/held.segy", b"h" * 3000))
    run(put(r.w, "m/free.segy", b"f" * 3000))
    r.seed()
    _hold(r, "held.segy")
    mid = r.request()
    assert r.drive(mid, until="verified") == "verified"

    # The hold reaches the verified copy first, then the flip.
    # (The sweep also tags the S3 object today, a write to the source; the
    # source-guard port stops that. Only the flip is measured here.)
    _stamp(r)
    _, dest = _src_dest(r, "held.segy")
    assert not dest["worm_sync_needed"]
    before = _s3_writes(r)
    assert r.drive(mid) == "cut_over"
    assert _s3_writes(r) == before  # the flip writes nothing in S3
    for name in ("held.segy", "free.segy"):
        src, dest = _src_dest(r, name)
        assert (src["role"], dest["role"]) == ("secondary", "primary")
        assert r.primary(name)["location_id"] == dest["location_id"]
    src, dest = _src_dest(r, "held.segy")
    ref = ObjectRef(r.w.home_id, dest["container"], dest["object_key"],
                    dest["backend_version_id"])  # fmt: skip
    assert run(r.home.get_tags(ref))[TAG_HOLD] == "1"
    (hold,) = db.execute(
        "SELECT legal_hold FROM catalog.file_version WHERE version_id = %s",
        (dest["version_id"],),
    ).fetchone()
    assert hold
    assert r.dataset_state() == "cut_over"


def test_cutover_waits_for_the_copys_hold(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/h.segy", b"h" * 100))
    r.seed()
    _hold(r, "h.segy")
    mid = r.request()
    assert r.drive(mid, until="verified") == "verified"
    _, dest = _src_dest(r, "h.segy")
    assert dest["worm_sync_needed"]
    # Not stamped yet: nothing flips, and the prefix says why.
    assert r.step(mid) == "verified"
    m = r.mig(mid)
    assert m["last_error"] == f"1 {WAITING}"
    src, dest = _src_dest(r, "h.segy")
    assert (src["role"], dest["role"]) == ("primary", "secondary")
    _stamp(r)
    assert r.drive(mid) == "cut_over"
    assert r.mig(mid)["last_error"] is None
    src, dest = _src_dest(r, "h.segy")
    assert (src["role"], dest["role"]) == ("secondary", "primary")


def _worm_rig(db, connect) -> Rig:  # type: ignore[no-untyped-def]
    """A WORM dataset (decision 31): one LRS account with versioning and
    version-level WORM (`azure_worm`), and a locked WORM share over it
    before seeding, so every seeded version is retained."""
    r = Rig(db, connect)
    w = r.w
    # Test setup: the home backend is repointed before any file exists. The catalog's
    # backend_guard (0035) refuses moving a backend a dataset uses, so it is set aside here.
    db.execute("SET session_replication_role = replica")
    db.execute(
        "UPDATE catalog.backend SET config = %s WHERE backend_id = %s",
        (Jsonb({"layout": "azure_worm", "container": "worm/data"}),
         w.home_id),
    )  # fmt: skip
    db.execute("RESET session_replication_role")
    db.execute(
        "UPDATE catalog.dataset SET storage_layout = 'single_account' WHERE dataset_id = %s",
        (w.dataset_id,),
    )
    now = w.clock.now()
    with db.transaction():
        _actor(db)
        db.execute(
            "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, owners_group_sid,"
            " worm_enabled, worm_mode, worm_retention_days, worm_enabled_at, worm_locked_at)"
            " SELECT dataset_id, edge_id, %s, '{smb}', 'S-1-5-21-1-2-3-512', true, 'locked', 3650,"
            "        %s, %s FROM catalog.dataset WHERE dataset_id = %s",
            (f"worm-{uuid.uuid4().hex[:8]}", now, now, w.dataset_id),
        )
    r.home = SimBackend.azure_worm(w.clock, backend_id=w.home_id)
    r.registry = BackendRegistry.from_mapping({w.s3_id: w.s3, w.home_id: r.home})
    r.w.registry = r.registry
    r.mover = SimMover(r.registry)
    return r


def test_cutover_of_a_worm_dataset(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = _worm_rig(db, connect)
    names = [f"w{i}.segy" for i in range(3)]
    for i, n in enumerate(names):
        run(put(r.w, f"m/{n}", bytes([70 + i]) * 2000))
    r.seed()
    (n_retained,) = db.execute(
        "SELECT count(*) FROM catalog.file_version WHERE dataset_id = %s"
        "   AND retention_until > now()",
        (r.w.dataset_id,),
    ).fetchone()
    assert n_retained == 3
    mid = r.request()
    assert r.mig(mid)["to_container"] == "worm/data"
    assert r.drive(mid, until="verified") == "verified"
    _stamp(r)
    assert all(not _src_dest(r, n)[1]["worm_sync_needed"] for n in names)
    before = _s3_writes(r)
    assert r.drive(mid) == "cut_over"
    assert _s3_writes(r) == before
    now = r.w.clock.now()
    for n in names:
        src, dest = _src_dest(r, n)
        assert (src["role"], dest["role"]) == ("secondary", "primary")
        assert dest["container"] == "worm/data"
        ref = ObjectRef(r.w.home_id, dest["container"], dest["object_key"],
                        dest["backend_version_id"])  # fmt: skip
        head = run(r.home.head(ref))
        assert head.retention_until is not None and head.retention_until > now
        tags = run(r.home.get_tags(ref))
        assert tags[TAG_WORM] == "locked" and TAG_HOLD in tags
    assert r.dataset_state() == "cut_over"


def test_the_guard_still_protects_held_copies(db, connect, retention_db_url) -> None:  # type: ignore[no-untyped-def]
    """Only a verified primary/secondary swap is new: deletes, marking a
    copy deleting or deleted, relabels, swaps that change what names the
    bytes, promoting an unverified copy, leaving a held version without a
    primary, and shortening retention all stay refused."""
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"a" * 500))
    r.seed()
    mid = r.request()
    assert r.drive(mid, until="verified") == "verified"
    _hold(r, "a.segy")
    src, dest = _src_dest(r, "a.segy")
    with db.transaction():
        _actor(db)
        db.execute(
            "UPDATE catalog.file_version SET retention_until = now() + interval '3650 days'"
            " WHERE version_id = %s",
            (src["version_id"],),
        )
    locs = _locs(r, "a.segy")

    def refused(*stmts: tuple[str, tuple], match: str = HELD) -> None:
        with psycopg.connect(retention_db_url) as c:  # one transaction, rolled back
            _actor(c)
            with pytest.raises(psycopg.Error, match=match):
                for sql, params in stmts:
                    c.execute(sql, params)
                c.commit()

    upd = "UPDATE catalog.location SET {} WHERE location_id = %s"
    for loc in (src, dest):
        lid = (loc["location_id"],)
        refused(("DELETE FROM catalog.location WHERE location_id = %s", lid))
        refused((upd.format("state = 'deleting'"), lid))
        refused((upd.format("state = 'deleted'"), lid))
        refused((upd.format("role = 'staging'"), lid))
        refused((upd.format("role = 'recall_copy'"), lid))
    # A demotion that also moves what names the bytes.
    for change in ("object_key = object_key || '.x'", "container = container || 'x'",
                   "backend_version_id = 'other'"):  # fmt: skip
        refused((upd.format(f"role = 'secondary', {change}"), (src["location_id"],)))
    refused(
        (upd.format("role = 'secondary'"), (src["location_id"],)),
        (upd.format("role = 'primary', object_key = object_key || '.x'"),
         (dest["location_id"],)),
    )  # fmt: skip
    # Promoting an unverified copy.
    refused(
        (upd.format("role = 'secondary'"), (src["location_id"],)),
        (upd.format("role = 'primary', verified = false, verified_at = NULL,"
                    " verified_by = NULL"), (dest["location_id"],)),
    )  # fmt: skip
    # Demoted with nobody promoted: refused at commit.
    refused((upd.format("role = 'secondary'"), (src["location_id"],)),
            match="keeps a committed primary")  # fmt: skip
    # Retention can't be shortened.
    refused(
        ("UPDATE catalog.file_version SET retention_until = retention_until - interval '1 day'"
         " WHERE version_id = %s", (src["version_id"],)),
        match="never shortened",
    )  # fmt: skip
    assert _locs(r, "a.segy") == locs  # nothing changed

    # The swap itself is allowed, towards a verified copy only: the S3 object
    # (unverified here: its CRC came from a HEAD) can't be made primary again.
    with psycopg.connect(retention_db_url) as c:
        _actor(c)
        c.execute(upd.format("role = 'secondary'"), (src["location_id"],))
        c.execute(upd.format("role = 'primary'"), (dest["location_id"],))
    refused(
        (upd.format("role = 'secondary'"), (dest["location_id"],)),
        (upd.format("role = 'primary'"), (src["location_id"],)),
    )
