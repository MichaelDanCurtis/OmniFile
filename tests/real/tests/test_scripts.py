"""The scripts a real run sends to the test VM, the EC2 probe and the browser, which no dry
run executes: they must at least parse. And the probe's dry-run path, which runs locally."""

from __future__ import annotations

import py_compile
import shutil
import subprocess

import pytest
from conftest import REAL


@pytest.mark.parametrize(
    "f",
    sorted((REAL / "vm").glob("*.py"))
    + [REAL / "collect.py"]
    + sorted((REAL / "rehearsal").glob("*.py")),
)
def test_python_scripts_compile(f, tmp_path):
    py_compile.compile(str(f), cfile=str(tmp_path / "x.pyc"), doraise=True)


@pytest.mark.parametrize(
    "f",
    sorted((REAL / "vm").glob("*.sh"))
    + [REAL / "run.sh"]
    + sorted((REAL / "rehearsal").glob("*.sh")),
)
def test_shell_scripts_parse(f):
    assert subprocess.run(["bash", "-n", str(f)]).returncode == 0


def test_the_portal_driver_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    assert subprocess.run([node, "--check", str(REAL / "portal/driver.mjs")]).returncode == 0


def test_every_vm_check_the_registry_names_exists():
    import re

    from harness.checks import azure  # noqa: F401  (registers)

    src = (REAL / "harness/checks/azure.py").read_text()
    used = set(re.findall(r'env\.on_vm\("(R[AT]-\d\d)"', src))
    have = set(re.findall(r'"(R[AT]-\d\d)": ', (REAL / "vm/azure_checks.py").read_text()))
    assert used <= have, used - have


def test_every_probe_check_the_registry_names_exists():
    import re

    src = (REAL / "harness/checks/aws.py").read_text()
    used = set(re.findall(r'env\.probe\("([a-z-]+)"', src))
    have = set(re.findall(r'"([a-z-]+)": [a-z_]+', (REAL / "vm/aws_probe.py").read_text()))
    assert used <= have, used - have


def test_every_exit_step_has_a_side_that_runs_it():
    import re

    from harness.m6exit import STEPS

    vm = (REAL / "vm/exit_steps.py").read_text()
    driver = (REAL / "portal/driver.mjs").read_text()
    for name, where, _ in STEPS:
        if where == "vm":
            assert re.search(rf'"{name}"', vm), name
        else:
            assert f'"exit-{name}"' in driver, name


def test_the_local_rehearsal_refuses_to_start_while_pending_on_wp2(tmp_path):
    """Pending on WP2's arm64 image: --run refuses before it starts any VM or process."""
    p = subprocess.run(
        [
            str(REAL / "rehearsal/m6-local.sh"),
            "--run",
            "--work",
            str(tmp_path),
            "--image",
            str(tmp_path / "no-such-image.qcow2"),
        ],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 3, p.stdout + p.stderr
    assert "pending" in p.stdout and "refusing to start" in p.stderr
    assert list(tmp_path.iterdir()) == []
