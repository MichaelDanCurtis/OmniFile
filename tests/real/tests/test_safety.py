"""The harness refuses what it may not touch, before it touches anything."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import DRY_ALLOW, REAL, harness

from harness import allowlist
from harness.core import DEFAULT_ALLOW_LIST, Context, Refused, UsageError


@pytest.mark.parametrize(
    "cloud,key,first",
    [
        ("azure", "azure-subscription", "az account show -o json"),
        ("azure", "azure-tenant", "az account show -o json"),
        ("aws", "aws-account", "boto3 sts.GetCallerIdentity {}"),
        ("tenant", "m365-tenant", "az account show -o json"),
    ],
)
def test_an_account_not_on_the_allow_list_is_refused_before_any_other_call(
    cloud, key, first, out, allow_without
):
    r = harness("all", cloud, out, allow=allow_without(key))
    assert r.code == 4, r.stdout[-2000:]
    assert "allow-list" in r.stdout and "refused" in r.stdout.lower() or "names no" in r.stdout
    # One identity call, nothing after it: no plan, no teardown, no state of a made thing.
    assert r.transcript == [first]
    assert not (out / "teardown.json").exists()


@pytest.mark.parametrize("phase", ["plan", "up", "exit", "checks", "down"])
def test_every_phase_checks_the_allow_list(phase, out, allow_without):
    r = harness(
        phase, "azure", out, "--run-id", "rr-dry-0001", allow=allow_without("azure-subscription")
    )
    assert r.code == 4, r.stdout[-1500:]
    assert r.transcript == ["az account show -o json"]


def test_a_missing_allow_list_is_refused(out, tmp_path):
    r = harness("plan", "azure", out, allow=tmp_path / "nothing-here")
    assert r.code == 4
    assert "the owner writes it" in r.stdout
    assert r.transcript == []


def test_a_real_run_reads_only_the_owners_allow_list(out):
    r = harness("plan", "azure", out, dry=False)
    assert r.code == 4
    assert "--allow-list is for dry runs" in r.stdout
    assert not (out / "transcript.txt").exists()


def test_a_real_run_never_approves_its_own_digest(out):
    r = harness("up", "azure", out, "--run-id", "rr-x", "--approve", "auto", dry=False, allow=None)
    assert r.code == 4
    assert "--approve auto is for dry runs only" in r.stdout


def test_a_committed_allow_list_is_refused(tmp_path, monkeypatch):
    """A real run refuses tests/real/allow-list.local if git tracks it."""
    import subprocess

    def tracked(*a, **k):
        return subprocess.CompletedProcess(a, 0)

    local = tmp_path / "allow-list.local"
    local.write_text(DRY_ALLOW.read_text())
    monkeypatch.setattr(allowlist, "DEFAULT_ALLOW_LIST", local)
    monkeypatch.setattr(allowlist.subprocess, "run", tracked)
    with pytest.raises(Refused, match="tracked by git"):
        allowlist.load(local, dry_run=False)


@pytest.mark.parametrize(
    "text,err",
    [
        ("azure-subscriptoin 00000000-0000-4000-8000-0000000000a1\n", "unknown key"),
        ("azure-subscription not-a-guid\n", "malformed"),
        ("aws-account 1234\n", "malformed"),
        ("ssh-from 0.0.0.0/0\n", "wider than a /24"),
        ("ssh-from 10.0.0.1/33\n", "ssh-from"),
        ("azure-subscription a b\n", "want `key value`"),
    ],
)
def test_a_malformed_allow_list_stops_the_run(tmp_path, text, err):
    f = tmp_path / "al"
    f.write_text(text)
    with pytest.raises(UsageError, match=err):
        allowlist.load(f, dry_run=True)


def test_the_harness_never_writes_the_owners_allow_list(out):
    before = DEFAULT_ALLOW_LIST.read_bytes() if DEFAULT_ALLOW_LIST.exists() else None
    r = harness("all", "azure", out)
    assert r.code == 0
    after = DEFAULT_ALLOW_LIST.read_bytes() if DEFAULT_ALLOW_LIST.exists() else None
    assert before == after
    # And nothing in the harness opens it for writing.
    for f in (REAL / "harness").rglob("*.py"):
        text = f.read_text()
        assert "DEFAULT_ALLOW_LIST.write" not in text and 'allow-list.local").write' not in text
    assert "allow-list.local" in (REAL / ".gitignore").read_text()


def test_a_dry_run_refuses_unless_every_cloud_tool_is_the_shim(tmp_path, monkeypatch):
    from harness import core

    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setattr(core, "FAKE_BIN", empty)
    ctx = Context(
        phase="plan",
        cloud="azure",
        dry_run=True,
        out=tmp_path / "o",
        run_id="rr-dry-0001",
        now=core.parse_iso(core.DRY_NOW),
    )
    with pytest.raises(Refused, match="not the shim"):
        ctx.build_env()


def test_a_dry_run_strips_real_credentials_and_hides_the_real_clis(tmp_path, monkeypatch):
    from harness import core

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "real-secret")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "real-secret")
    monkeypatch.setenv("GH_TOKEN", "real-token")
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:" + os.environ["PATH"])
    ctx = Context(
        phase="plan",
        cloud="aws",
        dry_run=True,
        out=tmp_path / "o",
        run_id="rr-dry-0001",
        now=core.parse_iso(core.DRY_NOW),
    )
    ctx.build_env("http://127.0.0.1:1")
    env = ctx.env
    assert env["AWS_SECRET_ACCESS_KEY"] == "fake-secret-key"
    assert "AZURE_CLIENT_SECRET" not in env and "GH_TOKEN" not in env
    assert "/opt/homebrew/bin" not in env["PATH"].split(":")
    assert Path(env["AZURE_CONFIG_DIR"]).is_relative_to(tmp_path)
    for tool in ("az", "aws", "gh"):
        import shutil

        assert Path(shutil.which(tool, path=env["PATH"])).parent == core.FAKE_BIN


def test_the_dry_run_allow_list_names_only_the_fakes():
    al = allowlist.load(DRY_ALLOW, dry_run=True)
    assert al.values("azure-subscription") == ["00000000-0000-4000-8000-0000000000a1"]
    assert al.values("aws-account") == ["123456789012"]  # moto's account
