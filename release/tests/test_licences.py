"""release/licences.py: LICENSES.md against the SBOMs, both ways, and the licence rules."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import licences as L  # noqa: E402


def comp(name, version, licence, purl_type="cargo", scope="required", direct=False, children=None):
    c = {"type": "library", "bom-ref": f"pkg:{purl_type}/{name}@{version}", "name": name, "version": version,
         "purl": f"pkg:{purl_type}/{name}@{version}", "scope": scope,
         "licenses": [{"expression": licence, "acknowledgement": "declared"}] if licence else [],
         "properties": [{"name": "omnifile:direct", "value": "true"}] if direct else []}
    if children:
        c["components"] = children
    return c


def sbom(name, components, tools=False):
    return {"bomFormat": "CycloneDX", "specVersion": "1.6",
            "metadata": {"component": {"name": "omnifile-release-tools" if tools else f"omnifile-{name}"}},
            "components": components}


TABLE = """# Dependency licences

<!-- licences: cargo -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `tokio` | 1 | runtime | MIT | |
| `serde`, `serde_json` | 1 | config | MIT OR Apache-2.0 | |

<!-- licences: pypi -->
| Package | Version | Used as | Licence | Notes |
|---|---|---|---|---|
| `psycopg` | 3.3 | driver | LGPL-3.0-only | |
"""


class Check(unittest.TestCase):
    def run_check(self, table, docs):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "LICENSES.md").write_text(table)
            (t / "sbom").mkdir()
            for name, doc in docs.items():
                (t / "sbom" / f"omnifile-{name}-0.1.0.cdx.json").write_text(json.dumps(doc))
            rows = L.read_licenses(t / "LICENSES.md")
            return L.check(rows, L.read_sboms(t / "sbom"))

    def good(self):
        return {
            "edge": sbom("edge", [comp("tokio", "1.53.2", "MIT", direct=True),
                                  comp("serde", "1.0.229", "Apache-2.0 OR MIT", direct=True),
                                  comp("serde_json", "1.0.151", "MIT OR Apache-2.0", direct=True),
                                  comp("memchr", "2.8.3", "Unlicense OR MIT")]),
            "service": sbom("service", [comp("psycopg", "3.3.6", "LGPL-3.0-only", "pypi", direct=True)]),
        }

    def test_a_matching_file_passes(self):
        self.assertEqual(self.run_check(TABLE, self.good()), [])

    def test_a_direct_dependency_without_a_row_fails(self):
        docs = self.good()
        docs["edge"]["components"].append(comp("nix", "0.31.3", "MIT", direct=True))
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("nix 0.31.3 (cargo, required) is a direct dependency with no row" in x for x in p), p)

    def test_a_row_for_a_package_no_sbom_has_fails(self):
        p = self.run_check(TABLE.replace("`serde`, `serde_json`", "`serde`, `serde_json`, `left-pad`"), self.good())
        self.assertTrue(any("left-pad (cargo) is in no SBOM" in x for x in p), p)

    def test_the_same_name_in_another_ecosystem_is_not_a_match(self):
        docs = self.good()
        docs["edge"]["components"].append(comp("psycopg", "3.3.6", "LGPL-3.0-only", "cargo", direct=True))
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("psycopg 3.3.6 (cargo, required) is a direct dependency with no row" in x for x in p), p)

    def test_a_licence_that_isnt_the_packages_own_fails(self):
        docs = self.good()
        docs["edge"]["components"][0] = comp("tokio", "1.53.2", "Apache-2.0", direct=True)
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("tokio 1.53.2 is Apache-2.0 by its own metadata" in x for x in p), p)

    def test_licence_order_does_not_matter(self):
        self.assertEqual(L.canonical(L.parse_spdx("MIT OR Apache-2.0")),
                         L.canonical(L.parse_spdx("Apache-2.0 OR MIT")))
        self.assertEqual(L.canonical(L.parse_spdx("MIT/Apache-2.0")), "Apache-2.0 OR MIT")

    def test_a_stale_version_fails(self):
        p = self.run_check(TABLE.replace("| `tokio` | 1 |", "| `tokio` | 2 |"), self.good())
        self.assertTrue(any("tokio is 1.53.2" in x for x in p), p)

    def test_agpl_anywhere_fails_even_transitive_and_test_only(self):
        for scope in ("required", "excluded"):
            docs = self.good()
            docs["edge"]["components"].append(comp("evil", "1.0.0", "AGPL-3.0-only", scope=scope))
            p = self.run_check(TABLE, docs)
            self.assertTrue(any("evil 1.0.0" in x and "AGPL-3.0-only not allowed" in x for x in p), (scope, p))

    def test_gpl_linked_into_the_edge_fails(self):
        docs = self.good()
        docs["edge"]["components"].append(comp("gpl-crate", "1.0.0", "GPL-2.0-only"))
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("gpl-crate" in x for x in p), p)

    def test_a_permissive_alternative_is_enough(self):
        docs = self.good()
        docs["edge"]["components"].append(comp("dual", "1.0.0", "MIT OR LGPL-2.1-or-later"))
        self.assertEqual(self.run_check(TABLE, docs), [])

    def test_lgpl_in_python_must_be_named(self):
        docs = self.good()
        docs["service"]["components"].append(comp("psycopg-binary", "3.3.6", "LGPL-3.0-only", "pypi"))
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("psycopg-binary" in x and "must at least be named" in x for x in p), p)
        named = TABLE + "| `psycopg-binary` | 3.3 | driver | LGPL-3.0-only | |\n"
        self.assertEqual(self.run_check(named, docs), [])

    def test_lgpl_in_rust_fails_even_when_named(self):
        table = TABLE.replace("| `tokio` | 1 | runtime | MIT | |", "| `tokio` | 1 | runtime | LGPL-3.0-only | |")
        docs = self.good()
        docs["edge"]["components"][0] = comp("tokio", "1.53.2", "LGPL-3.0-only", direct=True)
        p = self.run_check(table, docs)
        self.assertTrue(any("tokio" in x and "not allow" in x for x in p), p)

    def test_gpl_ubuntu_packages_in_the_image_and_gpl_tools_are_allowed(self):
        table = TABLE + ("\n<!-- licences: deb -->\n| Package | Version | Used as | Licence | Notes |\n|---|---|---|---|---|\n"
                         "| `samba` | 2:4.23 | SMB | GPL-3.0-or-later | |\n"
                         "\n<!-- licences: tool -->\n| Package | Version | Used as | Licence | Notes |\n|---|---|---|---|---|\n"
                         "| `qemu` | - | tests | GPL-2.0-only | |\n")
        docs = self.good()
        docs["appliance"] = sbom("appliance", [comp("samba", "2:4.23.6+dfsg-1ubuntu2.2", "GPL-3.0-or-later AND LicenseRef-debian-other", "deb", direct=True)])
        docs["tools"] = sbom("tools", [comp("qemu", "11.1.2", "GPL-2.0-only", "generic", scope="excluded", direct=True)], tools=True)
        self.assertEqual(self.run_check(table, docs), [])

    def test_agpl_in_an_ubuntu_package_still_fails(self):
        docs = self.good()
        docs["appliance"] = sbom("appliance", [comp("webthing", "1.0", "MIT AND LicenseRef-debian-AGPL-3", "deb")])
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("webthing" in x for x in p), p)

    def test_nested_image_components_are_checked(self):
        docs = self.good()
        docs["images"] = sbom("images", [comp("omnifile-service", "sha256:x", None, "oci",
                                              children=[comp("evil", "1", "SSPL-1.0", "pypi")])])
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("evil 1" in x for x in p), p)

    def test_a_shipped_component_without_a_licence_fails(self):
        for scope, fails in (("required", True), ("optional", True), ("excluded", False)):
            docs = self.good()
            docs["service"]["components"].append(comp("grpcio", "1.70.0", None, "pypi", scope=scope))
            p = self.run_check(TABLE, docs)
            self.assertEqual(any("grpcio 1.70.0" in x and "has no licence" in x for x in p), fails, (scope, p))

    def test_an_override_needs_a_reason(self):
        import sbom

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "o.toml"
            f.write_text('[deb]\nlinux = { licence = "GPL-2.0-only" }\n')
            with self.assertRaises(SystemExit):
                sbom.load_overrides(f)
            f.write_text('[deb]\nlinux = { licence = "GPL-2.0-only", reason = "debian/copyright", versions = ["7.0.0-38.38"] }\n')
            self.assertEqual(sbom.load_overrides(f)["deb"]["linux"]["licence"], "GPL-2.0-only")

    def test_an_unknown_licence_fails(self):
        docs = self.good()
        docs["edge"]["components"].append(comp("odd", "1.0.0", "LicenseRef-Proprietary"))
        p = self.run_check(TABLE, docs)
        self.assertTrue(any("odd 1.0.0" in x for x in p), p)

    def test_a_row_that_is_not_spdx_fails(self):
        p = self.run_check(TABLE.replace("| config | MIT OR Apache-2.0 |", "| config | MIT, Apache |"), self.good())
        self.assertTrue(any("not an SPDX expression" in x or "trailing" in x for x in p), p)


class TheRealFile(unittest.TestCase):
    def test_licenses_md_parses_and_every_licence_is_spdx(self):
        rows = L.read_licenses(REPO / "LICENSES.md")
        self.assertGreater(len(rows), 50)
        for r in rows:
            L.parse_spdx(r.licence)

    def test_every_tool_pin_is_written_where_it_says(self):
        tools = tomllib.loads((HERE.parent / "tools.toml").read_text())["tool"]
        for name, t in tools.items():
            for k in ("version", "licence", "used_for", "pinned_in"):
                self.assertTrue(t.get(k), f"{name}: no {k}")
            f = REPO / t["pinned_in"].split(" ", 1)[0]
            if t["pinned_in"].split(" ", 1)[0].count("/") and f.is_file() and f.name != "tools.toml":
                self.assertIn(t["version"], f.read_text(), f"{name} {t['version']} is not in {t['pinned_in']}")

    def test_the_sboms_of_this_tree_match_licenses_md(self):
        """The real check over this tree's lockfiles and the images fixture. sbom.py builds its own throwaway
        environments for the Python licences, so nothing here depends on a .venv. The appliance is left out: its
        Debian licences need a collected source offer (that run is the runbook's step 4), so the image's rows are
        left out too."""
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(HERE.parent / "sbom.py"), "--out", tmp, "--images-lock",
                                str(REPO_ROOT / "deploy" / "images" / "images.lock.json")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = [r_ for r_ in L.read_licenses(REPO / "LICENSES.md") if r_.eco not in ("deb", "generic")]
            problems = L.check(rows, L.read_sboms(Path(tmp)))
        self.assertEqual(problems, [])

    def test_an_appliance_sbom_without_licences_fails(self):
        """Without the source offer the image's packages have no licences, and that fails (review on #38)."""
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(HERE.parent / "sbom.py"), "--out", tmp, "--images-lock",
                                str(REPO_ROOT / "deploy" / "images" / "images.lock.json"), "--appliance-manifest",
                                str(HERE.parent / "testdata" / "appliance.manifest")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            problems = L.check(L.read_licenses(REPO / "LICENSES.md"), L.read_sboms(Path(tmp)))
        self.assertTrue(any("samba" in p and "has no licence" in p for p in problems), problems[:3])

    def test_every_package_the_image_names_has_a_row(self):
        """Every package of mkosi.conf's Packages= (a direct dependency) in a real build's manifest has a deb row,
        at its version, and every deb row's packages are in it. The licences themselves need the source offer
        (the runbook's step 4), so only those problems are left out here. The manifest is the arm64 build of
        WP14 A2.3, which added cifs-utils and nfs-common for the annex."""
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(HERE.parent / "sbom.py"), "--out", tmp, "--appliance-manifest",
                                str(HERE.parent / "testdata" / "appliance.manifest")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = [r_ for r_ in L.read_licenses(REPO / "LICENSES.md") if r_.eco == "deb"]
            sboms = [c for c in L.read_sboms(Path(tmp)) if c.eco == "deb"]
            problems = [p for p in L.check(rows, sboms) if "has no licence" not in p]
        self.assertEqual(problems, [])
        names = {c.name for c in sboms if c.direct}
        self.assertLessEqual({"cifs-utils", "nfs-common"}, names)

if __name__ == "__main__":
    unittest.main()
