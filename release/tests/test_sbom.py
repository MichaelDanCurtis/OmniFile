"""release/sbom.py: CycloneDX 1.6 from the lockfiles: scopes, direct dependencies, markers, determinism."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import sbom  # noqa: E402

UV_LOCK = """version = 1
requires-python = ">=3.12"

[[package]]
name = "app"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "httpx" },
    { name = "psycopg", extra = ["binary"] },
]

[package.optional-dependencies]
otlp = [{ name = "grpcio" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "certifi" }, { name = "exceptiongroup", marker = "python_full_version < '3.11'" }]
sdist = { url = "https://x/httpx.tar.gz", hash = "sha256:aaaa", size = 1 }

[[package]]
name = "certifi"
version = "2026.10.18"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "exceptiongroup"
version = "1.2.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "psycopg"
version = "3.3.6"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "tzdata", marker = "sys_platform == 'win32'" }]

[package.optional-dependencies]
binary = [{ name = "psycopg-binary", marker = "implementation_name != 'pypy'" }]

[[package]]
name = "psycopg-binary"
version = "3.3.6"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "tzdata"
version = "2026.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "grpcio"
version = "1.70.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.1.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "colorama", marker = "sys_platform == 'win32'" }, { name = "certifi" }]

[[package]]
name = "colorama"
version = "0.4.6"
source = { registry = "https://pypi.org/simple" }
"""

NPM_LOCK = {
    "name": "web", "version": "0.1.0", "lockfileVersion": 3, "requires": True,
    "packages": {
        "": {"name": "web", "version": "0.1.0", "license": "BUSL-1.1", "dependencies": {"react": "19.3.0"},
             "devDependencies": {"vitest": "5.0.3"}},
        "node_modules/react": {"version": "19.3.0", "license": "MIT", "integrity": "sha512-AAAA"},
        "node_modules/vitest": {"version": "5.0.3", "dev": True, "license": "MIT", "dependencies": {"debug": "^4"}},
        "node_modules/debug": {"version": "4.4.0", "dev": True, "license": "MIT"},
        "node_modules/vitest/node_modules/debug": {"version": "3.2.7", "dev": True, "license": "MIT"},
        "node_modules/@scope/opt": {"version": "1.0.0", "optional": True, "license": "Apache-2.0"},
    },
}


def by_name(doc):
    return {c["name"] + "@" + c["version"]: c for c in doc["components"]}


def props(c):
    return {p["name"]: p["value"] for p in c.get("properties", [])}


class Python(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t)
        (self.t / "app").mkdir()
        (self.t / "app" / "uv.lock").write_text(UV_LOCK)
        (self.t / "app" / "pyproject.toml").write_text('[project]\nname = "app"\nlicense = "BUSL-1.1"\n')

    def doc(self):
        return sbom.uv_sbom(self.t / "app" / "uv.lock", "0.1.0", repo=self.t, venv=self.t / "none")

    def test_scopes_follow_runtime_extras_and_dev_groups(self):
        c = by_name(self.doc())
        self.assertEqual(c["httpx@0.28.1"]["scope"], "required")
        self.assertEqual(c["certifi@2026.10.18"]["scope"], "required")  # also a dev dependency's: runtime wins
        self.assertEqual(c["psycopg-binary@3.3.6"]["scope"], "required")  # through psycopg's binary extra
        self.assertEqual(c["grpcio@1.70.0"]["scope"], "optional")
        self.assertEqual(c["pytest@9.1.1"]["scope"], "excluded")

    def test_markers_are_evaluated_for_the_images_python(self):
        c = by_name(self.doc())
        for gone in ("exceptiongroup@1.2.0", "tzdata@2026.1", "colorama@0.4.6"):
            self.assertNotIn(gone, c)

    def test_direct_dependencies_are_marked(self):
        c = by_name(self.doc())
        self.assertEqual(props(c["httpx@0.28.1"])["omnifile:direct"], "true")
        self.assertEqual(props(c["grpcio@1.70.0"])["omnifile:dependency-kind"], "extra:otlp")
        self.assertNotIn("omnifile:direct", props(c["certifi@2026.10.18"]))

    def test_marker_logic(self):
        env = dict(sbom.PY_ENV, extra="")
        self.assertTrue(sbom.marker_true("python_full_version >= '3.12' and sys_platform == 'linux'", env))
        self.assertFalse(sbom.marker_true("(sys_platform == 'win32' or os_name == 'nt') and python_version < '4'", env))
        self.assertTrue(sbom.marker_true("python_version < '3.13' or platform_machine == 'arm64'", env))


class Npm(unittest.TestCase):
    def test_npm_scopes_nesting_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "web").mkdir()
            (t / "web" / "package-lock.json").write_text(json.dumps(NPM_LOCK))
            doc = sbom.npm_sbom(t / "web" / "package-lock.json", "0.1.0", repo=t)
        c = {(x["name"], x["version"]): x for x in doc["components"]}
        self.assertEqual(c[("react", "19.3.0")]["scope"], "required")
        self.assertEqual(c[("vitest", "5.0.3")]["scope"], "excluded")
        self.assertEqual(c[("@scope/opt", "1.0.0")]["scope"], "optional")
        self.assertEqual(c[("@scope/opt", "1.0.0")]["purl"], "pkg:npm/%40scope/opt@1.0.0")
        self.assertEqual(c[("react", "19.3.0")]["hashes"][0]["alg"], "SHA-512")
        deps = {d["ref"]: d["dependsOn"] for d in doc["dependencies"]}
        self.assertEqual(deps[c[("vitest", "5.0.3")]["bom-ref"]], [c[("debug", "3.2.7")]["bom-ref"]])


class Cargo(unittest.TestCase):
    def test_dev_only_crates_are_excluded_and_runtime_ones_required(self):
        def pkg(name, version, deps=(), lic="MIT"):
            return {"id": f"{name} {version}", "name": name, "version": version, "license": lic,
                    "dependencies": [{"name": d, "kind": k, "path": None} for d, k in deps]}

        meta = {
            "workspace_members": ["edge 0.1.0", "proto 0.1.0"],
            "packages": [pkg("edge", "0.1.0", [("tokio", None), ("proptest", "dev")], "BUSL-1.1"),
                         pkg("proto", "0.1.0", [], "BUSL-1.1"),
                         pkg("tokio", "1.0.0"), pkg("mio", "1.0.0"), pkg("proptest", "1.0.0"), pkg("rand", "0.9.0")],
            "resolve": {"nodes": [
                {"id": "edge 0.1.0", "deps": [{"pkg": "tokio 1.0.0", "dep_kinds": [{"kind": None}]},
                                              {"pkg": "proto 0.1.0", "dep_kinds": [{"kind": None}]},
                                              {"pkg": "proptest 1.0.0", "dep_kinds": [{"kind": "dev"}]}]},
                {"id": "tokio 1.0.0", "deps": [{"pkg": "mio 1.0.0", "dep_kinds": [{"kind": None}]}]},
                {"id": "proptest 1.0.0", "deps": [{"pkg": "rand 0.9.0", "dep_kinds": [{"kind": None}]}]},
                {"id": "mio 1.0.0", "deps": []}, {"id": "rand 0.9.0", "deps": []}, {"id": "proto 0.1.0", "deps": []}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "edge").mkdir()
            (t / "edge" / "Cargo.lock").write_text('version = 4\n\n[[package]]\nname = "tokio"\nversion = "1.0.0"\n'
                                                   'checksum = "abcd"\n')
            doc = sbom.cargo_from_metadata(meta, t / "edge" / "Cargo.lock", "0.1.0", "x86_64-unknown-linux-gnu", t)
        c = by_name(doc)
        self.assertEqual([c[n]["scope"] for n in ("tokio@1.0.0", "mio@1.0.0", "proptest@1.0.0", "rand@0.9.0")],
                         ["required", "required", "excluded", "excluded"])
        self.assertEqual(props(c["proto@0.1.0"])["omnifile:own"], "true")
        self.assertNotIn("omnifile:direct", props(c["proto@0.1.0"]))
        self.assertEqual(c["tokio@1.0.0"]["hashes"], [{"alg": "SHA-256", "content": "abcd"}])


class Debian(unittest.TestCase):
    def test_dep5_licences_map_to_spdx(self):
        text = ("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n\n"
                "Files: *\nLicense: GPL-2+\n\nFiles: lib/*\nLicense: LGPL-2.1+ or BSD-3-clause\n\n"
                "License: GPL-2+\n text\n")
        self.assertEqual(sbom.dep5_licences(text), ["BSD-3-Clause", "GPL-2.0-or-later", "LGPL-2.1-or-later"])
        self.assertEqual(sbom.dep5_licences("free text, not DEP-5\nLicense: GPL"), [])


class Pins(unittest.TestCase):
    def test_pin_files_are_read_as_sh_reads_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin = Path(tmp) / "PIN"
            pin.write_text("# a comment\nV=1.8.5\nSHA_x86_64=abc\nURL=https://x/$V/bin\nSHA=${SHA_x86_64}\n")
            got = sbom.kv_file(pin)
        self.assertEqual((got["URL"], got["SHA"]), ("https://x/1.8.5/bin", "abc"))


class ThisTree(unittest.TestCase):
    def test_the_sboms_are_valid_cyclonedx_and_deterministic(self):
        """Every lockfile git tracks, the images fixture and the WP2 manifest, twice, then the official validator."""
        if not shutil.which("cargo") or not shutil.which("uv"):
            self.skipTest("needs cargo and uv")
        outs = []
        for _ in range(2):
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d)
            r = subprocess.run([sys.executable, str(HERE.parent / "sbom.py"), "--out", d, "--images-lock",
                                str(REPO_ROOT / "deploy" / "images" / "images.lock.json"), "--appliance-manifest",
                                str(HERE.parent / "testdata" / "appliance.manifest"), "--appliance-build-info",
                                str(HERE.parent / "testdata" / "appliance.BUILD-INFO")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.append(d)
        files = sorted(p.name for p in Path(outs[0]).glob("*.cdx.json"))
        names = {sbom.sbom_name(lk) for lk in sbom.tracked_lockfiles()}
        for n in names | {"images", "appliance", "tools"}:
            self.assertIn(f"omnifile-{n}-{(REPO / 'VERSION').read_text().strip()}.cdx.json", files)
        for f in files:
            self.assertEqual((Path(outs[0]) / f).read_bytes(), (Path(outs[1]) / f).read_bytes(), f)
        r = subprocess.run([sys.executable, str(HERE.parent / "validate_sbom.py"),
                            *[str(Path(outs[0]) / f) for f in files]], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_validator_refuses_an_invalid_sbom(self):
        if not shutil.which("uv"):
            self.skipTest("needs uv")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.cdx.json"
            bad.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
                                       "components": [{"type": "library", "name": "x", "scope": "sometimes"}]}))
            r = subprocess.run([sys.executable, str(HERE.parent / "validate_sbom.py"), str(bad)],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("INVALID", r.stdout)

    def test_every_tracked_lockfile_is_one_the_scan_reads(self):
        import scan

        self.assertEqual(scan.tracked_lockfiles(), sbom.tracked_lockfiles())
        self.assertTrue(any(p.name == "Cargo.lock" for p in sbom.tracked_lockfiles()))


if __name__ == "__main__":
    unittest.main()
