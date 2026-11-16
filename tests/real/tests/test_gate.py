"""up stops after what-if (Azure) or the harness's plan (AWS, tenant) until --approve gives
that digest, and a digest is stable for the same run."""

from __future__ import annotations

import re

import pytest
from conftest import harness

DIGEST = re.compile(r"(?:what-if|plan) digest (sha256:[0-9a-f]{64})")
MUTATING = (
    "deployment group create",
    "acr create",
    "vm create",
    "CreateBucket",
    "CreateVpc",
    "ad app create",
)


def digest(r) -> str:
    m = DIGEST.search(r.stdout)
    assert m, r.stdout[-1500:]
    return m.group(1)


@pytest.mark.parametrize("cloud", ["azure", "aws", "tenant"])
def test_up_without_approval_stops_at_the_gate_and_makes_nothing(cloud, out):
    p = harness("plan", cloud, out)
    assert p.code == 0, p.stdout[-1500:]
    n = len(p.transcript)
    r = harness("up", cloud, out, "--run-id", "rr-dry-0001")
    assert r.code == 3
    assert "awaiting approval" in r.stdout
    after = r.transcript[n:]
    assert not [ln for ln in after if any(m in ln for m in MUTATING)], after


@pytest.mark.parametrize("cloud", ["azure", "aws", "tenant"])
def test_a_wrong_digest_is_refused(cloud, out):
    n = len(harness("plan", cloud, out).transcript)
    wrong = "sha256:" + "0" * 64
    r = harness("up", cloud, out, "--run-id", "rr-dry-0001", "--approve", wrong)
    assert r.code == 3
    assert "changed" in r.stdout or "don't include this run's plan" in r.stdout
    assert not [ln for ln in r.transcript[n:] if any(m in ln for m in MUTATING)]


def test_the_digest_is_stable_for_a_run_and_approving_it_deploys(out):
    a = digest(harness("plan", "azure", out))
    b = digest(harness("plan", "azure", out, "--run-id", "rr-dry-0001"))
    assert a == b
    r = harness("up", "azure", out, "--run-id", "rr-dry-0001", "--approve", a)
    # The first deploy goes; the fixture's phase 2 then waits for its own digest.
    assert r.code == 3, r.stdout[-2000:]
    assert any("deployment group create" in ln for ln in r.transcript)
    a2 = r.state["pending_gate"]["digest"]
    r = harness("up", "azure", out, "--run-id", "rr-dry-0001", "--approve", a, "--approve", a2)
    assert r.code == 0, r.stdout[-2000:]
    harness("down", "azure", out, "--run-id", "rr-dry-0001")


def test_another_run_has_another_digest(tmp_path):
    a = digest(harness("plan", "azure", tmp_path / "a", "--run-id", "rr-dry-0001"))
    b = digest(harness("plan", "azure", tmp_path / "b", "--run-id", "rr-dry-0002"))
    assert a != b


def test_a_malformed_digest_is_a_usage_error(out):
    harness("plan", "aws", out)
    r = harness("up", "aws", out, "--run-id", "rr-dry-0001", "--approve", "abc")
    assert r.code == 2
    assert "not a digest" in r.stdout


def test_up_without_approval_in_a_real_run_makes_nothing(monkeypatch, tmp_path):
    """The gate's own code path: no --approve, not a dry run -> AwaitingApproval, before any
    resource is made."""
    from harness.azure import Azure
    from harness.core import AwaitingApproval, Context, parse_iso

    ctx = Context(
        phase="up",
        cloud="azure",
        dry_run=False,
        out=tmp_path,
        run_id="rr-dry-0001",
        now=parse_iso("2026-11-03T00:00:00Z"),
    )
    ctx.state = {"digest": "sha256:" + "1" * 64, "created": True}
    az = Azure(ctx, None)  # type: ignore[arg-type]
    monkeypatch.setattr(az, "identity", lambda: {})
    monkeypatch.setattr(ctx, "az", lambda *a, **k: pytest.fail(f"az {a} before an approval"))
    with pytest.raises(AwaitingApproval):
        az.up()


def test_plan_then_all_with_its_digest_deploys(out):
    """`plan`, then `all --run-id R --approve D`: all reuses the plan, so D still holds.
    Without a terminal, `all` pauses at phase 2's gate with the run left up, and the resume
    it prints (`all ... --approve D --approve D2`) carries on from there to the teardown."""
    import re

    d = digest(harness("plan", "azure", out))
    r = harness("all", "azure", out, "--run-id", "rr-dry-0001", "--approve", d)
    assert r.code == 3, r.stdout[-2000:]
    assert "guide step 18 previews another deploy" in r.stdout
    assert not r.state.get("down") and r.state["paused_at"] == 18
    assert not [ln for ln in r.transcript if ln.startswith("az group delete")]
    resume = re.search(
        r"tests/real/run\.sh (all --cloud azure --run-id \S+(?: --approve \S+)+)", r.stdout
    )
    assert resume, r.stdout[-1500:]
    args = resume.group(1).split()
    d2 = args[-1]
    assert d2 != d and args[-4:] == ["--approve", d, "--approve", d2]
    n = len(r.transcript)
    r = harness(*args[:1], "azure", out, *args[3:])
    assert r.code == 0, r.stdout[-2000:]
    later = r.transcript[n:]
    assert "resuming at guide step 18" in r.stdout
    assert len([c for c in later if c.startswith("az deployment group create")]) == 1
    assert sum(1 for ln in r.transcript if ln.startswith("az ad group create")) == 1
    assert r.state.get("down") is True


def test_up_refuses_to_resume_a_run_that_is_down(out):
    d = digest(harness("plan", "azure", out))
    r = harness("up", "azure", out, "--run-id", "rr-dry-0001", "--approve", d)
    assert r.code == 3 and r.state["paused_at"] == 18
    d2 = r.state["pending_gate"]["digest"]
    assert harness("down", "azure", out, "--run-id", "rr-dry-0001").code == 0
    n = len((out / "transcript.txt").read_text().splitlines())
    r = harness("up", "azure", out, "--run-id", "rr-dry-0001", "--approve", d, "--approve", d2)
    assert r.code == 2 and "was torn down; plan it again" in r.stdout, r.stdout[-1500:]
    assert r.transcript[n:] == []  # not even an identity call
    # planned again, it starts from step 1
    assert digest(harness("plan", "azure", out, "--run-id", "rr-dry-0001")) == d
    r = harness("up", "azure", out, "--run-id", "rr-dry-0001", "--approve", d, "--approve", d2)
    assert r.code == 0, r.stdout[-1500:]
    assert "resuming" not in r.stdout
    harness("down", "azure", out, "--run-id", "rr-dry-0001")


def test_the_owner_approves_the_guides_own_what_if(out):
    """The digest is the one the guide's gate step prints for what the guide deploys."""
    r = harness("plan", "azure", out)
    d = digest(r)
    assert f"digest: {d}" in (out / "evidence" / "whatif.txt").read_text()
    # WP1's whatif.sh compiles the guide's own parameters file (PARAMS) and previews that.
    assert (
        "PARAMS='docs/guide/customer.bicepparam'" in (out / "release" / "omnifile.env").read_text()
    )
    assert '-p "$PARAMS"' in (out / "guide" / "step-4.sh").read_text()
    assert any(ln.startswith("az deployment group what-if") for ln in r.transcript)


def test_a_change_after_approval_stops_right_after_the_guides_what_if(tmp_path):
    """customer.bicepparam changes between plan and up: up stops after the gate step, and
    the guide's deploy step never runs."""
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / "guide"
    shutil.copytree(FIXTURE_GUIDE, g)
    out = tmp_path / "out"
    d = digest(harness("plan", "azure", out, guide=g))
    p = g / "customer.bicepparam"
    p.write_text(p.read_text() + "param apiMaxReplicas = 10\n")
    n = len((out / "transcript.txt").read_text().splitlines())
    r = harness("up", "azure", out, "--run-id", "rr-dry-0001", "--approve", d, guide=g)
    assert r.code == 3, r.stdout[-1500:]
    assert "now gives" in r.stdout
    assert not [ln for ln in r.transcript[n:] if "deployment group create" in ln]
    assert 5 not in r.state["guide_done"]


def test_a_guide_without_a_gate_is_refused(tmp_path):
    import shutil

    from conftest import FIXTURE_GUIDE

    g = tmp_path / "guide"
    shutil.copytree(FIXTURE_GUIDE, g)
    readme = g / "README.md"
    readme.write_text(readme.read_text().replace(" gate=whatif", ""))
    r = harness("plan", "azure", tmp_path / "out", guide=g)
    assert r.code == 4
    assert "gate=whatif" in r.stdout
