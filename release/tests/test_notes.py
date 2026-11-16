"""release/notes.py: pull requests from local git history, grouped by area."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import notes  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


class Notes(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
                              env=ENV).stdout.strip()

    def commit(self, path, text, msg):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git("add", path)
        self.git("commit", "-q", "-m", msg)

    def pr(self, number, branch, title, files):
        self.git("checkout", "-q", "-b", branch, "main")
        for f in files:
            self.commit(f, branch + f, f"work on {f}")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{number} from owner/{branch}",
                 "-m", title)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.commit("README.md", "x", "first")
        self.git("tag", "v0.1.0")
        self.pr(41, "edge-fix", "Edge: a fill no longer reads zeros", ["edge/a.rs", "edge/b.rs", "docs/x.md"])
        self.pr(42, "docs-only", "Decision 51 recorded", ["docs/DECISIONS.md", "AGENTS.md"])
        self.pr(43, "appliance", "Appliance: arm64 builds", ["appliance/build.sh", "edge/c.rs", "appliance/x"])
        self.commit("service/x.py", "y", "Service: catalog speed-up (#44)")  # a squash merge
        self.commit("service/y.py", "y", "a commit straight to main")      # not a pull request

    def tearDown(self):
        self.tmp.cleanup()

    def test_merges_and_squashes_are_found_and_grouped_by_most_files(self):
        prs = notes.pull_requests("v0.1.0", "main", repo=self.repo)
        got = {p["number"]: (p["title"], p["area"]) for p in prs}
        self.assertEqual(got, {
            41: ("Edge: a fill no longer reads zeros", "Edge daemon and tools"),
            42: ("Decision 51 recorded", "Documentation"),
            43: ("Appliance: arm64 builds", "Appliance image and updates"),
            44: ("Service: catalog speed-up", "Service"),
        })

    def test_the_rendered_notes_have_each_area_once_in_order(self):
        prs = notes.pull_requests("v0.1.0", "main", repo=self.repo)
        text = notes.render("0.2.0", "v0.1.0", "main", prs, "No known vulnerabilities.", "None.")
        self.assertIn("# OmniFile Gateway 0.2.0", text)
        self.assertIn("4 pull requests merged from `v0.1.0` to `main`", text)
        order = [text.index(h) for h in ("### Edge daemon and tools", "### Appliance image and updates",
                                         "### Service", "### Documentation")]
        self.assertEqual(order, sorted(order))
        self.assertIn("- Service: catalog speed-up (#44)", text)
        self.assertRegex(text, r"## Security\n\nWhat was scanned: [^#]*\n\nNo known vulnerabilities\.")
        self.assertIn("## Known issues\n\nNone.", text)
        self.assertNotIn("{{", text)

    def test_a_section_file_loses_its_heading_and_comments(self):
        self.assertEqual(notes.strip_heading("# Known issues\n\n<!-- edit me\nplease -->\n\n- one\n"), "- one")


if __name__ == "__main__":
    unittest.main()
