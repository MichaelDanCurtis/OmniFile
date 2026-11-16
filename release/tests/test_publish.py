"""The dry run's publishing fakes and the Bicep parameters: release/fakes/gh, release/fakes/az, bicep_params.py."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
REPO = HERE.parent.parent
FAKE_GH = HERE.parent / "fakes" / "gh"
sys.path.insert(0, str(HERE.parent))
import bicep_params as bp  # noqa: E402

LOCK = json.loads((REPO_ROOT / "deploy" / "images" / "images.lock.json").read_text())


class FakeGh(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t)
        self.env = {**os.environ, "GH_FAKE_ROOT": str(self.t / "root"), "GH_FAKE_LOG": str(self.t / "gh.log")}
        (self.t / "SHA256SUMS").write_text("x  omnifile-edge_0.2.0.efi\n")
        (self.t / "notes.md").write_text("notes\n")

    def gh(self, *args):
        return subprocess.run([str(FAKE_GH), *args], capture_output=True, text=True, env=self.env)

    def test_create_view_and_layout(self):
        self.assertIn("release not found", self.gh("release", "view", "v0.2.0", "--repo", "o/r").stderr)
        r = self.gh("release", "create", "v0.2.0", str(self.t / "SHA256SUMS"), "--repo", "o/r", "--latest",
                    "--title", "OmniFile 0.2.0", "--notes-file", str(self.t / "notes.md"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.t / "root/o/r/LATEST").read_text().strip(), "v0.2.0")
        self.assertTrue((self.t / "root/o/r/v0.2.0/SHA256SUMS").is_file())
        view = json.loads(self.gh("release", "view", "v0.2.0", "--repo", "o/r", "--json", "assets").stdout)
        self.assertEqual(view["assets"], [{"name": "SHA256SUMS", "size": (self.t / "SHA256SUMS").stat().st_size}])
        self.assertEqual(self.gh("release", "create", "v0.2.0", "--repo", "o/r").returncode, 1)
        self.assertEqual(len((self.t / "gh.log").read_text().splitlines()), 4)

    def test_the_sources_publisher_sees_a_public_repository(self):
        info = json.loads(self.gh("api", "repos/o/sources").stdout)
        self.assertEqual((info["visibility"], info["private"], info["has_issues"]), ("public", False, True))

    def test_anything_else_is_refused(self):
        for args in (["auth", "login"], ["repo", "create", "x"], ["api", "user"], ["release", "list"]):
            self.assertEqual(self.gh(*args).returncode, 2, args)

    def test_what_it_publishes_is_what_the_github_style_stub_serves(self):
        self.gh("release", "create", "v0.2.0", str(self.t / "SHA256SUMS"), "--repo", "omnifile/omnifile-edge",
                "--latest")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        stub = subprocess.Popen([sys.executable, str(REPO / "appliance/tests/gh-releases-stub.py"), str(port),
                                 str(self.t / "root"), str(self.t / "stub.log")])
        self.addCleanup(stub.wait)
        self.addCleanup(stub.kill)
        url = f"http://127.0.0.1:{port}/omnifile/omnifile-edge/releases/latest/download/SHA256SUMS"
        for _ in range(50):
            try:
                body = urllib.request.urlopen(url, timeout=2).read()
                break
            except OSError:
                time.sleep(0.1)
        self.assertEqual(body, (self.t / "SHA256SUMS").read_bytes())


class FakeAz(unittest.TestCase):
    def test_only_key_vault_key_show_and_sign_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k.pem"
            subprocess.run(f"openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt -out {key}",
                           shell=True, check=True, capture_output=True)
            env = {**os.environ, "FAKE_AZ_KEY": str(key)}
            az = str(HERE.parent / "fakes" / "az")
            kid = "https://v.vault.azure.net/keys/k/" + "0" * 32
            r = subprocess.run([az, "keyvault", "key", "show", "--id", kid, "-o", "json"], capture_output=True,
                               text=True, env=env)
            self.assertEqual(json.loads(r.stdout)["key"]["crv"], "P-256")
            r = subprocess.run([az, "account", "show"], capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0)


class BicepParams(unittest.TestCase):
    EXAMPLE = (REPO / "deploy/azure/main.example.bicepparam").read_text()

    def test_every_image_is_the_registry_repository_and_digest(self):
        out = bp.render(self.EXAMPLE, LOCK, "reg.azurecr.io", "0.1.0")
        svc = LOCK["images"]["service"]
        for p in ("apiImage", "enrolImage", "workerImage", "verifierImage"):
            self.assertIn(f"param {p} = 'reg.azurecr.io/{svc['repository']}@{svc['digest']}'", out)
        self.assertIn(f"param webImage = 'reg.azurecr.io/omnifile-web@{LOCK['images']['web']['digest']}'", out)
        self.assertIn("param registryServer = 'reg.azurecr.io'", out)
        self.assertNotIn(":0.1.0'", out)
        # Nothing else changed.
        a = [ln for ln in self.EXAMPLE.splitlines() if "Image =" not in ln and "registryServer" not in ln
             and not ln.startswith("// Images")]
        b = [ln for ln in out.splitlines() if "Image =" not in ln and "registryServer" not in ln
             and not ln.startswith("// Images")]
        self.assertEqual(a, b)

    def test_an_image_without_a_digest_or_a_bad_registry_is_refused(self):
        bad = json.loads(json.dumps(LOCK))
        bad["images"]["relay"]["digest"] = "latest"
        with self.assertRaises(SystemExit):
            bp.render(self.EXAMPLE, bad, "reg.azurecr.io", "0.1.0")
        with self.assertRaises(SystemExit):
            bp.render(self.EXAMPLE, LOCK, "https://reg.azurecr.io/", "0.1.0")

    def test_it_compiles_against_main_bicep(self):
        if not shutil.which("bicep") and not shutil.which("az"):
            self.skipTest("no Bicep CLI")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "omnifile-images-0.1.0.bicepparam"
            out.write_text(bp.render(self.EXAMPLE, LOCK, "reg.azurecr.io", "0.1.0"))
            bp.compile_check(out, REPO / "deploy" / "azure")


if __name__ == "__main__":
    unittest.main()
