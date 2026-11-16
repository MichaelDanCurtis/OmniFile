"""`run.sh all --dry-run` end to end for each cloud, against the golden transcripts.

Set REAL_UPDATE_GOLDEN=1 to rewrite tests/real/golden/ after an intended change, and read
the diff before committing it.
"""

from __future__ import annotations

import json
import os

import pytest
from conftest import GOLDEN, REPO, harness, normalize


@pytest.mark.parametrize("cloud", ["azure", "aws", "tenant"])
def test_all_dry_run_passes_end_to_end_and_matches_its_golden_transcript(cloud, out):
    r = harness("all", cloud, out)
    assert r.code == 0, r.stdout[-3000:]
    assert r.teardown["runs"]["rr-dry-0001"]["residue"] == []
    assert r.state["down"] is True
    counts = {}
    for v in r.results.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    assert counts.get("fail", 0) == 0
    assert counts.get("pass", 0) > 0
    golden = GOLDEN / f"{cloud}.transcript"
    got = normalize(r.transcript)
    if os.environ.get("REAL_UPDATE_GOLDEN") == "1":
        golden.write_text(got)
    assert got == golden.read_text(), f"{cloud} transcript differs from {golden}"


def test_the_golden_transcripts_reach_no_real_endpoint():
    for f in GOLDEN.glob("*.transcript"):
        text = f.read_text()
        assert "127.0.0.1:" not in text  # the moto server's address is written as $MOTO
        assert str(REPO) not in text
        for line in text.splitlines():
            if line.startswith("boto3 "):
                continue
            assert line.split()[0] in {
                "az",
                "aws",
                "gh",
                "ssh",
                "scp",
                "docker",
                "node",
                "env",
                "env-file",
            }, line


def test_the_azure_dry_run_runs_every_kind_of_guide_step(out):
    r = harness("all", "azure", out)
    assert r.code == 0, r.stdout[-2000:]
    st = r.state
    manual = {m["n"]: m["on"] for m in st["guide_manual"]}
    # manual=yes and a host the run doesn't have are recorded, not run.
    assert manual == {8: "workstation", 11: "hyperv"}
    # The AWS step (cloud=aws) belongs to the AWS run.
    assert 17 not in st["guide_done"]
    assert sorted(st["guide_done"]) == [*range(1, 17), 18, 19]
    # plan ran only the plan=yes steps and the gate.
    assert sorted(st["plan_done"]) == [1, 3, 4]
    t = "\n".join(r.transcript)
    # on=workstation: the guide's own deploy, of what its own what-if previewed
    assert "az deployment group create --resource-group rg-ofreal-rr-dry-0001 --name omnifile" in t
    assert "/vm/console-answers.py" in t  # on=edge-console
    assert "/vm/edge-shell.sh" in t or "cat > /srv/ofreal/release//etc/omnifile/extra.toml" in t
    assert "node tests/real/portal/driver.mjs guide" in t  # on=portal
    # The portal's enrolment code is captured and fills the console's answers.
    secrets = json.loads((out / "secrets.json").read_text())
    assert secrets["captured:ENROL_CODE"] == "dry-enrol_code"
    assert "captured" not in st  # a credential: never in state.json


def test_each_workstation_step_runs_exactly_as_written(out):
    from harness import guide

    r = harness("all", "azure", out)
    assert r.code == 0
    for s in guide.parse(REPO / "tests/real/fakes/guide"):
        f = out / "guide" / f"step-{s.n}.sh"
        if s.on == "workstation" and not s.manual and s.cloud == "azure":
            assert f.read_text() == s.body, f"step {s.n} was not run as written"


def test_a_dry_run_never_ticks_the_real_docs(out):
    docs = [REPO / f"docs/milestones/real-{c}-checks.md" for c in ("azure", "aws", "tenant")]
    before = [d.read_bytes() for d in docs]
    r = harness("all", "azure", out)
    assert r.code == 0
    assert [d.read_bytes() for d in docs] == before
    ticked = (out / "docs" / "real-azure-checks.md").read_text()
    assert "- [x] RA-01 " in ticked and "(passed in run rr-dry-0001:" in ticked
