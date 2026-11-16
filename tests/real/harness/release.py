"""The release folder a run executes the guide in: what a customer unpacks.

OUT/release/ has every top-level entry of the repo as a symlink, except deploy/, which is a
copy (guide steps write into it: relay-allowed-cidrs.json, deploy/teams/out/, the images
lock), so nothing a step writes lands in the checkout. Dry runs and real runs use the same
scripts: WP1's, from the checkout.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .core import FAKES, REPO

#: The guide's tools a dry run replaces in its copy of the release: check-private-dns.sh asks
#: this machine's resolver about the deployment's real Azure names; its fake asks the fake az's
#: DNS model (private zones linked, endpoints made) instead.
DRY_TOOLS = ("check-private-dns.sh",)

SKIP = {".git", "tests", ".venv", "target", "node_modules"}


def guide_copy(rel: Path, g: Path) -> None:
    """A dry run's docs/guide: a fresh copy each time, so the guide's few tools that ask this
    machine's network about real Azure names can be the fakes' (DRY_TOOLS). The steps
    themselves stay exactly as written."""
    dst = rel / "docs" / "guide"
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    if not g.is_dir():
        return
    shutil.copytree(g.resolve(), dst, symlinks=True)
    for name in DRY_TOOLS:
        tool = dst / "tools" / name
        if tool.exists():
            shutil.copy2(FAKES / "guide-tools" / name, tool)


def make(out: Path, *, dry_run: bool, guide: Path | None = None) -> Path:
    """GUIDE, when given, is the guide under test: it appears at docs/guide in the release
    folder (its steps name docs/guide/tools/...), whether or not it is the repo's own."""
    rel = out / "release"
    g = guide or (REPO / "docs" / "guide")
    if rel.exists():
        if dry_run:
            guide_copy(rel, g)  # as the guide is now, as a symlink would show it
        return rel
    rel.mkdir(parents=True)
    for entry in sorted(REPO.iterdir()):
        if entry.name in SKIP or entry.name in ("deploy", "docs"):
            continue
        (rel / entry.name).symlink_to(entry)
    (rel / "docs").mkdir()
    for entry in sorted((REPO / "docs").iterdir()):
        if entry.name != "guide":
            (rel / "docs" / entry.name).symlink_to(entry)
    if g.is_dir() and dry_run:
        guide_copy(rel, g)
    elif g.is_dir():
        (rel / "docs" / "guide").symlink_to(g.resolve())
    # tests/real's own VM and portal scripts, which some steps name.
    (rel / "tests").mkdir()
    (rel / "tests" / "real").symlink_to(REPO / "tests" / "real")
    shutil.copytree(
        REPO / "deploy",
        rel / "deploy",
        symlinks=False,
        ignore=shutil.ignore_patterns("relay-allowed-cidrs.json", "out"),
    )
    return rel
