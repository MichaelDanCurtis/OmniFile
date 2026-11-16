"""The M6 exit on real Azure (docs/BUILD-PLAN.md M6): the fresh appliance plus the Bicep
deploy, set up by the guide only, reach the M1-M4 behaviour. Driven only through public
interfaces: NFS and SMB from the test VM (the client), the portal and its REST API from a
signed-in browser, and Azure's own storage API for what a user can't see (the tier).

Each step's result is kept in state["exit"][step] and evidence/exit-<step>.json; the
real-* lines proven by the exit (kind "exit" in harness/checks) take their status from it.
"""

from __future__ import annotations

import json
from typing import Any

from .core import Context, InjectedFailure, StepFailed, parse_iso
from .portal import Portal
from .results import exit_result
from .vm import REMOTE, TestVM

# (step, where, what it proves)
STEPS: list[tuple[str, str, str]] = [
    ("edge-enrolled", "portal", "the appliance enrolled through the guide and reports health"),
    ("nfs3-write", "vm", "a 64 MiB SEG-Y-named file written over NFSv3"),
    ("nfs4-write", "vm", "the same over NFSv4.2"),
    ("smb-write", "vm", "the same over SMB 3 as an AD user (Kerberos)"),
    ("uploaded", "portal", "the three files are committed in the catalog with their CRC"),
    ("stubbed", "vm", "the edge freed them: blocks on the share drop to the resident headers"),
    ("read-back", "vm", "a stub read over NFSv3, NFSv4 and SMB returns the original bytes"),
    ("pin-archive", "portal", "an Owner pins one file to Archive (decision 29)"),
    ("archived", "vm", "Azure reports the blob in the Archive tier of the archive account"),
    ("recall-high", "portal", "a High-priority recall of it is queued"),
    ("recalled", "vm", "the recall completes and the file reads back byte for byte"),
    ("worm-share", "portal", "a share on the WORM dataset is added"),
    ("worm-enable", "portal", "WORM turned on after a fresh sign-in (prompt=login)"),
    ("worm-write", "vm", "an overwrite and a delete over NFS and SMB on the WORM share"),
    ("worm-retained", "portal", "both versions and the tombstone are kept, with retention"),
    ("worm-immutable", "vm", "Azure refuses to delete the retained version"),
    ("r3-gate", "vm", "the signed x86 R3 gate on this VM's KVM (appliance/release.sh)"),
]


class M6Exit:
    def __init__(self, ctx: Context, vm: TestVM, portal: Portal) -> None:
        self.ctx, self.vm, self.portal = ctx, vm, portal
        self.facts: dict[str, Any] = ctx.state.setdefault("exit_facts", {})
        pre = ctx.state["prefix"]
        self.facts.setdefault("prefix", pre)
        outs = ctx.state.get("outputs", {})
        self.facts.setdefault("archive_account", outs.get("archiveAccount") or f"st{pre}arch")
        self.facts.setdefault("worm_account", outs.get("wormAccount") or f"st{pre}wtst")

    def run(self) -> None:
        failed = []
        for name, where, what in STEPS:
            prev = self.ctx.state.get("exit", {}).get(name)
            if prev and prev["status"] == "pass":
                continue
            try:
                with self.ctx.step(f"exit.{name}"):
                    got = self._vm(name) if where == "vm" else self._portal(name)
                status = got.pop("status", "pass")
                self.facts.update(got.pop("facts", {}))
                (self.ctx.evidence / f"exit-{name}.json").write_text(
                    json.dumps(got, indent=2, sort_keys=True) + "\n"
                )
                exit_result(self.ctx, name, status, what, f"evidence/exit-{name}.json")
            except InjectedFailure:
                raise
            except StepFailed as e:
                exit_result(self.ctx, name, "fail", f"{what}: {e}")
                failed.append(name)
                if name in ("nfs3-write", "uploaded", "edge-enrolled"):
                    break  # nothing after these can pass
        self.ctx.save_state()
        if failed:
            raise StepFailed(f"M6 exit failed at: {', '.join(failed)}")
        pending = [n for n, r in self.ctx.state.get("exit", {}).items() if r["status"] != "pass"]
        self.ctx.log(
            "M6 exit: every step passed"
            if not pending
            else f"M6 exit: no step failed; not passed yet: {', '.join(pending)}"
        )

    def _vm(self, name: str) -> dict[str, Any]:
        params = json.dumps(
            {"step": name, "facts": self.facts, "secrets_file": f"{REMOTE}/secrets.json"}
        )
        if name == "r3-gate" and not self.ctx.state.get("r3_dir_on_vm"):
            return {"status": "pending", "detail": "no R3 build given (--r3-dir)"}
        out = self.vm.sh(f"sudo {REMOTE}/vm/exit.sh {name}", input=params, tag=f"exit-{name}")
        rows = self.vm.json_lines(out)
        if not rows or not rows[-1].get("ok"):
            raise StepFailed(f"{name}: {rows[-1] if rows else 'no result'}")
        r = rows[-1]
        return {"facts": r.get("facts", {}), "result": r}

    def _portal(self, name: str) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if name == "worm-enable":
            # Longer than the run: the service must not lock the policy before teardown.
            left = (parse_iso(self.ctx.expires) - self.ctx.now).total_seconds() / 3600
            extra["test_window_hours"] = int(left) + 24
        r = self.portal.act(f"exit-{name}", facts=self.facts, **extra)
        return {"facts": r.get("facts", {}), "result": r}
