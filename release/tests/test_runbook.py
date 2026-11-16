"""release/runbook.sh: its refusals, its record, and the order of its publishing steps (review on #38).

The whole dry run (two releases and the install) is release/tests/dry_run_install.sh; these are the fast checks.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
RUNBOOK = HERE.parent / "runbook.sh"
TEXT = RUNBOOK.read_text()


def run(*args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("AZURE_", "AWS_", "GH_", "GITHUB_"))}
    e.update(env or {})
    return subprocess.run([str(RUNBOOK), *args], capture_output=True, text=True, env=e, timeout=600)


class Refusals(unittest.TestCase):
    def test_stop_after_must_name_a_step(self):
        with tempfile.TemporaryDirectory() as out:
            r = run("--dry-run", "--out", out, "--stop-after", "sboms")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--stop-after sboms: one of stamp build offer sbom scan notes gate+sign params", r.stderr)

    def test_a_real_release_refuses_a_given_images_lock(self):
        with tempfile.TemporaryDirectory() as out:
            r = run("--out", out, "--images-lock", str(REPO_ROOT / "deploy" / "images" / "images.lock.json"))
            record = (Path(out) / "RELEASE-RECORD").read_text()
        self.assertEqual(r.returncode, 1)
        self.assertIn("--images-lock is for dry runs", r.stderr)
        self.assertIn("result=failed: --images-lock is for dry runs", record)


class Record(unittest.TestCase):
    def test_any_failure_leaves_result_failed_in_the_record(self):
        """A step that fails outside die() (here: the cached build doesn't exist, under set -e) still records it."""
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "keys").mkdir()
            for k in ("update.pem", "update-ring.pgp", "sb.key", "sb.crt"):
                (t / "keys" / k).write_text("x")
            (t / "offer").mkdir()
            r = run("--dry-run", "--out", str(t / "out"), "--appliance-out", str(t / "no-such-build"),
                    "--keys", str(t / "keys"), "--source-offer", str(t / "offer"))
            record = (t / "out" / "RELEASE-RECORD").read_text()
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(record, r"(?m)^result=failed: exit [1-9]\d* in step build$")


class ImagesLock(unittest.TestCase):
    def test_a_lock_built_at_another_commit_or_dirty_is_refused(self):
        import json
        import sys

        sys.path.insert(0, str(HERE.parent))
        import images_lock

        lock = json.loads((REPO_ROOT / "deploy" / "images" / "images.lock.json").read_text())
        built = {i["source_commit"] for i in lock["images"].values()}
        self.assertEqual(len(built), 1)
        commit = built.pop()
        self.assertEqual(images_lock.stale(lock, commit), {})
        self.assertEqual(set(images_lock.stale(lock, "0" * 40)), set(lock["images"]))
        lock["images"]["relay"]["source_commit"] = commit + "+dirty"
        self.assertEqual(images_lock.stale(lock, commit), {"relay": commit + "+dirty"})
        self.assertIn("(none)", images_lock.stale({"images": {}}, commit))
        self.assertIn('images_lock.py" "$IMAGES_LOCK" "$TARGET"', TEXT)


class Publishing(unittest.TestCase):
    def block(self):
        return TEXT[TEXT.index("# ---- 9 publish"):]

    def test_the_tag_is_made_on_the_built_commit(self):
        b = self.block()
        self.assertRegex(b, r'gh release create "v\$V" --repo "\$RELEASE_REPO" --draft --target "\$TARGET"')
        self.assertIn('TARGET=$(git -C "$REPO" rev-parse HEAD)', TEXT)
        self.assertIn('branch -r --contains "$TARGET"', TEXT)

    def test_sources_then_a_draft_feed_then_live(self):
        b = self.block()
        run_lines = [i for i, ln in enumerate(b.splitlines()) if re.match(r'\s+"\$\{(SRC|PUB|LIVE)\[@\]\}"', ln)]
        order = [re.search(r"(SRC|PUB|LIVE)", b.splitlines()[i]).group(1) for i in run_lines]
        self.assertEqual(order, ["SRC", "PUB", "LIVE"])
        self.assertIn('LIVE=(gh release edit "v$V" --repo "$RELEASE_REPO" --draft=false --latest)', b)
        self.assertNotRegex(b.split("LIVE=")[0], r"gh release create[^\n]*--latest")

    def test_a_full_gate_dry_run_still_allows_fake_keys(self):
        gate = TEXT[TEXT.index("# ---- 7 the R3 gate"):TEXT.index("# ---- 8")]
        self.assertRegex(gate, r'if \[ \$DRY = 1 \]; then\n\s+GATE\+=\(--allow-partial\)')
        self.assertRegex(gate, r'\[ \$FULL_GATE = 1 \] \|\| GATE\+=\(--a-suite-only')


if __name__ == "__main__":
    unittest.main()
