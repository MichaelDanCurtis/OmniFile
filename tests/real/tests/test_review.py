"""The safety review of #36: each finding, and a test that fails without its fix."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import REAL, clean_env, harness

RUN = "rr-dry-0001"
SUB = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000ff"


# ------------------------------------------------------------- identity is pinned


def owner_profile(out, default):
    """The owner's own az directory in a dry run, signed in to both subscriptions."""
    f = out / "dry" / "azure" / "azureProfile.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps(
            {
                "subscriptions": [
                    {
                        "id": x,
                        "tenantId": "00000000-0000-4000-8000-0000000000b1",
                        "isDefault": x == default,
                        "user": {"name": "owner@fake.example", "type": "user"},
                    }
                    for x in (SUB, OTHER)
                ]
            }
        )
    )
    return f


def test_another_shells_az_account_set_cant_redirect_a_run(out):
    """After plan, the owner's az default moves to another subscription (another shell's
    `az account set`). The next phase still pins to the subscription the run was planned
    in, and acts only there."""
    owner_profile(out, SUB)
    assert harness("plan", "azure", out).code == 0
    owner_profile(out, OTHER)
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto")
    assert r.code == 0, r.stdout[-1500:]
    assert set(r.fake_state()["acted_in"]) == {SUB}
    harness("down", "azure", out, "--run-id", RUN)


def test_aws_steps_get_the_allow_listed_profile_and_a_fresh_identity_check(out):
    r = harness("all", "aws", out)
    assert r.code == 0, r.stdout[-1500:]
    assert "aws profile=fake region=us-west-2" in (out / "guide" / "step-17.log").read_text()
    t = r.transcript
    first_cli = next(i for i, ln in enumerate(t) if ln.startswith("aws "))
    checks = [i for i, ln in enumerate(t[:first_cli]) if "sts.GetCallerIdentity" in ln]
    # the last identity check comes after everything up made, right before the steps
    assert checks and all(
        "Create" not in ln and "Put" not in ln and "Run" not in ln
        for ln in t[checks[-1] + 1 : first_cli]
        if ln.startswith("boto3")
    )


def test_the_azure_run_never_fills_the_guides_aws_section(monkeypatch):
    from harness import guidevalues

    monkeypatch.setenv("REAL_AWS_RUN", RUN)
    assert set(guidevalues.aws_values().values()) == {""}


def test_a_dry_run_drops_real_aws_run(tmp_path, monkeypatch):
    from harness import core

    monkeypatch.setenv("REAL_AWS_RUN", RUN)
    ctx = core.Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id=RUN,
        now=core.parse_iso(core.DRY_NOW),
    )
    ctx.build_env()
    assert "REAL_AWS_RUN" not in ctx.env


# --------------------------------------------------------- the guide's own values


def test_the_test_values_reach_the_guides_parameters(out):
    r = harness("all", "azure", out)
    assert r.code == 0, r.stdout[-1500:]
    env = next(ln for ln in r.transcript if ln.startswith("env ") and "OMNI_TEST_RUN_ID" in ln)
    for want in (
        "OMNI_TEST_RUN_ID=rr-dry-0001",
        "OMNI_TEST_RUN_EXPIRES=2026-11-03T12:00Z",
        "OMNI_DEPLOYMENT_SIZE=small",
        "OMNI_TEST_KV_PURGE_PROTECTION=false",
        "OMNI_TEST_LOCK_WORM=false",
    ):
        assert want in env, want
    # the size's own server and replicas: a test run overrides none of them (#59)
    for k in (
        "OMNI_TEST_PG_SKU_TIER",
        "OMNI_TEST_PG_SKU_NAME",
        "OMNI_TEST_PG_STORAGE_GB",
        "OMNI_TEST_API_MIN_REPLICAS",
        "OMNI_TEST_API_MAX_REPLICAS",
        "OMNI_TEST_WORKER_MAX_REPLICAS",
    ):
        assert not [w for w in env.split() if w.startswith(f"{k}=") and w != f"{k}="], k


# -------------------------------------------------- a dry run's evidence stays dry


def test_a_real_run_takes_no_exit_result_from_a_dry_azure_run(tmp_path, monkeypatch):
    from harness import results
    from harness.checks import load_all
    from harness.core import Context

    dry = tmp_path / "out" / RUN
    dry.mkdir(parents=True)
    dry.joinpath("state.json").write_text(
        json.dumps(
            {
                "dry_run": True,
                "up": True,
                "exit": {
                    "worm-enable": {"status": "pass", "detail": "dry", "evidence": None},
                    "edge-enrolled": {"status": "pass", "detail": "dry", "evidence": None},
                },
            }
        )
    )
    monkeypatch.setattr(results, "REAL", tmp_path)
    reg = load_all()
    only = {c for c, v in reg.items() if (v.exit_step or "").startswith("azure:")}
    ctx = Context(
        phase="checks",
        cloud="tenant",
        dry_run=False,
        out=tmp_path / "t",
        run_id="rr-202611030000-abcd",
        now=datetime.now(UTC),
        azure_run=RUN,
    )
    counts = results.run_checks(ctx, env=None, only=only)
    assert counts["pass"] == 0 and counts["pending"] == len(only)


def test_collect_refuses_results_of_the_other_kind(out):
    r = harness("all", "tenant", out)
    assert r.code == 0
    res = json.loads((out / "results.json").read_text())
    res["RT-13"]["dry_run"] = False
    (out / "results.json").write_text(json.dumps(res))
    p = subprocess.run(
        [sys.executable, str(REAL / "collect.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        env=clean_env(),
    )
    assert p.returncode != 0 and "come from a real run" in (p.stdout + p.stderr)


def test_collect_copies_an_exit_steps_evidence_with_the_line(out):
    r = harness("all", "azure", out)
    assert r.code == 0
    copied = out / "results" / RUN
    assert (copied / "RA-19.json").exists()  # RA-19 is proven by the exit's edge-enrolled
    assert (copied / "exit-edge-enrolled.json").exists()


# ------------------------------------------------------------- run ids are strict


@pytest.mark.parametrize("bad", ["rr-x'; drop", "../../etc", "rr-20261103-0001", ""])
def test_a_run_id_that_isnt_one_is_refused_before_anything(tmp_path, bad):
    r = harness("down", "azure", tmp_path / "o", "--run-id", bad)
    assert r.code == 2
    assert "not a run id" in r.stdout
    assert not (tmp_path / "o" / "transcript.txt").exists()


def test_an_azure_run_id_is_checked_too(tmp_path):
    r = harness("checks", "tenant", tmp_path / "o", "--run-id", RUN, "--azure-run", "x' or 1")
    assert r.code == 2


# -------------------------------------------------------------- the aws shim


def test_the_aws_shim_never_falls_through_to_aws(tmp_path):
    env = clean_env(
        {
            "REAL_FAKE_PY": sys.executable,
            "REAL_FAKE_STATE": str(tmp_path / "s"),
            "REAL_FAKE_TRANSCRIPT": str(tmp_path / "t"),
            "REAL_FAKE_OUT": str(tmp_path),
        }
    )
    env.pop("REAL_FAKE_MOTO", None)
    p = subprocess.run(
        [str(REAL / "fakes/bin/aws"), "sts", "get-caller-identity"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode == 2 and "refusing" in p.stderr


# --------------------------------------------------- what the goldens show


def test_the_probe_role_can_pass_only_the_batch_role():
    """The probe's role is the owner's now (tests/real/aws/harness-roles.yaml), not made by a
    run; its policy still passes only the batch role, and only to Batch Operations."""
    from fakes import owner_stack

    probe = owner_stack.load()["Resources"]["ProbeRole"]["Properties"]
    for pol in probe["Policies"]:
        for s in pol["PolicyDocument"]["Statement"]:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            if "iam:PassRole" in actions:
                # the one role and no service condition (S3 refuses CreateJob under one);
                # the batch role's trust policy keeps it to S3 Batch Operations
                assert s["Resource"] == {"Fn::GetAtt": ["BatchRole", "Arn"]}
                assert "Condition" not in s
            if s["Resource"] == "*":
                assert not [a for a in actions if a.startswith(("iam:", "s3:Put", "s3:Delete"))]


def test_the_portal_tunnel_is_recorded_before_the_browser_starts(out):
    r = harness("all", "azure", out)
    assert r.code == 0, r.stdout[-2000:]
    t = r.transcript
    tunnel = next(i for i, ln in enumerate(t) if ln.startswith("ssh -N"))
    browser = next(i for i, ln in enumerate(t) if "driver.mjs sign-in" in ln)
    assert tunnel < browser


# ------------------------------------------------------- options that do something


def test_r3_dir_reaches_the_vm_and_the_gate_runs(out, tmp_path):
    r3 = tmp_path / "r3build"
    (r3 / "build").mkdir(parents=True)
    (r3 / "build" / "omnifile-edge_0.0.0.raw").write_text("fake: a build")
    r = harness("all", "azure", out, "--r3-dir", str(r3))
    assert r.code == 0, r.stdout[-1500:]
    t = "\n".join(r.transcript)
    assert "payload-r3build.tar" in t and "exit step r3-gate" in t
    assert r.state["exit"]["r3-gate"]["status"] == "pass"


def test_without_r3_dir_the_gate_is_pending_not_passed(out):
    r = harness("all", "azure", out)
    assert r.state["exit"]["r3-gate"]["status"] == "pending"


# ------------------------------------------------------------------ secrets


def test_secrets_file_is_private_and_enrolment_codes_never_logged(out):
    r = harness("all", "azure", out)
    assert r.code == 0
    sf = out / "secrets.json"
    assert (os.stat(sf).st_mode & 0o777) == 0o600
    codes = [v for k, v in json.loads(sf.read_text()).items() if k.startswith("captured:")]
    assert codes
    for f in [
        out / "log.txt",
        out / "commands.txt",
        *Path(out / "guide").glob("*.log"),
        *Path(out / "evidence").glob("*"),
    ]:
        if f.is_file():
            text = f.read_text(errors="replace")
            for c in codes:
                assert c not in text, f"{c} in {f}"
