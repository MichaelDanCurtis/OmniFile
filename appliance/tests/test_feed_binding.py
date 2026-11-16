"""feed_binding.py on a small synthetic disk (python3 -m unittest discover -s appliance/tests).

A disk with a GPT holding a usr and a verity partition, and feeds made from it: this
build's feed binds; another build's (other partition UUIDs), a feed with other bytes under
the right UUID, a missing UKI or an extra file of the version are each refused.
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import feed_binding as fb  # noqa: E402

V = "0.2.0"


def make_disk(path, parts):
    """parts: [(name, type_uuid, part_uuid, bytes)], each a whole number of sectors."""
    entries = b""
    lba = 34
    layout = []
    for name, ptype, puuid, data in parts:
        n = len(data) // 512
        entries += (
            ptype.bytes_le
            + puuid.bytes_le
            + struct.pack("<QQQ", lba, lba + n - 1, 0)
            + name.encode("utf-16-le").ljust(72, b"\0")
        )
        layout.append((lba, data))
        lba += n
    entries = entries.ljust(128 * 128, b"\0")
    hdr = b"EFI PART" + struct.pack("<IIII", 0x10000, 92, 0, 0)
    hdr += struct.pack("<QQQQ", 1, lba + 33, 34, lba - 1) + uuid.uuid4().bytes_le
    hdr += struct.pack("<QIII", 2, 128, 128, 0)
    with open(path, "wb") as f:
        f.write(b"\0" * 512 + hdr.ljust(512, b"\0") + entries)
        for at, data in layout:
            f.seek(at * 512)
            f.write(data)
        f.truncate((lba + 33) * 512)


def zst(data, path):
    subprocess.run(["zstd", "-q", "-f", "-o", path], input=data, check=True)


@unittest.skipUnless(shutil.which("zstd"), "needs zstd")
class FeedBinding(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.usr, self.ver = os.urandom(64 * 512), os.urandom(8 * 512)
        self.uu, self.vu = uuid.uuid4(), uuid.uuid4()
        self.disk = os.path.join(self.d, f"omnifile-edge_{V}.raw")
        make_disk(
            self.disk,
            [
                (f"omnifile-edge_{V}", fb.USR_X86_64, self.uu, self.usr),
                (f"omnifile-edge_{V}_verity", fb.USR_VERITY_X86_64, self.vu, self.ver),
            ],
        )

    def tearDown(self):
        shutil.rmtree(self.d)

    def feed(self, name, usr=None, ver=None, uu=None, vu=None, uki=True, arch="x86-64"):
        f = os.path.join(self.d, name)
        os.makedirs(f)
        if uki:
            with open(os.path.join(f, f"omnifile-edge_{V}.efi"), "wb") as o:
                o.write(b"UKI of this build")
        zst(usr or self.usr, os.path.join(f, f"omnifile-edge_{V}.usr-{arch}.{(uu or self.uu).hex}.raw.zst"))
        zst(
            ver or self.ver,
            os.path.join(f, f"omnifile-edge_{V}.usr-{arch}-verity.{(vu or self.vu).hex}.raw.zst"),
        )
        return f

    def test_this_builds_feed_binds_and_says_how(self):
        b = fb.check(self.disk, self.feed("good"), V)
        self.assertEqual(b["usr_partuuid"], str(self.uu))
        self.assertEqual(b["verity_partuuid"], str(self.vu))
        self.assertEqual(b["architecture"], "x86-64")
        self.assertEqual(len(b["uki_sha256"]), 64)

    def test_an_arm64_build_binds_to_its_arm64_disk_only(self):
        arm = os.path.join(self.d, "arm.raw")
        make_disk(
            arm,
            [
                (f"omnifile-edge_{V}", fb.USR_ARM64, self.uu, self.usr),
                (f"omnifile-edge_{V}_verity", fb.USR_VERITY_ARM64, self.vu, self.ver),
            ],
        )
        f = self.feed("arm", arch="arm64")
        self.assertEqual(fb.check(arm, f, V)["architecture"], "arm64")
        # The same bytes and UUIDs, but the x86-64 disk's partitions are another type.
        with self.assertRaisesRegex(fb.Mismatch, "no arm64 usr partition"):
            fb.check(self.disk, f, V)
        # And an x86-64 feed is no arm64 disk's.
        with self.assertRaisesRegex(fb.Mismatch, "no x86-64 usr partition"):
            fb.check(arm, self.feed("x86"), V)

    def test_a_feed_of_two_architectures_is_refused(self):
        f = self.feed("mixed")
        zst(self.usr, os.path.join(f, f"omnifile-edge_{V}.usr-arm64.{self.uu.hex}.raw.zst"))
        with self.assertRaisesRegex(fb.Mismatch, "2 architectures"):
            fb.check(self.disk, f, V)

    def test_another_builds_feed_is_refused(self):
        f = self.feed("other", usr=os.urandom(64 * 512), uu=uuid.uuid4(), vu=uuid.uuid4())
        with self.assertRaisesRegex(fb.Mismatch, "another build"):
            fb.check(self.disk, f, V)

    def test_other_bytes_under_the_right_uuid_are_refused(self):
        with self.assertRaisesRegex(fb.Mismatch, "usr bytes differ"):
            fb.check(self.disk, self.feed("bytes", usr=os.urandom(64 * 512)), V)
        with self.assertRaisesRegex(fb.Mismatch, "verity bytes differ"):
            fb.check(self.disk, self.feed("vbytes", ver=os.urandom(8 * 512)), V)
        with self.assertRaisesRegex(fb.Mismatch, "bytes, the disk's partition"):
            fb.check(self.disk, self.feed("short", usr=self.usr[:-512]), V)

    def test_a_feed_without_its_uki_or_with_extra_files_is_refused(self):
        with self.assertRaisesRegex(fb.Mismatch, "no omnifile-edge_0.2.0.efi"):
            fb.check(self.disk, self.feed("nouki", uki=False), V)
        f = self.feed("extra")
        with open(os.path.join(f, f"omnifile-edge_{V}.usr-x86-64.{uuid.uuid4().hex}.raw.zst"), "wb"):
            pass
        with self.assertRaisesRegex(fb.Mismatch, "2 usr files"):
            fb.check(self.disk, f, V)

    def test_the_command_exits_1_on_a_mismatch(self):
        bad = self.feed("cmd", uu=uuid.uuid4())
        self.assertEqual(fb.main(["x", "check", self.disk, bad, V]), 1)
        self.assertEqual(fb.main(["x", "check", self.disk, self.feed("cmdok"), V]), 0)


if __name__ == "__main__":
    unittest.main()
