"""build.sh: the builder container gets every tool the mkosi step needs.

The container is a bare ubuntu:26.04, which has no CA store. mkosi.conf pins the archive
to https://snapshot.ubuntu.com (Snapshot=), so without ca-certificates apt in mkosi's
sandbox fails every TLS handshake and the build stops at "Unable to locate package
base-files" (the first container build of the annex image, WP14 A2.3). Nor does it have
systemd-hwdb, so mkosi wrote no hwdb.bin into /usr (the same build's smoke run).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE.parent / "build.sh"
MKOSI = HERE.parent / "mkosi.conf"


def tool_packages() -> set[str]:
    """The packages the TOOLS=1 step installs in the builder container."""
    text = BUILD.read_text()
    m = re.search(r"apt-get install -y -qq --no-install-recommends (.*?)>/dev/null", text, re.S)
    if not m:
        raise AssertionError("build.sh no longer installs its tools with apt-get")
    return set(m.group(1).replace("\\\n", " ").split())


class BuilderTools(unittest.TestCase):
    def test_mkosi_and_its_image_tools(self) -> None:
        need = {"mkosi", "systemd-repart", "systemd-ukify", "erofs-utils", "zstd", "patchelf"}
        self.assertLessEqual(need, tool_packages())

    def test_a_ca_store_for_the_https_snapshot(self) -> None:
        snap = re.search(r"^Snapshot=(\S+)", MKOSI.read_text(), re.M)
        self.assertIsNotNone(snap, "mkosi.conf pins no Snapshot=")
        self.assertIn("ca-certificates", tool_packages())

    def test_systemd_hwdb_for_the_images_hwdb_bin(self) -> None:
        # mkosi runs systemd-hwdb (Ubuntu's udev package) from the builder to write
        # /usr/lib/udev/hwdb.bin; without it the image has none, and its read-only /usr
        # can't get one at boot (smoke.sh: hwdb_in_usr).
        self.assertIn("udev", tool_packages())

    def test_build_info_records_the_builders_udev(self) -> None:
        # hwdb.bin is the builder's systemd-hwdb output, so the builder's udev is an input of /usr:
        # BUILD-INFO names its version, as it does the other tools' (review of #53).
        m = re.search(r"for p in ([^;]*); do\n\s*echo \"\$p=\$\(dpkg-query", BUILD.read_text())
        self.assertIsNotNone(m, "build.sh no longer records its tools' versions")
        self.assertIn("udev", m.group(1).split())


def mkosi_packages() -> list[str]:
    """mkosi.conf's Packages=, in order, comments left out."""
    out, inside = [], False
    for line in MKOSI.read_text().splitlines():
        if line.startswith("Packages="):
            inside = True
            continue
        if inside:
            if line and not line[0].isspace():
                break
            word = line.strip()
            if word and not word.startswith("#"):
                out.append(word)
    return out


class ImagePackages(unittest.TestCase):
    def test_each_package_is_named_once(self) -> None:
        names = mkosi_packages()
        self.assertIn("cifs-utils", names)
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], "named twice in mkosi.conf's Packages=")


if __name__ == "__main__":
    unittest.main()
