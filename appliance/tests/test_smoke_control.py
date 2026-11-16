"""smoke.sh's control run: the /run drop-in that unbinds smbd from the guard never outlives it (review of #50).

The guest's control block is cut out of smoke.sh between its markers and run in bash with /run/systemd/system
moved to a temporary directory and systemctl stubbed. The start of smbd is where the script stops part-way: by an
error (exit) or by the unit's timeout (SIGTERM). Either way the drop-in must be gone and smbd stopped.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SMOKE = (HERE / "smoke.sh").read_text()


def control_block() -> str:
    begin, end = "# smoke-control: begin\n", "# smoke-control: end\n"
    if begin not in SMOKE or end not in SMOKE:
        raise AssertionError("smoke.sh has no control block between its markers")
    return SMOKE[SMOKE.index(begin):SMOKE.index(end) + len(end)]


def run(abort: str) -> tuple[Path, str]:
    """Runs the block; the stubbed `systemctl start smbd.service` checks the drop-in is there, then aborts."""
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "calls"
    stubs = f"""
systemctl() {{
  echo "systemctl $*" >>{log}
  if [ "$*" = "start smbd.service" ]; then
    [ -e "$CONTROL" ] && echo "control present at start" >>{log}
    {abort}
  fi
  return 0
}}
sleep() {{ :; }}
"""
    script = stubs + control_block().replace("/run/systemd/system", str(tmp / "run"))
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return tmp, (log.read_text() if log.exists() else "") + f"rc={p.returncode}\n{p.stderr}"


class ControlDropIn(unittest.TestCase):
    def check_cleaned(self, abort: str) -> None:
        tmp, out = run(abort)
        self.assertIn("control present at start", out)
        self.assertFalse((tmp / "run/smbd.service.d/10-omnifile-guard.conf").exists(), out)
        after = out.split("control present at start", 1)[1]
        self.assertIn("systemctl stop smbd.service", after, out)
        self.assertIn("systemctl daemon-reload", after, out)

    def test_an_error_part_way_leaves_no_control_drop_in(self):
        self.check_cleaned("exit 1")

    def test_the_units_timeout_part_way_leaves_no_control_drop_in(self):
        self.check_cleaned("kill -TERM $$")

    def test_a_full_run_removes_it_and_clears_the_traps(self):
        tmp, out = run(":")
        self.assertFalse((tmp / "run/smbd.service.d/10-omnifile-guard.conf").exists(), out)
        self.assertIn("rc=0", out)
        self.assertIn("trap - EXIT TERM HUP INT", control_block())


if __name__ == "__main__":
    unittest.main()
