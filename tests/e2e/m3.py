"""Helpers for the M3 exit test (tests/e2e/m3_exit_test.sh).

Runs on the edge machine with the service's Python environment, like
e2e.py (whose helpers it reuses). Every subcommand prints one JSON object.

  m3.py seed --enrol-url URL --server-ca PEM --bundle-out FILE --owners-sid S ...
        two sim_http backends (two-account :10000, WORM :10001), the edge and
        its bundle, and one dataset + share + M3 policy each: wait, failfast
        (NFS), smbwait, smbfail (SMB), smart (Smart + archive day), worm
        (single account, WORM); a second, never-enrolled edge with a
        "budget" dataset for the High-priority budget check
  m3.py clock                         the shared virtual clock: sim, worker file, Postgres
  m3.py advance SECONDS               POST /_sim/advance (moves sim, api, worker, Postgres)
  m3.py tier SHARE NAME               the file's locations (catalog) + sim's view of each
  m3.py recall SHARE NAME             the latest recall, its restore and requesters
  m3.py pin SHARE NAME TIER HOURS [--override-archive]
                                      a tier pin (decision 29), set by the admin identity
  m3.py move SHARE NAME TIER          queue a tiering move by hand (the Smart one-way check)
  m3.py audit ACTION [FILE_ID]        audit rows of that action
  m3.py worm-enable SHARE DAYS WINDOW_MIN
  m3.py worm SHARE                    the share's WORM state
  m3.py rewind SHARE DAYS             rewind depth (max and depth)
  m3.py versions SHARE NAME           every version: state, retention, its blobs in sim
  m3.py delete-old SHARE NAME         try to destroy the oldest superseded version
                                      (backend delete, catalog delete, location delete)
  m3.py budget-seed SIZES_GIB...      synthetic archived files on the budget dataset,
                                      and one High-priority recall each (in one go)
  m3.py budget                        those recalls: state, priority, restore, ETA
  m3.py lag                           change-feed lag per container and its samples
  m3.py dlq                           dead-lettered worker messages, per queue
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e  # noqa: E402
import httpx  # noqa: E402
import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

VAR = os.environ.get("E2E_VAR", "/var/lib/omnifile-dev")
SIM = os.environ.get("E2E_SIM_URL", "http://127.0.0.1:10000")
SIM_WORM = os.environ.get("E2E_SIM_WORM_URL", "http://127.0.0.1:10001")
KEY = f"{VAR}/sim-key.json"
KEY_WORM = f"{VAR}/sim-worm-key.json"
CLOCK_FILE = os.environ.get("OMNIFILE_DEV_CLOCK_FILE", f"{VAR}/clock-offset")
ADMIN_ACTOR = "e2e:archive-admin"
GiB = 1 << 30

SCHEDULED = {"mode": "scheduled", "clock": "written", "warm_day": 1, "cold_day": 2,
             "archive_day": 3, "stub_at": "archive"}  # fmt: skip
SMART = {"mode": "smart", "clock": "written", "archive_day": 3, "stub_at": "archive"}
WORM = {"mode": "scheduled", "clock": "written", "warm_day": 1, "archive_day": 3,
        "stub_at": "archive"}  # fmt: skip

out = e2e.out
db = e2e.db


def clock():
    from omnifile_service.backends.sim import OffsetClock

    return OffsetClock(CLOCK_FILE)


def backend(backend_id: int, config: dict):
    from omnifile_service.worker.backends import SimHttpBackend

    return SimHttpBackend.from_config(backend_id, config, clock=clock())


def backend_of(c, backend_id: int):
    cur = c.cursor(row_factory=psycopg.rows.tuple_row)
    (config,) = cur.execute(
        "SELECT config FROM catalog.backend WHERE backend_id = %s", (backend_id,)
    ).fetchone()
    return backend(backend_id, config)


def file_row(share: str, name: str) -> dict:
    row = e2e.file_row(share, name)
    if not row:
        raise SystemExit(f"no file {share}/{name}")
    return row


def admin_identity(c) -> int:
    (iid,) = c.execute(
        "SELECT identity_id FROM catalog.identity WHERE sid = %s", (ADMIN_SID,)
    ).fetchone()
    return iid


ADMIN_SID = "S-1-5-21-1000-2000-3000-1104"


# --- seed ---------------------------------------------------------------------------


def seed(a) -> None:
    from omnifile_service.enrol.__main__ import add_edge

    url = os.environ["OMNIFILE_DATABASE_URL"]
    with db() as c:
        c.execute("SELECT set_config('omnifile.actor', 'e2e:seed', false)")
        backends = {}
        for name, url_, key, layout, container in (
            ("e2e-sim", SIM, KEY, "azure_two_account", "live/data"),
            ("e2e-sim-worm", SIM_WORM, KEY_WORM, "azure_worm", "worm/data"),
        ):
            config = {"data_plane": "sim_http", "url": url_, "key_file": key,
                      "container": container, "layout": layout}  # fmt: skip
            (bid,) = c.execute(
                "INSERT INTO catalog.backend (name, kind, config, private_network)"
                " VALUES (%s, 'sim', %s, false) RETURNING backend_id",
                (name, Jsonb(config)),
            ).fetchone()
            backends[layout] = bid
        (iid,) = c.execute(
            "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id", (ADMIN_SID,)
        ).fetchone()
    # Provisioning, as the setup guide has the customer do on Azure: the WORM
    # share's container, with version-level immutability, before WORM is enabled.
    worm_backend = backend(
        backends["azure_worm"],
        {"data_plane": "sim_http", "url": SIM_WORM, "key_file": KEY_WORM, "layout": "azure_worm"},
    )

    async def provision():
        try:
            await worm_backend.create_container("worm/data", worm=True)
        finally:
            await worm_backend.close()

    asyncio.run(provision())
    bundle = add_edge(url, "edge1", a.enrol_url, Path(a.server_ca).read_bytes())
    Path(a.bundle_out).write_text(bundle + "\n")
    os.chmod(a.bundle_out, 0o600)
    shares = {}
    with db() as c:
        c.execute("SELECT set_config('omnifile.actor', 'e2e:seed', false)")
        (edge_id,) = c.execute("SELECT edge_id FROM catalog.edge WHERE name = 'edge1'").fetchone()
        two, worm = backends["azure_two_account"], backends["azure_worm"]
        nfs = (["nfs3", "nfs4"], ["sys"], 0o755, a.uid)
        smb = (["smb"], [], 0o770, 20005)
        specs = [
            ("wait", "wait", *nfs, two, "two_account", SCHEDULED),
            ("failfast", "fail", *nfs, two, "two_account", SCHEDULED),
            ("smbwait", "wait", *smb, two, "two_account", SCHEDULED),
            ("smbfail", "fail", *smb, two, "two_account", SCHEDULED),
            ("smart", "wait", *nfs, two, "two_account", SMART),
            ("worm", "wait", *nfs, worm, "single_account", WORM),
        ]
        for name, on_open, protocols, nfs_sec, mode, uid, bid, layout, policy in specs:
            (ds,) = c.execute(
                "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
                " VALUES (%s, %s, %s, %s) RETURNING dataset_id",
                (f"e2e-{name}", bid, edge_id, layout),
            ).fetchone()
            (root,) = c.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, uid, gid, mode)"
                " VALUES (%s, NULL, '', 'dir', %s, %s, %s) RETURNING file_id",
                (ds, uid, a.gid, mode),
            ).fetchone()
            clients = ["127.0.0.1"] + list(a.nfs_client or []) if "sys" in nfs_sec else []
            (share,) = c.execute(
                "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols,"
                " nfs_sec, nfs_clients, root_squash, owners_group_sid, writers_group_sid,"
                " readers_group_sid, on_open)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s) RETURNING share_id",
                (ds, edge_id, name, root, protocols, nfs_sec, clients, a.owners_sid,
                 a.writers_sid, a.readers_sid, on_open),
            ).fetchone()  # fmt: skip
            c.execute(
                "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
                " VALUES (%s, 1, %s, %s)",
                (ds, Jsonb(policy), iid),
            )
            shares[name] = {"dataset_id": ds, "root_dir_id": root, "share_id": share,
                            "policy": policy}  # fmt: skip
        # The High-priority budget runs on files no edge holds: a second edge,
        # never enrolled, so its synthetic multi-GiB files reach no edge.
        (other,) = c.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES ('edge-budget', %s, now() + interval '1 day') RETURNING edge_id",
            (uuid.uuid4().bytes * 2,),
        ).fetchone()
        (ds,) = c.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES ('e2e-budget', %s, %s)"
            " RETURNING dataset_id",
            (two, other),
        ).fetchone()
        (root,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
            " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
            (ds,),
        ).fetchone()
        shares["budget"] = {"dataset_id": ds, "root_dir_id": root, "edge_id": other}
    out({"backends": backends, "edge_id": edge_id, "admin_identity_id": iid, "shares": shares})


# --- clock --------------------------------------------------------------------------


def clock_cmd(_a) -> None:
    sim = httpx.get(f"{SIM}/_sim/clock", trust_env=False).json()["now"]
    worm = httpx.get(f"{SIM_WORM}/_sim/clock", trust_env=False).json()["now"]
    with db() as c:
        (dbnow,) = c.execute("SELECT now()").fetchone()
    wall = datetime.now(UTC)
    out({"sim": sim, "sim_worm": worm, "db": dbnow.isoformat(), "wall": wall.isoformat(),
         "offset_s": clock().offset().total_seconds(),
         "db_offset_s": round((dbnow - wall).total_seconds(), 1)})  # fmt: skip


def advance(a) -> None:
    r = httpx.post(f"{SIM}/_sim/advance", json={"seconds": a.seconds}, trust_env=False)
    r.raise_for_status()
    # The WORM facade shares the clock file; asking it rotates its key if it lapsed.
    httpx.post(f"{SIM_WORM}/_sim/advance", json={"seconds": 0}, trust_env=False)
    out({"now": r.json()["now"], "offset_s": clock().offset().total_seconds()})


# --- tiering ------------------------------------------------------------------------


def tier(a) -> None:
    row = file_row(a.share, a.name)
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        locs = c.execute(
            "SELECT l.location_id, l.version_id, l.backend_id, l.container, l.object_key,"
            " l.backend_version_id, l.role, l.state, l.logical_tier, l.storage_class,"
            " l.explicit_tier_set, l.verified, l.verified_by, f.next_action, f.next_action_at,"
            " f.archive_at, f.state AS file_state, f.stub_version_id,"
            " (l.location_id = f.primary_location_id) AS is_primary"
            " FROM catalog.location l JOIN catalog.file f ON f.file_id = l.file_id"
            " WHERE l.file_id = %s ORDER BY l.location_id",
            (row["file_id"],),
        ).fetchall()
        # What the scheduler did and why: the file's last pin, its tier moves
        # (audit), and when it plans to look again (UTC).
        pin = c.execute(
            "SELECT pin_id, logical_tier, until, override_archive, released_at"
            " FROM catalog.tier_pin WHERE file_id = %s ORDER BY pin_id DESC LIMIT 1",
            (row["file_id"],),
        ).fetchone()
        moves = c.execute(
            "SELECT details->>'from' AS from_tier, details->>'to' AS to_tier, at"
            " FROM catalog.audit_log WHERE action = 'tier.move' AND file_id = %s ORDER BY at",
            (row["file_id"],),
        ).fetchall()
        (next_at,) = (
            c.cursor(row_factory=psycopg.rows.tuple_row)
            .execute(
                "SELECT next_action_at FROM catalog.file WHERE file_id = %s", (row["file_id"],)
            )
            .fetchone()
        )
        backends = {}
        for loc in locs:
            if loc["backend_id"] not in backends:
                backends[loc["backend_id"]] = backend_of(c, loc["backend_id"])

    async def heads():
        from omnifile_service.backends.base import BackendError, ObjectRef

        for loc in locs:
            b = backends[loc["backend_id"]]
            ref = ObjectRef(loc["backend_id"], loc["container"], loc["object_key"],
                            loc["backend_version_id"])  # fmt: skip
            try:
                h = await b.head(ref)
                loc["sim"] = {
                    "native_class": h.native_class,
                    "tier": str(h.logical_tier),
                    "restore_state": str(h.restore_state),
                    "retention_until": h.retention_until,
                    "legal_hold": h.legal_hold,
                }
            except BackendError as e:  # fmt: skip
                loc["sim"] = {"error": type(e).__name__}
        for b in backends.values():
            await b.close()

    asyncio.run(heads())
    primary = next((x for x in locs if x["is_primary"]), None)
    until = pin["until"] if pin else None
    out(
        {
            "file_id": row["file_id"],
            "state": row["state"],
            "primary": primary,
            "locations": locs,
            "pin": pin,
            "moves": [[m["from_tier"], m["to_tier"]] for m in moves],
            "next_action_at": next_at.astimezone(UTC).isoformat() if next_at else None,
            # The scheduler planned its next look for when the pin ends.
            "next_at_is_pin_until": bool(next_at and until and next_at == until),
            "next_at_after_pin_until": bool(next_at and until and next_at > until),
            "now": clock().now().isoformat(),
        }
    )


def recall(a) -> None:
    # `--id`: the recall itself, for a file that has no name any more (deleted).
    row = None if a.id else file_row(a.share, a.name)
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        r = c.execute(
            "SELECT r.recall_id, r.state, r.priority, r.eta, r.requested_at, r.completed_at,"
            " r.failure, r.version_id, r.pinned, r.superseded_reason, r.held_by_jobs,"
            " (SELECT f.deleted_at IS NOT NULL FROM catalog.file f"
            "   WHERE f.file_id = r.file_id) AS file_deleted,"
            " (SELECT v.kind FROM catalog.file_version v"
            "   WHERE v.version_id = r.superseded_by_version_id) AS superseded_by_kind,"
            " s.restore_id, s.speed, s.state AS restore_state, s.mechanism,"
            " t.container AS target_container, t.state AS target_state,"
            " (SELECT count(*) FROM catalog.recall_requester q"
            "   WHERE q.recall_id = r.recall_id) AS requesters"
            " FROM catalog.recall r LEFT JOIN catalog.restore s ON s.restore_id = r.restore_id"
            " LEFT JOIN catalog.location t ON t.location_id = s.target_location_id"
            + (
                " WHERE r.recall_id = %s"
                if a.id
                else " WHERE r.file_id = %s ORDER BY r.recall_id DESC LIMIT 1"
            ),
            (a.id or row["file_id"],),
        ).fetchone()
    out(r or {})


def restore_copy(a) -> None:
    """Restore the file's current version under another name (decision 34), as the
    portal does for a version in Archive: the restore joins the active recall of that
    version and waits on it, and the worker's portal job does the rest."""
    row = file_row(a.share, a.name)
    dot = a.name.rfind(".")
    name = (a.name[:dot] + "-restored" + a.name[dot:]) if dot > 0 else a.name + "-restored"
    now = clock().now()
    with db() as c:
        c.execute("SELECT set_config('omnifile.actor', %s, false)", (ADMIN_ACTOR,))
        (ds, vid) = c.execute(
            "SELECT dataset_id, content_version_id FROM catalog.file WHERE file_id = %s",
            (row["file_id"],),
        ).fetchone()
        (rid,) = c.execute(
            "SELECT recall_id FROM catalog.recall WHERE file_id = %s AND version_id = %s"
            " AND state IN ('requested', 'rehydrating', 'copying', 'downloading') FOR UPDATE",
            (row["file_id"], vid),
        ).fetchone()
        (restore_id,) = c.execute(
            "INSERT INTO catalog.version_restore (dataset_id, file_id, version_id, target, name,"
            " state, recall_id, requested_by_identity_id, requested_at, updated_at)"
            " VALUES (%s, %s, %s, 'restored', %s, 'waiting_recall', %s, %s, %s, %s)"
            " RETURNING restore_id",
            (ds, row["file_id"], vid, name, rid, admin_identity(c), now, now),
        ).fetchone()
        c.execute(
            "SELECT pgmq.send('portal', %s)",
            (Jsonb({"kind": "version_restore", "restore_id": str(restore_id)}),),
        )
    out({"restore_id": restore_id, "recall_id": rid, "version_id": vid, "name": name})


def restore_state(a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        r = c.execute(
            "SELECT v.restore_id, v.state, v.failure, v.name, v.recall_id, v.result_file_id,"
            " (SELECT f.name FROM catalog.file f WHERE f.file_id = v.result_file_id)"
            "   AS result_name"
            " FROM catalog.version_restore v WHERE v.restore_id = %s",
            (a.restore_id,),
        ).fetchone()
    out(r or {})


def readmix(a) -> None:
    """What a reader got of a file replaced while it read: per 64 KiB block, the new
    file's, the old file's, zeros, a mix of the new file's bytes and zeros, or something
    else; and where the old file's end. Zeros and a short read are what a reader gets of
    any file that another NFS client is still writing (docs/milestones/results/wp24d.md);
    `other` is bytes that are in neither file."""
    got, new, old = (Path(p).read_bytes() for p in (a.got, a.new, a.old))
    block = 65536
    n = {"new": 0, "old": 0, "zeros": 0, "mixed": 0, "other": 0}
    old_end = 0
    for i in range(0, len(got), block):
        g = got[i : i + block]
        if g == new[i : i + block]:
            n["new"] += 1
        elif g == old[i : i + block]:
            n["old"] += 1
            old_end = i + len(g)
        elif not any(g):
            n["zeros"] += 1
        elif all(x == 0 or x == y for x, y in zip(g, new[i : i + block], strict=False)):
            n["mixed"] += 1
        else:
            n["other"] += 1
    out({"bytes": len(got), "new_bytes": len(new), "old_end": old_end, **n})


def pin(a) -> None:
    row = file_row(a.share, a.name)
    until = clock().now() + timedelta(hours=a.hours) if a.hours > 0 else None
    with db() as c:
        c.execute("SELECT set_config('omnifile.actor', %s, false)", (ADMIN_ACTOR,))
        (ds,) = c.execute(
            "SELECT dataset_id FROM catalog.file WHERE file_id = %s", (row["file_id"],)
        ).fetchone()
        (pid,) = c.execute(
            "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, until, reason,"
            " set_by_identity_id, override_archive) VALUES (%s, %s, %s, %s, 'e2e', %s, %s)"
            " RETURNING pin_id",
            (ds, row["file_id"], a.tier, until, admin_identity(c), a.override_archive),
        ).fetchone()
    out({"pin_id": pid, "file_id": row["file_id"], "tier": a.tier, "until": until,
         "override_archive": a.override_archive})  # fmt: skip


def move(a) -> None:
    row = file_row(a.share, a.name)
    with db() as c:
        (loc,) = c.execute(
            "SELECT primary_location_id FROM catalog.file WHERE file_id = %s", (row["file_id"],)
        ).fetchone()
        c.execute(
            "SELECT pgmq.send('tiering', %s::jsonb)",
            (json.dumps({"kind": "move", "file_id": str(row["file_id"]),
                         "version_id": str(row["content_version_id"]),
                         "location_id": str(loc), "tier": a.tier}),),
        )  # fmt: skip
    out({"queued": True, "file_id": row["file_id"], "tier": a.tier})


def audit(a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        rows = c.execute(
            "SELECT action, decision, file_id, version_id, details, actor FROM catalog.audit_log"
            " WHERE action = %s AND (%s::bigint IS NULL OR file_id = %s::bigint)"
            " ORDER BY 1",
            (a.action, a.file_id, a.file_id),
        ).fetchall()
    out({"n": len(rows), "rows": rows[-5:]})


# --- WORM, versions, depth -----------------------------------------------------------


def _share_id(c, name: str) -> int:
    (sid,) = c.execute("SELECT share_id FROM catalog.share WHERE name = %s", (name,)).fetchone()
    return sid


def worm_enable(a) -> None:
    from omnifile_service.retention.worm import enable_worm

    async def run():
        conn = await psycopg.AsyncConnection.connect(os.environ["OMNIFILE_DATABASE_URL"],
                                                     autocommit=True)  # fmt: skip
        try:
            cur = await conn.execute("SELECT share_id FROM catalog.share WHERE name = %s",
                                     (a.share,))  # fmt: skip
            (sid,) = await cur.fetchone()
            return await enable_worm(conn, sid, retention_days=a.days, confirmation=a.share,
                                     actor=ADMIN_ACTOR, now=clock().now(),
                                     test_window=timedelta(minutes=a.window_min))  # fmt: skip
        finally:
            await conn.close()

    out({"lock_deadline": asyncio.run(run())})


def worm(a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        r = c.execute(
            "SELECT share_id, worm_enabled, worm_mode, worm_retention_days, worm_lock_deadline,"
            " worm_locked_at, rewind_depth_days, rewind_depth_max_days"
            " FROM catalog.share WHERE name = %s",
            (a.share,),
        ).fetchone()
    out(r or {})


def rewind(a) -> None:
    from omnifile_service.retention.depth import set_rewind_depth, set_rewind_max

    async def run():
        conn = await psycopg.AsyncConnection.connect(os.environ["OMNIFILE_DATABASE_URL"],
                                                     autocommit=True)  # fmt: skip
        try:
            cur = await conn.execute("SELECT share_id FROM catalog.share WHERE name = %s",
                                     (a.share,))  # fmt: skip
            (sid,) = await cur.fetchone()
            await set_rewind_max(conn, sid, max(a.days, 1), actor=ADMIN_ACTOR)
            await set_rewind_depth(conn, sid, a.days, actor="e2e:share-owner")
        finally:
            await conn.close()

    asyncio.run(run())
    worm(a)


def _versions(c, file_id: int) -> list[dict]:
    c.row_factory = psycopg.rows.dict_row
    return c.execute(
        "SELECT v.version_id, v.state, v.kind, v.committed_at, v.superseded_at,"
        " v.retention_until, v.legal_hold, l.location_id, l.backend_id, l.container,"
        " l.object_key, l.backend_version_id, l.state AS loc_state, l.role, l.logical_tier,"
        " l.worm_stamped_locked"
        " FROM catalog.file_version v"
        " LEFT JOIN catalog.location l ON l.version_id = v.version_id"
        "   AND l.role IN ('primary', 'secondary')"
        " WHERE v.file_id = %s AND v.kind = 'content' ORDER BY v.version_id, l.location_id",
        (file_id,),
    ).fetchall()


def versions(a) -> None:
    row = file_row(a.share, a.name)
    with db() as c:
        vs = _versions(c, row["file_id"])
        b = backend_of(c, vs[0]["backend_id"]) if vs and vs[0]["backend_id"] else None

    async def heads():
        from omnifile_service.backends.base import BackendError, ObjectRef

        for v in vs:
            if b is None or v["container"] is None:
                continue
            ref = ObjectRef(v["backend_id"], v["container"], v["object_key"],
                            v["backend_version_id"])  # fmt: skip
            try:
                h = await b.head(ref)
                v["sim"] = {
                    "exists": True,
                    "native_class": h.native_class,
                    "retention_until": h.retention_until,
                    "legal_hold": h.legal_hold,
                }
            except BackendError as e:  # fmt: skip
                v["sim"] = {"exists": False, "error": type(e).__name__}
        if b is not None:
            await b.close()

    asyncio.run(heads())
    out({"file_id": row["file_id"], "content_version_id": row["content_version_id"],
         "versions": vs})  # fmt: skip


def delete_old(a) -> None:
    """Try to destroy the oldest superseded content version three ways."""
    row = file_row(a.share, a.name)
    res: dict = {}
    with db() as c:
        vs = [v for v in _versions(c, row["file_id"]) if v["superseded_at"] is not None]
        if not vs:
            out({"error": "no superseded version"})
            return
        v = vs[0]
        res["version_id"] = v["version_id"]
        b = backend_of(c, v["backend_id"])
        c.row_factory = psycopg.rows.tuple_row
        for what, sql, params in (
            ("catalog_delete_version", "DELETE FROM catalog.file_version WHERE version_id = %s",
             (v["version_id"],)),
            ("catalog_prune_version",
             "UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = %s",
             (v["version_id"],)),
            ("catalog_delete_location",
             "UPDATE catalog.location SET state = 'deleting' WHERE location_id = %s",
             (v["location_id"],)),
        ):  # fmt: skip
            try:
                with c.transaction():
                    c.execute(sql, params)
                    raise RuntimeError("rollback")  # never keep it, even if allowed
            except psycopg.Error as e:
                res[what] = {
                    "refused": True,
                    "sqlstate": e.sqlstate,
                    "error": str(e).split("\n")[0],
                }
            except RuntimeError:
                res[what] = {"refused": False}

    async def backend_delete():
        from omnifile_service.backends.base import BackendError, ObjectRef

        ref = ObjectRef(v["backend_id"], v["container"], v["object_key"], v["backend_version_id"])
        try:
            await b.delete(ref)
            res["backend_delete"] = {"refused": False}
        except BackendError as e:
            res["backend_delete"] = {"refused": True, "error": type(e).__name__, "msg": str(e)}
        finally:
            await b.close()

    asyncio.run(backend_delete())
    out(res)


# --- the High-priority budget --------------------------------------------------------


def budget_seed(a) -> None:
    from omnifile_service.backends.base import LogicalTier

    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        ds = c.execute(
            "SELECT d.dataset_id, d.edge_id, d.backend_id, b.config, f.file_id AS root"
            " FROM catalog.dataset d JOIN catalog.backend b ON b.backend_id = d.backend_id"
            " JOIN catalog.file f ON f.dataset_id = d.dataset_id AND f.parent_id IS NULL"
            " WHERE d.name = 'e2e-budget'"
        ).fetchone()
        b = backend(ds["backend_id"], ds["config"])

    async def put():
        refs = []
        for i, gib in enumerate(a.sizes):
            refs.append(await b.put_synthetic("archive/data", f"budget/{i}", int(gib * GiB),
                                              LogicalTier.ARCHIVE, seed=i))  # fmt: skip
        await b.close()
        return refs

    refs = asyncio.run(put())
    now = clock().now()
    made = []
    with db() as c:
        with c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'e2e:budget', true)")
            for i, (gib, ref) in enumerate(zip(a.sizes, refs, strict=True)):
                size = int(gib * GiB)
                name = f"budget-{i}.segy"
                (fid,) = c.execute(
                    "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
                    " resident_bytes, written_at) VALUES (%s, %s, %s, 'file', 'resident', %s, %s,"
                    " %s) RETURNING file_id",
                    (ds["dataset_id"], ds["root"], name, size, size, now),
                ).fetchone()
                (vid,) = c.execute(
                    "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name,"
                    " kind, state, size) VALUES (%s, %s, 1, %s, %s, 'content', 'pending', %s)"
                    " RETURNING version_id",
                    (fid, ds["dataset_id"], ds["root"], name, size),
                ).fetchone()
                c.execute(
                    "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
                    " object_key, backend_version_id, role, state, logical_tier, storage_class,"
                    " explicit_tier_set, size, crc64nvme, verified, verified_at, verified_by,"
                    " committed_at, tier_changed_at) VALUES (%s, %s, %s, %s, %s, %s, 'primary',"
                    " 'committed', 'archive', 'Archive', true, %s, %s, true, %s,"
                    " 'upload_block_crc', %s, %s)",
                    (fid, vid, ds["backend_id"], ref.container, ref.key, ref.version_id, size,
                     b"\0" * 8, now, now, now),
                )  # fmt: skip
                c.execute(
                    "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
                    " WHERE version_id = %s",
                    (now, vid),
                )
                priority = "high" if gib * GiB <= 10 * 10**9 else "standard"
                (rid,) = c.execute(
                    "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority,"
                    " requested_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s)"
                    " RETURNING recall_id",
                    (fid, vid, ds["edge_id"], priority, now + timedelta(microseconds=i), now),
                ).fetchone()
                c.execute(
                    "SELECT pgmq.send('restores', %s::jsonb)",
                    (json.dumps({"recall_id": str(rid)}),),
                )
                made.append({"recall_id": rid, "gib": gib, "priority": priority})
    out({"recalls": made})


def budget(_a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        rows = c.execute(
            "SELECT r.recall_id, r.state, r.priority, r.eta, r.requested_at, r.restore_id,"
            " v.size, s.speed, s.state AS restore_state, s.eta AS restore_eta"
            " FROM catalog.recall r JOIN catalog.file f ON f.file_id = r.file_id"
            " JOIN catalog.dataset d ON d.dataset_id = f.dataset_id"
            " JOIN catalog.file_version v ON v.version_id = r.version_id"
            " LEFT JOIN catalog.restore s ON s.restore_id = r.restore_id"
            " WHERE d.name = 'e2e-budget' ORDER BY r.recall_id"
        ).fetchall()
    out({"now": clock().now(), "recalls": rows})


def lag(_a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        cursors = c.execute(
            "SELECT backend_id, container, caught_up, last_record_at, last_lag_seconds,"
            " last_read_at, records_applied, records_unmatched, last_error"
            " FROM catalog.changefeed_cursor ORDER BY 1, 2"
        ).fetchall()
        samples = c.execute(
            "SELECT backend_id, container, count(*) AS pages, sum(records) AS records,"
            " max(max_lag_seconds) AS max_lag_s,"
            " percentile_cont(0.5) WITHIN GROUP (ORDER BY max_lag_seconds) AS p50_page_max_s,"
            " sum(mean_lag_seconds * records) / nullif(sum(records), 0) AS mean_lag_s"
            " FROM catalog.changefeed_lag GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    out({"cursors": cursors, "samples": samples})


def dlq(_a) -> None:
    """Dead-lettered messages per *_dlq queue (the worker gave up on them)."""
    dead = {}
    with db() as c:
        names = [r[0] for r in c.execute(
            "SELECT queue_name FROM pgmq.list_queues() WHERE queue_name LIKE '%%\\_dlq'"
        ).fetchall()]  # fmt: skip
        for q in names:
            rows = c.execute(f"SELECT message FROM pgmq.q_{q} LIMIT 3").fetchall()  # noqa: S608
            if rows:
                dead[q] = [r[0] for r in rows]
    out({"queues": names, "dead": dead})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="m3.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--enrol-url", required=True)
    s.add_argument("--server-ca", required=True)
    s.add_argument("--bundle-out", required=True)
    s.add_argument("--uid", type=int, default=20002)
    s.add_argument("--gid", type=int, default=10000)
    s.add_argument("--owners-sid", required=True)
    s.add_argument("--writers-sid")
    s.add_argument("--readers-sid")
    s.add_argument("--nfs-client", action="append")
    s.set_defaults(fn=seed)
    sub.add_parser("clock").set_defaults(fn=clock_cmd)
    s = sub.add_parser("advance")
    s.add_argument("seconds", type=float)
    s.set_defaults(fn=advance)
    for name, fn in (("tier", tier), ("recall", recall), ("versions", versions),
                     ("delete-old", delete_old)):  # fmt: skip
        s = sub.add_parser(name)
        s.add_argument("share")
        s.add_argument("name")
        if name == "recall":
            s.add_argument("--id", type=int)
        s.set_defaults(fn=fn)
    s = sub.add_parser("restore-copy")
    s.add_argument("share")
    s.add_argument("name")
    s.set_defaults(fn=restore_copy)
    s = sub.add_parser("restore-state")
    s.add_argument("restore_id", type=int)
    s.set_defaults(fn=restore_state)
    s = sub.add_parser("readmix")
    s.add_argument("got")
    s.add_argument("new")
    s.add_argument("old")
    s.set_defaults(fn=readmix)
    s = sub.add_parser("pin")
    s.add_argument("share")
    s.add_argument("name")
    s.add_argument("tier")
    s.add_argument("hours", type=float)
    s.add_argument("--override-archive", action="store_true")
    s.set_defaults(fn=pin)
    s = sub.add_parser("move")
    s.add_argument("share")
    s.add_argument("name")
    s.add_argument("tier")
    s.set_defaults(fn=move)
    s = sub.add_parser("audit")
    s.add_argument("action")
    s.add_argument("file_id", nargs="?", type=int)
    s.set_defaults(fn=audit)
    s = sub.add_parser("worm-enable")
    s.add_argument("share")
    s.add_argument("days", type=int)
    s.add_argument("window_min", type=int)
    s.set_defaults(fn=worm_enable)
    s = sub.add_parser("worm")
    s.add_argument("share")
    s.set_defaults(fn=worm)
    s = sub.add_parser("rewind")
    s.add_argument("share")
    s.add_argument("days", type=int)
    s.set_defaults(fn=rewind)
    s = sub.add_parser("budget-seed")
    s.add_argument("sizes", type=float, nargs="+")
    s.set_defaults(fn=budget_seed)
    sub.add_parser("budget").set_defaults(fn=budget)
    sub.add_parser("lag").set_defaults(fn=lag)
    sub.add_parser("dlq").set_defaults(fn=dlq)
    a = p.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
