"""release/scan.py: severities, waivers and the exit code, from recorded osv-scanner results (no network)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import scan  # noqa: E402

TODAY = "2026-11-03"


def vuln(vid, *, aliases=(), label=None, cvss=None, informational=None, fixed="9.9.9", summary="a bug"):
    v = {"id": vid, "aliases": list(aliases), "summary": summary,
         "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": fixed}]}],
                       "database_specific": {"informational": informational} if informational else {}}]}
    if label:
        v["database_specific"] = {"severity": label}
    if cvss:
        v["severity"] = [{"type": "CVSS_V3", "score": cvss}]
    return v


def osv(*packages):
    """packages: (lockfile, name, version, [(vuln, max_severity)])"""
    results = {}
    for lock, name, version, vulns in packages:
        r = results.setdefault(lock, {"source": {"path": str(scan.REPO / lock), "type": "lockfile"}, "packages": []})
        r["packages"].append({
            "package": {"name": name, "version": version, "ecosystem": "crates.io"},
            "vulnerabilities": [v for v, _ in vulns],
            "groups": [{"ids": [v["id"], *v.get("aliases", [])], "max_severity": s} for v, s in vulns],
        })
    return {"results": list(results.values())}


HIGH = ("edge/Cargo.lock", "ruzstd", "0.8.3", [(vuln("RUSTSEC-2026-0344", cvss="CVSS:3.1/AV:N"), "7.5")])
CRIT = ("service/uv.lock", "certifi", "2026.7.22",
        [(vuln("GHSA-6q48-6c7w-9gw2", aliases=["PYSEC-2026-4320"], label="CRITICAL"), "9.1")])
MED = ("edge/Cargo.lock", "rustls", "0.23.45", [(vuln("GHSA-c8m5-c952-6cm4", label="MODERATE"), "4.0")])
UNSCORED = ("edge/Cargo.lock", "ctutils", "0.4.3", [(vuln("RUSTSEC-2026-0385"), "")])
INFO = ("edge/Cargo.lock", "unsafe-libyaml", "0.2.11", [(vuln("RUSTSEC-2024-0449", informational="unmaintained"), "")])
HIGH_LABEL_ONLY = ("portal/package-lock.json", "zod", "4.6.5", [(vuln("GHSA-mrhx-c7xc-m3w5", label="HIGH"), "")])


def waiver(id_="RUSTSEC-2026-0344", package="ruzstd", expires="2026-12-15", **kw):
    w = {"id": id_, "package": package, "reason": "the input is hash-checked first", "owner": "edge",
         "expires": expires}
    w.update(kw)
    return w


class ScanCase(unittest.TestCase):
    def run_scan(self, result, waivers=(), today=TODAY):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "osv.json").write_text(json.dumps(result))
            lines = []
            for w in waivers:
                lines.append("[[waiver]]")
                for k, v in w.items():
                    lines.append(f"{k} = {v}" if k == "expires" and v and v[0].isdigit() else f"{k} = {json.dumps(v)}")
            (t / "w.toml").write_text("\n".join(lines) + "\n")
            r = subprocess.run([sys.executable, str(HERE.parent / "scan.py"), "--osv-json", str(t / "osv.json"),
                                "--waivers", str(t / "w.toml"), "--today", today, "--out", str(t / "out")],
                               capture_output=True, text=True)
            report = json.loads((t / "out" / "scan.json").read_text())
            md = (t / "out" / "scan.md").read_text()
        return r, report, md


class Scan(ScanCase):
    def test_an_unwaived_high_finding_fails_the_scan(self):
        r, report, md = self.run_scan(osv(HIGH))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL   HIGH     ruzstd 0.8.3", r.stdout)
        self.assertFalse(report["passed"])
        self.assertIn("**FAILS the release**", md)

    def test_a_high_finding_by_its_database_label_alone_fails(self):
        r, _, _ = self.run_scan(osv(HIGH_LABEL_ONLY))
        self.assertEqual(r.returncode, 1)
        self.assertIn("HIGH     zod", r.stdout)

    def test_an_unwaived_critical_finding_fails_the_scan(self):
        r, _, _ = self.run_scan(osv(CRIT))
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITICAL certifi", r.stdout)

    def test_a_finding_nobody_scored_fails_until_someone_reads_it(self):
        r, _, _ = self.run_scan(osv(UNSCORED))
        self.assertEqual(r.returncode, 1)
        self.assertIn("UNKNOWN  ctutils", r.stdout)

    def test_a_waived_high_finding_passes_and_the_report_says_why(self):
        r, report, md = self.run_scan(osv(HIGH), [waiver()])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(report["passed"])
        self.assertIn("waived until 2026-12-15: the input is hash-checked first", md)

    def test_a_waiver_matches_any_alias(self):
        r, _, _ = self.run_scan(osv(CRIT), [waiver("PYSEC-2026-4320", "certifi")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_waiver_for_another_lockfile_does_not_cover_it(self):
        r, _, _ = self.run_scan(osv(HIGH), [waiver(lockfile="spikes/filer/guard/Cargo.lock")])
        self.assertEqual(r.returncode, 1)

    def test_an_expired_waiver_fails_the_scan_even_for_the_finding_it_covered(self):
        r, report, _ = self.run_scan(osv(HIGH), [waiver(expires="2026-11-02")])
        self.assertEqual(r.returncode, 1)
        self.assertIn("expired on 2026-11-02", r.stderr)
        self.assertTrue(report["waiver_problems"])

    def test_an_expired_waiver_fails_even_with_nothing_to_cover(self):
        r, _, _ = self.run_scan(osv(MED), [waiver(expires="2026-01-01")])
        self.assertEqual(r.returncode, 1)

    def test_a_waiver_without_an_owner_or_reason_fails(self):
        for missing in ("owner", "reason", "expires"):
            w = waiver()
            w[missing] = ""
            r, _, _ = self.run_scan(osv(HIGH), [w])
            self.assertEqual(r.returncode, 1, missing)
            self.assertIn(f"missing {missing}", r.stderr)

    def test_the_day_a_waiver_expires_it_still_holds(self):
        r, _, _ = self.run_scan(osv(HIGH), [waiver(expires=TODAY)])
        self.assertEqual(r.returncode, 0)

    def test_medium_low_and_informational_findings_are_reported_only(self):
        r, report, md = self.run_scan(osv(MED, INFO))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual({f["severity"] for f in report["findings"]}, {"MEDIUM", "INFO"})
        self.assertIn("| MEDIUM | rustls |", md)

    def test_a_stale_waiver_is_reported_not_failed(self):
        r, report, _ = self.run_scan(osv(MED), [waiver()])
        self.assertEqual(r.returncode, 0)
        self.assertIn("stale", r.stderr)
        self.assertEqual(len(report["stale_waivers"]), 1)


def deb_vuln(vid, fixed=None, label="HIGH"):
    """An Ubuntu advisory with entries for two releases; only 26.04's counts."""
    aff = [{"package": {"ecosystem": "Ubuntu:22.04:LTS"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.0"}]}]}]
    ev = [{"introduced": "0"}] + ([{"fixed": fixed}] if fixed else [])
    aff.append({"package": {"ecosystem": "Ubuntu:26.04:LTS"}, "ranges": [{"events": ev}]})
    return {"id": vid, "summary": "x", "affected": aff, "database_specific": {"severity": label}}


def deb_osv(*vulns):
    return {"results": [{"source": {"path": "/x/omnifile-appliance-0.1.0.cdx.json", "type": "sbom"}, "packages": [
        {"package": {"name": n, "version": "1", "ecosystem": "Ubuntu:26.04:LTS"}, "vulnerabilities": [v],
         "groups": [{"ids": [v["id"]], "max_severity": "8.0"}]} for n, v in vulns]}]}


class OperatingSystem(ScanCase):
    def test_an_os_finding_its_distribution_has_fixed_fails(self):
        r, report, _ = self.run_scan(deb_osv(("ca-certificates", deb_vuln("USN-1", fixed="2"))))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(report["findings"][0]["fixed"], ["2"])

    def test_an_os_finding_with_no_fix_yet_is_reported_not_failed(self):
        """The 22.04 entry's fix doesn't count for a 26.04 package."""
        r, report, md = self.run_scan(deb_osv(("linux-firmware", deb_vuln("UBUNTU-CVE-1"))))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(report["findings"][0]["fixed"], [])
        self.assertIn("no fix in the distribution yet", md)

    def test_images_not_built_here_are_a_recorded_gap_and_fail_a_real_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "lock.json").write_text(json.dumps(
                {"images": {"relay": {"local_tag": "omnifile-test/absent:none", "digest": "sha256:0"}}}))
            (t / "osv.json").write_text(json.dumps({"results": []}))
            base = [sys.executable, str(HERE.parent / "scan.py"), "--osv-json", str(t / "osv.json"), "--waivers",
                    str(t / "none.toml"), "--today", TODAY, "--out", str(t / "out"), "--images-lock", str(t / "lock.json")]
            r = subprocess.run(base, capture_output=True, text=True)
            report = json.loads((t / "out" / "scan.json").read_text())
            md = (t / "out" / "scan.md").read_text()
            strict = subprocess.run(base + ["--require-images"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(report["not_scanned"], ["image relay (omnifile-test/absent:none): not built here, its OS "
                                                 "layers not scanned"])
        self.assertIn("**Not scanned:** image relay", md)
        self.assertEqual(strict.returncode, 1)


class Severity(unittest.TestCase):
    def test_cvss_bands(self):
        self.assertEqual([scan.level_of_score(s) for s in ("9.0", "8.9", "7.0", "6.9", "4.0", "3.9", "", "x")],
                         ["CRITICAL", "HIGH", "HIGH", "MEDIUM", "MEDIUM", "LOW", "NONE", "NONE"])

    def test_the_label_raises_a_lower_score(self):
        f = scan.findings_from(osv(("edge/Cargo.lock", "x", "1", [(vuln("GHSA-x", label="CRITICAL"), "5.0")])))
        self.assertEqual(f[0].severity, "CRITICAL")


class TheRealWaivers(unittest.TestCase):
    def test_every_waiver_is_complete_and_dated(self):
        data = tomllib.loads((HERE.parent / "vuln-waivers.toml").read_text())
        for w in data.get("waiver", []):
            for k in scan.WAIVER_FIELDS:
                self.assertTrue(w.get(k), f"{w.get('id')}: no {k}")
            self.assertIsInstance(w["expires"], dt.date)


if __name__ == "__main__":
    unittest.main()
