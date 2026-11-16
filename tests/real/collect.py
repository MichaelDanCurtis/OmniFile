#!/usr/bin/env python3
"""Tick the real-* checklists from a run's results, and record manual steps.

    tests/real/collect.py --out tests/real/out/<run id>
        Tick every line that passed in that run (as `run.sh collect` does). A real run's
        evidence is copied to tests/real/results/<run id>/ and linked from the line; a dry
        run's ticks go to copies in OUT/docs/, never to docs/milestones/.

    tests/real/collect.py --out DIR --record RT-08=pass --note "card updated on mobile" \
        [--evidence screenshot.png]
        Record the outcome of a manual step (harness/checks lists each one's steps, and
        `run.sh checks` writes them to OUT/manual-steps-<cloud>.txt), then tick as above.

Run it with the harness's environment: uv run --project tests/real tests/real/collect.py ...
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import collect as collect_mod  # noqa: E402
from harness.checks import STATUSES, load_all  # noqa: E402
from harness.core import Context, iso  # noqa: E402
from harness.results import record  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--record", action="append", default=[])
    ap.add_argument("--note", default="")
    ap.add_argument("--evidence", type=Path)
    a = ap.parse_args(argv)
    state = json.loads((a.out / "state.json").read_text())
    ctx = Context(
        phase="collect",
        cloud=state["cloud"],
        dry_run=state["dry_run"],
        out=a.out.resolve(),
        run_id=state["run_id"],
        now=datetime.now(UTC),
    )
    ctx.state = state
    reg = load_all()
    for item in a.record:
        cid, _, status = item.partition("=")
        if cid not in reg:
            print(f"collect.py: no check {cid}", file=sys.stderr)
            return 2
        if reg[cid].kind != "manual":
            print(f"collect.py: {cid} is proven by the harness, not by hand", file=sys.stderr)
            return 2
        if status not in STATUSES or status in ("manual", "pending"):
            print(f"collect.py: record pass, fail or partial, not {status!r}", file=sys.stderr)
            return 2
        ev = {"note": a.note, "recorded_by_hand": True, "at": iso(ctx.now)}
        if a.evidence:
            dst = ctx.evidence / f"{cid}-{a.evidence.name}"
            shutil.copy2(a.evidence, dst)
            ev["file"] = dst.name
        record(ctx, cid, status, a.note or f"manual step recorded {status}", ev)
    return collect_mod.collect(ctx)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
