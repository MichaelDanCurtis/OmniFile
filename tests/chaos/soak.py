#!/opt/omnifile-service/venv/bin/python
"""The chaos soak (docs/BUILD-PLAN.md M7, "Chaos tests"): random faults for hours while
load runs, with the invariants checked as it goes and once more at the end. Run by
tests/chaos/soak.sh, which sets up the stack; as root on the dev edge VM.

Faults, each on a schedule drawn from --seed (every type --each times at least):
  kill_edge         SIGKILL the edge daemon (systemd restarts it; Ganesha and smbd stay)
  kill_ganesha      SIGKILL Ganesha (systemd restarts it; NFS clients ride grace)
  kill_smbd         SIGKILL smbd (Ubuntu's unit has no Restart=: started again after a hold)
  kill_worker       SIGKILL the service worker (systemd restarts it)
  crash_postgres    SIGKILL the whole cluster, started again after a hold; or SIGKILL one
                    backend, after which Postgres restarts every backend itself (crash recovery)
  fill_disk         fallocate the data volume's free space, hold, release
  drop_network      drop every packet between the edge daemon and the service and the sim;
                    half the time the flows open during the drop stay dead afterwards
  restart_mid_fill  restart the daemon (SIGTERM) while it fills a large stub, readers waiting

Load, throughout: writes over NFSv3/4.1/4.2 and SMB (create, overwrite, delete); reads
of stubs over NFSv3, 4.1, 4.2 (O_DIRECT) and SMB; files archived (the catalog moves their
copy to Archive, as M2's tests do) so reads of them are recalls.

Invariants:
  zeros             no read returns a block of zeros where the file has data
  wrong_bytes       no read returns other wrong bytes, or a wrong length
  enoent            a file that exists (archived or not) is never ENOENT, nor missing
                    from its folder's listing
  lost_commit       every write that returned success is committed in the catalog with
                    its CRC-64/NVME, and the backend's bytes have its SHA-256; a deleted
                    file stays deleted
  recall            every recall reaches `available`: none failed, none stuck. One whose
                    file was replaced or deleted meanwhile ends `superseded` instead,
                    and only then: the file must really hold another version, or be gone
  recovery          every unit is active again within 180 s of a fault
Errors (EIO, timeouts, ENOSPC while the disk is full, an SMB error status) are allowed:
failing closed is the design. They are counted, by fault.

Replay: the fault schedule and every load decision come from random.Random(f"{seed}:{name}");
`--seed N` with the same --duration and --each replays the run's schedule and choices
(timing between threads still varies).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "tests" / "e2e"))
import e2e  # noqa: E402  (the e2e harness: catalog, sim SAS, markers)
import httpx  # noqa: E402
from omnifile_service.backends.sim.crc64 import crc64nvme  # noqa: E402

FAULTS = ["kill_edge", "kill_ganesha", "kill_smbd", "kill_worker", "crash_postgres",
          "fill_disk", "drop_network", "restart_mid_fill"]
UNITS = ["omnifile-edge", "nfs-ganesha-omni", "smbd", "omnifile-dev-worker", "omnifile-dev-api",
         "postgresql@17-main"]
DATA = "/srv/omnifile/data"
MNT = "/mnt/omnifile-soak"
IO = "/var/tmp/omnifile-soak/soak_io.py"
WUID, RUID, GID = 20002, 20002, 10000
SMB_URL = "//edge1.omni.test/smbwait"
SIM = "http://127.0.0.1:10000"
STOP_FILE = "/run/omnifile-soak.stop"
SMBGET = "/var/tmp/omnifile-soak/smbget"  # where SMB reads land, one file per reader
KiB, MiB = 1 << 10, 1 << 20
BIG = 96 * MiB
NFS_VERS = ["v3", "v41", "v42"]
SETTLED_S = 90  # a write this old must be in its folder's listing


SMB_NOT_FOUND = ("NT_STATUS_OBJECT_NAME_NOT_FOUND", "NT_STATUS_OBJECT_PATH_NOT_FOUND",
                 "NT_STATUS_NO_SUCH_FILE")


def judge_read(r: dict, changed: bool) -> str:
    """ok, zeros, wrong_bytes, enoent, error (allowed: failing closed) or discarded (the
    file was rewritten during the read). Zeros and wrong bytes count even when the read
    then failed: soak_io reports the blocks it saw before the error."""
    if r.get("zero_blocks"):
        return "zeros"
    if changed:
        return "discarded"
    if r.get("ok"):
        return "ok"
    if r.get("bad_blocks"):
        return "wrong_bytes"
    if r.get("name") == "ENOENT":
        return "enoent"
    if r.get("name") is None and ("short_at" in r or r.get("longer")):
        return "wrong_bytes"  # a clean read of the wrong length
    return "error"


# What a run must exercise, per protocol, to be GREEN: NFS (v3, v4.1 and v4.2 together)
# and SMB each, so a run whose SMB reads all fail cannot pass on NFS alone. Recalls are
# counted by the catalog, whichever protocol's read started them.
PROTOS = ("nfs", "smb")
MINIMUM_KINDS = ("write_ok", "reads", "unfilled", "archived", "listings")
MINIMUM_KEYS = tuple(f"{p}.{k}" for p in PROTOS for k in MINIMUM_KINDS) + ("recalls",)
DEFAULT_MINIMUMS = ("nfs.write_ok=50,nfs.reads=100,nfs.unfilled=20,nfs.archived=10,nfs.listings=10,"
                    "smb.write_ok=20,smb.reads=50,smb.unfilled=10,smb.archived=10,smb.listings=5,"
                    "recalls=5")


def parse_minimums(spec: str) -> dict[str, int]:
    """KEY=N,... with KEY one of MINIMUM_KEYS and N >= 0. Raises ValueError on anything
    else, so a bad value fails at the start of a run, not in its report hours later."""
    out = {}
    for item in (x.strip() for x in spec.split(",")):
        if not item:
            continue
        k, sep, v = item.partition("=")
        if not sep or k not in MINIMUM_KEYS:
            raise ValueError(f"unknown minimum {item!r}; known: {', '.join(MINIMUM_KEYS)}")
        if not v.isdigit():
            raise ValueError(f"minimum {k} must be a whole number >= 0, not {v!r}")
        out[k] = int(v)
    return out


def proto_of(via: str) -> str:
    """The protocol of a count's VIA: smb, or nfs for v3, v41, v42 and nfs-v3 and so on."""
    return "smb" if via == "smb" else "nfs"


def load_counts(counts) -> dict[str, int]:
    """The counts the minimums are judged on, per protocol."""
    got = {k: 0 for k in MINIMUM_KEYS}
    for k, v in counts.items():
        parts = k.split(":")
        if parts[0] == "write_ok" and len(parts) == 2:
            got[f"{proto_of(parts[1])}.write_ok"] += v
        elif parts[0] == "read_ok" and len(parts) == 3:
            p, kind = proto_of(parts[1]), parts[2]
            if kind in ("filled", "unfilled"):  # archived reads are counted in these too
                got[f"{p}.reads"] += v
            if kind in ("unfilled", "archived"):
                got[f"{p}.{kind}"] += v
        elif parts[0] == "listings" and len(parts) == 2:
            got[f"{parts[1]}.listings"] += v
    got["recalls"] = counts.get("recalls_seen", 0)
    return got


def shortfalls(counts, minimums: dict[str, int]) -> list[str]:
    """What the run exercised too little of: a run that reads nothing proves nothing."""
    got = load_counts(counts)
    return [f"{k} {got[k]} < {m}" for k, m in minimums.items() if got[k] < m]


def green(violations: list, aborted: bool, enough: bool, short: list[str]) -> bool:
    return not violations and not aborted and enough and not short


def replay_cmd(a, seed: int) -> str:
    """Everything the schedule and the load's choices depend on."""
    return (f"sudo tests/chaos/soak.sh --seed {seed} --duration {a.duration} --each {a.each}"
            f" --warmup {a.warmup} --cooldown {a.cooldown} --files {a.files} --big {a.big}")


# --- evidence kept when the run ends ------------------------------------------------------
# tests/chaos/storage.sh keeps Postgres and the journal in RAM for the run, so both are gone
# once the VM stops: the 2026-11-08 regression's one violation had no log line left to read.
# So the run saves, in its results folder, before soak.sh stops anything: the units' journal
# for the whole run, and the catalog's rows for every violation's recall and file.

EVIDENCE_UNITS = ("omnifile-edge", "omnifile-dev-worker", "omnifile-dev-api", "omnifile-dev-sim",
                  "omnifile-dev-sim-worm", "nfs-ganesha-omni", "smbd", "postgresql@17-main")

#: Table -> the query for it; `%(recalls)s` and `%(files)s` are the violations' ids.
EVIDENCE_SQL = {
    "recall": "SELECT * FROM catalog.recall WHERE recall_id = ANY(%(recalls)s)"
              " OR file_id = ANY(%(files)s) ORDER BY recall_id",
    "recall_requester": "SELECT * FROM catalog.recall_requester WHERE recall_id = ANY(%(recalls)s)",
    "file": "SELECT * FROM catalog.file WHERE file_id = ANY(%(files)s)",
    "file_version": "SELECT * FROM catalog.file_version WHERE file_id = ANY(%(files)s)"
                    " ORDER BY version_id",
    "location": "SELECT l.* FROM catalog.location l JOIN catalog.file_version v"
                " ON v.version_id = l.version_id WHERE v.file_id = ANY(%(files)s)"
                " ORDER BY l.location_id",
    "audit_log": "SELECT * FROM catalog.audit_log WHERE file_id = ANY(%(files)s) ORDER BY at",
}


def journal_cmd(since: float) -> list[str]:
    cmd = ["journalctl", "--no-pager", "-o", "short-iso-precise", "--since", f"@{int(since)}"]
    for u in EVIDENCE_UNITS:
        cmd += ["-u", u]
    return cmd


def save_journal(out: Path, since: float, popen=subprocess.Popen) -> Path:
    """The units' journal since `since`, gzipped, as `journal.log.gz` in `out`."""
    path = out / "journal.log.gz"
    with gzip.open(path, "wb") as gz:
        p = popen(journal_cmd(since), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
            gz.write(chunk)
        p.wait()
    return path


def violation_ids(violations: list[dict], recalls: dict, cat: dict) -> tuple[list[int], list[int]]:
    """The recall ids and file ids the violations name: a recall violation's recall and its
    file; any other violation's file, by its key, while the catalog still lists it."""
    rids, fids = set(), set()
    for v in violations:
        rid = v.get("recall_id")
        if rid is not None:
            rids.add(int(rid))
            if rid in recalls:
                fids.add(int(recalls[rid]["file_id"]))
        row = cat.get(v.get("key"))
        if row:
            fids.add(int(row["file_id"]))
    return sorted(rids), sorted(fids)


def dump_catalog(conn, recall_ids: list[int], file_ids: list[int]) -> dict:
    """Each EVIDENCE_SQL table's rows for these ids. A query that fails (a column renamed
    since) is recorded as its error, and the others still run."""
    out: dict = {"recall_ids": recall_ids, "file_ids": file_ids}
    for table, sql in EVIDENCE_SQL.items():
        try:
            cur = conn.execute(sql, {"recalls": recall_ids, "files": file_ids})
            cols = [c.name for c in cur.description]
            out[table] = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        except Exception as e:
            out[table] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return out


def judge_recall(state: str, file_live: bool, reason: str | None = None,
                 still_current: bool = False) -> str | None:
    """Why a recall at the end of a run is a violation, or None. A cancelled recall of a
    file that still exists was never finished; one of a file deleted since is fine.

    A `superseded` recall is fine when what it says happened did: `reason` is why it
    ended (`replaced` or `deleted`), and `still_current` whether the file still holds
    the version the recall was for. A recall superseded as deleted whose file is still
    there, or as replaced whose file still holds that version, was ended for nothing."""
    if state == "failed":
        return "failed"
    if state in ("requested", "rehydrating", "copying", "downloading"):
        return f"still {state}"
    if state == "cancelled" and file_live:
        return "cancelled, the file still there"
    if state == "superseded":
        if reason == "deleted":
            return "superseded as deleted, the file still there" if file_live else None
        if reason == "replaced":
            return "superseded as replaced, the file still holds that version" \
                if still_current else None
        return f"superseded for no known reason ({reason!r})"
    return None


def delete_outcome(was: str, ok: bool, err: str | None) -> str:
    """deleted, failed (unknown: the file leaves every check) or enoent (a violation: a
    file in `written` was not there). A file whose write failed may never have existed."""
    if ok:
        return "deleted"
    if err == "ENOENT":
        return "deleted" if was == "failed" else "enoent"
    return "failed"


def draw_size(rng: random.Random) -> int:
    """A stub keeps SEG-Y's headers and 1 MiB resident: 60% of files are larger than
    that, so reading them crosses into the unfilled region; the rest are small."""
    lo, hi = (1.25 * MiB, 6 * MiB) if rng.random() < 0.6 else (16 * KiB, MiB)
    return int(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def pattern(size: int, seed: int) -> bytes:
    rng = random.Random(seed)
    out = bytearray()
    while len(out) < size:
        out += rng.randbytes(min(MiB, size - len(out)))
    return bytes(out)


@dataclass
class Rec:
    share: str  # wait (NFS) or smbwait (SMB)
    rel: str  # path in the share
    size: int = 0
    seed: int = 0
    sha256: str = ""
    crc: str = ""
    state: str = "new"  # writing, written, failed, deleting, deleted
    t_written: float = 0.0
    readers: int = 0
    archived: bool = False
    big: bool = False
    gen: int = 0

    @property
    def key(self) -> str:
        return f"{self.share}/{self.rel}"

    @property
    def local(self) -> str:
        return f"{DATA}/{self.share}/{self.rel}"


class Soak:
    def __init__(self, a):
        self.a = a
        self.minimums = parse_minimums(a.minimums)  # main() has checked it already
        self.seed = a.seed
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.events = open(self.out / "events.jsonl", "a", buffering=1)
        self.lock = threading.RLock()
        self.files: dict[str, Rec] = {}
        self.cat: dict[str, dict] = {}
        self.recalls: dict[int, dict] = {}
        self.counts: Counter = Counter()
        self.by_fault: Counter = Counter()
        self.violations: list[dict] = []
        self.faults_done: list[dict] = []
        self.fault_active = "none"
        self.load_stop = threading.Event()
        self.stop = threading.Event()
        self.n = 0
        self.t0 = time.time()
        self.auth = {u: f"/root/chaos-auth/{u}" for u in ("geo-reader", "geo-writer")}
        self.restart_budget = 120.0

    # --- plumbing -------------------------------------------------------------------
    def rng(self, name: str) -> random.Random:
        return random.Random(f"{self.seed}:{name}")

    def ev(self, kind: str, **kw) -> None:
        kw.update(t=round(time.time(), 3), dt=round(time.time() - self.t0, 1), kind=kind,
                  during=self.fault_active)
        self.events.write(json.dumps(kw, default=str) + "\n")

    def violation(self, kind: str, **kw) -> None:
        with self.lock:
            self.counts[f"violation:{kind}"] += 1
            self.violations.append({"kind": kind, "dt": round(time.time() - self.t0, 1),
                                    "fault": self.fault_active, **kw})
        self.ev("VIOLATION", what=kind, **kw)
        print(f"[soak {time.time() - self.t0:7.0f}s] VIOLATION {kind}: {kw}", flush=True)

    def say(self, msg: str) -> None:
        print(f"[soak {time.time() - self.t0:7.0f}s] {msg}", flush=True)

    def sh(self, *cmd, timeout=120) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, "timeout"

    def io(self, uid: int, *args, timeout: float = 420) -> dict:
        cmd = ["setpriv", f"--reuid={uid}", f"--regid={GID}", "--clear-groups",
               "python3", IO, *[str(x) for x in args]]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "name": "TIMEOUT"}
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return {"ok": False, "name": "BADOUT", "out": (p.stdout + p.stderr)[-300:]}

    def smb(self, user: str, command: str, timeout: int = 420) -> tuple[int, str]:
        return self.sh("smbclient", SMB_URL, "-A", self.auth[user], "-t", str(timeout),
                       "-c", command, timeout=timeout + 30)

    def active(self, unit: str) -> bool:
        return self.sh("systemctl", "is-active", "-q", unit)[0] == 0

    def stopped(self) -> bool:
        return self.stop.is_set() or os.path.exists(STOP_FILE)

    def sleep(self, s: float) -> bool:
        """Sleep, waking early when the load stops. True if still running."""
        return not self.load_stop.wait(s) and not self.stopped()

    def nfs_path(self, ver: str, rel: str) -> str:
        return f"{MNT}/{ver}/{rel}"

    # --- the catalog's view ------------------------------------------------------------
    CAT_SQL = """
    WITH RECURSIVE t AS (
      SELECT f.file_id, f.name::text AS path FROM catalog.share s
        JOIN catalog.file f ON f.parent_id = s.root_dir_id AND f.name = 'soak' AND f.deleted_at IS NULL
       WHERE s.name = %s
      UNION ALL
      SELECT c.file_id, t.path || '/' || c.name FROM catalog.file c JOIN t ON c.parent_id = t.file_id
       WHERE c.deleted_at IS NULL)
    SELECT t.path, f.file_id, f.state, f.size, f.content_version_id, encode(f.crc64nvme, 'hex'),
           f.stub_version_id, l.logical_tier, l.container, l.object_key, l.backend_version_id
      FROM t JOIN catalog.file f USING (file_id)
      LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
     WHERE f.kind = 'file'
    """
    REC_SQL = """
    SELECT r.recall_id, r.file_id, r.state, extract(epoch FROM r.requested_at),
           extract(epoch FROM r.updated_at), r.failure, r.version_id,
           to_jsonb(r) ->> 'superseded_reason'
      FROM catalog.recall r WHERE r.requested_at > to_timestamp(%s)
    """

    def sync_catalog(self) -> bool:
        try:
            with e2e.db() as c:
                cat = {}
                for share in ("wait", "smbwait"):
                    for row in c.execute(self.CAT_SQL, (share,)).fetchall():
                        cat[f"{share}/{row[0]}"] = dict(zip(
                            ("file_id", "state", "size", "cv", "crc", "stub", "tier", "container",
                             "key", "bver"), row[1:]))
                recalls = {}
                for r in c.execute(self.REC_SQL, (self.t0 - 60,)).fetchall():
                    recalls[r[0]] = {"file_id": r[1], "state": r[2], "requested": float(r[3]),
                                     "updated": float(r[4]), "failure": r[5],
                                     "version_id": r[6], "reason": r[7]}
        except Exception as e:  # Postgres crashed or restarting: try again later
            self.counts["catalog_sync_error"] += 1
            self.ev("catalog_sync_error", error=str(e)[:200])
            return False
        with self.lock:
            self.cat = cat
            for rid, r in recalls.items():
                old = self.recalls.get(rid)
                if old is None:
                    self.counts["recalls_seen"] += 1
                if old is None or old["state"] != r["state"]:
                    self.ev("recall", recall_id=rid, **r)
                self.recalls[rid] = r
        return True

    def cat_row(self, rec: Rec) -> dict | None:
        return self.cat.get(rec.key)

    def committed_ok(self, rec: Rec) -> bool:
        row = self.cat_row(rec)
        return bool(row and row["cv"] and row["crc"] == rec.crc and row["size"] == rec.size)

    # --- load: writes -------------------------------------------------------------------
    def new_rec(self, share: str, rng: random.Random, big=False) -> Rec:
        with self.lock:
            self.n += 1
            n = self.n
        if big:
            rel = f"soak/big/b{n:03d}.bin"
        elif share == "wait":
            rel = f"soak/d{n % 8}/f{n:05d}.bin"
        else:
            rel = f"soak/s{n:05d}.bin"
        rec = Rec(share=share, rel=rel, big=big)
        with self.lock:
            self.files[rec.key] = rec
        return rec

    def write_nfs(self, rec: Rec, size: int, seed: int, ver: str) -> bool:
        with self.lock:
            rec.state = "writing"
        r = self.io(WUID, "write", self.nfs_path(ver, rec.rel), size, seed)
        return self.wrote(rec, size, seed, r.get("ok"), r.get("name"), f"nfs-{ver}")

    def wrote(self, rec: Rec, size: int, seed: int, ok: bool, err: str | None, via: str) -> bool:
        if ok:
            data = pattern(size, seed)
            with self.lock:
                rec.size, rec.seed = size, seed
                rec.sha256 = hashlib.sha256(data).hexdigest()
                rec.crc = f"{crc64nvme(data):016x}"
                rec.state, rec.t_written = "written", time.time()
                rec.gen += 1
                rec.archived = False
                self.counts[f"write_ok:{via}"] += 1
            self.ev("write", key=rec.key, size=size, seed=seed, via=via, crc=rec.crc)
            return True
        with self.lock:
            # The bytes on the share are now unknown (a partial write may still be committed):
            # the file leaves every check, and the writer deletes it later.
            rec.state = "failed"
            self.counts[f"write_err:{via}:{err}"] += 1
            self.by_fault[f"{self.fault_active}:write_err:{err}"] += 1
        self.ev("write_err", key=rec.key, via=via, err=err)
        return False

    def pick(self, rng: random.Random, pred) -> Rec | None:
        with self.lock:
            cands = [r for r in self.files.values() if pred(r)]
            return rng.choice(cands) if cands else None

    def live(self) -> list[Rec]:
        with self.lock:
            return [r for r in self.files.values() if r.state == "written"]

    def nfs_writer(self) -> None:
        rng = self.rng("nfs_writer")
        while self.sleep(rng.uniform(5, 12)):
            n_live = len([r for r in self.live() if r.share == "wait" and not r.big])
            op = rng.choices(["create", "overwrite", "delete", "cleanup"],
                             weights=[0.6 if n_live < self.a.files else 0.15, 0.2,
                                      0.12 if n_live < self.a.files else 0.5, 0.08])[0]
            ver = rng.choice(NFS_VERS)
            size = draw_size(rng)
            seed = rng.randrange(1 << 30)
            if op == "create":
                self.write_nfs(self.new_rec("wait", rng), size, seed, ver)
                continue
            if op == "cleanup":
                rec = self.pick(rng, lambda r: r.share == "wait" and r.state == "failed")
            else:
                rec = self.pick(rng, lambda r: r.share == "wait" and not r.big
                                and r.state == "written" and r.readers == 0)
            if rec is None:
                continue
            if op == "overwrite":
                self.write_nfs(rec, size, seed, ver)
                continue
            with self.lock:
                was, rec.state = rec.state, "deleting"
            r = self.io(WUID, "unlink", self.nfs_path(ver, rec.rel))
            self.deleted(rec, was, r.get("ok"), r.get("name"), f"nfs-{ver}")

    def deleted(self, rec: Rec, was: str, ok: bool, err: str | None, via: str) -> None:
        outcome = delete_outcome(was, ok, err)
        with self.lock:
            rec.state = "failed" if outcome in ("failed", "enoent") else "deleted"
            if outcome == "deleted":
                self.counts[f"delete_ok:{via}"] += 1
            elif outcome == "failed":
                self.counts[f"delete_err:{err}"] += 1
        if outcome == "enoent":
            # The soak wrote this file and saw it committed: a delete that finds it gone
            # means the share lost it.
            self.violation("enoent", key=rec.key, via=f"delete-{via}")
        self.ev("delete", key=rec.key, ok=ok, err=err, outcome=outcome)

    def smb_writer(self) -> None:
        rng = self.rng("smb_writer")
        tmp = Path("/var/tmp/omnifile-soak/smbput")
        tmp.mkdir(parents=True, exist_ok=True)
        while self.sleep(rng.uniform(30, 75)):
            n_live = len([r for r in self.live() if r.share == "smbwait"])
            size = draw_size(rng)
            seed = rng.randrange(1 << 30)
            if n_live >= self.a.files // 3:
                rec = self.pick(rng, lambda r: r.share == "smbwait" and r.state == "written"
                                and r.readers == 0)
                if rec is None:
                    continue
                with self.lock:
                    rec.state = "deleting"
                rc, out = self.smb("geo-writer", f"del {rec.rel.replace('/', chr(92))}")
                bad = rc != 0 or "NT_STATUS" in out
                name = next((w.strip("():,") for w in out.split() if w.startswith("NT_STATUS")),
                            f"rc{rc}") if bad else None
                self.deleted(rec, "written", not bad, "ENOENT" if name in SMB_NOT_FOUND else name, "smb")
                continue
            rec = self.new_rec("smbwait", rng)
            src = tmp / Path(rec.rel).name
            self.io(0, "pattern", src, size, seed)
            with self.lock:
                rec.state = "writing"
            rc, out = self.smb("geo-writer", f"put {src} {rec.rel.replace('/', chr(92))}")
            src.unlink(missing_ok=True)
            bad = rc != 0 or "NT_STATUS" in out
            err = next((w.strip("():,") for w in out.split() if w.startswith("NT_STATUS")), f"rc{rc}") if bad else None
            self.wrote(rec, size, seed, not bad, err, "smb")

    # --- load: reads, archive, listings --------------------------------------------------
    def readable(self, rec: Rec, share: str) -> bool:
        if rec.share != share or rec.state != "written":
            return False
        row = self.cat_row(rec)
        return bool(row and row["crc"] == rec.crc)

    def read_one(self, rec: Rec, via: str, timeout: float = 420) -> dict:
        """Read REC whole over VIA (v3, v41, v42 or smb) and judge the answer."""
        mk = e2e.marker_of(rec.local)
        unfilled = bool(mk and mk.get("state") == "PARTIAL" and mk.get("resident", 0) < rec.size)
        archived = bool(mk and (mk.get("flags", 0) & 2) and not (mk.get("flags", 0) & 1))
        with self.lock:
            rec.readers += 1
            gen, size, seed = rec.gen, rec.size, rec.seed
            fault = self.fault_active
        try:
            if via == "smb":
                dst = f"{SMBGET}/{threading.get_ident()}.bin"
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                rc, out = self.smb("geo-reader", f"get {rec.rel.replace('/', chr(92))} {dst}", int(timeout))
                if "NT_STATUS" in out or rc != 0:
                    name = next((w.strip("():,") for w in out.split() if w.startswith("NT_STATUS")), f"rc{rc}")
                    r = {"ok": False, "name": "ENOENT" if name in SMB_NOT_FOUND else name}
                    # What smbclient kept before the error must still be the file's bytes.
                    if os.path.exists(dst) and os.path.getsize(dst) > 0:
                        part = self.io(0, "prefix", dst, size, seed)
                        for k in ("zero_blocks", "bad_blocks", "first_zero", "first_bad", "bytes"):
                            if k in part:
                                r[k] = part[k]
                else:
                    r = self.io(0, "check", dst, size, seed)
                try:
                    os.unlink(dst)
                except FileNotFoundError:
                    pass
            else:
                r = self.io(RUID, "read", self.nfs_path(via, rec.rel), size, seed, timeout=timeout)
        finally:
            with self.lock:
                rec.readers -= 1
        changed = rec.gen != gen
        kind = "unfilled" if unfilled else "filled"
        verdict = judge_read(r, changed)
        if verdict == "ok":
            with self.lock:
                self.counts[f"read_ok:{via}:{kind}"] += 1
                if archived:
                    self.counts[f"read_ok:{via}:archived"] += 1
        elif verdict == "discarded":
            self.counts["read_discarded_rewritten"] += 1
        elif verdict == "zeros":
            self.violation("zeros", key=rec.key, via=via, unfilled=unfilled, archived=archived, r=r)
        elif verdict == "wrong_bytes":
            self.violation("wrong_bytes", key=rec.key, via=via, unfilled=unfilled, r=r)
        elif verdict == "enoent":
            self.violation("enoent", key=rec.key, via=via, archived=archived, r=r)
        else:
            with self.lock:
                self.counts[f"read_err:{via}:{r.get('name')}"] += 1
                self.by_fault[f"{fault}:read_err:{via}:{r.get('name')}"] += 1
        self.ev("read", key=rec.key, via=via, unfilled=unfilled, archived=archived,
                ok=r.get("ok"), err=r.get("name"), s=r.get("s"))
        return r

    def reader(self, via: str) -> None:
        rng = self.rng(f"reader:{via}")
        share = "smbwait" if via == "smb" else "wait"
        while self.sleep(rng.uniform(2, 7)):
            prefer_stub = rng.random() < 0.85
            rec = self.pick(rng, lambda r: self.readable(r, share) and not r.big and (
                not prefer_stub or (self.cat_row(r) or {}).get("state") == "stub"))
            if rec is not None:
                self.read_one(rec, via)

    def archive(self, rec: Rec) -> bool:
        try:
            row = self.cat_row(rec)
            with e2e.db() as c:
                n = c.execute(
                    "UPDATE catalog.location SET logical_tier = 'archive', storage_class = 'Archive'"
                    " WHERE location_id = (SELECT primary_location_id FROM catalog.file"
                    " WHERE file_id = %s)", (row["file_id"],)).rowcount
        except Exception as e:
            self.counts["archive_error"] += 1
            self.ev("archive_error", key=rec.key, error=str(e)[:200])
            return False
        with self.lock:
            rec.archived = bool(n)
            self.counts["archived"] += int(bool(n))
        self.ev("archive", key=rec.key, updated=n)
        return bool(n)

    def archiver(self) -> None:
        rng = self.rng("archiver")
        while self.sleep(rng.uniform(25, 70)):
            rec = self.pick(rng, lambda r: (r.state == "written" and not r.big and not r.archived
                                            and (self.cat_row(r) or {}).get("state") == "stub"
                                            and self.committed_ok(r)))
            if rec is not None:
                self.archive(rec)

    def lister(self) -> None:
        rng = self.rng("lister")
        while self.sleep(rng.uniform(45, 120)):
            now = time.time()
            with self.lock:
                want = defaultdict(set)
                for r in self.files.values():
                    if r.state == "written" and now - r.t_written > SETTLED_S and r.readers >= 0:
                        want[(r.share, os.path.dirname(r.rel))].add(os.path.basename(r.rel))
            for (share, d), names in sorted(want.items()):
                if share == "wait":
                    res = self.io(RUID, "ls", self.nfs_path(rng.choice(NFS_VERS), d), timeout=300)
                    if not res.get("ok"):
                        self.counts[f"list_err:{res.get('name')}"] += 1
                        continue
                    got = set(res["names"])
                else:
                    rc, out = self.smb("geo-reader", f"ls {d.replace('/', chr(92))}{chr(92)}*", 300)
                    if rc != 0 or "NT_STATUS" in out:
                        self.counts["list_err:smb"] += 1
                        continue
                    got = {ln.split()[0] for ln in out.splitlines() if ln.startswith("  ") and ln.split()}
                with self.lock:  # deleted or rewritten meanwhile: not missing
                    missing = [n for n in names if self.files.get(f"{share}/{d}/{n}") is not None
                               and self.files[f"{share}/{d}/{n}"].state == "written" and n not in got]
                self.counts["listings"] += 1
                self.counts[f"listings:{'nfs' if share == 'wait' else 'smb'}"] += 1
                for n in missing:
                    rec = self.files[f"{share}/{d}/{n}"]
                    self.violation("enoent", key=rec.key, via=f"listing-{share}", archived=rec.archived)

    def catalog_loop(self) -> None:
        while not self.stop.is_set():
            self.sync_catalog()
            if self.stop.wait(10):
                break

    # --- faults --------------------------------------------------------------------------
    def schedule(self) -> list[tuple[float, str]]:
        rng = self.rng("schedule")
        order = [f for f in FAULTS for _ in range(self.a.each)]
        for _ in range(1000):  # no fault twice in a row
            rng.shuffle(order)
            if all(a != b for a, b in zip(order, order[1:])):
                break
        span = self.a.duration - self.a.warmup - self.a.cooldown
        mean = span / len(order)
        t, out = float(self.a.warmup), []
        for f in order:
            out.append((round(t, 1), f))
            t += mean * rng.uniform(0.6, 1.4)
        # Rescale so the last fault lands inside the span.
        scale = span / max(1.0, out[-1][0] - self.a.warmup + mean * 0.5)
        return [(round(self.a.warmup + (o - self.a.warmup) * min(1.0, scale), 1), f) for o, f in out]

    def nrestarts(self) -> dict:
        return {u: self.sh("systemctl", "show", "-p", "NRestarts", "--value", u)[1] for u in UNITS}

    def recover(self, name: str, budget: float = 180, since: float | None = None) -> float:
        """Seconds from SINCE (a kill's own time; else now) until every unit is active."""
        t = since if since is not None else time.time()
        while time.time() - t < budget:
            if all(self.active(u) for u in UNITS):
                return round(time.time() - t, 1)
            time.sleep(1)
        down = [u for u in UNITS if not self.active(u)]
        self.violation("recovery", fault=name, down=down)
        for u in down:  # carry on: start what is down
            self.sh("systemctl", "reset-failed", u)
            self.sh("systemctl", "start", u, timeout=180)
        return -1.0

    def kill_and_wait(self, unit: str) -> dict:
        """SIGKILL UNIT and time systemd's restart: with RestartMode=direct the unit never
        leaves `active`, so wait for a new main pid instead."""
        def mainpid():
            return self.sh("systemctl", "show", "-p", "MainPID", "--value", unit)[1]
        old, t = mainpid(), time.time()
        self.sh("systemctl", "kill", "-s", "KILL", unit)
        while time.time() - t < self.restart_budget:
            new = mainpid()
            if new not in ("", "0", old) and self.active(unit):
                return {"old_pid": old, "new_pid": new, "restarted_s": round(time.time() - t, 1),
                        "kill_t": round(t, 3)}
            time.sleep(0.2)
        # systemd never brought up a new process: the unit did not recover from the kill.
        self.violation("recovery", unit=unit, why=f"no new main pid within {self.restart_budget} s",
                       old_pid=old)
        return {"old_pid": old, "restarted_s": None, "kill_t": round(t, 3)}

    def daemon_stop_after(self, t0: float, within: float) -> str | None:
        """A daemon kill or restart in [t0, t0 + within]: a file closed at t0 was still in
        the detector's quiet period then (the known gap: nsync.rs reconcile_dir leaves a
        new regular file to the detector, whose close event dies with the process)."""
        for r in self.faults_done:
            k = r.get("kill_t") if r["fault"] == "kill_edge" else r.get("restart_t")
            if k is not None and t0 <= k <= t0 + within:
                return f"{r['fault']} {k - t0:.1f} s after the close"
        return None

    def f_kill_edge(self, rng):
        return self.kill_and_wait("omnifile-edge")

    def f_kill_ganesha(self, rng):
        return self.kill_and_wait("nfs-ganesha-omni")

    def f_kill_smbd(self, rng):
        hold = rng.uniform(5, 25)
        self.sh("systemctl", "kill", "-s", "KILL", "smbd")
        time.sleep(hold)
        self.sh("systemctl", "reset-failed", "smbd")
        rc, out = self.sh("systemctl", "start", "smbd", timeout=120)
        return {"hold_s": round(hold, 1), "restarted_by": "the soak (smbd.service has no Restart=)",
                "start_rc": rc}

    def f_kill_worker(self, rng):
        return self.kill_and_wait("omnifile-dev-worker")

    def f_crash_postgres(self, rng):
        if rng.random() < 0.5:
            hold = rng.uniform(5, 30)
            self.sh("systemctl", "kill", "-s", "KILL", "postgresql@17-main")
            time.sleep(hold)
            self.sh("systemctl", "reset-failed", "postgresql@17-main")
            rc, _ = self.sh("systemctl", "start", "postgresql@17-main", timeout=180)
            return {"mode": "cluster SIGKILL, started again", "hold_s": round(hold, 1), "start_rc": rc}
        pm = self.sh("systemctl", "show", "-p", "MainPID", "--value", "postgresql@17-main")[1]
        kids = self.sh("ps", "-o", "pid=,args=", "--ppid", pm)[1].splitlines()
        backends = [k.split()[0] for k in kids if "omnifile" in k] or [k.split()[0] for k in kids]
        if not backends:
            return {"mode": "backend SIGKILL", "skipped": "no backend"}
        pid = rng.choice(backends)
        self.sh("kill", "-9", pid)
        return {"mode": "one backend SIGKILL (Postgres's own crash restart)", "pid": pid}

    def f_fill_disk(self, rng):
        hold = rng.uniform(30, 90)
        path = f"{DATA}/.chaos-fill"
        st = os.statvfs(DATA)
        free = st.f_bavail * st.f_frsize
        size, filled = free - 2 * MiB, 0
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while size > 0:  # xfs keeps a reserve: shrink until it fits
                try:
                    os.posix_fallocate(fd, 0, size)
                    filled = size
                    break
                except OSError:
                    size -= 64 * MiB
            left = os.statvfs(DATA).f_bavail * st.f_frsize
            time.sleep(hold)
        finally:
            os.close(fd)
            os.unlink(path)
        return {"hold_s": round(hold, 1), "filled_gib": round(filled / (1 << 30), 2), "left_mib": left // MiB}

    def nft_on(self) -> str:
        """As step 12 of tests/e2e/m5_exit_test.sh: the edge daemon's flows to the API and
        the sim marked on the way out (its cgroup) and dropped on the way in, so requests
        hang rather than fail."""
        pid = self.sh("systemctl", "show", "-p", "MainPID", "--value", "omnifile-edge")[1]
        cg = open(f"/proc/{pid}/cgroup").read().strip().split("::", 1)[1].lstrip("/")
        t = ["nft", "add"]
        for cmd in (t + ["table", "inet", "omnifile_chaos"],
                    t + ["chain", "inet", "omnifile_chaos", "out", "{ type filter hook output priority 0; }"],
                    t + ["chain", "inet", "omnifile_chaos", "in", "{ type filter hook input priority 0; }"],
                    t + ["rule", "inet", "omnifile_chaos", "out", "socket", "cgroupv2", "level",
                         str(len(cg.split("/"))), cg, "tcp", "dport", "{ 8443, 10000 }",
                         "ct", "mark", "set", "0x13"],
                    t + ["rule", "inet", "omnifile_chaos", "in", "ct", "mark", "0x13", "drop"]):
            rc, out = self.sh(*cmd)
            if rc != 0:
                self.nft_off()
                raise RuntimeError(f"nft: {' '.join(cmd)}: {out[-200:]}")
        listed = self.sh("nft", "list", "table", "inet", "omnifile_chaos")[1]
        if "ct mark set 0x00000013" not in listed or "ct mark 0x00000013 drop" not in listed:
            self.nft_off()
            raise RuntimeError(f"nft rules not in place: {listed[-300:]}")
        return cg

    def nft_off(self) -> None:
        self.sh("nft", "delete", "table", "inet", "omnifile_chaos")

    def f_drop_network(self, rng):
        hold = rng.uniform(20, 60)
        forget = rng.random() < 0.5
        cg = self.nft_on()
        time.sleep(hold)
        if forget:  # new flows pass; the outage's flows stay dead a while (a firewall that forgot)
            self.sh("nft", "flush", "chain", "inet", "omnifile_chaos", "out")
            time.sleep(rng.uniform(20, 60))
        self.nft_off()
        return {"hold_s": round(hold, 1), "forgotten_flows": forget, "cgroup": cg}

    def f_restart_mid_fill(self, rng):
        def ready(r):
            if not (r.big and r.state == "written" and self.committed_ok(r)):
                return False
            mk = e2e.marker_of(r.local)
            return bool(mk and mk.get("state") == "PARTIAL" and mk.get("flags", 0) & 1)
        rec = None
        for _ in range(60):  # a big stub may still be re-stubbing after the last fill
            rec = self.pick(rng, ready)
            if rec is not None:
                break
            time.sleep(5)
        if rec is None:
            return {"skipped": "no big stub ready in 300 s"}
        httpx.post(f"{SIM}/_sim/faults", json={"op": "get_range", "latency_ms": 250}, trust_env=False)
        res = {}
        readers = [threading.Thread(target=lambda v=v: res.__setitem__(v, self.read_one(rec, v, 600)))
                   for v in ("v3", "v41")]
        for t in readers:
            t.start()
        mk0 = (e2e.marker_of(rec.local) or {}).get("resident", 0)
        t0, progressed = time.time(), 0
        while time.time() - t0 < 90:
            progressed = os.stat(rec.local).st_blocks * 512 - mk0
            if progressed > 16 * MiB:
                break
            time.sleep(0.2)
        restart_t = time.time()
        self.sh("systemctl", "restart", "omnifile-edge", timeout=120)
        restarted_at = round(time.time() - t0, 1)
        for t in readers:
            t.join(660)
        httpx.post(f"{SIM}/_sim/faults", json={"clear": True}, trust_env=False)
        return {"file": rec.key, "filled_before_restart_mib": progressed // MiB,
                "restart_after_s": restarted_at, "restart_t": round(restart_t, 3),
                "readers": {v: {"ok": r.get("ok"), "err": r.get("name")} for v, r in res.items()}}

    def fault_loop(self, plan: list[tuple[float, str]]) -> None:
        rng = self.rng("faults")
        for at, name in plan:
            while time.time() - self.t0 < at:
                if self.stopped():
                    return
                time.sleep(1)
            if self.stopped():
                return
            before = self.nrestarts()
            self.fault_active = name
            self.say(f"fault {len(self.faults_done) + 1}/{len(plan)}: {name}")
            t = time.time()
            try:
                detail = getattr(self, f"f_{name}")(rng)
            except Exception as e:  # a harness error, not the system's: record and go on
                detail = {"harness_error": repr(e)[:300]}
            took = round(time.time() - t, 1)
            rec_s = self.recover(name, since=detail.get("kill_t"))
            self.fault_active = "none"
            row = {"n": len(self.faults_done) + 1, "fault": name, "at_s": round(t - self.t0, 1),
                   "took_s": took, "recovered_s": rec_s, "restarts_before": before,
                   "restarts_after": self.nrestarts(), **detail}
            with self.lock:
                self.faults_done.append(row)
                if not detail.get("skipped") and "harness_error" not in detail:
                    self.counts[f"fault:{name}"] += 1
                else:
                    self.counts[f"fault_skipped:{name}"] += 1
            self.ev("fault", **row)
            self.status()

    # --- the end ------------------------------------------------------------------------
    def settle(self, budget: float) -> dict:
        t = time.time()
        while time.time() - t < budget:
            self.sync_catalog()
            with self.lock:
                pending = [r.key for r in self.files.values()
                           if (r.state == "written" and not self.committed_ok(r))
                           or (r.state == "deleted" and self.cat_row(r) is not None)]
                active = [k for k, r in self.recalls.items() if r["state"] in
                          ("requested", "rehydrating", "copying", "downloading")]
            if not pending and not active:
                return {"settled_s": round(time.time() - t, 1)}
            time.sleep(10)
        return {"settled_s": None, "pending": pending[:20], "n_pending": len(pending), "active_recalls": active[:20]}

    def verify_end(self) -> dict:
        res = Counter()
        with self.lock:
            recs = list(self.files.values())
        for rec in recs:
            if rec.state == "written":
                row = self.cat_row(rec)
                if not row or not row["cv"]:
                    # The daemon's commit quiet period is 3 s on the dev VM (dev/edge/edge.toml).
                    gap = self.daemon_stop_after(rec.t_written - 1, 5)
                    self.violation("lost_commit", key=rec.key, why="not committed", row=row,
                                   local=os.path.exists(rec.local), daemon_stopped=gap)
                    continue
                if row["crc"] != rec.crc or row["size"] != rec.size:
                    # An overwrite the catalog never saw: the older version is still current.
                    self.violation("lost_commit", key=rec.key, why="catalog CRC or size differs",
                                   want=[rec.crc, rec.size], got=[row["crc"], row["size"]],
                                   daemon_stopped=self.daemon_stop_after(rec.t_written - 1, 5))
                    continue
                try:
                    digest = None
                    for ver in (row["bver"], None):  # the committed version, else the current blob
                        url = e2e.sas_url(row["container"], row["key"], ver)
                        h = hashlib.sha256()
                        with httpx.Client(timeout=300, trust_env=False) as http, \
                                http.stream("GET", url, headers={"x-ms-version": "2026-04-06"}) as r:
                            if r.status_code >= 400:
                                continue
                            for chunk in r.iter_bytes(8 * MiB):
                                h.update(chunk)
                        digest = h.hexdigest()
                        break
                    if digest is None:
                        self.violation("lost_commit", key=rec.key, why="the backend has no such blob")
                    elif digest != rec.sha256:
                        self.violation("lost_commit", key=rec.key, why="backend bytes differ")
                    else:
                        res["committed_and_hashed"] += 1
                except Exception as e:
                    self.violation("lost_commit", key=rec.key, why=f"backend read failed: {e!r}"[:200])
                if not os.path.exists(rec.local):
                    self.violation("enoent", key=rec.key, via="edge volume", why="placeholder missing")
                else:
                    res["placeholder_present"] += 1
            elif rec.state == "deleted":
                if self.cat_row(rec) is not None or os.path.exists(rec.local):
                    self.violation("lost_commit", key=rec.key, why="a deleted file came back")
                else:
                    res["deleted_stays_deleted"] += 1
            else:
                res[f"excluded:{rec.state}"] += 1
        with self.lock:
            live = {row["file_id"]: row["cv"] for row in self.cat.values()}
            for rid, r in self.recalls.items():
                res[f"recall:{r['state']}"] += 1
                why = judge_recall(r["state"], r["file_id"] in live, r.get("reason"),
                                   live.get(r["file_id"]) == r.get("version_id"))
                if why:
                    self.violation("recall", recall_id=rid, why=why, failure=r.get("failure"))
        return dict(res)

    def status(self) -> None:
        with self.lock:
            s = {"dt": round(time.time() - self.t0), "faults": len(self.faults_done),
                 "counts": dict(self.counts), "violations": len(self.violations)}
        (self.out / "status.json").write_text(json.dumps(s, indent=1, sort_keys=True))

    # --- run ----------------------------------------------------------------------------
    def run(self) -> int:
        plan = self.schedule()
        (self.out / "schedule.json").write_text(json.dumps(plan))
        self.say(f"seed {self.seed}, {self.a.duration} s, {len(plan)} faults ({self.a.each} of each); "
                 f"out {self.out}")
        self.ev("start", seed=self.seed, duration=self.a.duration, each=self.a.each, plan=plan)
        self.sync_catalog()
        rng = self.rng("setup")
        for d in ["soak", "soak/big"] + [f"soak/d{i}" for i in range(8)]:
            self.io(WUID, "mkdir", self.nfs_path("v41", d))
        self.smb("geo-writer", "mkdir soak")
        for _ in range(self.a.big):
            self.write_nfs(self.new_rec("wait", rng, big=True), BIG, rng.randrange(1 << 30), "v41")
        for _ in range(self.a.files // 2):
            size = draw_size(rng)
            self.write_nfs(self.new_rec("wait", rng), size, rng.randrange(1 << 30), rng.choice(NFS_VERS))
        threads = [threading.Thread(target=self.catalog_loop, daemon=True, name="catalog")]
        for fn, args in [(self.nfs_writer, ()), (self.smb_writer, ()), (self.archiver, ()),
                         (self.lister, ())] + [(self.reader, (v,)) for v in NFS_VERS + ["smb"]]:
            threads.append(threading.Thread(target=fn, args=args, daemon=True, name=fn.__name__))
        for t in threads:
            t.start()
        faults = threading.Thread(target=self.fault_loop, args=(plan,), daemon=True)
        faults.start()
        try:
            # Until the planned time is up and every planned fault has run (faults that ran
            # long push the last ones late), at most an hour past the plan.
            while ((time.time() - self.t0 < self.a.duration or faults.is_alive())
                   and time.time() - self.t0 < self.a.duration + 3600 and not self.stopped()):
                time.sleep(30)
                self.status()
                if int(time.time() - self.t0) % 600 < 30:
                    with self.lock:
                        self.say(f"{len(self.faults_done)} faults, {len(self.violations)} violations, "
                                 f"{sum(v for k, v in self.counts.items() if k.startswith('read_ok'))} good reads, "
                                 f"{sum(v for k, v in self.counts.items() if k.startswith('write_ok'))} writes")
        finally:
            aborted = self.stopped()
            self.say("stopping the load" + (" (stop file or signal)" if aborted else ""))
            self.load_stop.set()
            faults.join(900)
            for t in threads[1:]:
                t.join(900)
            self.nft_off()
            httpx.post(f"{SIM}/_sim/faults", json={"clear": True}, trust_env=False)
        self.say("settling: every write committed, every recall finished")
        settle = self.settle(self.a.settle)
        self.stop.set()
        end = self.verify_end()
        self.save_evidence()
        self.report(plan, settle, end, aborted)
        short = shortfalls(self.counts, self.minimums)
        return 0 if green(self.violations, aborted, self.enough(), short) else 1

    def save_evidence(self) -> None:
        """Pass or fail, before soak.sh stops anything: the journal of the run, and the
        catalog's rows behind every violation. Never changes the verdict."""
        try:
            path = save_journal(self.out, self.t0 - 60)
            self.say(f"journal saved: {path} ({path.stat().st_size >> 20} MiB)")
        except Exception as e:
            self.say(f"journal NOT saved: {e!r}"[:300])
        with self.lock:
            rids, fids = violation_ids(self.violations, self.recalls, self.cat)
        if not rids and not fids:
            return
        try:
            with e2e.db() as c:
                dump = dump_catalog(c, rids, fids)
            path = self.out / "violations-catalog.json"
            path.write_text(json.dumps(dump, indent=1, default=str))
            self.say(f"catalog rows for {len(rids)} recalls and {len(fids)} files saved: {path}")
        except Exception as e:
            self.say(f"catalog rows NOT saved: {e!r}"[:300])

    def enough(self) -> bool:
        return all(self.counts[f"fault:{f}"] >= self.a.each for f in FAULTS)

    def report(self, plan, settle, end, aborted) -> None:
        c = self.counts
        lines = [f"# Chaos soak: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", "",
                 f"seed **{self.seed}**, {self.a.duration} s planned, ran {round(time.time() - self.t0)} s"
                 f"{' (ABORTED)' if aborted else ''}; {len(plan)} faults planned ({self.a.each} of each); "
                 f"label: {self.a.label}", "",
                 f"Replay: `{replay_cmd(self.a, self.seed)}`", "",
                 "| fault | injected | skipped | new main pid after (s, max) | every unit active after (s, max) |",
                 "|---|---|---|---|---|"]
        for f in FAULTS:
            done = [r for r in self.faults_done if r["fault"] == f]
            rs = [r["recovered_s"] for r in done]
            ps = [r["restarted_s"] for r in done if r.get("restarted_s") is not None]
            lines.append(f"| {f} | {c[f'fault:{f}']} | {c[f'fault_skipped:{f}']} | "
                         f"{max(ps) if ps else '-'} | {max(rs) if rs else '-'} |")
        lines += ["", "| invariant | violations |", "|---|---|"]
        for v in ("zeros", "wrong_bytes", "enoent", "lost_commit", "recall", "recovery"):
            lines.append(f"| {v} | {c[f'violation:{v}']} |")
        gap = [v for v in self.violations if v["kind"] == "lost_commit" and v.get("daemon_stopped")]
        lines += ["", f"Of the lost_commit violations, {len(gap)} had the edge daemon killed or restarted"
                  " within the commit quiet period after the file's close (the detector gap:"
                  " `edge/crates/omnifile-edge/src/nsync.rs` reconcile_dir leaves a new regular file to"
                  " the detector, whose close event dies with the process)."]
        lines += ["", "| load | count |", "|---|---|"]
        for k in sorted(c):
            if not k.startswith(("violation:", "fault:", "fault_skipped:")):
                lines.append(f"| {k} | {c[k]} |")
        lines += ["", "| at the end | count |", "|---|---|"]
        for k, v in sorted(end.items()):
            lines.append(f"| {k} | {v} |")
        lines += ["", f"settle: `{json.dumps(settle)}`", "", "Errors by the fault active when they happened:", ""]
        for k, v in sorted(self.by_fault.items()):
            lines.append(f"- {k}: {v}")
        lines += ["", "Faults:", ""]
        for r in self.faults_done:
            d = {k: v for k, v in r.items() if k not in ("restarts_before", "restarts_after")}
            lines.append(f"- `{json.dumps(d, default=str)}`")
        lines += ["", f"Violations ({len(self.violations)}):", ""]
        for v in self.violations[:50]:
            lines.append(f"- `{json.dumps(v, default=str)[:400]}`")
        short = shortfalls(c, self.minimums)
        ok = green(self.violations, aborted, self.enough(), short)
        lines += ["", f"**{'GREEN' if ok else 'RED'}**: {len(self.violations)} violations, every fault "
                  f"{'at least ' + str(self.a.each) + ' times' if self.enough() else 'NOT injected enough'}; "
                  f"load {'below its minimums: ' + ', '.join(short) if short else 'at or above its minimums'}"
                  f" ({self.a.minimums})."]
        Path(self.a.report).write_text("\n".join(lines) + "\n")
        (self.out / "summary.json").write_text(json.dumps(
            {"seed": self.seed, "counts": dict(c), "end": end, "settle": settle,
             "faults": self.faults_done, "violations": self.violations}, indent=1, default=str))
        self.say(f"report: {self.a.report} ({'GREEN' if ok else 'RED'})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--duration", type=int, default=6 * 3600 + 900)
    p.add_argument("--each", type=int, default=12)
    p.add_argument("--warmup", type=int, default=300)
    p.add_argument("--cooldown", type=int, default=300)
    p.add_argument("--settle", type=int, default=1800)
    p.add_argument("--files", type=int, default=240, help="live NFS files to aim for")
    p.add_argument("--big", type=int, default=4, help="96 MiB files for restart_mid_fill")
    p.add_argument("--minimums", default=DEFAULT_MINIMUMS,
                   help="what a run must exercise to be GREEN, per protocol: "
                        "{nfs,smb}.{write_ok,reads,unfilled,archived,listings}=N, and recalls=N")
    p.add_argument("--out", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--label", default="")
    a = p.parse_args(argv)
    try:  # now, not in the report at the end of a six-hour run
        parse_minimums(a.minimums)
    except ValueError as e:
        p.error(f"--minimums: {e}")
    if a.seed is None:
        a.seed = int.from_bytes(os.urandom(4), "little")
    return Soak(a).run()


if __name__ == "__main__":
    sys.exit(main())
