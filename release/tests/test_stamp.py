"""release/stamp.py: one version written into every manifest and lockfile, line edits only, and --check."""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import stamp  # noqa: E402

FILES = ["edge/Cargo.toml", "edge/Cargo.lock", "service/pyproject.toml", "service/uv.lock",
         "relay/pyproject.toml", "relay/uv.lock", "portal/package.json", "portal/package-lock.json", "VERSION"]


class Stamp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for f in FILES:
            (self.tmp / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / f, self.tmp / f)
        for toml in (REPO / "edge" / "crates").glob("*/Cargo.toml"):
            d = self.tmp / "edge" / "crates" / toml.parent.name
            d.mkdir(parents=True)
            shutil.copy(toml, d / "Cargo.toml")

    def run_stamp(self, *args):
        return subprocess.run([sys.executable, str(HERE.parent / "stamp.py"), "--repo", str(self.tmp), *args],
                              capture_output=True, text=True)

    def test_stamping_changes_only_version_lines(self):
        before = {f: (self.tmp / f).read_text() for f in FILES if f != "VERSION"}
        r = self.run_stamp("--version", "1.2.0-rc.1")
        self.assertEqual(r.returncode, 0, r.stderr)
        crates = len(list((self.tmp / "edge" / "crates").glob("*/Cargo.toml")))
        expect = {"edge/Cargo.toml": 1, "edge/Cargo.lock": crates, "service/pyproject.toml": 1,
                  "service/uv.lock": 1, "relay/pyproject.toml": 1, "relay/uv.lock": 1,
                  "portal/package.json": 1, "portal/package-lock.json": 2}
        for f, n in expect.items():
            after = (self.tmp / f).read_text()
            changed = [ln for ln in difflib.unified_diff(before[f].splitlines(), after.splitlines(), lineterm="", n=0)
                       if ln.startswith("+") and not ln.startswith("+++")]
            self.assertEqual(len(changed), n, f"{f}: {changed}")
            want = "1.2.0rc1" if f.startswith(("service/", "relay/")) else "1.2.0-rc.1"
            for ln in changed:
                self.assertIn(want, ln, f)
                self.assertRegex(ln, r'^\+\s*"?version"?\s*[=:]\s*"', f)
        self.assertEqual((self.tmp / "VERSION").read_text(), "1.2.0-rc.1\n")
        lock = json.loads((self.tmp / "portal/package-lock.json").read_text())
        self.assertEqual((lock["version"], lock["packages"][""]["version"]), ("1.2.0-rc.1", "1.2.0-rc.1"))

    def test_check_fails_until_stamped_then_passes(self):
        (self.tmp / "VERSION").write_text("9.8.7\n")
        r = self.run_stamp("--check", "--no-tools")
        self.assertEqual(r.returncode, 1)
        self.assertIn("edge/Cargo.toml", r.stderr)
        self.assertIn("portal/package-lock.json", r.stderr)
        self.assertEqual(self.run_stamp().returncode, 0)
        r = self.run_stamp("--check", "--no-tools")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_stamp().stdout.strip(), "stamp: 9.8.7 (Python 9.8.7); changed: nothing")

    def test_one_stale_lock_entry_is_found(self):
        self.run_stamp("--version", "2.0.0")
        lock = self.tmp / "relay" / "uv.lock"
        lock.write_text(lock.read_text().replace('name = "omnifile-relay"\nversion = "2.0.0"',
                                                 'name = "omnifile-relay"\nversion = "1.9.0"'))
        r = self.run_stamp("--check", "--no-tools")
        self.assertEqual(r.returncode, 1)
        self.assertIn("relay/uv.lock: 1.9.0, not 2.0.0", r.stderr)

    def test_a_version_that_is_not_a_release_version_is_refused(self):
        for bad in ("1.2", "v1.2.0", "1.2.0-dev", "1.2.0+abc", "01.2.0", "1.2.0-rc1"):
            r = self.run_stamp("--version", bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("not MAJOR.MINOR.PATCH", r.stderr)

    def test_pep440_spelling(self):
        self.assertEqual([stamp.pep440(v) for v in ("0.1.0", "1.2.0-alpha.3", "1.2.0-beta.0", "1.2.0-rc.12")],
                         ["0.1.0", "1.2.0a3", "1.2.0b0", "1.2.0rc12"])


class ThisTree(unittest.TestCase):
    def test_the_tree_is_stamped_with_version(self):
        """release/stamp.sh --check --no-tools on the repository itself."""
        r = subprocess.run([sys.executable, str(HERE.parent / "stamp.py"), "--check", "--no-tools"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_edge_binary_reports_version(self):
        """The heartbeat and `omnifile-edge --version` say CARGO_PKG_VERSION, which the workspace version sets."""
        if not shutil.which("cargo"):
            self.skipTest("no cargo")
        r = subprocess.run(["cargo", "build", "--locked", "-q", "-p", "omnifile-edge"], cwd=REPO / "edge",
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"omnifile-edge does not build here: {r.stderr[-300:]}")
        r = subprocess.run([sys.executable, str(HERE.parent / "stamp.py"), "--check", "--no-tools", "--edge-bin",
                            str(REPO / "edge" / "target" / "debug" / "omnifile-edge")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = subprocess.run([str(REPO / "edge" / "target" / "debug" / "omnifile-edge"), "--version"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, f"omnifile-edge {(REPO / 'VERSION').read_text().strip()}")

if __name__ == "__main__":
    unittest.main()
