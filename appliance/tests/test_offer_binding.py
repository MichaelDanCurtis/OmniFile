"""offer_binding.py: release.sh signs only a source offer that is the one inside the image."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import offer_binding as ob  # noqa: E402

INFO = {
    "version": "1.2.0",
    "architecture": "x86-64",
    "snapshot": "20261020T000000Z",
    "sources_repo": "owner/omnifile-gateway-sources",
}
MANIFEST = {
    "release": "1.2.0",
    "architecture": "amd64",
    "snapshot": "20261020T000000Z",
    "sources_repo": "owner/omnifile-gateway-sources",
    "complete": True,
}


class Problems(unittest.TestCase):
    def test_a_matching_offer(self) -> None:
        self.assertEqual(ob.problems(dict(INFO), dict(MANIFEST), "1.2.0"), [])

    def test_each_difference_is_named(self) -> None:
        for key, value in (
            ("snapshot", "20261021T000000Z"),
            ("sources_repo", "someone/else"),
            ("architecture", "arm64"),
            ("release", "1.2.1"),
        ):
            with self.subTest(key=key):
                found = ob.problems(dict(INFO), dict(MANIFEST, **{key: value}), "1.2.0")
                self.assertTrue(found, key)
        self.assertTrue(ob.problems(dict(INFO), dict(MANIFEST, complete=False), "1.2.0"))

    def test_a_build_info_without_the_fields_is_refused(self) -> None:
        info = {k: v for k, v in INFO.items() if k != "sources_repo"}
        self.assertTrue(any("sources_repo" in p for p in ob.problems(info, dict(MANIFEST), "1.2.0")))


class Command(unittest.TestCase):
    def run_check(self, info: str, manifest: dict) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "BUILD-INFO").write_text(info)
            (Path(d) / "MANIFEST.json").write_text(json.dumps(manifest))
            return subprocess.run(
                [
                    sys.executable,
                    str(HERE.parent / "offer_binding.py"),
                    "check",
                    str(Path(d) / "BUILD-INFO"),
                    str(Path(d) / "MANIFEST.json"),
                    "1.2.0",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_build_info_as_build_sh_writes_it(self) -> None:
        info = "".join(f"{k}={v}\n" for k, v in INFO.items()) + "seed=x\nmkosi=mkosi 26\n"
        self.assertEqual(self.run_check(info, MANIFEST).returncode, 0)
        r = self.run_check(info, dict(MANIFEST, snapshot="20261021T000000Z"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("snapshot", r.stderr)


class Wiring(unittest.TestCase):
    def test_build_sh_records_the_sources_repo(self) -> None:
        text = (HERE.parent / "build.sh").read_text()
        block = text[text.index('echo "version=$VERSION"') : text.index('>"$OUT/BUILD-INFO"')]
        self.assertIn('echo "sources_repo=$SOURCES_REPO"', block)
        self.assertIn('echo "snapshot=$SNAPSHOT"', block)

    def test_release_sh_runs_the_check(self) -> None:
        text = (HERE.parent / "release.sh").read_text()
        self.assertIn('offer_binding.py" check "$OUT/BUILD-INFO" "$OFFER/MANIFEST.json" "$V"', text)

    def test_the_readme_example_uses_the_image_snapshot(self) -> None:
        conf = (HERE.parent / "mkosi.conf").read_text()
        snap = next(l.split("=", 1)[1] for l in conf.splitlines() if l.startswith("Snapshot="))
        readme = (HERE.parent / "README.md").read_text()
        self.assertIn(f"--snapshot {snap}", readme)


if __name__ == "__main__":
    unittest.main()
