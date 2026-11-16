"""Helpers for the harness's tests: every run is a dry run, against the fakes only.

    uv run --project tests/real pytest tests/real/tests -q

A run is `tests/real/run.sh ... --dry-run` in a subprocess (the real entry point), with a
temporary output directory, the dry-run allow-list (or a test's own), and the guide fixture.
The test process's own cloud variables are stripped first, so nothing a test starts could
use a real credential even if the harness's own guard were wrong.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REAL = Path(__file__).resolve().parents[1]
REPO = REAL.parents[1]
FIXTURE_GUIDE = REAL / "fakes" / "guide"
DRY_ALLOW = REAL / "fakes" / "allow-list.dry"
GOLDEN = REAL / "golden"
sys.path.insert(0, str(REAL))


@dataclass
class Run:
    code: int
    out: Path
    stdout: str

    @property
    def transcript(self) -> list[str]:
        f = self.out / "transcript.txt"
        return f.read_text().splitlines() if f.exists() else []

    @property
    def state(self) -> dict:
        return json.loads((self.out / "state.json").read_text())

    @property
    def results(self) -> dict:
        return json.loads((self.out / "results.json").read_text())

    @property
    def teardown(self) -> dict:
        return json.loads((self.out / "teardown.json").read_text())

    def fake_state(self) -> dict:
        return json.loads((self.out / "fake-state.json").read_text())


VOLATILE = [
    # What a run makes at random: moto's resource ids and version ids, keys and test
    # passwords made at run time (seen only through hashes and lengths), ETags of manifests
    # that list those version ids.
    (re.compile(r"\bvpce-[0-9a-f]{8,17}(-[0-9a-z]{8})?\b"), "vpce-<id>"),
    (re.compile(r"\b(vpc|sg|subnet|i|ami|eni|rtb|igw|vol)-[0-9a-f]{8,17}\b"), r"\1-<id>"),
    (re.compile(r"sha256:[0-9a-f]{12}\b"), "sha256:<h>"),
    # WP1's what-if digests cover main.bicep itself, so they change whenever main does; what
    # the harness does with them is checked in test_gate.py and test_rereview.py instead.
    (re.compile(r"sha256:[0-9a-f]{64}\b"), "sha256:<digest>"),
    (re.compile(r"ssh-ed25519 \S+ \S+"), "<ssh key>"),
    (re.compile(r"<\d+ chars"), "<n chars"),
    (re.compile(r'"(ETag|VersionId)": "[^"]+"'), r'"\1": "<v>"'),
    (re.compile(r"--version-id \S+"), "--version-id <v>"),
    # mktemp paths (WP1's whatif.sh compiles into one)
    (re.compile(r"(/private)?(/var/folders|/tmp)/\S*?tmp\.\w+"), "<tmp>"),
]


def normalize(lines: list[str]) -> str:
    out = []
    for line in lines:
        for pat, rep in VOLATILE:
            line = pat.sub(rep, line)
        out.append(line)
    return "\n".join(out) + "\n"


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AZURE_", "AWS_", "ARM_", "GH_", "GITHUB_", "REAL_", "OMNIFILE_"))
    }
    env["REAL_HARNESS_PY"] = sys.executable
    env.update(extra or {})
    return env


def harness(
    phase: str,
    cloud: str,
    out: Path,
    *args: str,
    allow: Path | None = DRY_ALLOW,
    guide: Path | None = FIXTURE_GUIDE,
    dry: bool = True,
    env: dict[str, str] | None = None,
) -> Run:
    cmd = [str(REAL / "run.sh"), phase, "--cloud", cloud, "--out", str(out)]
    if dry:
        cmd.append("--dry-run")
    if allow is not None:
        cmd += ["--allow-list", str(allow)]
    if guide is not None:
        cmd += ["--guide", str(guide)]
    cmd += list(args)
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=clean_env(env),
        timeout=600,
        stdin=subprocess.DEVNULL,
    )
    return Run(p.returncode, out, p.stdout + p.stderr)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "out"


@pytest.fixture
def allow_without(tmp_path: Path):
    """An allow-list like the dry run's, without the lines whose key is given."""

    def make(*keys: str) -> Path:
        lines = [
            ln
            for ln in DRY_ALLOW.read_text().splitlines()
            if not any(ln.startswith(k + " ") for k in keys)
        ]
        f = tmp_path / "allow-list"
        f.write_text("\n".join(lines) + "\n")
        return f

    return make
