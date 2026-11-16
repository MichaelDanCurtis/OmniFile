"""release.sh: scaled time budgets (an emulated guest) never sign a release."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELEASE = HERE.parent / "release.sh"


def release(*args: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as d:
        return subprocess.run(
            ["bash", str(RELEASE), "--out", d, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


class BudgetScale(unittest.TestCase):
    def test_a_scaled_gate_needs_allow_partial(self) -> None:
        r = release("--budget-scale", "8")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("--budget-scale 8 loosens the gate's time limits", r.stderr)

    def test_not_a_positive_number(self) -> None:
        for bad in ("0", "-2", "x", "1.", ""):
            with self.subTest(bad=bad):
                r = release("--budget-scale", bad, "--allow-partial")
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("a positive number", r.stderr)

    def test_scale_one_and_test_feeds_get_past_it(self) -> None:
        for args in (("--budget-scale", "1"), ("--budget-scale", "8", "--allow-partial"), ()):
            with self.subTest(args=args):
                r = release(*args)
                self.assertNotIn("--budget-scale", r.stderr)

    def test_r3_gate_records_a_scaled_run_as_partial(self) -> None:
        text = RELEASE.read_text()
        self.assertIn('[ "$BUDGET_SCALE" != 1 ]; then\n    echo "partial=yes', text)


if __name__ == "__main__":
    unittest.main()
