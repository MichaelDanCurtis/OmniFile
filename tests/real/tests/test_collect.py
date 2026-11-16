"""collect.py: ticking from a run's results, and recording manual steps."""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import REAL, REPO, clean_env, harness


def collect(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REAL / "collect.py"), *args],
        capture_output=True,
        text=True,
        env=clean_env(),
        cwd=REPO,
    )


def test_a_manual_step_is_recorded_and_ticked_in_the_dry_runs_copy(out, tmp_path):
    r = harness("all", "tenant", out)
    assert r.code == 0
    shot = tmp_path / "card.png"
    shot.write_bytes(b"\x89PNG fake")
    p = collect(
        "--out",
        str(out),
        "--record",
        "RT-11=pass",
        "--note",
        "mobile and new Teams",
        "--evidence",
        str(shot),
    )
    assert p.returncode == 0, p.stderr
    res = json.loads((out / "results.json").read_text())["RT-11"]
    assert res["status"] == "pass"
    ev = json.loads((out / res["evidence"]).read_text())
    assert ev["evidence"]["recorded_by_hand"] and ev["evidence"]["file"] == "RT-11-card.png"
    assert "- [x] RT-11 " in (out / "docs" / "real-tenant-checks.md").read_text()
    assert "- [ ] RT-11 " in (REPO / "docs/milestones/real-tenant-checks.md").read_text()


def test_an_automated_line_cant_be_recorded_by_hand(out):
    harness("all", "tenant", out)
    p = collect("--out", str(out), "--record", "RT-13=pass")
    assert p.returncode == 2 and "proven by the harness" in p.stderr


def test_pending_or_manual_is_not_a_recordable_outcome(out):
    harness("all", "tenant", out)
    p = collect("--out", str(out), "--record", "RT-11=manual")
    assert p.returncode == 2


def test_partial_and_manual_lines_are_never_ticked(out):
    r = harness("all", "aws", out)
    assert r.code == 0
    ticked = (out / "docs" / "real-aws-checks.md").read_text()
    # A line a real run has ticked in the repo's file stays ticked in the dry run's copy,
    # whatever the fakes can prove of it; the dry run itself ticks only what it passes.
    before = (REPO / "docs/milestones/real-aws-checks.md").read_text()
    for cid, v in r.results.items():
        was = f"- [x] {cid} " in before
        box = "x" if v["status"] == "pass" or was else " "
        assert f"- [{box}] {cid} " in ticked, (cid, v["status"])
