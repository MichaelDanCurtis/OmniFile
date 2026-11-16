"""release/public_snapshot.py: the scrub rules, and the checks that refuse a snapshot."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import public_snapshot as ps  # noqa: E402

GITLEAKS = shutil.which("gitleaks")


class Rules(unittest.TestCase):
    def test_each_kind_of_rule_is_read(self):
        r = ps.load_rules("# a comment\n\nreplace\t123\t999\ndrop-line\tsecret.example\ndrop-path\tdocs/private/*\n")
        self.assertEqual(r.replace, [("123", "999")])
        self.assertEqual(r.drop_line, ["secret.example"])
        self.assertEqual(r.drop_path, ["docs/private/*"])
        self.assertEqual(r.forbidden(), ["123", "secret.example"])

    def test_a_malformed_rule_is_refused_not_skipped(self):
        for bad in ("replace\tonly-one-field", "replace\t\tnew", "drop-line", "delete\tx", "replace 1 2"):
            with self.assertRaises(ValueError, msg=bad):
                ps.load_rules(bad + "\n")


class Scrub(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree)

    def write(self, rel, data):
        p = self.tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)
        return p

    def test_replaces_drops_lines_and_paths_and_leaves_binaries_alone(self):
        a = self.write("a.json", '{"arn": "arn:aws:iam::123456789012:role/x"}\n')
        b = self.write("notes.md", "keep\nsee https://private.example/notes/1\nkeep too\n")
        c = self.write("docs/private/plan.md", "123456789012\n")
        png = self.write("img.png", b"\x89PNG\x00\x00" + b"123456789012")
        rules = ps.load_rules("replace\t123456789012\t111122223333\n"
                              "drop-line\thttps://private.example/notes/\ndrop-path\tdocs/private/*\n")
        changed = ps.scrub_tree(self.tree, rules)
        self.assertIn("111122223333", a.read_text())
        self.assertEqual(b.read_text(), "keep\nkeep too\n")
        self.assertFalse(c.exists())
        self.assertEqual(png.read_bytes(), b"\x89PNG\x00\x00" + b"123456789012")
        self.assertIn("dropped docs/private/plan.md", changed)
        # the binary still holds the string, so the check refuses the snapshot
        with self.assertRaises(ps.Refused) as e:
            ps.check_tree(self.tree, rules, gitleaks=None)
        self.assertIn("img.png", str(e.exception))

    def test_a_clean_tree_passes_and_a_big_file_is_refused(self):
        self.write("ok.txt", "nothing to see\n")
        rules = ps.load_rules("replace\t123456789012\t111122223333\n")
        ps.check_tree(self.tree, rules, gitleaks=None)
        big = self.write("big.bin", b"")
        with big.open("wb") as f:
            f.truncate(ps.MAX_FILE + 1)
        with self.assertRaises(ps.Refused) as e:
            ps.check_tree(self.tree, rules, gitleaks=None)
        self.assertIn("over 50 MB", str(e.exception))

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_gitleaks_refuses_a_private_key(self):
        # assembled at run time, so this source file isn't itself something gitleaks refuses
        label = "RSA " + "PRIVATE" + " KEY"
        self.write("k.pem", f"-----BEGIN {label}-----\nMIIEowIBAAKCAQEA{'x' * 64}\n-----END {label}-----\n")
        with self.assertRaises(ps.Refused) as e:
            ps.check_tree(self.tree, ps.Rules(), gitleaks=GITLEAKS)
        self.assertIn("gitleaks", str(e.exception))

    @unittest.skipUnless(GITLEAKS, "gitleaks is not installed")
    def test_gitleaks_passes_a_clean_tree(self):
        self.write("ok.txt", "nothing to see\n")
        ps.check_tree(self.tree, ps.Rules(), gitleaks=GITLEAKS)


if __name__ == "__main__":
    unittest.main()
