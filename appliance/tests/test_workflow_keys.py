"""The appliance lane uses one update key end to end (Codex on #31).

Every image the workflow builds embeds the keyring pgp_kv.py makes from the update key,
every feed it signs is signed with that key, and release.sh is told that keyring. Before
this, the images embedded a separate gpg ring, so the feed verified on the runner while the
built appliance would have rejected it.
"""

import os
import re
import unittest

WF = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows", "appliance.yml")


class WorkflowKeys(unittest.TestCase):
    def setUp(self):
        self.text = open(WF).read()
        # Continuation lines joined, so each command is one line.
        self.lines = [l for l in re.sub(r"\\\n\s*", " ", self.text).splitlines()
                      if not l.strip().startswith("#")]

    def test_one_keyring_made_from_the_update_key(self):
        made = [l for l in self.lines if "pgp_kv.py pubkey" in l]
        self.assertEqual(len(made), 1, made)
        self.assertIn('--signer "fake:$K/update.pem"', made[0])
        self.assertIn('--out "$K/update-ring.pgp"', made[0])
        self.assertNotIn("gpg --export", self.text, "no second, gpg-made keyring")

    def test_every_image_embeds_it(self):
        builds = [l for l in self.lines if "appliance/build.sh" in l and "--version" in l]
        self.assertGreaterEqual(len(builds), 2, builds)
        for b in builds:
            self.assertIn('--pubring "$KEYS/update-ring.pgp"', b, b)

    def test_every_feed_is_signed_with_its_key(self):
        signed = [l for l in self.lines
                  if "appliance/release.sh" in l or "appliance/tests/update-round.sh" in l]
        self.assertTrue(signed)
        text = "\n".join(self.lines)
        self.assertIn('--signer "fake:$KEYS/update.pem"', text)
        self.assertIn('--update-ring "$KEYS/update-ring.pgp"', text)
        for l in signed:
            self.assertNotIn("--sign-key", l, l)
        rounds = [l for l in self.lines if "appliance/tests/update-round.sh" in l]
        for r in rounds:
            self.assertIn('--signer "fake:$KEYS/update.pem"', r, r)

    def test_the_lane_checks_the_images_keyring(self):
        self.assertIn("ring-fingerprints appliance/build/stage/usr/lib/systemd/import-pubring.pgp",
                      self.text)


if __name__ == "__main__":
    unittest.main()
