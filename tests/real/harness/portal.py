"""The portal, driven by Playwright (tests/real/portal/driver.mjs) on the owner's workstation.

The portal is private: the browser reaches it through an SSH SOCKS proxy over the test VM,
so it sees the portal's real origin and its Entra redirect works unchanged. The first action
opens a headed browser and waits while the owner completes the Entra sign-in themselves
(the harness never sees a password); the session is kept in OUT/portal/state.json for the
later actions, which run headless. A WORM change asks for a fresh sign-in (prompt=login),
so that action is headed again.
"""

from __future__ import annotations

import json
from pathlib import Path

from .core import REAL, Context, StepFailed

DRIVER = REAL / "portal" / "driver.mjs"


class Portal:
    def __init__(self, ctx: Context, url: str, socks_port: int | None) -> None:
        self.ctx, self.url, self.socks_port = ctx, url, socks_port
        self.dir = ctx.out / "portal"
        self.dir.mkdir(parents=True, exist_ok=True)

    def act(self, action: str, **params) -> dict:
        cfg = {
            "action": action,
            "url": self.url,
            "socks": f"socks5://127.0.0.1:{self.socks_port}" if self.socks_port else None,
            "state": str(self.dir / "state.json"),
            "evidence": str(self.ctx.evidence),
            "params": params,
        }
        f = self.dir / f"{action}.json"
        f.write_text(json.dumps(cfg, indent=2, sort_keys=True))
        p = self.ctx.run(
            ["node", str(DRIVER), str(f)],
            check=False,
            timeout=3600,
            env={"NODE_PATH": str(REAL.parents[1] / "portal" / "e2e" / "node_modules")},
        )
        rows = [json.loads(ln) for ln in (p.stdout or "").splitlines() if ln.startswith("{")]
        if p.returncode != 0 or not rows or not rows[-1].get("ok"):
            raise StepFailed(f"portal {action} failed: {(p.stderr or '').strip()[-400:]}")
        return rows[-1]

    def steps(self, body: str, tag: str) -> dict:
        """A guide on=portal step (open, click, fill, choose, expect), as written."""
        return self.act("guide", lines=body.splitlines(), tag=tag)


def evidence_path(ctx: Context, name: str) -> Path:
    return ctx.evidence / name
