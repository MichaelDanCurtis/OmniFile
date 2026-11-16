"""Helpers for tests/e2e/annex_a1_test.sh (annex.md phase A1, Copy mode).

Run with the service's Python and OMNIFILE_DATABASE_URL set. Every command
prints one JSON object.

  seed --bundle-out F --enrol-url U --server-ca F      backend, edge bundle
  annex --name N --protocol smb|nfs3 --host H --export E --path P [--sec-sys]
                                                       filer, dataset, annex
  copies ANNEX_ID ROOT        every NAS file vs the catalog's verified copy
  versions ANNEX_ID PATH      committed content versions of the live file
  gone ANNEX_ID PATH          the tombstoned file and its kept copies
  drift ANNEX_ID              reconciliation drift rows
  events ANNEX_ID [KIND]      annex event counts by kind
  tree ROOT                   a digest of the tree: names, sizes, bytes,
                              mtimes, atimes, modes and owners
  go-live ANNEX_ID            phase A2.2: files under rm/ move to Azure
                              (approved by an ArchiveAdmin, out of its dry
                              run, confirmed), pointer files on
  pause ANNEX_ID on|off       the annex's removals switch
  removals ANNEX_ID           each removal (state, reason, pointer) and the
                              removal events, by path
  snapshot ROOT FILE          every file under ROOT and its SHA-256, to FILE
  diff BEFORE AFTER           added, removed and changed files between two
  returns ANNEX_ID            phase A3.3: return rules running: rm/a.bin back
                              where it came from (held 14 days), the rest to
                              elsewhere/restored on the filer
  place ANNEX_ID PATH         a placement of the removed file at PATH, ready,
                              for a person who may read it (as POST
                              /v1/placements makes one)
  placements ANNEX_ID         each placement by path: state, reason, outcome,
                              where it landed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import uuid
from pathlib import Path

import psycopg
import psycopg.types.json
from psycopg.types.json import Jsonb

SIM_URL = os.environ.get("E2E_SIM_URL", "http://127.0.0.1:55471")
SIM_KEY = os.environ.get("E2E_SIM_KEY", "")
OWNERS_SID = "S-1-5-21-1000-2000-3000-512"


def out(obj) -> None:
    print(json.dumps(obj, default=str))


def db() -> psycopg.Connection:
    return psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"], autocommit=True)


def open_noatime(path) -> "os.FileIO":
    """The test's own reads of the NAS (as root, on the server) never touch
    atime, so an atime that moved was moved by the annex's reads."""
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOATIME), "rb")


def crc64(path: str) -> str:
    from omnifile_service.backends.sim.crc64 import crc64nvme

    crc = 0
    with open_noatime(path) as f:
        while chunk := f.read(8 << 20):
            crc = crc64nvme(chunk, crc)
    return f"{crc:016x}"


def seed(a) -> None:
    from omnifile_service.enrol.__main__ import add_edge

    url = os.environ["OMNIFILE_DATABASE_URL"]
    with db() as c:
        (backend_id,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES ('a1-sim', 'sim', %s, false) RETURNING backend_id",
            (Jsonb({"data_plane": "sim_http", "url": SIM_URL, "key_file": SIM_KEY,
                    "container": "live/data", "layout": "azure_two_account"}),),
        ).fetchone()  # fmt: skip
    bundle = add_edge(url, "annex-edge", a.enrol_url, Path(a.server_ca).read_bytes())
    Path(a.bundle_out).write_text(bundle + "\n")
    os.chmod(a.bundle_out, 0o600)
    with db() as c:
        (edge_id,) = c.execute(
            "SELECT edge_id FROM catalog.edge WHERE name = 'annex-edge'"
        ).fetchone()
        c.execute(
            "INSERT INTO catalog.identity (sid, kind, display_name) VALUES (%s, 'user', 'e2e')"
            " ON CONFLICT (sid) DO NOTHING",
            ("S-1-5-21-1000-2000-3000-1105",),
        )
    out({"backend_id": backend_id, "edge_id": edge_id})


def annex(a) -> None:
    """A filer and a Copy annex, as POST /v1/filers and /v1/annexes make them
    (the portal path is tested in service/tests/web/test_annex.py)."""
    from omnifile_service.annex.rules import copy_template

    tag = uuid.uuid4().hex[:6]
    with db() as c:
        backend_id, edge_id = c.execute(
            "SELECT (SELECT backend_id FROM catalog.backend WHERE name = 'a1-sim'),"
            " (SELECT edge_id FROM catalog.edge WHERE name = 'annex-edge')"
        ).fetchone()
        (ident,) = c.execute(
            "SELECT identity_id FROM catalog.identity WHERE sid = %s",
            ("S-1-5-21-1000-2000-3000-1105",),
        ).fetchone()
        (nas,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES (%s, 'nas', '{}', false) RETURNING backend_id",
            (f"nas-{a.name}-{tag}",),
        ).fetchone()
        (filer,) = c.execute(
            # Decision 65: setup answered that the share or export limits nothing.
            "INSERT INTO catalog.filer (name, host, protocol, export, edge_id, backend_id,"
            " credential_mode, event_source, created_by, access_limited, access_confirmed_by,"
            " access_confirmed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'e2e', false, 'e2e', now())"
            " RETURNING filer_id",
            (f"{a.name}-{tag}", a.host, a.protocol, a.export, edge_id, nas,
             "sec_sys" if a.sec_sys else "machine_account",
             "smb_notify" if a.protocol == "smb" else "scan"),
        ).fetchone()  # fmt: skip
        (ds,) = c.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
            " RETURNING dataset_id",
            (f"annex-{a.name}-{tag}", backend_id, edge_id),
        ).fetchone()
        c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, NULL, '', 'dir')",
            (ds,),
        )
        prefix = f"{a.path}/" if a.path else ""
        (src,) = c.execute(
            "INSERT INTO catalog.source (dataset_id, path, backend_id, container, prefix,"
            " owners_sid, kind) VALUES (%s, '', %s, %s, %s, %s, 'annex') RETURNING source_id",
            (ds, nas, a.export, prefix, OWNERS_SID),
        ).fetchone()
        (annex_id,) = c.execute(
            "INSERT INTO catalog.annex (name, source_id, filer_id, dataset_id, edge_id, path,"
            " owners_sid, ruleset_version, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'e2e') RETURNING annex_id",
            (f"{a.name}-{tag}", src, filer, ds, edge_id, a.path, OWNERS_SID),
        ).fetchone()
        c.execute(
            "INSERT INTO catalog.annex_ruleset (annex_id, version, body, state, owner_identity_id,"
            " proposed_via, decided_by_identity_id, decided_at)"
            " VALUES (%s, 1, %s, 'active', %s, 'portal', %s, now())",
            (annex_id, Jsonb(copy_template(ds)), ident, ident),
        )
    out({"annex_id": annex_id, "dataset_id": ds, "filer_id": filer})


def nas_files(root: Path) -> dict[str, Path]:
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        for n in files:
            if n.endswith(".omnifile.txt"):
                continue
            p = Path(dirpath) / n
            if p.is_file() and not p.is_symlink():
                found[str(p.relative_to(root))] = p
    return found


def copies(a) -> None:
    """Every NAS file has a live catalog file whose current content version
    is the NAS bytes (CRC-64/NVME), verified by the in-Azure read."""
    root = Path(a.root)
    files = {k: v for k, v in nas_files(root).items() if k.startswith(a.only)}
    with db() as c:
        rows = c.execute(
            """
            SELECT af.path, af.verified_version_id = f.content_version_id AS verified,
                   encode(l.crc64nvme, 'hex'), l.verified_by, l.size, l.verified,
                   af.copied_version_id, af.verified_version_id, f.content_version_id
              FROM catalog.annex_file af
              JOIN catalog.file f ON f.file_id = af.file_id
              LEFT JOIN catalog.location l ON l.version_id = f.content_version_id
                   AND l.role = 'primary' AND l.state = 'committed'
             WHERE af.annex_id = %s AND af.gone_since IS NULL
            """,
            (a.annex_id,),
        ).fetchall()
    by = {r[0]: r for r in rows if r[0].startswith(a.only)}
    matched, verified, problems = 0, 0, []
    for rel, p in sorted(files.items()):
        r = by.get(rel)
        if r is None:
            problems.append(f"{rel}: not in the catalog")
            continue
        if r[2] != crc64(str(p)):
            problems.append(f"{rel}: CRC {r[2]} != {crc64(str(p))}")
            continue
        matched += 1
        if r[1] and r[3] == "azure_read_crc":
            verified += 1
        else:
            problems.append(f"{rel}: not verified (location verified={r[5]} by {r[3]}; annex file"
                            f" copied={r[6]} verified={r[7]}; file content={r[8]})")
    extra = sorted(set(by) - set(files))
    out({"files": len(files), "matched": matched, "verified": verified, "extra": extra,
         "problems": problems[:20]})  # fmt: skip


def versions(a) -> None:
    with db() as c:
        rows = c.execute(
            """
            SELECT v.version_id, encode(v.crc64nvme, 'hex'), v.state, f.content_version_id
              FROM catalog.annex_file af
              JOIN catalog.file f ON f.file_id = af.file_id
              JOIN catalog.file_version v ON v.file_id = f.file_id AND v.kind = 'content'
             WHERE af.annex_id = %s AND af.path = %s AND af.gone_since IS NULL
             ORDER BY v.version_id
            """,
            (a.annex_id, a.path),
        ).fetchall()
        uploads = c.execute(
            "SELECT count(*) FROM catalog.file_version v JOIN catalog.annex_file af"
            " USING (file_id) WHERE af.annex_id = %s AND af.path = %s AND af.gone_since IS NULL",
            (a.annex_id, a.path),
        ).fetchone()[0]
    out({"committed": sum(1 for r in rows if r[2] == "committed"), "all": uploads,
         "crcs": [r[1] for r in rows if r[2] == "committed"],
         "current": rows[-1][3] if rows else None})  # fmt: skip


def gone(a) -> None:
    with db() as c:
        r = c.execute(
            """
            SELECT af.gone_since IS NOT NULL, af.nas_state, f.deleted_at IS NOT NULL,
                   (SELECT count(*) FROM catalog.location l WHERE l.file_id = f.file_id
                     AND l.state = 'committed'),
                   (SELECT count(*) FROM catalog.file_version v WHERE v.file_id = f.file_id
                     AND v.kind = 'tombstone')
              FROM catalog.annex_file af JOIN catalog.file f USING (file_id)
             WHERE af.annex_id = %s AND af.path = %s ORDER BY af.first_seen DESC LIMIT 1
            """,
            (a.annex_id, a.path),
        ).fetchone()
    if r is None:
        out({"found": False})
        return
    out({"found": True, "gone": r[0], "nas_state": r[1], "deleted": r[2], "kept_copies": r[3],
         "tombstones": r[4]})  # fmt: skip


def drift(a) -> None:
    with db() as c:
        rows = c.execute(
            """
            SELECT d.kind, d.object_key, d.observed->>'resolved_by'
              FROM catalog.reconcile_drift d
              JOIN catalog.filer fl ON fl.backend_id = d.backend_id
              JOIN catalog.annex a ON a.filer_id = fl.filer_id
             WHERE d.source = 'annex' AND a.annex_id = %s ORDER BY d.drift_id
            """,
            (a.annex_id,),
        ).fetchall()
    out({"drift": [{"kind": k, "key": key, "resolved_by": rb} for k, key, rb in rows]})


def dry_run(a) -> None:
    """Phase A2.1: ask the annex's unit for a dry run of Archive after N days
    (as the portal does), wait for its answer, and print the report."""
    import time

    from omnifile_service.annex import rules

    with db() as c:
        ds = c.execute("SELECT dataset_id FROM catalog.annex WHERE annex_id = %s",
                       (a.annex_id,)).fetchone()[0]  # fmt: skip
        body = rules.normalize(rules.archive_after_template(ds, a.days), ds)
        (run_id,) = c.execute(
            "INSERT INTO catalog.annex_dry_run (annex_id, body, requested_by)"
            " VALUES (%s, %s, 'e2e') RETURNING run_id",
            (a.annex_id, psycopg.types.json.Jsonb(body)),
        ).fetchone()
        deadline = time.monotonic() + a.wait
        while time.monotonic() < deadline:
            state, report, error = c.execute(
                "SELECT state, report, error FROM catalog.annex_dry_run WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if state not in ("requested", "running"):
                out({"state": state, "report": report, "error": error})
                return
            time.sleep(1)
    out({"state": "requested", "report": None, "error": "no answer in time"})


def events(a) -> None:
    with db() as c:
        rows = c.execute(
            "SELECT kind, count(*) FROM catalog.annex_event WHERE annex_id = %s GROUP BY kind",
            (a.annex_id,),
        ).fetchall()
        health = c.execute(
            "SELECT fl.health FROM catalog.filer fl JOIN catalog.annex a USING (filer_id)"
            " WHERE a.annex_id = %s",
            (a.annex_id,),
        ).fetchone()[0]
        state = c.execute(
            "SELECT state, baseline_files FROM catalog.annex WHERE annex_id = %s", (a.annex_id,)
        ).fetchone()
    out({"events": dict(rows), "health": health, "state": state[0], "baseline_files": state[1]})


REMOVING_RULES = [
    {"id": "move", "trigger": "arrived", "conditions": {"path": ["rm/*"]},
     "actions": [{"copy_to": {"kind": "tier"}}, {"remove_from_source": True}]},
    {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {"kind": "tier"}}]},
    {"id": "recopy", "trigger": "changed", "conditions": {},
     "actions": [{"copy_to": {"kind": "tier"}}]},
    {"id": "keep", "trigger": "deleted_at_source", "conditions": {},
     "actions": [{"keep_copy": True}]},
]  # fmt: skip


def go_live(a) -> None:
    """The removing rule set running, as an ArchiveAdmin approved it, its dry
    run over and confirmed (decision 59): the state the portal reaches."""
    from omnifile_service.annex import rules

    with db() as c:
        ds = c.execute("SELECT dataset_id FROM catalog.annex WHERE annex_id = %s",
                       (a.annex_id,)).fetchone()[0]  # fmt: skip
        body = rules.normalize({"rules": REMOVING_RULES}, ds)
        c.execute(
            "UPDATE catalog.annex_ruleset SET body = %s, removes = true,"
            " needs_archive_admin = true, decided_as_archive_admin = true"
            " WHERE annex_id = %s AND state = 'active'",
            (Jsonb(body), a.annex_id),
        )
        c.execute(
            "UPDATE catalog.annex SET mode = 'custom', removals_paused = false,"
            " dry_run_until = now() - interval '1 minute', pointer_files = true, grace_minutes = 1,"
            " removals_confirmed_version = ruleset_version, removals_confirmed_at = now()"
            " WHERE annex_id = %s",
            (a.annex_id,),
        )
    out({"live": True})


def pause(a) -> None:
    with db() as c:
        c.execute("UPDATE catalog.annex SET removals_paused = %s WHERE annex_id = %s",
                  (a.state == "on", a.annex_id))  # fmt: skip
    out({"paused": a.state == "on"})


def removals(a) -> None:
    with db() as c:
        rows = c.execute(
            "SELECT r.path, r.state, r.cancel_reason, r.pointer_written, r.hidden_name,"
            "       l.azure_read_at IS NOT NULL AS read_in_azure"
            "  FROM catalog.annex_removal r"
            "  LEFT JOIN catalog.location l ON l.version_id = r.version_id AND l.role = 'primary'"
            " WHERE r.annex_id = %s ORDER BY r.removal_id",
            (a.annex_id,),
        ).fetchall()
        evs = c.execute(
            "SELECT path, kind, count(*) FROM catalog.annex_event WHERE annex_id = %s"
            " AND kind IN ('removal_scheduled', 'renamed_for_removal', 'removed',"
            " 'removal_cancelled') GROUP BY path, kind",
            (a.annex_id,),
        ).fetchall()
    by: dict = {}
    for path, state, reason, ptr, hidden, read in rows:
        # The latest removal of each path wins; all are counted.
        e = by.setdefault(path, {"removals": 0})
        e.update({"state": state, "reason": reason, "pointer": ptr, "hidden_name": hidden,
                  "read_in_azure": read})  # fmt: skip
        e["removals"] += 1
    for path, kind, n in evs:
        by.setdefault(path, {"removals": 0}).setdefault("events", {})[kind] = n
    out(by)


def returns(a) -> None:
    from omnifile_service.annex import rules

    with db() as c:
        ds, filer, body = c.execute(
            "SELECT a.dataset_id, a.filer_id, r.body FROM catalog.annex a"
            " JOIN catalog.annex_ruleset r ON r.annex_id = a.annex_id AND r.state = 'active'"
            " WHERE a.annex_id = %s",
            (a.annex_id,),
        ).fetchone()
        back = [
            {"id": "back", "trigger": "recall_completed", "conditions": {"path": ["rm/a.bin"]},
             "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                         {"on_conflict": "restored_name"}, {"hold_days": 14}]},
            {"id": "restore", "trigger": "recall_completed", "conditions": {},
             "actions": [{"place_at": {"kind": "nas_folder", "filer_id": str(filer),
                                       "path": "elsewhere/restored"}},
                         {"on_conflict": "restored_name"}]},
        ]  # fmt: skip
        new = rules.normalize({"rules": rules.outbound(body["rules"]) + back}, ds)
        c.execute(
            "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
            (Jsonb(new), a.annex_id),
        )
    out({"returns": len(back)})


def returns_replace(a) -> None:
    """A3.4's rule: a file of rp/ comes back to its own name, replacing the one there."""
    from omnifile_service.annex import rules

    with db() as c:
        ds, body = c.execute(
            "SELECT a.dataset_id, r.body FROM catalog.annex a"
            " JOIN catalog.annex_ruleset r ON r.annex_id = a.annex_id AND r.state = 'active'"
            " WHERE a.annex_id = %s",
            (a.annex_id,),
        ).fetchone()
        keep = [r for r in body["rules"] if r.get("trigger") == "recall_completed"
                and r["id"] != "rp"]  # fmt: skip
        rp = {"id": "rp", "trigger": "recall_completed", "conditions": {"path": ["rp/*"]},
              "actions": [{"place_at": {"kind": "nas_folder", "origin": True}},
                          {"on_conflict": "overwrite_keep_version"}, {"hold_days": 14}]}  # fmt: skip
        new = rules.normalize({"rules": rules.outbound(body["rules"]) + [rp] + keep}, ds)
        c.execute(
            "UPDATE catalog.annex_ruleset SET body = %s WHERE annex_id = %s AND state = 'active'",
            (Jsonb(new), a.annex_id),
        )
        # The shortest grace, which the service keeps too.
        c.execute("UPDATE catalog.annex SET grace_minutes = 1 WHERE annex_id = %s", (a.annex_id,))
    out({"returns": len(keep) + 1})


def copied(a) -> None:
    """Exit 0 when PATH has at least N content versions and its current one is the
    annex's verified copy, read back in Azure."""
    with db() as c:
        r = c.execute(
            "SELECT (SELECT count(*) FROM catalog.file_version v WHERE v.file_id = af.file_id"
            "          AND v.kind = 'content' AND v.state <> 'pending'),"
            "       af.verified_version_id = f.content_version_id"
            "       AND af.copied_version_id = f.content_version_id"
            "       AND af.nas_state = 'present'"
            "       AND l.azure_read_at IS NOT NULL"
            "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
            "  LEFT JOIN catalog.location l ON l.version_id = f.content_version_id"
            "       AND l.role = 'primary' AND l.state = 'committed'"
            " WHERE af.annex_id = %s AND af.path = %s",
            (a.annex_id, a.path),
        ).fetchone()
    sys.exit(0 if r and r[0] >= a.n and r[1] else 1)


def legal_hold(a) -> None:
    with db() as c:
        c.execute("SELECT set_config('omnifile.actor', 'a1-test', false)")
        c.execute(
            "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
            " SELECT f.dataset_id, f.file_id, 'legal', 'a1 replace', 'a1-test'"
            "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
            " WHERE af.annex_id = %s AND af.path = %s",
            (a.annex_id, a.path),
        )
    out({"held": a.path})


def heads(a) -> None:
    """PATH's content versions, oldest first, and which is the head: after a replace,
    a new head that references the placed version's blob, the old one kept."""
    with db() as c:
        rows = c.execute(
            "SELECT v.version_id, v.version_id = f.content_version_id, l.object_key"
            "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
            "  JOIN catalog.file_version v ON v.file_id = f.file_id AND v.kind = 'content'"
            "  LEFT JOIN catalog.location l ON l.version_id = v.version_id AND l.role = 'primary'"
            " WHERE af.annex_id = %s AND af.path = %s ORDER BY v.version_id",
            (a.annex_id, a.path),
        ).fetchall()
    out([{"version_id": v, "head": h, "key": k} for v, h, k in rows])


def place(a) -> None:
    import asyncio

    from omnifile_service.annex import returns as R

    with db() as c:
        row = c.execute(
            "SELECT af.file_id, f.content_version_id, acl.read_sids::text[], af.nas_state,"
            "       coalesce(af.copied_size, af.size), acl.owner_sid::text, acl.group_sid::text"
            "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
            "  JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
            " WHERE af.annex_id = %s AND af.path = %s",
            (a.annex_id, a.path),
        ).fetchone()
        fid, vid, read, state, size, owner, group = row
        if getattr(a, "older", False):
            # The version before the current one (A3.4: an older version replaces it).
            (vid,) = c.execute(
                "SELECT version_id FROM catalog.file_version WHERE file_id = %s"
                " AND kind = 'content' AND state <> 'pending' AND version_id <> %s"
                " ORDER BY version_id DESC LIMIT 1",
                (fid, vid),
            ).fetchone()
        sid = f"S-1-5-21-1000-2000-3000-{4000 + a.annex_id}"
        (ident,) = c.execute(
            "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)"
            " ON CONFLICT (sid) DO UPDATE SET group_sids = EXCLUDED.group_sids"
            " RETURNING identity_id",
            (sid, read),
        ).fetchone()

    async def resolve():
        import psycopg as pg
        from psycopg.rows import dict_row

        async with await pg.AsyncConnection.connect(
            os.environ["OMNIFILE_DATABASE_URL"], row_factory=dict_row
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT a.*, r.body AS rs_body, r.version AS rs_version FROM catalog.annex a"
                    " JOIN catalog.annex_ruleset r ON r.annex_id = a.annex_id"
                    " AND r.state = 'active' WHERE a.annex_id = %s",
                    (a.annex_id,),
                )
                ann = await cur.fetchone()
            facts = R.Facts(fid, a.annex_id, a.path, int(size or 0), state, owner, group)
            who = R.Requester(sid, list(read or []))
            d = R.decide(ann["rs_body"], facts, who)
            t = await R.resolve(conn, d["default"], facts, ann)
            return ann, d, t

    ann, d, t = asyncio.run(resolve())
    target = {**t.json(), "display": t.path}
    with db() as c:
        (pid,) = c.execute(
            "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id,"
            " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict,"
            " hold_days, state) VALUES (%s, %s, %s, %s, 'portal', %s, %s, %s, %s, %s, 'ready')"
            " RETURNING placement_id",
            (a.annex_id, fid, vid, ident, d["rule"], ann["rs_version"], Jsonb(target),
             d["on_conflict"], d["hold_days"]),
        ).fetchone()  # fmt: skip
    out({"placement_id": pid, "target": target})


def placements(a) -> None:
    with db() as c:
        rows = c.execute(
            "SELECT af.path, p.state, p.fail_reason, p.outcome, p.final_path, p.placement_id,"
            "       p.replace_state, p.replace_refused, p.replaced_aside_name,"
            "       p.replaced_version_id"
            "  FROM catalog.annex_placement p JOIN catalog.annex_file af"
            "    ON af.file_id = p.file_id AND af.annex_id = p.annex_id"
            " WHERE p.annex_id = %s ORDER BY p.placement_id",
            (a.annex_id,),
        ).fetchall()
        held = dict(c.execute(
            "SELECT path, hold_until IS NOT NULL AND hold_until > now() FROM catalog.annex_file"
            " WHERE annex_id = %s", (a.annex_id,)).fetchall())  # fmt: skip
        evs = dict(c.execute(
            "SELECT kind, count(*) FROM catalog.annex_event WHERE annex_id = %s"
            " AND kind IN ('placed', 'returned', 'placement_failed') GROUP BY kind",
            (a.annex_id,)).fetchall())  # fmt: skip
    by: dict = {}
    for path, state, reason, outcome, final, pid, rs, rr, aside, rv in rows:
        by[path] = {"state": state, "reason": reason, "outcome": outcome, "final_path": final,
                    "placement_id": pid, "held": held.get(path), "replace": rs,
                    "replace_refused": rr, "kept_as": aside, "replaced_version_id": rv}  # fmt: skip
    out({"by_path": by, "events": evs})


def mount_reports(a) -> None:
    """The errors of its `mount` reports since an event, newest first; or its
    `nas_not_answering` problems."""
    kind = "problem" if a.problems else "mount"
    extra = " AND details->>'reason' = 'nas_not_answering'" if a.problems else ""
    with db() as c:
        rows = c.execute(
            "SELECT coalesce(details->>'error', details->>'detail') FROM catalog.annex_event"
            f" WHERE annex_id = %s AND kind = %s AND event_id > %s{extra}"
            " ORDER BY event_id DESC LIMIT 40",
            (a.annex_id, kind, a.after),
        ).fetchall()
    out([r[0] for r in rows])


def last_event(a) -> None:
    with db() as c:
        (n,) = c.execute(
            "SELECT coalesce(max(event_id), 0) FROM catalog.annex_event WHERE annex_id = %s",
            (a.annex_id,),
        ).fetchone()
    print(n)


def verify_denies(a) -> None:
    """Read-backs that refused a copy, and messages waiting or dead on the verify queue."""
    with db() as c:
        rows = c.execute(
            "SELECT file_id, version_id, details->>'reason' FROM catalog.audit_log"
            " WHERE action = 'location.verify' AND decision = 'deny'"
        ).fetchall()
        queues = c.execute(
            "SELECT queue_name FROM pgmq.meta WHERE queue_name LIKE '%verify%'"
        ).fetchall()
        depth = {
            q: c.execute(f"SELECT count(*) FROM pgmq.q_{q}").fetchone()[0] for (q,) in queues
        }
    out({"denied": [list(r) for r in rows], "queues": depth})


def gone_count(a) -> None:
    with db() as c:
        (n,) = c.execute(
            "SELECT count(*) FROM catalog.annex_file WHERE annex_id = %s"
            " AND gone_since IS NOT NULL",
            (a.annex_id,),
        ).fetchone()
    print(n)


def cancel_placement(a) -> None:
    with db() as c:
        c.execute(
            "UPDATE catalog.annex_placement SET state = 'cancelled', done_at = now(),"
            " updated_at = now() WHERE placement_id = %s",
            (a.placement_id,),
        )
    out({"cancelled": a.placement_id})


def filer_report(a) -> None:
    """What the portal shows before registering: `placement_account_report`."""
    from omnifile_service.web.routes_annex import _report_json

    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        f = c.execute(
            "SELECT f.* FROM catalog.filer f JOIN catalog.annex x ON x.filer_id = f.filer_id"
            " WHERE x.annex_id = %s",
            (a.annex_id,),
        ).fetchone()
    out(_report_json(f))


def register_placer(a) -> None:
    """The setup step, through the real route: `PATCH /v1/filers/{id}`, with a
    dev sign-in as an ArchiveAdmin, freshly, and If-Match. Nothing is seeded: it
    checks what the filer's edge reported. Prints the status, with the filer or
    the refusal."""
    _patch_filer(a, {"placement_sid": a.sid})


def answer_access(a) -> None:
    """Decision 65's question, through the same route: `limited` with the groups the
    share lets in (`--groups`), or not."""
    groups = [g for g in (a.groups or "").split(",") if g]
    _patch_filer(a, {"access": {"limited": bool(groups), "groups": groups}})


def _patch_filer(a, body: dict) -> None:
    from urllib.parse import urlencode

    import httpx
    from starlette.testclient import TestClient

    from omnifile_service.api.db import Catalog
    from omnifile_service.catalog.privileges import as_app
    from omnifile_service.web.app import create_app
    from omnifile_service.web.auth import Authenticator, StaticJwks, StaticSecret
    from omnifile_service.web.config import OidcEndpoints, WebSettings
    from omnifile_service.web.context import WebServices
    from omnifile_service.web.devidp import DevClient, DevIdp
    from omnifile_service.web.preview import PolicyPreviewPort

    origin, tenant, client_id = "https://a1.test", str(uuid.uuid4()), str(uuid.uuid4())
    with db() as c:
        (filer,) = c.execute(
            "SELECT filer_id FROM catalog.annex WHERE annex_id = %s", (a.annex_id,)
        ).fetchone()
    endpoints = OidcEndpoints.dev(origin)
    settings = WebSettings(origin=origin, tenant_id=tenant, client_id=client_id, oidc=endpoints,
                           api_audiences=(client_id,), dev_mode=True)  # fmt: skip
    idp = DevIdp(issuer=endpoints.issuer, tenant_id=tenant,
                 clients={client_id: DevClient(client_id, "dev-secret", [settings.redirect_uri,
                                                                         settings.azure_redirect_uri])},
                 users={}, api_audience=client_id)  # fmt: skip
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.mounted()), base_url=origin)
    catalog = Catalog(as_app(os.environ["OMNIFILE_DATABASE_URL"], "web"), max_size=2)
    services = WebServices(
        catalog=catalog, settings=settings,
        auth=Authenticator(settings, catalog, StaticJwks(idp.public_keys), http,
                           StaticSecret("dev-secret")),
        preview=PolicyPreviewPort(), closers=[http.aclose],
    )  # fmt: skip
    app = create_app(services, dev_idp=idp.app())
    user = {"dev_oid": str(uuid.uuid4()), "dev_name": "A1 admin", "dev_roles": "ArchiveAdmin",
            "dev_groups": "", "dev_sid": "S-1-5-21-1000-2000-3000-9001"}  # fmt: skip
    with TestClient(app, base_url=origin, client=("10.1.2.3", 50000)) as t:
        r1 = t.get("/auth/login", params={"return_to": "/", "reauth": "1"}, follow_redirects=False)
        r2 = t.get(r1.headers["location"] + "&" + urlencode(user), follow_redirects=False)
        t.get(r2.headers["location"], follow_redirects=False)
        csrf = t.get("/auth/session").json()["csrf_token"]
        got = t.get(f"/v1/filers/{filer}")
        got.raise_for_status()
        r = t.patch(f"/v1/filers/{filer}", json=body,
                    headers={"If-Match": got.headers["ETag"], "X-CSRF-Token": csrf, "Origin": origin,
                             "Idempotency-Key": str(uuid.uuid4())})  # fmt: skip
    out({"status": r.status_code, **r.json()})


def staging_rows(a) -> None:
    """Anything of a staging folder that reached the catalog."""
    with db() as c:
        (files,) = c.execute(
            "SELECT count(*) FROM catalog.annex_file WHERE annex_id = %s"
            " AND path LIKE '.omnifile-staging%%'",
            (a.annex_id,),
        ).fetchone()
        (events,) = c.execute(
            "SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s"
            " AND path LIKE '.omnifile-staging%%'",
            (a.annex_id,),
        ).fetchone()
    out({"files": files, "events": events})


def snapshot(a) -> None:
    root = Path(a.root)
    files = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            h = hashlib.sha256()
            with open_noatime(p) as f:
                while chunk := f.read(8 << 20):
                    h.update(chunk)
            files[str(p.relative_to(root))] = h.hexdigest()
    Path(a.file).write_text(json.dumps(files))
    out({"files": len(files)})


def diff(a) -> None:
    b = json.loads(Path(a.before).read_text())
    c = json.loads(Path(a.after).read_text())
    out({"added": sorted(set(c) - set(b)), "removed": sorted(set(b) - set(c)),
         "changed": sorted(k for k in set(b) & set(c) if b[k] != c[k])})  # fmt: skip


def tree(a) -> None:
    """Everything a write could change: names, sizes, bytes, mtimes, ctimes,
    modes and owners, of files and folders (`digest`). Atimes are apart
    (`atimes`): the exports are `strictatime`, so a read moves them, and the
    annex reads every file it copies (a read, not a write; it matters for an
    accessed_age rule on the NAS). The test reads with O_NOATIME."""
    root = Path(a.root)
    h = hashlib.sha256()
    atimes = {}
    n = 0
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for name in [""] + sorted(files):
            p = Path(dirpath) / name if name else Path(dirpath)
            st = os.lstat(p)
            rel = str(p.relative_to(root))
            line = [rel, st.st_mode, st.st_uid, st.st_gid, st.st_size,
                    st.st_mtime_ns, st.st_ctime_ns]  # fmt: skip
            if name:
                atimes[rel] = st.st_atime_ns
                fh = hashlib.sha256()
                with open_noatime(p) as f:
                    while chunk := f.read(8 << 20):
                        fh.update(chunk)
                line.append(fh.hexdigest())
            h.update(json.dumps(line).encode())
            n += 1
    out({"entries": n, "digest": h.hexdigest(), "atimes": atimes})


def atimes_moved(a) -> None:
    """How many files' atimes moved since `before` (a `tree` output)."""
    before = json.loads(Path(a.before).read_text())["atimes"]
    moved = 0
    for rel, t in before.items():
        p = Path(a.root) / rel
        if p.exists() and os.lstat(p).st_atime_ns != t:
            moved += 1
    out({"files": len(before), "moved": moved})


def acl_matches_mode(a) -> None:
    """Each copied SMB file's read set is what its server-side mode grants,
    as Samba presents it in the NT descriptor: the owner for u+r, the group
    for g+r, Everyone for o+r; no deny."""
    root = Path(a.root)
    problems, n = [], 0
    with db() as c:
        rows = c.execute(
            "SELECT af.path, acl.raw_kind, acl.deny_first, acl.owner_sid::text,"
            "       acl.group_sid::text, acl.read_sids::text[], acl.deny_sids::text[]"
            "  FROM catalog.annex_file af JOIN catalog.file f USING (file_id)"
            "  JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
            " WHERE af.annex_id = %s AND af.gone_since IS NULL",
            (a.annex_id,),
        ).fetchall()
    for path, kind, first, owner, group, read, deny in rows:
        p = root / path
        if not p.exists():
            continue
        n += 1
        mode = os.lstat(p).st_mode
        want = set()
        if mode & 0o400 and owner:
            want.add(owner)
        if mode & 0o040 and group:
            want.add(group)
        if mode & 0o004:
            want.add("S-1-1-0")
        if kind != "ntfs_sd" or not first:
            problems.append(f"{path}: {kind}, deny_first={first}")
        elif set(read) != want or deny:
            problems.append(f"{path}: mode {mode & 0o777:o} read {sorted(read)} deny {deny},"
                            f" want {sorted(want)}")  # fmt: skip
    out({"files": n, "problems": problems})


def smb_links(a) -> None:
    """Every SMB2 hard-link request (SET_INFO, FileLinkInformation) in a
    capture, with its ReplaceIfExists byte: the placer's link must never ask
    the server to replace a name (R3). And every open for WRITE_OWNER, with
    whether it asks for backup intent: P5's must, or a NAS that honours only
    that wouldn't let the restore privilege set the owner. Reads a classic
    pcap of an Ethernet or Linux loopback capture; SMB2 headers are found in
    each TCP payload, and a compound's chain is followed. An encrypted session
    shows no headers, so nothing, and the checks fail rather than pass."""
    data = Path(a.file).read_bytes()
    magic = struct.unpack("<I", data[:4])[0]
    end = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    link = struct.unpack(end + "I", data[20:24])[0]
    off, links, owner_opens = 24, [], []
    while off + 16 <= len(data):
        incl = struct.unpack(end + "I", data[off + 8:off + 12])[0]
        frame = data[off + 16:off + 16 + incl]
        off += 16 + incl
        if link == 1:  # Ethernet (Linux's lo too)
            if frame[12:14] != b"\x08\x00":
                continue
            ip = frame[14:]
        elif link == 113:  # Linux cooked
            ip = frame[16:]
        else:
            ip = frame
        if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 6:
            continue
        ihl = (ip[0] & 15) * 4
        total = struct.unpack(">H", ip[2:4])[0]
        tcp = ip[ihl:total]
        payload = tcp[(tcp[12] >> 4) * 4:]
        i = payload.find(b"\xfeSMB")
        while i >= 0:
            h = i
            while h + 64 <= len(payload):
                cmd = struct.unpack("<H", payload[h + 12:h + 14])[0]
                flags = payload[h + 16]
                nxt = struct.unpack("<I", payload[h + 20:h + 24])[0]
                body = payload[h + 64:]
                if cmd == 0x05 and not flags & 1 and len(body) >= 44:
                    access, options = struct.unpack("<I", body[24:28])[0], struct.unpack("<I", body[40:44])[0]
                    if access & 0x00080000:  # WRITE_OWNER
                        owner_opens.append({"backup_intent": bool(options & 0x00004000)})
                if cmd == 0x11 and not flags & 1 and len(body) >= 12 and body[2] == 1 and body[3] == 11:
                    boff = struct.unpack("<H", body[8:10])[0]
                    buf = payload[h + boff:h + boff + 20]
                    if len(buf) == 20:
                        n = struct.unpack("<I", buf[16:20])[0]
                        name = payload[h + boff + 20:h + boff + 20 + n]
                        links.append({"replace_if_exists": buf[0],
                                      "name": name.decode("utf-16-le", "replace")})
                if not nxt:
                    break
                h += nxt
            i = payload.find(b"\xfeSMB", max(h, i) + 4)
    out({"links": links, "owner_opens": owner_opens})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="annex_a1.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--bundle-out", required=True)
    s.add_argument("--enrol-url", required=True)
    s.add_argument("--server-ca", required=True)
    s.set_defaults(fn=seed)
    s = sub.add_parser("annex")
    for f in ("--name", "--protocol", "--host", "--export"):
        s.add_argument(f, required=True)
    s.add_argument("--path", default="")
    s.add_argument("--sec-sys", action="store_true")
    s.set_defaults(fn=annex)
    s = sub.add_parser("copies")
    s.add_argument("annex_id", type=int)
    s.add_argument("root")
    s.add_argument("--only", default="")
    s.set_defaults(fn=copies)
    for name, fn in (("versions", versions), ("gone", gone)):
        s = sub.add_parser(name)
        s.add_argument("annex_id", type=int)
        s.add_argument("path")
        s.set_defaults(fn=fn)
    s = sub.add_parser("dry-run")
    s.add_argument("annex_id", type=int)
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--wait", type=int, default=60)
    s.set_defaults(fn=dry_run)
    for name, fn in (("drift", drift), ("events", events)):
        s = sub.add_parser(name)
        s.add_argument("annex_id", type=int)
        s.set_defaults(fn=fn)
    s = sub.add_parser("tree")
    s.add_argument("root")
    s.set_defaults(fn=tree)
    s = sub.add_parser("atimes-moved")
    s.add_argument("root")
    s.add_argument("before")
    s.set_defaults(fn=atimes_moved)
    for name, fn in (("go-live", go_live), ("removals", removals)):
        s = sub.add_parser(name)
        s.add_argument("annex_id", type=int)
        s.set_defaults(fn=fn)
    s = sub.add_parser("pause")
    s.add_argument("annex_id", type=int)
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=pause)
    s = sub.add_parser("snapshot")
    s.add_argument("root")
    s.add_argument("file")
    s.set_defaults(fn=snapshot)
    s = sub.add_parser("diff")
    s.add_argument("before")
    s.add_argument("after")
    s.set_defaults(fn=diff)
    s = sub.add_parser("returns")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=returns)
    s = sub.add_parser("filer-report")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=filer_report)
    s = sub.add_parser("register-placer")
    s.add_argument("annex_id", type=int)
    s.add_argument("sid")
    s.set_defaults(fn=register_placer)
    s = sub.add_parser("answer-access")
    s.add_argument("annex_id", type=int)
    s.add_argument("--groups", default="")
    s.set_defaults(fn=answer_access)
    s = sub.add_parser("staging-rows")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=staging_rows)
    s = sub.add_parser("place")
    s.add_argument("annex_id", type=int)
    s.add_argument("path")
    s.add_argument("--older", action="store_true")
    s.set_defaults(fn=place)
    s = sub.add_parser("returns-replace")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=returns_replace)
    s = sub.add_parser("copied")
    s.add_argument("annex_id", type=int)
    s.add_argument("path")
    s.add_argument("n", type=int)
    s.set_defaults(fn=copied)
    s = sub.add_parser("legal-hold")
    s.add_argument("annex_id", type=int)
    s.add_argument("path")
    s.set_defaults(fn=legal_hold)
    s = sub.add_parser("heads")
    s.add_argument("annex_id", type=int)
    s.add_argument("path")
    s.set_defaults(fn=heads)
    s = sub.add_parser("mount-reports")
    s.add_argument("annex_id", type=int)
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--problems", action="store_true")
    s.set_defaults(fn=mount_reports)
    s = sub.add_parser("last-event")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=last_event)
    s = sub.add_parser("verify-denies")
    s.set_defaults(fn=verify_denies)
    s = sub.add_parser("gone-count")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=gone_count)
    s = sub.add_parser("cancel-placement")
    s.add_argument("placement_id", type=int)
    s.set_defaults(fn=cancel_placement)
    s = sub.add_parser("placements")
    s.add_argument("annex_id", type=int)
    s.set_defaults(fn=placements)
    s = sub.add_parser("smb-links")
    s.add_argument("file")
    s.set_defaults(fn=smb_links)
    s = sub.add_parser("acl-matches-mode")
    s.add_argument("annex_id", type=int)
    s.add_argument("root")
    s.set_defaults(fn=acl_matches_mode)
    a = p.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
