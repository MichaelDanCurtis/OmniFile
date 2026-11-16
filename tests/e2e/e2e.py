"""Helpers for the M1 exit test (tests/e2e/exit_test.sh).

Runs on the edge machine with the service's Python environment
(/opt/omnifile-service/venv), so it can use the catalog and the sim's SAS
signer directly. Every subcommand prints one JSON object.

  e2e.py reset-db                         drop the catalog, migrate again
  e2e.py seed --enrol-url URL --server-ca PEM --bundle-out FILE
                                          backend (sim_http), a pending edge
                                          and its bundle, two datasets with one
                                          share each: wait (on_open wait) and
                                          failfast (on_open fail)
  e2e.py file SHARE NAME                  the catalog's view of SHARE/NAME
  e2e.py blob SHARE NAME                  GET the blob from sim: size, CRC64, sha256
  e2e.py versions SHARE NAME              committed content versions and their edge op_ids
  e2e.py local PATH                       marker, blocks, first hole, an S3 fill's
                                          recorded progress, CRC64, sha256
  e2e.py pattern PATH SIZE SEED           write SIZE pseudo-random bytes (seeded)
  e2e.py compare PATH SOURCE              byte compare; counts zero blocks read
                                          where the source has data
  e2e.py fault JSON                       POST /_sim/faults ("backend": "s3" for the S3 sim)
  e2e.py s3-served                        bytes of each S3 object sent over presigned GETs
  e2e.py stub-set SHARE NAME              is a stub instruction in the catalog?

M2 (tests/e2e/m2_exit_test.sh):
  e2e.py seed ... --m2 --owners-sid S [--writers-sid S] [--readers-sid S]
                                          also: smbwait (SMB, wait), smbfail
                                          (SMB, fail), krb (NFSv4 krb5, wait),
                                          each its own dataset (proven shares)
  e2e.py shares                           every share as the catalog has it
  e2e.py archive SHARE NAME               move the file's primary copy to Archive
  e2e.py recall SHARE NAME                the file's latest recall + requesters
  e2e.py advance SHARE NAME STATE         set that recall's state (the worker's job)
  e2e.py acl SHARE NAME                   the catalog's ACL for the file (entries, read_sids)
  e2e.py flood PATH SOURCE N TIMEOUT      N concurrent readers (threads) of PATH

M5 (tests/e2e/m5_exit_test.sh; the sim facade runs with --s3):
  e2e.py s3-setup SHARE --uid U --gid G   an S3-flavoured sim backend and its
                                          bucket; SHARE's dataset gets U:G as
                                          its POSIX defaults (seeded files)
  e2e.py s3-put KEY SIZE SEED [--archive] put SIZE seeded bytes in the bucket
                                          (Standard, or the archive class)
  e2e.py source SOURCE_ID                 the source's seeding state
  e2e.py locations SHARE NAME             every copy of the file's current content
  e2e.py rewind SHARE DAYS                rewind depth (and max) DAYS, daily labels at 00:00
  e2e.py backdate SHARE HOURS             SHARE's version history moved HOURS back (so
                                          it is in today's and yesterday's labels)
  e2e.py marker PATH                      the marker, read by path (opens nothing)
  e2e.py path SHARE PATH                  a file row found through subfolders
  e2e.py actors FILE_ID                   each version of the file with who made it
                                          (file_version.actor_*, migration 0030)
  e2e.py s3-seed-one SOURCE KEY SIZE      one more object of a seeded source into the
                                          catalog, as its follower writes a new one
  e2e.py s3-requests                     requests to the S3 sim per bucket, OmniFile's
                                          and this harness's apart (decision 40)
  e2e.py ranges SIZE SEED OFF:LEN,...     SHA-256 of each range of s3-put's bytes
  e2e.py identity UID [--sid S]           UID's directory entry (as AD sync writes one)
  e2e.py follow BUCKET                    follow the S3 bucket's change feed from now
  e2e.py migrate SHARE PREFIX             a migration of PREFIX, copied by the worker's
                                          copier (sim), cut over once verified
  e2e.py migration ID                     its state, counts, runs and staging objects
  e2e.py copies SHARE PATH                every location of the file's current content
  e2e.py cutover-report SHARE PREFIX      a migration of PREFIX in SHARE's dataset (an
                                          external copier, manual cutover: nothing
                                          is copied), and its cutover report's items
                                          as GET /v1/migrations/{id}/tombstones gives them

Environment: OMNIFILE_DATABASE_URL (from /etc/omnifile-dev/service.env).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import struct
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Not needed by `compare`, which also runs as an unprivileged test user with
# the system python3 (the service's venv interpreter is root-only).
try:
    import httpx
    import psycopg
    import psycopg.rows
except ImportError:  # pragma: no cover - compare/pattern only
    httpx = None  # type: ignore[assignment]
    psycopg = None  # type: ignore[assignment]

SIM_URL = "http://127.0.0.1:10000"
SIM_KEY = "/var/lib/omnifile-dev/sim-key.json"
CONTAINER = "live/data"
S3_BUCKET = "e2e-bucket"
#: A second bucket, for a source of its own (m5_exit_test.sh step 11).
S3_BUCKET2 = "e2e-chaos"
#: The bucket step 15 migrates, and OmniFile's own staging bucket (decision 40:
#: the only S3 bucket OmniFile writes).
S3_MIG = "e2e-mig"
S3_OPS = "e2e-ops"
OWNERS_SID = "S-1-5-21-1000-2000-3000-512"
MiB = 1 << 20
BLOCK = 4096


def out(obj) -> None:
    print(json.dumps(obj, default=str))


def db() -> psycopg.Connection:
    return psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"], autocommit=True)


def crc64(data_or_path) -> str:
    from omnifile_service.backends.sim import crc64nvme

    if isinstance(data_or_path, (bytes, bytearray, memoryview)):
        return f"{crc64nvme(bytes(data_or_path)):016x}"
    c = 0
    with open(data_or_path, "rb") as fh:
        while chunk := fh.read(8 * MiB):
            c = crc64nvme(chunk, c)
    return f"{c:016x}"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(8 * MiB):
            h.update(chunk)
    return h.hexdigest()


# --- catalog -------------------------------------------------------------------


def reset_db(_a) -> None:
    with db() as c:
        c.execute("DROP SCHEMA IF EXISTS catalog CASCADE")
        c.execute("DROP SCHEMA IF EXISTS pgmq CASCADE")
        c.execute("DROP TABLE IF EXISTS public.schema_migrations")
    from omnifile_service.catalog.migrate import migrate

    out({"reset": True, "applied": migrate(os.environ["OMNIFILE_DATABASE_URL"])})


def seed(a) -> None:
    from omnifile_service.enrol.__main__ import add_edge

    url = os.environ["OMNIFILE_DATABASE_URL"]
    with db() as c:
        (backend_id,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES ('e2e-sim', 'sim', %s, false) RETURNING backend_id",
            (
                json.dumps(
                    {
                        "data_plane": "sim_http",
                        "url": SIM_URL,
                        "key_file": SIM_KEY,
                        "container": CONTAINER,
                        "layout": "azure_two_account",
                    }
                ),
            ),
        ).fetchone()
    bundle = add_edge(url, "edge1", a.enrol_url, Path(a.server_ca).read_bytes())
    Path(a.bundle_out).write_text(bundle + "\n")
    os.chmod(a.bundle_out, 0o600)
    shares = {}
    with db() as c:
        (edge_id,) = c.execute("SELECT edge_id FROM catalog.edge WHERE name = 'edge1'").fetchone()
        specs = [
            ("wait", "wait", ["nfs3", "nfs4"], ["sys"], 0o755, a.uid),
            ("failfast", "fail", ["nfs3", "nfs4"], ["sys"], 0o755, a.uid),
        ]
        if a.m2:
            # Proven shares (decision 28): SMB only, or NFS with Kerberos
            # only. Owned by geo-owner; their ACL comes from the share's
            # groups (omniacl apply-root in the test).
            specs += [
                ("smbwait", "wait", ["smb"], [], 0o770, 20005),
                ("smbfail", "fail", ["smb"], [], 0o770, 20005),
                ("krb", "wait", ["nfs4"], ["krb5", "krb5i", "krb5p"], 0o770, 20005),
            ]
        for name, on_open, protocols, nfs_sec, mode, uid in specs:
            (ds,) = c.execute(
                "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
                " RETURNING dataset_id",
                (f"e2e-{name}", backend_id, edge_id),
            ).fetchone()
            # The share root belongs to the test's writer: the share squashes
            # root (the catalog requires it for sec=sys shares).
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
                (
                    ds,
                    edge_id,
                    name,
                    root,
                    protocols,
                    nfs_sec,
                    clients,
                    a.owners_sid or OWNERS_SID,
                    a.writers_sid,
                    a.readers_sid,
                    on_open,
                ),
            ).fetchone()
            shares[name] = {"dataset_id": ds, "root_dir_id": root, "share_id": share}
    out({"backend_id": backend_id, "edge_id": edge_id, "shares": shares})


def add_share_cmd(a) -> None:
    """A dataset with its root folder and a share of it, the rows POST
    /v1/datasets and POST /v1/shares make (M6; the M1 stack runs no portal).
    The catalog's triggers give the share its NFS export id and put it in the
    edge's feed."""
    with db() as c:
        (edge_id,) = c.execute("SELECT edge_id FROM catalog.edge WHERE name = 'edge1'").fetchone()
        (backend_id,) = c.execute(
            "SELECT backend_id FROM catalog.backend WHERE name = 'e2e-sim'"
        ).fetchone()
        (ds,) = c.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
            " RETURNING dataset_id",
            (f"e2e-{a.name}", backend_id, edge_id),
        ).fetchone()
        (root,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, uid, gid, mode)"
            " VALUES (%s, NULL, '', 'dir', %s, %s, 493) RETURNING file_id",
            (ds, a.uid, a.gid),
        ).fetchone()
        share, export_id = c.execute(
            "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols,"
            " nfs_sec, nfs_clients, root_squash, owners_group_sid, on_open)"
            " VALUES (%s, %s, %s, %s, ARRAY['nfs3', 'nfs4'], ARRAY['sys'], ARRAY['127.0.0.1'],"
            " true, %s, 'wait') RETURNING share_id, nfs_export_id",
            (ds, edge_id, a.name, root, OWNERS_SID),
        ).fetchone()
    out({"dataset_id": ds, "root_dir_id": root, "share_id": share, "nfs_export_id": export_id})


def remove_share_cmd(a) -> None:
    """What DELETE /v1/shares/{id} does: the share row goes (its trigger tells
    the edge); the dataset and its files stay."""
    with db() as c:
        n = c.execute("DELETE FROM catalog.share WHERE name = %s", (a.name,)).rowcount
    out({"removed": n})


FILE_SQL = """
SELECT f.file_id, f.state, f.size, f.content_version_id, f.stub_version_id,
       f.stub_resident_bytes, f.resident_bytes, f.uid, f.gid, f.mode,
       encode(f.crc64nvme, 'hex') AS crc64nvme, encode(f.blake3, 'hex') AS blake3,
       l.backend_id, l.container, l.object_key, l.backend_version_id,
       l.storage_class, l.state AS location_state,
       l.verified, l.verified_by
  FROM catalog.share s
  JOIN catalog.file f ON f.parent_id = s.root_dir_id AND f.name = %s AND f.deleted_at IS NULL
  LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
 WHERE s.name = %s
"""


def file_row(share: str, name: str) -> dict | None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        return c.execute(FILE_SQL, (name, share)).fetchone()


def file_cmd(a) -> None:
    out(file_row(a.share, a.name) or {})


def path_row(c, share: str, path: str) -> dict | None:
    """SHARE/PATH's live file row, through subfolders (None until each part exists)."""
    row = c.execute(
        "SELECT root_dir_id AS file_id FROM catalog.share WHERE name = %s", (share,)
    ).fetchone()
    for part in [p for p in path.split("/") if p]:
        if row is None:
            break
        row = c.execute(
            "SELECT file_id, state, size, content_version_id, uid, gid FROM catalog.file"
            " WHERE parent_id = %s AND name = %s AND deleted_at IS NULL",
            (row["file_id"], part),
        ).fetchone()
    return row


def path_cmd(a) -> None:
    """SHARE/PATH's file row, through subfolders: {} until each part exists."""
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        row = path_row(c, a.share, a.path)
    out(row or {})


def actors_cmd(a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        rows = c.execute(
            "SELECT v.version_id, v.kind, v.name, v.edge_op_id, v.actor_via, v.actor_uid,"
            " v.actor_sid, v.actor_sec, v.actor_proof, host(v.actor_client_addr) AS actor_addr,"
            " v.actor_identity_id, i.sid AS identity_sid, i.posix_id AS identity_uid"
            " FROM catalog.file_version v LEFT JOIN catalog.identity i"
            " ON i.identity_id = v.actor_identity_id WHERE v.file_id = %s ORDER BY v.seq",
            (a.file_id,),
        ).fetchall()
    out({"versions": rows})


def cutover_report_cmd(a) -> None:
    """The cutover report (portal-api.md 5.18) of a migration of PREFIX, as
    the route builds it: its SQL and tombstone_json."""
    import asyncio

    from omnifile_service.migrate import request_location
    from omnifile_service.web.context import tuple_rows
    from omnifile_service.web.routes_migrations import _TOMBSTONES, _load, tombstone_json

    async def run() -> dict:
        url = os.environ["OMNIFILE_DATABASE_URL"]
        async with await psycopg.AsyncConnection.connect(
            url, row_factory=psycopg.rows.dict_row
        ) as c:
            ds = await (
                await c.execute(
                    "SELECT d.dataset_id, d.backend_id FROM catalog.share s"
                    " JOIN catalog.dataset d USING (dataset_id) WHERE s.name = %s",
                    (a.share,),
                )
            ).fetchone()
            m = await (
                await c.execute(
                    "SELECT migration_id FROM catalog.migration_prefix"
                    " WHERE dataset_id = %s AND prefix = %s",
                    (ds["dataset_id"], a.prefix),
                )
            ).fetchone()
            if m is None:
                with tuple_rows(c):
                    mid = await request_location(
                        c,
                        dataset_id=ds["dataset_id"],
                        prefix=a.prefix,
                        to_backend_id=ds["backend_id"],
                        copy_by="external",
                        requested_by="app:e2e",
                        cutover="manual",
                    )
                await c.commit()
            else:
                mid = m["migration_id"]
            mp = await _load(c, mid)
            rows = await (
                await c.execute(_TOMBSTONES, {"mid": mid, "after": 0, "limit": 1000})
            ).fetchall()
            items = [tombstone_json(r, mp["from_backend_id"]) for r in rows]
            return {"migration_id": str(mid), "items": items}

    out(asyncio.run(run()))


def s3_seed_one_cmd(a) -> None:
    """One more object of a seeded source written into the catalog, as the
    source's follower does for a new object (sources.seed.write_batch): the
    edge materialises it as a placeholder with no local bytes."""
    import asyncio
    from datetime import UTC, datetime

    from omnifile_service.sources.model import SeedObject
    from omnifile_service.sources.seed import Source, write_batch

    async def run() -> dict:
        url = os.environ["OMNIFILE_DATABASE_URL"]
        async with await psycopg.AsyncConnection.connect(url) as c:
            cur = c.cursor(row_factory=psycopg.rows.dict_row)
            await cur.execute("SELECT * FROM catalog.source WHERE source_id = %s", (a.source_id,))
            row = await cur.fetchone()
            src = Source.from_row(row)
            now = datetime.now(UTC)
            obj = SeedObject(key=a.key, version_id=None, size=a.size, last_modified=now,
                             native_class="STANDARD", logical_tier="hot")  # fmt: skip
            w = await write_batch(c, src, [obj], now)
            await c.commit()
            return {"files": w.files, "skipped": w.skipped}

    out(asyncio.run(run()))


def shares_cmd(_a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        rows = c.execute(
            "SELECT share_id, name, protocols, nfs_sec, nfs_clients, root_squash, trust_level,"
            " owners_group_sid, writers_group_sid, readers_group_sid, on_open,"
            " wait_threshold_minutes FROM catalog.share ORDER BY share_id"
        ).fetchall()
    out({"shares": rows})


def archive_cmd(a) -> None:
    """Move SHARE/NAME's primary copy to the Archive tier (catalog only: the
    sim keeps serving the bytes, as a restored copy would)."""
    row = file_row(a.share, a.name)
    with db() as c:
        c.execute(
            "UPDATE catalog.location SET logical_tier = 'archive', storage_class = 'Archive'"
            " WHERE location_id = (SELECT primary_location_id FROM catalog.file"
            " WHERE file_id = %s)",
            (row["file_id"],),
        )
    out({"file_id": row["file_id"], "archived": True})


RECALL_SQL = """
SELECT r.recall_id, r.state, r.version_id, r.restore_id,
       (SELECT json_build_object('state', x.state, 'triggered_by', x.triggered_by,
               'migrate_on_complete', x.migrate_on_complete)
          FROM catalog.restore x WHERE x.restore_id = r.restore_id) AS restore,
       coalesce((SELECT json_agg(json_build_object('uid', q.uid, 'via', q.via,
                       'proof', q.proof, 'sec', q.sec_flavour, 'sid', i.sid,
                       'client_addr', host(q.client_addr)) ORDER BY q.requester_id)
                  FROM catalog.recall_requester q
                  LEFT JOIN catalog.identity i ON i.identity_id = q.identity_id
                 WHERE q.recall_id = r.recall_id), '[]') AS requesters
  FROM catalog.recall r WHERE r.file_id = %s ORDER BY r.recall_id DESC LIMIT 1
"""


def recall_cmd(a) -> None:
    """SHARE/NAME's latest recall (NAME may be a path through subfolders)."""
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        row = (path_row(c, a.share, a.name) if "/" in a.name else file_row(a.share, a.name)) or {}
        r = c.execute(RECALL_SQL, (row.get("file_id"),)).fetchone() if row else None
    out(r or {})


def acl_cmd(a) -> None:
    """The catalog's ACL for SHARE/NAME (what the edge's scans reported)."""
    row = file_row(a.share, a.name) or {}
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        r = c.execute(
            "SELECT a.acl_id, encode(a.sha256, 'hex') AS sha256, a.entries, a.read_sids"
            " FROM catalog.file f JOIN catalog.acl a ON a.acl_id = f.acl_id"
            " WHERE f.file_id = %s",
            (row.get("file_id"),),
        ).fetchone()
    out(r or {})


def advance_cmd(a) -> None:
    """Move the file's active recall on, as the worker would after a restore."""
    row = file_row(a.share, a.name)
    with db() as c:
        n = c.execute(
            "UPDATE catalog.recall SET state = %s WHERE recall_id = ("
            " SELECT recall_id FROM catalog.recall WHERE file_id = %s"
            " AND state IN ('requested', 'rehydrating', 'copying', 'downloading')"
            " ORDER BY recall_id DESC LIMIT 1)",
            (a.state, row["file_id"]),
        ).rowcount
    out({"file_id": row["file_id"], "state": a.state, "updated": n})


def flood_cmd(a) -> None:
    """N threads each open PATH and read it whole; every reader blocks in the
    kernel (a held pre-content event) until the bytes are there. Reports how
    many read exactly SOURCE's bytes, how many saw zeros where SOURCE has
    data, and how many failed."""
    import threading

    # When the fill lands, thousands of readers leave the kernel at once and
    # all want the one GIL. At CPython's default 5 ms switch interval every
    # waiter also wakes 200 times a second to ask for it: a futex storm. On a
    # 2-vCPU GitHub runner 5,000 released readers spent 82% of the CPU in the
    # kernel and only 49 finished in 11 minutes, although the daemon had
    # answered every held read (held_now=0). Reproduced in a 2-CPU container
    # (5,000 readers released together from a pipe): 425 s, 670 s of system
    # time; with a 0.5 s interval, 28 s. The readers hand the GIL over at
    # every read() anyway; the long interval only stops the timed wake-ups.
    sys.setswitchinterval(0.5)
    want = Path(a.source).read_bytes()
    res = {"identical": 0, "zeros": 0, "mismatch": 0, "errors": 0, "unfinished": 0}
    lock = threading.Lock()
    t0 = time.monotonic()
    times: list[float] = []

    def reader() -> None:
        # Streamed in 64 KiB reads: thousands of readers must not each hold
        # the whole file.
        step = 64 * 1024
        k = "identical"
        try:
            with open(a.path, "rb", buffering=0) as fh:
                off = 0
                while True:
                    got = fh.read(step)
                    exp = want[off : off + step]
                    if not got:
                        if exp:
                            k = "mismatch"
                        break
                    if got != exp:
                        zero = any(
                            got[i : i + BLOCK] == bytes(len(got[i : i + BLOCK]))
                            and exp[i : i + BLOCK] != got[i : i + BLOCK]
                            for i in range(0, len(got), BLOCK)
                        )
                        k = "zeros" if zero else "mismatch"
                        break
                    off += len(got)
        except OSError:
            k = "errors"
        with lock:
            res[k] += 1
            times.append(time.monotonic() - t0)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(a.n)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + a.timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    res["unfinished"] = sum(t.is_alive() for t in threads)
    res["started"] = a.n
    res["seconds"] = round(time.monotonic() - t0, 2)
    res["first_done_s"] = round(min(times), 2) if times else None
    res["last_done_s"] = round(max(times), 2) if times else None
    out(res)


def versions_cmd(a) -> None:
    """Committed content versions of SHARE/NAME, with the edge op that made
    each, its size and its CRC-64/NVME."""
    row = file_row(a.share, a.name)
    if not row:
        out({"versions": []})
        return
    with db() as c:
        rows = c.execute(
            "SELECT version_id, edge_op_id, size, encode(crc64nvme, 'hex')"
            " FROM catalog.file_version"
            " WHERE file_id = %s AND kind = 'content' AND state = 'committed' ORDER BY version_id",
            (row["file_id"],),
        ).fetchall()
    out(
        {
            "versions": [
                {"version_id": v, "edge_op_id": o, "size": n, "crc64nvme": crc}
                for v, o, n, crc in rows
            ]
        }
    )


def sas_url(container: str, key: str, version: str | None = None) -> str:
    from omnifile_service.api.dataplane import SimHttpDataPlane, Target

    plane = SimHttpDataPlane.from_key_file(SIM_URL, SIM_KEY)
    return plane.url(
        Target(0, container, key, version),
        "r",
        datetime.now(UTC) + timedelta(minutes=10),
        version=bool(version),
    )


def blob_cmd(a) -> None:
    row = file_row(a.share, a.name)
    if not row or not row.get("object_key"):
        out({"exists": False})
        return
    url = sas_url(row["container"], row["object_key"])
    h = hashlib.sha256()
    from omnifile_service.backends.sim import crc64nvme

    c = 0
    size = 0
    with (
        httpx.Client(timeout=120, trust_env=False) as http,
        http.stream("GET", url, headers={"x-ms-version": "2026-04-06"}) as r,
    ):
        if r.status_code == 404:
            out({"exists": False, "status": 404})
            return
        r.raise_for_status()
        meta = {
            k[len("x-ms-meta-") :]: v for k, v in r.headers.items() if k.startswith("x-ms-meta-")
        }
        tier = r.headers.get("x-ms-access-tier")
        tier_inferred = r.headers.get("x-ms-access-tier-inferred")
        for chunk in r.iter_bytes(8 * MiB):
            h.update(chunk)
            c = crc64nvme(chunk, c)
            size += len(chunk)
    out(
        {
            "exists": True,
            "size": size,
            "crc64nvme": f"{c:016x}",
            "sha256": h.hexdigest(),
            "metadata": meta,
            "access_tier": tier,
            "access_tier_inferred": tier_inferred,
            "object_key": row["object_key"],
        }
    )


# --- local files ------------------------------------------------------------------

MARKER = struct.Struct("<IBBHQQQQQQB3x")
MAGIC = 0x31464D4F


def marker_of(path: str) -> dict | None:
    try:
        raw = os.getxattr(path, "trusted.omnifile")
    except OSError:
        return None
    magic, _layout, state, flags, fid, vid, size, resident, _seq, share, cs = MARKER.unpack(
        raw[: MARKER.size]
    )
    return {
        "valid": magic == MAGIC,
        "state": {1: "RESIDENT", 2: "PARTIAL"}.get(state, state),
        "flags": flags,
        "online": bool(flags & 1),
        "file_id": fid,
        "version_id": vid,
        "size": size,
        "resident": resident,
        "share_id": share,
        "chunk_map": bool(flags & 16),
        "chunk_shift": cs,
    }


def chunks_mapped(path: str) -> int | None:
    """How many chunks the placeholder's chunk map (`trusted.omnifile.map`,
    one bit per chunk) says are local, None without a map."""
    try:
        raw = os.getxattr(path, "trusted.omnifile.map")
    except OSError:
        return None
    return sum(bin(b).count("1") for b in raw)


def fetched_to(path: str) -> int | None:
    """How far an S3 fill wrote unverified bytes (`trusted.omnifile.fetched`:
    version id, then offset, little-endian), None when it records nothing."""
    try:
        raw = os.getxattr(path, "trusted.omnifile.fetched")
    except OSError:
        return None
    return int.from_bytes(raw[8:16], "little")


def first_hole(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_HOLE)
    finally:
        os.close(fd)


def data_after(path: str, offset: int) -> int | None:
    """The first data byte at or after `offset` (SEEK_DATA), None if only
    holes follow up to EOF."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, offset, os.SEEK_DATA)
    except OSError:
        return None
    finally:
        os.close(fd)


def local_cmd(a) -> None:
    st = os.stat(a.path)
    r = {
        "size": st.st_size,
        "allocated": st.st_blocks * 512,
        "marker": marker_of(a.path),
        "fetched_to": fetched_to(a.path),
        "chunks_mapped": chunks_mapped(a.path),
        "inode": st.st_ino,
        "first_hole": (h := first_hole(a.path)),
        "data_after_hole": data_after(a.path, h) if h < st.st_size else None,
        "uid": st.st_uid,
        "gid": st.st_gid,
        "mode": f"{st.st_mode & 0o7777:04o}",
    }
    if a.hash:
        r["crc64nvme"] = crc64(a.path)
        r["sha256"] = sha256_file(a.path)
    out(r)


def pattern_cmd(a) -> None:
    """Pseudo-random, seeded: every 4 KiB block differs from every other and
    from zeros, so a misplaced or zero-filled block is always caught."""
    rng = random.Random(a.seed)
    left = a.size
    with open(a.path, "wb") as fh:
        while left:
            n = min(left, 8 * MiB)
            fh.write(rng.randbytes(n))
            left -= n
    out(
        {
            "path": a.path,
            "size": a.size,
            "sha256": sha256_file(a.path),
            "crc64nvme": crc64(a.path),
        }
    )


def compare_cmd(a) -> None:
    """Read PATH (through NFS) once, in order, and compare with SOURCE."""
    zero = bytes(BLOCK)
    t0 = time.monotonic()
    mismatched_blocks = 0
    zero_blocks = 0
    first_bad = None
    n = 0
    error = None
    try:
        with open(a.path, "rb", buffering=0) as got, open(a.source, "rb") as src:
            while True:
                want = src.read(MiB)
                have = b""
                while len(have) < len(want):
                    piece = got.read(len(want) - len(have))
                    if not piece:
                        break
                    have += piece
                if have != want:
                    for i in range(0, max(len(want), len(have)), BLOCK):
                        w, h = want[i : i + BLOCK], have[i : i + BLOCK]
                        if w != h:
                            mismatched_blocks += 1
                            if h == zero[: len(h)] and w != zero[: len(w)]:
                                zero_blocks += 1
                            if first_bad is None:
                                first_bad = n + i
                n += len(have)
                if not want:
                    if got.read(1):
                        error = "longer than the source"
                    break
                if len(have) < len(want):
                    error = f"short read at {n}"
                    break
    except OSError as e:
        error = f"{type(e).__name__}: errno {e.errno} ({os.strerror(e.errno or 0)}) at {n}"
    out(
        {
            "identical": error is None and mismatched_blocks == 0,
            "bytes_read": n,
            "mismatched_blocks": mismatched_blocks,
            "zero_blocks": zero_blocks,
            "first_bad_offset": first_bad,
            "error": error,
            "seconds": round(time.monotonic() - t0, 3),
        }
    )


def fault_cmd(a) -> None:
    r = httpx.post(f"{SIM_URL}/_sim/faults", json=json.loads(a.body), trust_env=False)
    out({"status": r.status_code, "body": r.json()})


def s3_served_cmd(a) -> None:
    out(httpx.get(f"{SIM_URL}/_sim/s3-served", trust_env=False).json())


def s3_requests_cmd(a) -> None:
    """Requests to the S3 sim per bucket: OmniFile's and this harness's apart."""
    out(httpx.get(f"{SIM_URL}/_sim/s3-requests", trust_env=False).json())


def ranges_cmd(a) -> None:
    """SHA-256 of each OFFSET:LENGTH range of the SIZE seeded bytes s3-put makes."""
    data = random.Random(a.seed).randbytes(a.size)
    res = []
    for r in a.ranges.split(","):
        off, n = (int(x) for x in r.split(":"))
        res.append(hashlib.sha256(data[off : off + n]).hexdigest())
    out({"sha256": res})


def identity_cmd(a) -> None:
    """The directory entry for uid UID (with SID, winbind's when the edge is
    joined), as the identity sync writes one from AD, unless the catalog has
    one. The service names who made a change from it: by the SID the edge
    reports (winbind), or by the uid alone on a stack without winbind. A SID
    of a domain the catalog doesn't know is never added from an edge's
    report (`api/recalls.py` `check_new_sid`)."""
    with db() as c:
        row = c.execute(
            "SELECT identity_id FROM catalog.identity WHERE sid = %s"
            " OR (kind = 'user' AND posix_id = %s) ORDER BY (sid = %s) DESC LIMIT 1",
            (a.sid, a.uid, a.sid),
        ).fetchone()
        if row is None:
            row = c.execute(
                "INSERT INTO catalog.identity (sid, kind, sam_account_name, posix_id,"
                " winbind_resolved_at) VALUES (%s, 'user', %s, %s, now()) RETURNING identity_id",
                (a.sid, a.name, a.uid),
            ).fetchone()
    out({"identity_id": row[0], "uid": a.uid, "sid": a.sid})


def follow_cmd(a) -> None:
    """Follow BUCKET's change feed on the S3 backend from now, as registering
    a source does on S3 (`S3Backend.change_cursor_at`); the sim has no cursor
    at a time, and a listing's snapshot is now anyway. The migration engine
    cuts a prefix over only once its bucket's feed has been read past the
    copy's start."""
    with db() as c:
        (backend_id,) = c.execute(
            "SELECT backend_id FROM catalog.backend WHERE name = 'e2e-s3'"
        ).fetchone()
        c.execute(
            "INSERT INTO catalog.changefeed_cursor (backend_id, container) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (backend_id, a.bucket),
        )
    out({"backend_id": backend_id, "container": a.bucket})


def migrate_cmd(a) -> None:
    """POST /v1/locations as the partner API runs it (`request_location`):
    PREFIX of SHARE's dataset moves to the dataset's own backend, copied by
    the worker's copier (OMNIFILE_STORAGE_MOVER=sim) and cut over once
    verified."""
    import asyncio

    from omnifile_service.migrate import request_location
    from omnifile_service.web.context import tuple_rows

    async def run() -> int:
        url = os.environ["OMNIFILE_DATABASE_URL"]
        async with await psycopg.AsyncConnection.connect(
            url, row_factory=psycopg.rows.dict_row
        ) as c:
            ds = await (
                await c.execute(
                    "SELECT d.dataset_id, d.backend_id FROM catalog.share s"
                    " JOIN catalog.dataset d USING (dataset_id) WHERE s.name = %s",
                    (a.share,),
                )
            ).fetchone()
            with tuple_rows(c):
                mid = await request_location(
                    c,
                    dataset_id=ds["dataset_id"],
                    prefix=a.prefix,
                    to_backend_id=ds["backend_id"],
                    copy_by="omnifile",
                    requested_by="app:e2e",
                )
            await c.commit()
            return mid

    out({"migration_id": asyncio.run(run())})


def migration_cmd(a) -> None:
    """A migration prefix: its state and counts, its copier runs, its staging
    objects and their runs."""
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        m = c.execute(
            "SELECT migration_id, state, prefix, files_total, files_verified, last_run_id,"
            " run_state, run_counters, last_error, last_copy_started_at, cut_over_at"
            " FROM catalog.migration_prefix WHERE migration_id = %s",
            (a.migration_id,),
        ).fetchone()
        staging = c.execute(
            "SELECT o.location_id, l.object_key AS source_key, o.bucket, o.object_key, o.state,"
            " o.restore_id, r.triggered_by, r.migrate_on_complete, o.staging_run_id,"
            " sr.state AS run_state, sr.counters AS run_counters"
            " FROM catalog.staging_object o JOIN catalog.location l USING (location_id)"
            " LEFT JOIN catalog.restore r ON r.restore_id = o.restore_id"
            " LEFT JOIN catalog.staging_run sr ON sr.staging_run_id = o.staging_run_id"
            " WHERE o.migration_id = %s ORDER BY o.location_id",
            (a.migration_id,),
        ).fetchall()
    out({**(m or {}), "staging": staging})


def copies_cmd(a) -> None:
    """Every location of SHARE/PATH's current content: where its bytes are
    (role, backend, verified, by what), after a migration's flip."""
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        row = path_row(c, a.share, a.path)
        if row is None:
            out({})
            return
        locs = c.execute(
            "SELECT location_id, backend_id, role, state, container, object_key, verified,"
            " verified_by FROM catalog.location WHERE version_id = %s ORDER BY location_id",
            (row["content_version_id"],),
        ).fetchall()
    out({"file_id": row["file_id"], "locations": locs})


# --- M5: S3-resident files -------------------------------------------------------


def s3_rpc(op: str, *args, **kwargs):
    """One control call on the facade's S3 sim (`POST /_sim/backend`)."""
    from omnifile_service.backends.sim_http import rpc

    body = {
        "backend": "s3",
        # The harness's own requests, counted apart from OmniFile's (decision 40).
        "harness": True,
        "op": op,
        "args": [rpc.encode(x) for x in args],
        "kwargs": {k: rpc.encode(v) for k, v in kwargs.items()},
    }
    r = httpx.post(f"{SIM_URL}/_sim/backend", json=body, trust_env=False, timeout=60)
    r.raise_for_status()
    got = r.json()
    if "error" in got:
        raise SystemExit(f"s3 sim {op}: {got['error']}")
    return rpc.decode(got.get("result"))


def s3_setup_cmd(a) -> None:
    """The S3 side of a migration: a catalog backend for the facade's S3 sim
    (the api mints presigned URLs for it, the worker lists it over RPC), and
    its bucket. Seeded files take the dataset's POSIX defaults."""
    with db() as c:
        (backend_id,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES ('e2e-s3', 'sim', %s, false) RETURNING backend_id",
            (
                json.dumps(
                    {
                        "data_plane": "sim_http",
                        "flavour": "s3",
                        "url": SIM_URL,
                        "key_file": SIM_KEY,
                        "buckets": [S3_BUCKET, S3_BUCKET2, S3_MIG, S3_OPS],
                        # Urgent recalls of a migrating prefix are staged here.
                        "staging_bucket": S3_OPS,
                    }
                ),
            ),
        ).fetchone()
        (dataset_id,) = c.execute(
            "UPDATE catalog.dataset d SET posix_defaults = %s FROM catalog.share s"
            " WHERE s.dataset_id = d.dataset_id AND s.name = %s RETURNING d.dataset_id",
            (json.dumps({"uid": a.uid, "gid": a.gid}), a.share),
        ).fetchone()
    bucket = s3_rpc("create_container", S3_BUCKET)
    for b in (S3_BUCKET2, S3_MIG, S3_OPS):
        s3_rpc("create_container", b)
    out({"backend_id": backend_id, "dataset_id": dataset_id, "bucket": bucket})


def s3_put_cmd(a) -> None:
    from omnifile_service.backends.base import LogicalTier

    data = random.Random(a.seed).randbytes(a.size)
    tier = LogicalTier.ARCHIVE if a.archive else None
    ref = s3_rpc("put_bytes", a.bucket, a.key, data, tier)
    out(
        {
            "key": a.key,
            "version_id": ref.version_id,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "crc64nvme": crc64(data),
        }
    )


def locations_cmd(a) -> None:
    """Every copy of SHARE/NAME's current content the catalog knows of."""
    row = file_row(a.share, a.name) or {}
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        rows = c.execute(
            "SELECT l.backend_id, l.role, l.state, l.container, l.object_key,"
            " encode(l.crc64nvme, 'hex') AS crc64nvme"
            " FROM catalog.location l WHERE l.version_id = %s ORDER BY l.location_id",
            (row.get("content_version_id"),),
        ).fetchall()
    out({"file_id": row.get("file_id"), "locations": rows})


def rewind_cmd(a) -> None:
    with db() as c, c.transaction():
        # Rewind depth changes are audited, and need an actor.
        c.execute("SELECT set_config('omnifile.actor', 'e2e:m5', true)")
        n = c.execute(
            "UPDATE catalog.share SET rewind_depth_max_days = %s, rewind_depth_days = %s"
            " WHERE name = %s",
            (a.days, a.days, a.share),
        ).rowcount
    out({"share": a.share, "rewind_depth_days": a.days, "updated": n})


def backdate_cmd(a) -> None:
    """Move SHARE's version history into the past, as one block, so that its
    newest version was committed HOURS ago (database time; the service's
    clock may run ahead of it). Each version's validity (`committed_at`
    until `superseded_at`) moves by the same amount, so versions still
    follow one another. Setting every `committed_at` to one time instead
    would leave a superseded version valid until its successor's real
    commit, and an as-of listing would hold both (a file's create, then its
    content, are two versions since migration 0030)."""
    with db() as c:
        n = c.execute(
            "WITH h AS (SELECT v.version_id, v.committed_at FROM catalog.file_version v"
            "             JOIN catalog.file f USING (file_id) JOIN catalog.share s"
            "               ON s.dataset_id = f.dataset_id"
            "            WHERE s.name = %(share)s AND v.state = 'committed'),"
            "     d AS (SELECT max(committed_at) - (now() - make_interval(hours => %(h)s))"
            "                  AS delta FROM h)"
            " UPDATE catalog.file_version v"
            "    SET committed_at = v.committed_at - d.delta,"
            "        superseded_at = v.superseded_at - d.delta"
            "   FROM h, d WHERE v.version_id = h.version_id",
            {"share": a.share, "h": a.hours},
        ).rowcount
    out({"share": a.share, "versions": n})


def outbox_cmd(a) -> None:
    """The edge replica's outbox, by state and op (step 10: no namespace op left behind)."""
    import sqlite3

    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rows = c.execute("SELECT state, op, count(*) FROM outbox GROUP BY state, op").fetchall()
    out({f"{st}:{op}": n for st, op, n in rows})


def marker_cmd(a) -> None:
    out(marker_of(a.path) or {})


def source_cmd(a) -> None:
    with db() as c:
        c.row_factory = psycopg.rows.dict_row
        r = c.execute(
            "SELECT source_id, state, objects_seen, files_created, skipped, last_error"
            " FROM catalog.source WHERE source_id = %s",
            (a.source_id,),
        ).fetchone()
    out(r or {})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="e2e.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reset-db").set_defaults(fn=reset_db)
    s = sub.add_parser("seed")
    s.add_argument("--enrol-url", required=True)
    s.add_argument("--server-ca", required=True)
    s.add_argument("--bundle-out", required=True)
    s.add_argument("--uid", type=int, default=20002)
    s.add_argument("--gid", type=int, default=10000)
    s.add_argument("--m2", action="store_true")
    s.add_argument("--owners-sid")
    s.add_argument("--writers-sid")
    s.add_argument("--readers-sid")
    s.add_argument("--nfs-client", action="append")
    s.set_defaults(fn=seed)
    sub.add_parser("shares").set_defaults(fn=shares_cmd)
    for name, fn in (
        ("file", file_cmd),
        ("blob", blob_cmd),
        ("versions", versions_cmd),
        ("archive", archive_cmd),
        ("recall", recall_cmd),
        ("acl", acl_cmd),
        ("locations", locations_cmd),
    ):
        s = sub.add_parser(name)
        s.add_argument("share")
        s.add_argument("name")
        s.set_defaults(fn=fn)
    s = sub.add_parser("local")
    s.add_argument("path")
    s.add_argument("--hash", action="store_true")
    s.set_defaults(fn=local_cmd)
    s = sub.add_parser("pattern")
    s.add_argument("path")
    s.add_argument("size", type=int)
    s.add_argument("seed", type=int)
    s.set_defaults(fn=pattern_cmd)
    s = sub.add_parser("compare")
    s.add_argument("path")
    s.add_argument("source")
    s.set_defaults(fn=compare_cmd)
    s = sub.add_parser("advance")
    s.add_argument("share")
    s.add_argument("name")
    s.add_argument("state")
    s.set_defaults(fn=advance_cmd)
    s = sub.add_parser("flood")
    s.add_argument("path")
    s.add_argument("source")
    s.add_argument("n", type=int)
    s.add_argument("timeout", type=float)
    s.set_defaults(fn=flood_cmd)
    s = sub.add_parser("fault")
    s.add_argument("body")
    s.set_defaults(fn=fault_cmd)
    s = sub.add_parser("s3-served")
    s.set_defaults(fn=s3_served_cmd)
    s = sub.add_parser("s3-requests")
    s.set_defaults(fn=s3_requests_cmd)
    s = sub.add_parser("ranges")
    s.add_argument("size", type=int)
    s.add_argument("seed", type=int)
    s.add_argument("ranges")
    s.set_defaults(fn=ranges_cmd)
    s = sub.add_parser("identity")
    s.add_argument("uid", type=int)
    s.add_argument("--sid", default="S-1-5-21-1000-2000-3000-1109")
    s.add_argument("--name", default="e2e-writer")
    s.set_defaults(fn=identity_cmd)
    s = sub.add_parser("follow")
    s.add_argument("bucket")
    s.set_defaults(fn=follow_cmd)
    s = sub.add_parser("migrate")
    s.add_argument("share")
    s.add_argument("prefix")
    s.set_defaults(fn=migrate_cmd)
    s = sub.add_parser("migration")
    s.add_argument("migration_id", type=int)
    s.set_defaults(fn=migration_cmd)
    s = sub.add_parser("copies")
    s.add_argument("share")
    s.add_argument("path")
    s.set_defaults(fn=copies_cmd)
    s = sub.add_parser("s3-setup")
    s.add_argument("share")
    s.add_argument("--uid", type=int, default=20002)
    s.add_argument("--gid", type=int, default=10000)
    s.set_defaults(fn=s3_setup_cmd)
    s = sub.add_parser("s3-put")
    s.add_argument("key")
    s.add_argument("size", type=int)
    s.add_argument("seed", type=int)
    s.add_argument("--archive", action="store_true")
    s.add_argument("--bucket", default=S3_BUCKET)
    s.set_defaults(fn=s3_put_cmd)
    s = sub.add_parser("rewind")
    s.add_argument("share")
    s.add_argument("days", type=int)
    s.set_defaults(fn=rewind_cmd)
    s = sub.add_parser("backdate")
    s.add_argument("share")
    s.add_argument("hours", type=int)
    s.set_defaults(fn=backdate_cmd)
    s = sub.add_parser("path")
    s.add_argument("share")
    s.add_argument("path")
    s.set_defaults(fn=path_cmd)
    s = sub.add_parser("actors")
    s.add_argument("file_id", type=int)
    s.set_defaults(fn=actors_cmd)
    s = sub.add_parser("s3-seed-one")
    s.add_argument("source_id", type=int)
    s.add_argument("key")
    s.add_argument("size", type=int)
    s.set_defaults(fn=s3_seed_one_cmd)
    s = sub.add_parser("cutover-report")
    s.add_argument("share")
    s.add_argument("prefix")
    s.set_defaults(fn=cutover_report_cmd)
    s = sub.add_parser("outbox")
    s.add_argument("--db", default="/var/lib/omnifile/replica.db")
    s.set_defaults(fn=outbox_cmd)
    s = sub.add_parser("marker")
    s.add_argument("path")
    s.set_defaults(fn=marker_cmd)
    s = sub.add_parser("source")
    s.add_argument("source_id", type=int)
    s.set_defaults(fn=source_cmd)
    s = sub.add_parser("add-share")
    s.add_argument("name")
    s.add_argument("--uid", type=int, default=0)
    s.add_argument("--gid", type=int, default=0)
    s.set_defaults(fn=add_share_cmd)
    s = sub.add_parser("remove-share")
    s.add_argument("name")
    s.set_defaults(fn=remove_share_cmd)
    a = p.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
