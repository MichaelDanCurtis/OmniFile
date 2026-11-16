"""The M6 exit's client-side steps on the test VM, through public interfaces only: NFSv3,
NFSv4.2 and SMB 3 (as an AD user, Kerberos) against the appliance's first share, and Azure's
storage API (the VM's managed identity) for the tier and WORM facts a user can't see.

Each step reads {"step", "facts"} on stdin and prints one JSON line. Facts carry what later
steps need (paths, hashes, sizes) through the harness's state.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

R = Path("/srv/ofreal")
MNT = Path("/mnt/ofreal")
SHARE = os.environ.get("OFREAL_SHARE", "projects")
WORM_SHARE = os.environ.get("OFREAL_WORM_SHARE", "tst")
SIZE = 64 << 20
RESIDENT_HEADER = 4 << 20  # the SEG-Y headers that stay resident (AGENTS.md scenario)


def sh(*cmd: str, input: str | None = None, check: bool = True) -> str:
    p = subprocess.run(cmd, input=input, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {p.stderr.strip()[-300:]}")
    return p.stdout


def edge_ip() -> str:
    out = sh(
        "virsh",
        "domifaddr",
        "--source",
        "lease",
        os.environ.get("OFREAL_EDGE", "") or sh("virsh", "list", "--name").split()[0],
    )
    for line in out.splitlines():
        if "ipv4" in line:
            return line.split()[-1].split("/")[0]
    raise RuntimeError("the appliance has no address yet")


def secrets() -> dict:
    return json.loads((R / "secrets.json").read_text())


def mount(proto: str, share: str = SHARE) -> Path:
    mp = MNT / f"{proto}-{share}"
    mp.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["mountpoint", "-q", str(mp)]).returncode == 0:
        return mp
    ip = edge_ip()
    if proto == "nfs3":
        sh("mount", "-t", "nfs", "-o", "vers=3,hard", f"{ip}:/{share}", str(mp))
    elif proto == "nfs4":
        sh("mount", "-t", "nfs4", "-o", "vers=4.2,hard", f"{ip}:/{share}", str(mp))
    else:
        s = secrets()
        realm = s["realm"].upper()
        sh("kinit", f"geo-writer@{realm}", input=s["user"] + "\n")
        sh(
            "mount",
            "-t",
            "cifs",
            f"//{ip}/{share}",
            str(mp),
            "-o",
            "sec=krb5,cruid=0,vers=3.1.1,uid=20002,gid=30001",
        )
    return mp


def write(proto: str) -> dict:
    mp = mount(proto)
    d = mp / "exit" / proto
    d.mkdir(parents=True, exist_ok=True)
    data = os.urandom(SIZE)
    f = d / f"line_{proto}_{int(time.time())}.segy"
    with open(f, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    rel = f.relative_to(mp)
    return {
        "facts": {
            f"{proto}_path": str(rel),
            f"{proto}_sha256": hashlib.sha256(data).hexdigest(),
            f"{proto}_size": SIZE,
        }
    }


def stubbed(facts: dict) -> dict:
    """The edge freed the bytes: allocated blocks over NFS drop to the resident headers."""
    deadline = time.time() + int(os.environ.get("OFREAL_STUB_WAIT", "1800"))
    mp = mount("nfs4")
    left = {}
    while time.time() < deadline:
        left = {}
        for proto in ("nfs3", "nfs4", "smb"):
            st = os.stat(mp / facts[f"{proto}_path"])
            if st.st_blocks * 512 > RESIDENT_HEADER + (1 << 20):
                left[proto] = st.st_blocks * 512
        if not left:
            return {"blocks": "at most the resident headers"}
        time.sleep(30)
    raise RuntimeError(f"still resident: {left}")


def read_back(facts: dict, protos=("nfs3", "nfs4", "smb")) -> dict:
    got = {}
    for proto in protos:
        mp = mount(proto)
        for src in ("nfs3", "nfs4", "smb"):
            p = mp / facts[f"{src}_path"]
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if h != facts[f"{src}_sha256"]:
                raise RuntimeError(f"{proto} read of {src}'s file: {h} != {facts[f'{src}_sha256']}")
            got[f"{proto}<-{src}"] = "same bytes"
    return got


def blob_service(account: str):
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=ManagedIdentityCredential()
    )


def archived(facts: dict) -> dict:
    """Azure itself reports a blob of the pinned file's size in the Archive tier of the
    archive account (through the private endpoint)."""
    acct = facts.get("archive_account") or f"st{facts['prefix']}arch"
    svc = blob_service(acct)
    size = facts["nfs3_size"]
    deadline = time.time() + 3600
    while time.time() < deadline:
        for c in svc.list_containers():
            for b in svc.get_container_client(c.name).list_blobs():
                if b.size == size and (b.blob_tier or "").lower() == "archive":
                    return {"facts": {"archived_blob": f"{acct}/{c.name}/{b.name}"}}
        time.sleep(60)
    raise RuntimeError(f"no {size}-byte Archive blob in {acct} after an hour")


def recalled(facts: dict) -> dict:
    deadline = time.time() + 6 * 3600  # High priority is about an hour under 10 GB
    mp = mount("nfs4")
    p = mp / facts["nfs3_path"]
    while time.time() < deadline:
        try:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if h != facts["nfs3_sha256"]:
                raise RuntimeError(f"recalled bytes differ: {h}")
            return {"recalled": str(facts["nfs3_path"])}
        except OSError:
            time.sleep(60)  # EIO (fail fast) or EAGAIN while the recall runs
    raise RuntimeError("the recall didn't finish in 6 h")


def worm_write(facts: dict) -> dict:
    nfs = mount("nfs4", WORM_SHARE)
    smb = mount("smb", WORM_SHARE)
    f = nfs / "worm" / "ledger.segy"
    f.parent.mkdir(parents=True, exist_ok=True)
    v1, v2 = os.urandom(1 << 20), os.urandom(1 << 20)
    f.write_bytes(v1)
    time.sleep(120)  # let v1 commit before the overwrite
    (smb / "worm" / "ledger.segy").write_bytes(v2)
    time.sleep(120)
    os.unlink(nfs / "worm" / "ledger.segy")
    return {
        "facts": {
            "worm_path": "worm/ledger.segy",
            "worm_v1": hashlib.sha256(v1).hexdigest(),
            "worm_v2": hashlib.sha256(v2).hexdigest(),
        }
    }


def worm_immutable(facts: dict) -> dict:
    """Azure refuses to delete the retained version, whoever asks."""
    acct = facts.get("worm_account") or f"st{facts['prefix']}wtst"
    svc = blob_service(acct)
    refused = []
    for b in svc.get_container_client("data").list_blobs(
        include=["versions", "immutabilitypolicy"]
    ):
        if not b.name.endswith("ledger.segy"):
            continue
        try:
            svc.get_blob_client("data", b.name, version_id=b.version_id).delete_blob()
            raise RuntimeError(f"version {b.version_id} of {b.name} was deleted")
        except Exception as e:  # HttpResponseError 409 BlobImmutableDueToPolicy, or 403
            if "was deleted" in str(e):
                raise
            refused.append(
                {
                    "version": b.version_id,
                    "answer": type(e).__name__,
                    "code": getattr(e, "error_code", None),
                }
            )
    if len(refused) < 2:
        raise RuntimeError(f"want both versions retained, found {len(refused)}")
    return {"refused": refused}


def r3_gate(facts: dict) -> dict:
    """appliance/release.sh on this VM's KVM: the full A-suite and canary on the image's own
    kernel, signed with the signer the owner gave (else a throwaway fake: key)."""
    d = R / "r3"
    signer = (d / "SIGNER").read_text().strip() if (d / "SIGNER").exists() else ""
    args = [
        str(d / "repo/appliance/release.sh"),
        "--out",
        str(d / "build"),
        "--bin-dir",
        str(d / "bin"),
        "--canary-bin-dir",
        str(d / "canary-bin"),
        "--source-offer",
        str(d / "source-offer"),
    ]
    if signer:
        args += ["--signer", signer]
    else:
        key = d / "fake-update.pem"
        sh("openssl", "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", str(key))
        args += ["--signer", f"fake:{key}"]
    p = subprocess.run(args, capture_output=True, text=True)
    (R / "r3-gate.log").write_text(p.stdout + p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"release.sh exited {p.returncode}; see {R}/r3-gate.log")
    gate = next((d / "build").rglob("R3-GATE"), None)
    return {"r3_gate": gate.read_text() if gate else None, "signed_with": signer or "fake: key"}


def tenant_stub_read(args: dict) -> dict:
    mp = mount("smb")
    files = sorted((mp / "exit" / "nfs3").glob("*.segy"))
    if not files:
        raise RuntimeError("no exit file to read")
    try:
        files[0].read_bytes()
        return {"read": str(files[0].name), "note": "already resident"}
    except OSError as e:
        return {"read": str(files[0].name), "answer": str(e)}


def main() -> int:
    step = sys.argv[1]
    cfg = json.loads(sys.stdin.read() or "{}")
    facts = cfg.get("facts", {})
    try:
        if step in ("nfs3-write", "nfs4-write", "smb-write"):
            out = write(step.split("-")[0])
        elif step == "stubbed":
            out = stubbed(facts)
        elif step == "read-back":
            out = read_back(facts)
        elif step == "archived":
            out = archived(facts)
        elif step == "recalled":
            out = recalled(facts)
        elif step == "worm-write":
            out = worm_write(facts)
        elif step == "worm-immutable":
            out = worm_immutable(facts)
        elif step == "r3-gate":
            out = r3_gate(facts)
        elif step == "tenant-stub-read":
            out = tenant_stub_read(cfg)
        else:
            raise RuntimeError(f"no step {step}")
        print(json.dumps({"ok": True, "step": step, **out}))
    except Exception as e:
        print(json.dumps({"ok": False, "step": step, "error": f"{type(e).__name__}: {e}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
