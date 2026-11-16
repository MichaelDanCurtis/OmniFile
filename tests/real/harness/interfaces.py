"""The scripts other work packages own, which the harness and the guide call by their repo
paths: WP1's (PR #34, on main). Dry runs and real runs both use the real ones; only the
clouds behind them are faked (az and gh through the shims). A missing one is a refusal.
"""

from __future__ import annotations

from pathlib import Path

from .core import REPO, Refused

PATHS = {
    "whatif": "deploy/azure/whatif.sh",
    "test-params": "deploy/azure/test.bicepparam",
    "images-build": "deploy/images/build.sh",
    "images-lock": "deploy/images/images.lock.json",
    "entra-register": "deploy/entra/register.sh",
    "teams-build": "deploy/teams/build.sh",
}


def resolve(name: str) -> Path:
    rel = PATHS[name]
    real = REPO / rel
    if not real.exists():
        raise Refused(f"{rel} is not on this branch (WP1's); the harness needs it")
    return real


def missing() -> list[str]:
    return [rel for rel in PATHS.values() if not (REPO / rel).exists()]
