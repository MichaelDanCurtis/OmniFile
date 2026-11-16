"""release/check_offer.py: a source offer is complete for one build, or says what is missing."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import check_offer as co  # noqa: E402

GAN, RPC = "a" * 40, "b" * 40


class Offer(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t)
        # A repository with the pins.
        self.repo = self.t / "repo"
        (self.repo / "samba-vfs").mkdir(parents=True)
        (self.repo / "fsal").mkdir()
        samba = b"samba release"
        (self.repo / "samba-vfs" / "SHA256SUMS").write_text(
            f"{hashlib.sha256(samba).hexdigest()}  samba-4.23.6.tar.gz\n")
        (self.repo / "fsal" / "GANESHA_VERSION").write_text(f"GANESHA_TAG=V15.6\nGANESHA_COMMIT={GAN}\nNTIRPC_COMMIT={RPC}\n")
        # The build.
        self.build = self.t / "build"
        self.build.mkdir()
        (self.build / "omnifile-edge_0.1.0.manifest").write_text(json.dumps({
            "config": {"version": "0.1.0"},
            "packages": [{"type": "deb", "name": "samba", "version": "2:4.23.6"},
                         {"type": "deb", "name": "libacl1", "version": "2.3.2-2"}]}))
        (self.build / "BUILD-INFO").write_text("version=0.1.0\nsystemd-boot-efi=259.9\n")
        # The offer.
        self.o = self.t / "offer"
        files = {
            "ubuntu/samba_2:4.23.6/samba.dsc": b"dsc", "ubuntu/acl_2.3.2-2/acl.dsc": b"acl",
            "ubuntu/systemd_259.9/systemd.dsc": b"systemd", "ubuntu/btrfs-progs_6.1/btrfs.dsc": b"btrfs",
            "upstream/samba-4.23.6.tar.gz": samba, f"upstream/nfs-ganesha-{GAN}.tar.gz": b"g",
            f"upstream/ntirpc-{RPC}.tar.gz": b"r", "omnifile/fsal.tar.gz": b"f", "omnifile/samba-vfs.tar.gz": b"v",
        }
        sums = []
        for rel, data in files.items():
            (self.o / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.o / rel).write_bytes(data)
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
        (self.o / "SHA256SUMS").write_text("\n".join(sums) + "\n")

        def src(name, ver, rel):
            return {"name": name, "version": ver, "files": [{"path": rel, "sha256": hashlib.sha256(files[rel]).hexdigest()}]}

        self.manifest = {
            "release": "0.1.0", "complete": True,
            "sources": [src("samba", "2:4.23.6", "ubuntu/samba_2:4.23.6/samba.dsc"),
                        src("acl", "2.3.2-2", "ubuntu/acl_2.3.2-2/acl.dsc"),
                        src("systemd", "259.9", "ubuntu/systemd_259.9/systemd.dsc"),
                        src("btrfs-progs", "6.1", "ubuntu/btrfs-progs_6.1/btrfs.dsc")],
            "initrd": {"packages_not_in_the_image": ["btrfs-progs=6.1"], "unexplained": [], "files_the_archive_ships": 5},
        }
        self.write_manifest()
        (self.o / "packages.tsv").write_text(
            "binary\tversion\tsource\tsource_version\n"
            "samba\t2:4.23.6\tsamba\t2:4.23.6\nlibacl1\t2.3.2-2\tacl\t2.3.2-2\n"
            "systemd-boot-efi\t259.9\tsystemd\t259.9\nbtrfs-progs\t6.1\tbtrfs-progs\t6.1\n")
        (self.o / "SOURCE-OFFER.md").write_text("# Source code\n")

    def write_manifest(self):
        (self.o / "MANIFEST.json").write_text(json.dumps(self.manifest))

    def problems(self, **kw):
        args = dict(release=None, need_initrd=True, allow_draft=False)
        args.update(kw)
        p, _ = co.check(self.o, self.build / "omnifile-edge_0.1.0.manifest", self.build / "BUILD-INFO",
                        args["release"], args["need_initrd"], args["allow_draft"], repo=self.repo)
        return p

    def test_a_complete_offer_passes(self):
        self.assertEqual(self.problems(), [])

    def test_a_package_of_the_image_without_its_source_fails(self):
        (self.o / "packages.tsv").write_text((self.o / "packages.tsv").read_text().replace("libacl1\t2.3.2-2\tacl\t2.3.2-2\n", ""))
        self.assertIn("libacl1=2.3.2-2 is in the image but not in packages.tsv", self.problems())

    def test_another_version_of_a_package_does_not_count(self):
        t = (self.o / "packages.tsv").read_text().replace("samba\t2:4.23.6\tsamba", "samba\t2:4.23.5\tsamba")
        (self.o / "packages.tsv").write_text(t)
        self.assertIn("samba=2:4.23.6 is in the image but not in packages.tsv", self.problems())

    def test_the_ukis_stub_needs_its_source(self):
        t = (self.o / "packages.tsv").read_text().replace("systemd-boot-efi\t259.9\tsystemd\t259.9\n", "")
        (self.o / "packages.tsv").write_text(t)
        self.assertIn("systemd-boot-efi=259.9 is in the image but not in packages.tsv", self.problems())

    def test_an_unchecked_initrd_fails(self):
        del self.manifest["initrd"]
        self.write_manifest()
        self.assertTrue(any("the initrd was not checked" in p for p in self.problems()))
        self.assertEqual(self.problems(need_initrd=False), [])

    def test_an_initrd_check_that_matched_nothing_fails(self):
        self.manifest["initrd"]["files_the_archive_ships"] = 0
        self.write_manifest()
        self.assertTrue(any("checked nothing" in p for p in self.problems()))

    def test_an_initrd_file_from_an_unlisted_package_fails(self):
        self.manifest["initrd"]["unexplained"] = ["usr/bin/mdadm (mdadm)"]
        self.write_manifest()
        self.assertTrue(any("initrd files come from packages without source" in p for p in self.problems()))

    def test_a_file_that_does_not_match_its_checksum_fails(self):
        (self.o / "ubuntu/acl_2.3.2-2/acl.dsc").write_bytes(b"tampered")
        self.assertIn("ubuntu/acl_2.3.2-2/acl.dsc does not match SHA256SUMS", self.problems())

    def test_another_samba_release_than_the_pinned_one_fails(self):
        (self.o / "upstream/samba-4.23.6.tar.gz").write_bytes(b"other")
        sums = (self.o / "SHA256SUMS").read_text().splitlines()
        sums = [s if not s.endswith("samba-4.23.6.tar.gz") else f"{hashlib.sha256(b'other').hexdigest()}  upstream/samba-4.23.6.tar.gz" for s in sums]
        (self.o / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        self.assertTrue(any("is not the pinned file" in p for p in self.problems()))

    def test_missing_ganesha_sources_fail(self):
        (self.o / f"upstream/nfs-ganesha-{GAN}.tar.gz").unlink()
        self.assertIn(f"upstream/nfs-ganesha-{GAN}.tar.gz is not in the offer", self.problems())

    def test_an_offer_for_another_release_or_incomplete_fails(self):
        self.assertTrue(any("the build is 0.2.0" in p for p in self.problems(release="0.2.0")))
        self.manifest["complete"] = False
        self.write_manifest()
        self.assertTrue(any("does not say complete" in p for p in self.problems()))

    def test_a_draft_offer_fails_unless_allowed(self):
        (self.o / "SOURCE-OFFER.md").write_text("> **DRAFT: not set**\n")
        self.assertIn("SOURCE-OFFER.md is a DRAFT", self.problems())
        self.assertEqual(self.problems(allow_draft=True), [])


if __name__ == "__main__":
    unittest.main()
