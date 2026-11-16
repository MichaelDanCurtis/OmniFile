"""collect: the run's results, and the real-* docs ticked for every line that passed.

A real run copies each passed line's evidence to tests/real/results/<run id>/ (committed
with the docs, so the tick links to something) and ticks the line in docs/milestones/
real-*-checks.md with the run id and that path. A dry run never touches the docs: it ticks
copies in OUT/docs/ and links OUT/evidence/, so a test can check the ticking itself.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import docs
from .core import REAL, REPO, Context, HarnessError
from .results import load


def collect(ctx: Context) -> int:
    results = load(ctx)
    mixed = sorted(c for c, r in results.items() if r.get("dry_run") != ctx.dry_run)
    if mixed:
        raise HarnessError(
            f"results {', '.join(mixed)} come from a "
            f"{'real' if ctx.dry_run else 'dry'} run: nothing is ticked"
        )
    passed = {cid: r for cid, r in results.items() if r["status"] == "pass"}
    if ctx.dry_run:
        dest_docs = ctx.out / "docs"
        dest_docs.mkdir(parents=True, exist_ok=True)
        ev_root = ctx.out / "results" / ctx.run_id  # the same copy, in OUT
    else:
        dest_docs = None
        ev_root = REAL / "results" / ctx.run_id
    ev_root.mkdir(parents=True, exist_ok=True)
    notes: dict[str, str] = {}
    for cid, r in passed.items():
        src = ctx.out / r["evidence"]
        shutil.copy2(src, ev_root / src.name)
        # The files the line's evidence points to (an exit step's), copied too, so the
        # committed evidence is whole rather than pointers into OUT.
        for ref in _refs(src):
            f = ctx.out / ref
            if f.is_file():
                shutil.copy2(f, ev_root / Path(ref).name)
        dest = ev_root / src.name
        link = dest.relative_to(REPO) if dest.is_relative_to(REPO) else dest
        notes[cid] = f"{ctx.run_id}: {link}"
    ticked: list[str] = []
    for prefix, doc in docs.DOCS.items():
        mine = {c: n for c, n in notes.items() if c.startswith(prefix)}
        text, done = docs.tick(doc.read_text(), mine)
        ticked += done
        if dest_docs is not None:
            (dest_docs / doc.name).write_text(text)
        elif done:
            doc.write_text(text)
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = {
        "run": ctx.run_id,
        "cloud": ctx.cloud,
        "dry_run": ctx.dry_run,
        "counts": counts,
        "ticked": sorted(ticked),
        "docs": str(dest_docs or "docs/milestones"),
    }
    (ctx.out / "collect.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    ctx.log(
        "collect: "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        + f"; ticked {len(ticked)}"
        + (" (copies only: dry run)" if ctx.dry_run else "")
    )
    return 1 if counts.get("fail") else 0


def _refs(evidence_file: Path) -> list[str]:
    """`evidence/...` paths an evidence file names (an exit step's file, say)."""
    import re

    text = evidence_file.read_text()
    return sorted(
        set(re.findall(r"evidence/[A-Za-z0-9_.-]+", text)) - {f"evidence/{evidence_file.name}"}
    )
