"""The Azure run's test VM: a nested-virtualisation Dv5 VM in the deployment's VNet.

It stands in for the client's data centre. It is a KVM host for the fresh appliance qcow2
(imported by the guide's on=kvm steps), runs a Samba AD DC in a container (the client's
AD), is the Linux NFS and SMB client of the M6 exit, runs the VM-side real-Azure checks
from inside the VNet, and runs the signed x86 R3 gate where /dev/kvm is there.

The harness reaches it over SSH from the owner's workstation only: its NSG admits port 22
from the allow-list's ssh-from range and nothing else. The SSH key is made per run in
OUT/ssh/ and never leaves the workstation and the VM.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .core import REAL, Context, StepFailed

VM_SIZE = "Standard_D8s_v5"  # Dv5 supports nested virtualisation (VERIFY on the first run)
VM_IMAGE = "Canonical:ubuntu-24_04-lts:server:latest"
VM_USER = "omnitest"
EDGE_SUBNET = "snet-ofreal-edge"
EDGE_SUBNET_PREFIX = "10.60.1.0/24"
REMOTE = "/srv/ofreal"


class TestVM:
    def __init__(self, ctx: Context, rg: str, location: str, ssh_from: str) -> None:
        self.ctx, self.rg, self.location, self.ssh_from = ctx, rg, location, ssh_from
        self.name = f"vm-ofreal-{ctx.state['prefix']}"

    # ----------------------------------------------------------------- create

    @property
    def key(self) -> Path:
        return self.ctx.out / "ssh" / "id_ed25519"

    def _keypair(self) -> str:
        if not self.key.exists():
            self.key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.ctx.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"ofreal-{self.ctx.run_id}",
                    "-f",
                    str(self.key),
                ]
            )
        return self.key.with_suffix(".pub").read_text().strip()

    def ensure(self) -> None:
        st = self.ctx.state.setdefault("vm", {})
        if st.get("ready"):
            return
        ctx = self.ctx
        with ctx.step("vm.network"):
            vnets = [
                v
                for v in ctx.az("network", "vnet", "list", "-g", self.rg) or []
                if v["name"] != "vnet-ofreal-hub"
            ]
            if len(vnets) != 1:
                raise StepFailed(
                    f"want the deployment's one VNet in {self.rg}, found {len(vnets)}"
                    " (has the guide's deploy step run?)"
                )
            vnet = vnets[0]["name"]
            ctx.az(
                "network",
                "nsg",
                "create",
                "-g",
                self.rg,
                "-n",
                f"nsg-ofreal-{ctx.state['prefix']}",
                "-l",
                self.location,
                "--tags",
                *ctx.az_tags(),
            )
            ctx.az(
                "network",
                "nsg",
                "rule",
                "create",
                "-g",
                self.rg,
                "--nsg-name",
                f"nsg-ofreal-{ctx.state['prefix']}",
                "-n",
                "ssh-from-owner",
                "--priority",
                "100",
                "--access",
                "Allow",
                "--protocol",
                "Tcp",
                "--direction",
                "Inbound",
                "--source-address-prefixes",
                self.ssh_from,
                "--destination-port-ranges",
                "22",
            )
            ctx.az(
                "network",
                "vnet",
                "subnet",
                "create",
                "-g",
                self.rg,
                "--vnet-name",
                vnet,
                "-n",
                EDGE_SUBNET,
                "--address-prefixes",
                EDGE_SUBNET_PREFIX,
                "--network-security-group",
                f"nsg-ofreal-{ctx.state['prefix']}",
            )
            st["vnet"] = vnet
        with ctx.step("vm.create"):
            pub = self._keypair()
            vm = ctx.az(
                "vm",
                "create",
                "-g",
                self.rg,
                "-n",
                self.name,
                "-l",
                self.location,
                "--size",
                VM_SIZE,
                "--image",
                VM_IMAGE,
                "--security-type",
                "Standard",
                "--vnet-name",
                st["vnet"],
                "--subnet",
                EDGE_SUBNET,
                "--nsg",
                "",
                "--public-ip-sku",
                "Standard",
                "--admin-username",
                VM_USER,
                "--ssh-key-values",
                pub,
                "--os-disk-size-gb",
                "256",
                "--assign-identity",
                "[system]",
                "--tags",
                *ctx.az_tags(),
            )
            st["ip"] = vm["publicIpAddress"]
            st["private_ip"] = vm["privateIpAddress"]
            st["principal"] = (
                vm["identity"]["systemAssignedIdentity"]
                if "identity" in vm
                else vm.get("systemAssignedIdentity")
            )
            ctx.save_state()
        with ctx.step("vm.roles"):
            sub = ctx.state["identity"]["subscription"]
            scope = f"/subscriptions/{sub}/resourceGroups/{self.rg}"
            # Reader on the resource group, and blob data in its accounts for the VM-side checks.
            for role in ("Reader", "Storage Blob Data Contributor"):
                ctx.az(
                    "role",
                    "assignment",
                    "create",
                    "--assignee-object-id",
                    st["principal"],
                    "--assignee-principal-type",
                    "ServicePrincipal",
                    "--role",
                    role,
                    "--scope",
                    scope,
                )
        with ctx.step("vm.provision"):
            self.wait_ssh()
            self.put_dir(REAL / "vm", f"{REMOTE}/vm")
            self.sh(f"sudo {REMOTE}/vm/provision.sh", tag="provision")
            st["ready"] = True
            ctx.save_state()

    # ------------------------------------------------------------------ ssh

    def _ssh_base(self) -> list[str]:
        kh = self.ctx.out / "ssh" / "known_hosts"
        return [
            "-i",
            str(self.key),
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={kh}",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=30",
        ]

    @property
    def host(self) -> str:
        return f"{VM_USER}@{self.ctx.state['vm']['ip']}"

    def wait_ssh(self) -> None:
        def ok():
            p = self.ctx.run(
                ["ssh", *self._ssh_base(), "-o", "ConnectTimeout=10", self.host, "true"],
                check=False,
            )
            return p.returncode == 0

        self.ctx.wait("ssh to the test VM", ok, timeout_s=600)

    def sh(
        self, command: str, *, tag: str, input: str | None = None, timeout: int | None = None
    ) -> str:
        """Run COMMAND on the VM. Output goes to OUT/evidence/vm-<tag>.log as well."""
        p = self.ctx.run(
            ["ssh", *self._ssh_base(), self.host, command],
            input=input,
            check=False,
            timeout=timeout,
        )
        (self.ctx.evidence / f"vm-{tag}.log").write_text((p.stdout or "") + (p.stderr or ""))
        if p.returncode != 0:
            raise StepFailed(
                f"on the test VM, {tag} failed ({p.returncode}); see " f"evidence/vm-{tag}.log"
            )
        return p.stdout

    def script(self, body: str, *, tag: str, as_root: bool = False) -> str:
        """A guide step's bash, exactly as written, run on the VM from the release folder."""
        sudo = "sudo " if as_root else ""
        return self.sh(f"cd {REMOTE}/release && {sudo}bash -euo pipefail -s", input=body, tag=tag)

    def put(self, local: Path, remote: str) -> None:
        self.ctx.run(
            [
                "ssh",
                *self._ssh_base(),
                self.host,
                f"mkdir -p {shlex.quote(str(Path(remote).parent))}",
            ]
        )
        self.ctx.run(["scp", "-q", *self._ssh_base(), str(local), f"{self.host}:{remote}"])

    def put_dir(self, local: Path, remote: str) -> None:
        tar = self.ctx.out / f"payload-{local.name}.tar"
        subprocess.run(
            [
                "tar",
                "-C",
                str(local.parent),
                "-chf",
                str(tar),
                "--exclude",
                "__pycache__",
                local.name,
            ],
            check=True,
        )
        self.put(tar, f"{REMOTE}/{tar.name}")
        self.ctx.run(
            [
                "ssh",
                *self._ssh_base(),
                self.host,
                f"mkdir -p {shlex.quote(remote)} && tar -C {shlex.quote(remote)} "
                f"--strip-components=1 -xf {REMOTE}/{tar.name}",
            ]
        )

    def json_lines(self, out: str) -> list[dict]:
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows

    def socks(self, port: int) -> subprocess.Popen:
        """ssh -D: the owner's headed browser reaches the private portal through the VM,
        with names resolved on the VM side (the internal environment's private DNS)."""
        cmd = ["ssh", *self._ssh_base(), "-N", "-D", f"127.0.0.1:{port}", self.host]
        with (self.ctx.out / "commands.txt").open("a") as f:
            f.write(" ".join(cmd) + "\n")
        proc = subprocess.Popen(
            cmd,
            env=self.ctx.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # The tunnel must be up before the browser starts (and, in a dry run, the shim has
        # recorded it and exited: the transcript's order is then fixed).
        if self.ctx.dry_run:
            proc.wait(timeout=30)
        else:
            import socket
            import time

            end = time.monotonic() + 60
            while time.monotonic() < end:
                with socket.socket() as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        break
                if proc.poll() is not None:
                    raise StepFailed("the SSH tunnel to the test VM exited at once")
                time.sleep(1)
            else:
                raise StepFailed("the SSH tunnel to the test VM didn't open in 60 s")
        return proc
