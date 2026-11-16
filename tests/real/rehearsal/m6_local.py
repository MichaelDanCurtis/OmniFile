"""Steps 3 to 5 of tests/real/rehearsal/m6-local.sh (read its header first): an enrolment
code from the local portal, the arm64 appliance booted under HVF with the setup answers as
credentials, then the M6 exit's client steps from this Mac. PENDING ON WP2: m6-local.sh
refuses to call this until the arm64 image and HVF support are on main, so this has never
run. Runs with the service's venv (httpx).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[3]
DC_REALM = "OMNI.TEST"


def stack(work: Path) -> dict:
    return json.loads((work / "stack" / "stack.json").read_text())


def signed_in(st: dict) -> httpx.Client:
    """The dev OIDC issuer's admin (Ada Admin, ArchiveAdmin), as the M4 browser tests sign in."""
    c = httpx.Client(base_url=st["urls"]["web"], verify=st["pki"]["ca_crt"], follow_redirects=True)
    page = c.get("/auth/login", params={"return_to": "/files"})
    m = re.search(r'href="([^"]+)"[^>]*>\s*Ada Admin\s*<', page.text)
    if not m:
        raise SystemExit("the dev issuer's page has no Ada Admin link")
    c.get(m.group(1).replace("&amp;", "&"))
    csrf = c.get("/auth/session").json()["csrf_token"]
    c.headers.update({"X-CSRF-Token": csrf, "Origin": st["urls"]["web"]})
    return c


def enrol_code(c: httpx.Client, name: str) -> str:
    r = c.post(
        "/v1/edges", json={"name": name}, headers={"Idempotency-Key": f"rehearsal-{time.time_ns()}"}
    )
    r.raise_for_status()
    return r.json()["code"]


def boot(work: Path, image: Path, answers: str, join_pw: str, bundle: str) -> None:
    """qemu-system-aarch64 under HVF, UEFI (edk2), a vTPM (swtpm), the answers as systemd
    credentials (SMBIOS type 11), on vmnet-shared so the guest reaches omnifile-dc."""
    overlay = work / "edge.qcow2"
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-q",
            "-f",
            "qcow2",
            "-F",
            "qcow2" if image.suffix == ".qcow2" else "raw",
            "-b",
            str(image),
            str(overlay),
            "64G",
        ],
        check=True,
    )
    data = work / "data.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(data), "64G"], check=True)

    def cred(name: str, value: str) -> list[str]:
        b = base64.b64encode(value.encode()).decode()
        return ["-smbios", f"type=11,value=io.systemd.credential.binary:{name}={b}"]

    code = os.environ.get("EDK2_AARCH64", "/opt/homebrew/share/qemu/edk2-aarch64-code.fd")
    cmd = [
        "qemu-system-aarch64",
        "-machine",
        "virt",
        "-accel",
        "hvf",
        "-cpu",
        "host",
        "-smp",
        "4",
        "-m",
        "4096",
        "-nographic",
        "-serial",
        f"file:{work}/serial.log",
        "-drive",
        f"if=pflash,format=raw,readonly=on,file={code}",
        "-drive",
        f"if=virtio,format=qcow2,file={overlay}",
        "-drive",
        f"if=virtio,format=qcow2,file={data}",
        "-netdev",
        "vmnet-shared,id=n0",
        "-device",
        "virtio-net-pci,netdev=n0",
        *cred("omnifile.setup", answers),
        *cred("omnifile.join-password", join_pw),
        *cred("omnifile.enrol-bundle", bundle),
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(work / "qemu.err", "w"))
    (work / "qemu.pid").write_text(str(p.pid))


def edge_healthy(c: httpx.Client, timeout_s: int = 1800) -> dict:
    end = time.time() + timeout_s
    while time.time() < end:
        items = c.get("/v1/edges").json().get("items", [])
        ok = [e for e in items if e.get("status") in ("healthy", "ok")]
        if ok:
            return ok[0]
        time.sleep(15)
    raise SystemExit("the appliance never reported healthy")


def exit_steps(work: Path, edge_ip: str, c: httpx.Client, st: dict) -> dict:
    """The client side, as tests/real/vm/exit_steps.py does it on the Azure test VM."""
    mnt = work / "mnt"
    out = {}
    for proto, opts in (("nfs3", "vers=3,resvport"), ("nfs4", "vers=4,resvport")):
        mp = mnt / proto
        mp.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["sudo", "mount_nfs", "-o", opts, f"{edge_ip}:/projects", str(mp)], check=True
        )
        body = os.urandom(64 << 20)
        f = mp / f"line_{proto}.segy"
        f.write_bytes(body)
        out[proto] = {"path": f.name, "sha256": hashlib.sha256(body).hexdigest()}
    # SMB as geo-writer, with a Kerberos ticket from omnifile-dc (dev/.secrets/ad.env).
    ad = dict(
        ln.split("=", 1) for ln in (REPO / "dev/.secrets/ad.env").read_text().split() if "=" in ln
    )
    subprocess.run(
        ["kinit", f"geo-writer@{DC_REALM}"],
        input=ad.get("GEO_WRITER_PASSWORD", "") + "\n",
        text=True,
        check=True,
    )
    mp = mnt / "smb"
    mp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount_smbfs", f"//geo-writer@{edge_ip}/projects", str(mp)], check=True)
    body = os.urandom(64 << 20)
    (mp / "line_smb.segy").write_bytes(body)
    out["smb"] = {"path": "line_smb.segy", "sha256": hashlib.sha256(body).hexdigest()}
    # Uploaded, then the sim's clock moved on so the stub, the Archive pin and the High
    # recall complete (the sim answers at once once the clock passes their times).
    ds = c.get("/v1/datasets").json()["items"][0]["dataset_id"]
    ids = {}
    for proto, v in out.items():
        for _ in range(240):
            e = c.get("/v1/files", params={"dataset_id": ds, "path": v["path"]}).json()
            e = e.get("entry", e)
            if e.get("state") == "committed":
                ids[proto] = e["file_id"]
                break
            time.sleep(5)
    sim = httpx.Client(base_url=st["urls"]["sim"])
    sim.post("/_sim/advance", json={"seconds": 3600})
    pin = c.post(
        "/v1/pins",
        json={
            "items": [ids["nfs3"]],
            "tier": "archive",
            "until": "2099-01-01T00:00:00Z",
            "reason": "rehearsal",
        },
        headers={"Idempotency-Key": f"pin-{time.time_ns()}"},
    )
    pin.raise_for_status()
    sim.post("/_sim/advance", json={"seconds": 86400})
    rec = c.post(
        "/v1/recalls",
        json={"file_id": ids["nfs3"], "priority": "high"},
        headers={"Idempotency-Key": f"rec-{time.time_ns()}"},
    )
    rec.raise_for_status()
    sim.post("/_sim/advance", json={"seconds": 3600})
    for proto in ("nfs3", "nfs4", "smb"):
        got = hashlib.sha256((mnt / proto / out[proto]["path"]).read_bytes()).hexdigest()
        if got != out[proto]["sha256"]:
            raise SystemExit(f"{proto} read-back differs")
    return {"files": out, "pin": pin.json(), "recall": rec.json()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--image", type=Path, required=True)
    a = ap.parse_args()
    st = stack(a.work)
    c = signed_in(st)
    code = enrol_code(c, "edge-rehearsal")
    answers = (
        "\n".join(
            [
                "version = 1",
                'hostname = "edge-rehearsal"',
                "[network]",
                'mode = "dhcp"',
                "[data]",
                'device = "/dev/vdb"',
                "[ad]",
                f'domain = "{DC_REALM.lower()}"',
                'workgroup = "OMNI"',
                'join_user = "Administrator"',
                'admin_group = "filer admins"',
                "[service]",
                "private_endpoints = false",
                "[updates]",
                'reboot_window = "Sun *-*-* 03:00"',
            ]
        )
        + "\n"
    )
    ad = (REPO / "dev/.secrets/ad.env").read_text()
    join_pw = re.search(r"ADMIN_PASSWORD=(\S+)", ad).group(1)
    boot(a.work, a.image, answers, join_pw, code)
    edge = edge_healthy(c)
    ip = edge.get("address") or os.environ["REHEARSAL_EDGE_IP"]
    result = exit_steps(a.work, ip, c, st)
    (a.work / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print("M6 local rehearsal: every step passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
