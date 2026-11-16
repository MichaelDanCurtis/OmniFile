"""pgp_kv.py: its keyring and signatures pass gpg exactly as systemd-sysupdate runs it.

The Key Vault path runs against tests/fake_az.py (no Azure); the fake path against
openssl. gpg verifies with the options systemd-pull uses (its own keyring only).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import pgp_kv  # noqa: E402

CREATED = 1790000000


def newkey(path):
    """A named-curve P-256 key, PKCS#8 (LibreSSL's genpkey writes explicit parameters)."""
    ec = subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"],
                        check=True, capture_output=True).stdout
    subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-out", path], input=ec,
                   check=True, capture_output=True)


def gpg_verify(home, ring, sig, data):
    """As systemd-pull does: only the image's keyring, batch, no trust database."""
    r = subprocess.run(["gpg", "--homedir", home, "--no-options", "--no-default-keyring",
                        "--no-auto-key-locate", "--no-auto-check-trustdb", "--batch",
                        "--trust-model=always", f"--keyring={ring}", "--verify", sig, data],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stderr


@unittest.skipUnless(shutil.which("gpg") and shutil.which("openssl"), "needs gpg and openssl")
class PgpKv(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.home = os.path.join(self.d, "gnupg")
        os.mkdir(self.home, 0o700)
        self.key = os.path.join(self.d, "kv.pem")
        self.other = os.path.join(self.d, "other.pem")
        newkey(self.key)
        newkey(self.other)
        self.data = os.path.join(self.d, "SHA256SUMS")
        with open(self.data, "w") as f:
            f.write("0" * 64 + "  omnifile-edge_0.2.0.efi\n")
        os.environ["AZ"] = os.path.join(HERE, "fake_az.py")
        os.environ["FAKE_AZ_KEY"] = self.key
        os.environ["FAKE_AZ_LOG"] = os.path.join(self.d, "az.log")
        self.kid = "https://omnifile-rel.vault.azure.net/keys/updates/0123456789abcdef0123456789abcdef"

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_tool(self, *args):
        return pgp_kv.main(list(args))

    def ring(self, spec):
        out = os.path.join(self.d, "ring.pgp")
        self.run_tool("pubkey", "--signer", spec, "--uid", "OmniFile updates <u@example>",
                      "--created", str(CREATED), "--out", out)
        return out

    def test_a_key_vault_signature_verifies_and_the_key_only_signs_a_digest(self):
        ring = self.ring(f"kv:{self.kid}")
        sig = self.data + ".gpg"
        self.run_tool("sign", "--signer", f"kv:{self.kid}", "--created", str(CREATED),
                      "--ring", ring, "--in", self.data, "--out", sig)
        ok, err = gpg_verify(self.home, ring, sig, self.data)
        self.assertTrue(ok, err)
        self.assertIn("Good signature", err)
        log = open(os.environ["FAKE_AZ_LOG"]).read()
        self.assertIn(f"keyvault key sign --algorithm ES256 --digest", log)
        self.assertIn(f"--id {self.kid}", log)
        # The file's bytes never reach Key Vault: only digests of 32 bytes.
        for line in log.splitlines():
            if " sign " in line:
                digest = line.split("--digest ")[1].split()[0]
                self.assertEqual(len(__import__("base64").b64decode(digest)), 32)

    def test_the_fake_signer_matches_the_key_vault_one(self):
        self.assertEqual(
            pgp_kv.fingerprint(pgp_kv.key_body(pgp_kv.signer_from(f"kv:{self.kid}").public_point(), CREATED)),
            pgp_kv.fingerprint(pgp_kv.key_body(pgp_kv.signer_from(f"fake:{self.key}").public_point(), CREATED)),
        )

    def raw_sign(self, spec, created):
        """A signature made without the ring check, as a careless signer would."""
        with open(self.data, "rb") as f:
            sig = pgp_kv.sign(pgp_kv.signer_from(spec), created, f.read())
        path = self.data + ".gpg"
        with open(path, "wb") as f:
            f.write(sig)
        return path

    def test_a_tampered_file_another_key_or_another_creation_time_fails_gpg(self):
        ring = self.ring(f"kv:{self.kid}")
        ok, _ = gpg_verify(self.home, ring, self.raw_sign(f"fake:{self.other}", CREATED), self.data)
        self.assertFalse(ok, "another key's signature")
        ok, _ = gpg_verify(self.home, ring, self.raw_sign(f"kv:{self.kid}", CREATED + 1), self.data)
        self.assertFalse(ok, "a signature naming another fingerprint")
        sig = self.raw_sign(f"kv:{self.kid}", CREATED)
        with open(self.data, "a") as f:
            f.write("tampered\n")
        ok, err = gpg_verify(self.home, ring, sig, self.data)
        self.assertFalse(ok)
        self.assertIn("BAD signature", err)

    def test_a_rotated_key_vault_key_is_refused_before_signing(self):
        # The image's keyring is the key's version 1; Key Vault now holds another key
        # (a rotation, or the wrong version): nothing is signed (Codex on #31).
        ring = self.ring(f"kv:{self.kid}")
        os.environ["FAKE_AZ_KEY"] = self.other
        before = len(open(os.environ["FAKE_AZ_LOG"]).read().splitlines())
        sig = self.data + ".gpg"
        with self.assertRaises(SystemExit) as e:
            self.run_tool("sign", "--signer", f"kv:{self.kid}", "--created", str(CREATED),
                          "--ring", ring, "--in", self.data, "--out", sig)
        self.assertIn("is not in", str(e.exception))
        self.assertFalse(os.path.exists(sig))
        log = open(os.environ["FAKE_AZ_LOG"]).read().splitlines()[before:]
        self.assertFalse([l for l in log if l.startswith("keyvault key sign")],
                         "Key Vault was not asked to sign")
        # The same with a fake signer whose key isn't the ring's.
        with self.assertRaises(SystemExit):
            self.run_tool("sign", "--signer", f"fake:{self.other}", "--created", str(CREATED),
                          "--ring", ring, "--in", self.data, "--out", sig)
        # And the ring lists exactly the one key.
        self.assertEqual(
            [f.hex() for f in pgp_kv.ring_fingerprints(open(ring, "rb").read())],
            [pgp_kv.fingerprint(pgp_kv.key_body(pgp_kv.FakeSigner(self.key).public_point(), CREATED)).hex()],
        )

    def test_a_key_id_without_its_version_is_refused(self):
        for kid in ("https://omnifile-rel.vault.azure.net/keys/updates",
                    "https://omnifile-rel.vault.azure.net/keys/updates/latest"):
            with self.assertRaises(SystemExit) as e:
                pgp_kv.signer_from(f"kv:{kid}")
            self.assertIn("with its version", str(e.exception))

    def test_the_secure_boot_key_must_be_the_enrolled_certificates(self):
        rsa = os.path.join(self.d, "sb.pem")
        rotated = os.path.join(self.d, "sb2.pem")
        cert = os.path.join(self.d, "sb.crt")
        for k in (rsa, rotated):
            subprocess.run(["openssl", "genrsa", "-out", k, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "req", "-new", "-x509", "-key", rsa, "-out", cert, "-days", "2",
                        "-subj", "/CN=OmniFile test Secure Boot", "-sha256"],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
        kid = "https://omnifile-rel.vault.azure.net/keys/uki-signing/" + "ab" * 16
        os.environ["FAKE_AZ_KEY"] = rsa
        self.assertEqual(self.run_tool("check-cert", "--signer", f"kv:{kid}", "--cert", cert), 0)
        self.assertEqual(self.run_tool("check-cert", "--signer", f"fake:{rsa}", "--cert", cert), 0)
        os.environ["FAKE_AZ_KEY"] = rotated
        with self.assertRaises(SystemExit) as e:
            self.run_tool("check-cert", "--signer", f"kv:{kid}", "--cert", cert)
        self.assertIn("not the key of", str(e.exception))

    def test_a_signer_spec_must_be_kv_or_fake(self):
        with self.assertRaises(SystemExit):
            pgp_kv.signer_from("gpg:ABCDEF")


if __name__ == "__main__":
    unittest.main()
