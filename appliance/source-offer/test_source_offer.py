"""Offline tests of the source-offer tool's parsing: python3 -m unittest appliance/source-offer/test_source_offer.py"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_offer as so  # noqa: E402


class Inputs(unittest.TestCase):
    def write(self, text: str) -> Path:
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return Path(f.name)

    def test_a_mkosi_json_manifest(self) -> None:
        manifest = {
            "manifest_version": 1,
            "config": {"name": "omnifile-edge", "version": "1.2.0", "architecture": "x86-64"},
            "packages": [
                {
                    "type": "deb",
                    "name": "samba",
                    "version": "2:4.23.6+dfsg-1ubuntu1",
                    "architecture": "amd64",
                    "size": 1,
                },
                {
                    "type": "rpm",
                    "name": "ignored",
                    "version": "1",
                    "architecture": "x86_64",
                    "size": 1,
                },
            ],
        }
        got = so.read_packages(self.write(json.dumps(manifest)))
        self.assertEqual(got, [so.Binary("samba", "2:4.23.6+dfsg-1ubuntu1", "amd64")])

    def test_text_lines_and_dpkg_query_output(self) -> None:
        got = so.read_packages(
            self.write("# a comment\nlibc6:amd64 2.42-0ubuntu3\nbash 5.3-1 amd64\n\n")
        )
        self.assertEqual(
            got, [so.Binary("libc6", "2.42-0ubuntu3", "amd64"), so.Binary("bash", "5.3-1", "amd64")]
        )

    def test_a_line_without_a_version_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            so.read_packages(self.write("samba\n"))

    def test_the_packages_of_the_image_config(self) -> None:
        names = so.config_packages(Path(__file__).resolve().parent.parent / "mkosi.conf")
        for n in (
            "linux-image-generic",
            "samba",
            "samba-vfs-modules",
            "winbind",
            "tpm2-tools",
            "libuuid1",
        ):
            self.assertIn(n, names)
        self.assertNotIn("/usr/share/doc", names)  # RemoveFiles= is not a package list
        for n in ("nfs-common", "nftables", "libacl1", "liburcu8t64"):
            self.assertIn(n, names)  # after a comment line in the list

    def test_comments_at_any_column_do_not_end_the_list(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d) / "mkosi.conf"
            conf.write_text(
                "[Content]\nPackages=\n        a\n# a comment at column 0\n        b\n"
                "    # an indented comment\n        c\nRemoveFiles=/x\n"
            )
            self.assertEqual(so.config_packages(conf), ["a", "b", "c"])


class SourceField(unittest.TestCase):
    def test_source_with_and_without_its_own_version(self) -> None:
        recs = so.parse_records(
            "Package: libsmbclient0\nVersion: 2:4.23.6+dfsg-1ubuntu1\nSource: samba\nDescription: x\n continued\n\n"
            "Package: linux-image-7.0.0-12-generic\nVersion: 7.0.0-12.12\nSource: linux-signed (7.0.0-12.12)\n\n"
            "Package: bash\nVersion: 5.3-1\n"
        )
        self.assertEqual(
            [so.source_of(r) for r in recs],
            [
                ("samba", "2:4.23.6+dfsg-1ubuntu1"),
                ("linux-signed", "7.0.0-12.12"),
                ("bash", "5.3-1"),
            ],
        )

    def test_a_binnmu_keeps_the_source_version(self) -> None:
        (rec,) = so.parse_records("Package: libfoo1\nVersion: 1.0-1build1\nSource: foo (1.0-1)\n")
        self.assertEqual(so.source_of(rec), ("foo", "1.0-1"))


def newc(entries: list[tuple[str, int, bytes]]) -> bytes:
    """A newc cpio archive of (name, mode, data), with its trailer."""
    out = b""
    for i, (name, mode, data) in enumerate([*entries, ("TRAILER!!!", 0, b"")]):
        n = name.encode() + b"\0"
        fields = [i + 1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(n), 0]
        hdr = b"070701" + b"".join(b"%08X" % f for f in fields) + n
        out += hdr + b"\0" * (-len(hdr) % 4) + data + b"\0" * (-len(data) % 4)
    return out


class Initrd(unittest.TestCase):
    def test_regular_files_of_concatenated_archives(self) -> None:
        first = newc([("kernel/x86/microcode/GenuineIntel.bin", 0o100644, b"ucode")])
        main = newc([
            (".", 0o40755, b""),
            ("usr/bin/bash", 0o100755, b"\x7fELF..."),
            ("bin", 0o120777, b"usr/bin"),
            ("usr/lib/os-release", 0o100644, b"ID=ubuntu\n"),
        ])
        got = so.cpio_regular_files(first + b"\0" * 512 + main)
        self.assertEqual(got, ["kernel/x86/microcode/GenuineIntel.bin", "usr/bin/bash", "usr/lib/os-release"])

    def test_not_a_cpio_archive_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            so.cpio_regular_files(b"PK\x03\x04 a zip, not an initrd" + b"x" * 200)

    def test_build_info_and_the_initrd_package_list(self) -> None:
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write("version=0.1.0\nsystemd-boot-efi=259.9-0ubuntu0.1\n")
        f.close()
        self.addCleanup(Path(f.name).unlink)
        self.assertEqual(so.build_info(Path(f.name))["systemd-boot-efi"], "259.9-0ubuntu0.1")
        for p in ("systemd", "udev", "btrfs-progs", "util-linux", "dmsetup"):
            self.assertIn(p, so.MKOSI_INITRD_PACKAGES)


class Pins(unittest.TestCase):
    def test_the_samba_and_ganesha_pins_are_read(self) -> None:
        ver, digest = so.pinned_samba()
        self.assertRegex(ver, r"^\d+\.\d+\.\d+$")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        pins = so.ganesha_pins()
        self.assertRegex(pins["GANESHA_COMMIT"], r"^[0-9a-f]{40}$")
        self.assertRegex(pins["NTIRPC_COMMIT"], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
