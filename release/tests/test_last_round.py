"""The re-review's last round on #38: the pinned uv, dry-run-only waivers, portable scan keys, unchecked components,
versioned Debian overrides, the record's trap, and the release target on main."""

from __future__ import annotations

import datetime as dt
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import licences as L  # noqa: E402
import pinned  # noqa: E402
import sbom  # noqa: E402
import scan  # noqa: E402

RUNBOOK = (HERE.parent / "runbook.sh").read_text()


class PinnedUv(unittest.TestCase):
    def fake_uv(self, version):
        d = tempfile.mkdtemp()
        f = Path(d) / "uv"
        f.write_text(f"#!/bin/sh\necho 'uv {version} (fake)'\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
        return str(f)

    def setUp(self):
        pinned.uv.cache_clear()
        self.addCleanup(pinned.uv.cache_clear)
        self.old = os.environ.get("UV")
        self.addCleanup(lambda: os.environ.__setitem__("UV", self.old) if self.old else os.environ.pop("UV", None))

    def test_another_uv_is_refused(self):
        os.environ["UV"] = self.fake_uv("0.11.1")
        with self.assertRaises(SystemExit) as e:
            pinned.uv()
        self.assertIn("not the pinned 0.9.26", str(e.exception))

    def test_the_pinned_uv_is_used(self):
        os.environ["UV"] = self.fake_uv(pinned.pin("uv"))
        self.assertEqual(pinned.uv(), os.environ["UV"])

    def test_every_script_that_runs_uv_takes_it_from_the_pin(self):
        for f in ("sbom.py", "stamp.py", "validate_sbom.py"):
            text = (HERE.parent / f).read_text()
            self.assertNotRegex(text, r'\["uv", ', f)
            self.assertIn("pinned.uv()", text, f)


def finding(lockfile, ids=("USN-8944-1",), package="ca-certificates"):
    return scan.Finding(lockfile, "Ubuntu:26.04:LTS", package, "1", list(ids), "UNKNOWN", "", "x", ["2"])


WAIVER = {"id": "USN-8944-1", "package": "ca-certificates", "reason": "r", "owner": "o",
          "expires": dt.date(2026, 12, 1), "lockfile": "sbom:appliance", "dry_run_only": True}


class Waivers(unittest.TestCase):
    def test_sbom_and_image_findings_have_portable_keys(self):
        self.assertEqual(scan.source_key("/anywhere/out/sbom/omnifile-appliance-0.2.0-rc.1.cdx.json"), "sbom:appliance")
        self.assertEqual(scan.source_key("/x/omnifile-portal-e2e-0.1.0.cdx.json"), "sbom:portal-e2e")
        self.assertEqual(scan.source_key("image:web@sha256:00"), "image:web")
        self.assertEqual(scan.source_key(str(scan.REPO / "edge" / "Cargo.lock")), "edge/Cargo.lock")

    def test_a_dry_run_only_waiver_covers_a_dry_run(self):
        f = finding("sbom:appliance")
        self.assertEqual(scan.apply_waivers([f], [WAIVER], dt.date(2026, 11, 4)), [])
        self.assertFalse(f.fails)

    def test_a_dry_run_only_waiver_fails_a_real_release(self):
        f = finding("sbom:appliance")
        problems = scan.apply_waivers([f], [WAIVER], dt.date(2026, 11, 4), release=True)
        self.assertTrue(f.fails)
        self.assertTrue(any("for dry runs only" in p for p in problems), problems)
        self.assertIn("SCAN+=(--require-images --release)", RUNBOOK)

    def test_a_dry_run_only_waiver_in_a_real_release_is_a_problem_not_stale(self):
        # It names a finding that is there: "matches nothing: remove it" would be wrong advice.
        f = finding("sbom:appliance")
        scan.apply_waivers([f], [WAIVER], dt.date(2026, 11, 4), release=True)
        self.assertEqual(scan.stale_waivers([f], [WAIVER]), [])

    def test_an_expired_waiver_for_a_finding_still_there_is_not_stale(self):
        f = finding("sbom:appliance")
        problems = scan.apply_waivers([f], [WAIVER], dt.date(2026, 12, 2))
        self.assertTrue(any("expired" in p for p in problems), problems)
        self.assertEqual(scan.stale_waivers([f], [WAIVER]), [])

    def test_a_waiver_that_names_no_finding_is_still_stale(self):
        for f in (finding("sbom:appliance", ids=("USN-0000-1",)), finding("edge/Cargo.lock")):
            scan.apply_waivers([f], [WAIVER], dt.date(2026, 11, 4), release=True)
            self.assertEqual(scan.stale_waivers([f], [WAIVER]), [WAIVER])

    def test_appliance_waivers_are_checked_against_the_images_snapshot(self):
        # A waiver for one of the image's packages holds only for the snapshot it was read against: once
        # mkosi.conf moves on, the package may be fixed (USN-8944-1 was, at 20261023) and the waiver must go.
        conf = (HERE.parent.parent / "appliance" / "mkosi.conf").read_text()
        snapshot = next(line.split("=", 1)[1].strip() for line in conf.splitlines() if line.startswith("Snapshot="))
        for w in scan.load_waivers(HERE.parent / "vuln-waivers.toml"):
            if w.get("lockfile") == "sbom:appliance":
                self.assertEqual(w.get("snapshot"), snapshot,
                                 f"waiver {w['id']} ({w['package']}) was not checked against {snapshot}")

    def test_ca_certificates_is_not_waived_once_the_snapshot_carries_its_fix(self):
        conf = (HERE.parent.parent / "appliance" / "mkosi.conf").read_text()
        snapshot = next(line.split("=", 1)[1].strip() for line in conf.splitlines() if line.startswith("Snapshot="))
        self.assertGreaterEqual(snapshot, "20261023T000000Z")  # the first snapshot with USN-8944-1's fix
        ids = [w["id"] for w in scan.load_waivers(HERE.parent / "vuln-waivers.toml")]
        self.assertNotIn("USN-8944-1", ids)


class Unchecked(unittest.TestCase):
    def test_excluded_components_without_a_licence_are_named(self):
        c = L.Comp("omnifile-appliance-0.1.0.cdx.json", "deb", "mkosi", "26", "excluded", None, "", False, False)
        self.assertEqual(L.check([], [c]), [])
        self.assertEqual(L.UNCHECKED, ["mkosi 26 (deb, omnifile-appliance-0.1.0.cdx.json)"])


class DebianOverrides(unittest.TestCase):
    def test_an_override_holds_only_for_the_versions_it_was_read_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            o = Path(tmp)
            (o / "packages.tsv").write_text("binary\tversion\tsource\tsource_version\n"
                                            "libkrb5-3\t1.22.1-2ubuntu4.1\tkrb5\t1.22.1-2ubuntu4.1\n"
                                            "libkrb5-3\t1.23.0-1\tkrb5\t1.23.0-1\n")
            for v in ("1.22.1-2ubuntu4.1", "1.23.0-1"):
                (o / "ubuntu" / f"krb5_{v}").mkdir(parents=True)
            (o / "packages.tsv").write_text("binary\tversion\tsource\tsource_version\n"
                                            "libkrb5-3\t1.22.1-2ubuntu4.1\tkrb5\t1.22.1-2ubuntu4.1\n")
            self.assertEqual(sbom.offer_licences(o), {"libkrb5-3": ["MIT"]})
            (o / "packages.tsv").write_text("binary\tversion\tsource\tsource_version\n"
                                            "libkrb5-3\t1.23.0-1\tkrb5\t1.23.0-1\n")
            self.assertEqual(sbom.offer_licences(o), {"libkrb5-3": []})

    def test_a_deb_override_without_versions_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "o.toml"
            f.write_text('[deb]\nkrb5 = { licence = "MIT", reason = "debian/copyright" }\n')
            with self.assertRaises(SystemExit):
                sbom.load_overrides(f)

    def test_python_dateutil_needs_both_licences(self):
        self.assertEqual(sbom.load_overrides()["pypi"]["python-dateutil"]["licence"], "Apache-2.0 AND BSD-3-Clause")


class Runbook(unittest.TestCase):
    def test_the_record_is_emptied_before_the_trap_is_set(self):
        self.assertLess(RUNBOOK.index(': >"$REC"'), RUNBOOK.index("trap finish EXIT"))

    def test_the_release_target_must_be_on_main_or_a_release_branch(self):
        self.assertIn("grep -Eqx 'origin/(main|release/.+)'", RUNBOOK)


if __name__ == "__main__":
    unittest.main()
