#!/usr/bin/env python3
"""The source offer a release ships must be the one written into its image.

build.sh renders the written offer into the image (/usr/share/doc/omnifile/GPL-SOURCE-OFFER.txt)
from the release, the archive snapshot and the sources repository, and records them in
OUT/BUILD-INFO. source-offer/source_offer.py writes the bundle's MANIFEST.json from its own
arguments. release.sh signs only when the two agree, so the offer inside a box points at the
bundle that is actually published:

  offer_binding.py check BUILD-INFO MANIFEST.json VERSION

It prints nothing and exits 0 when they match; otherwise it names every difference and exits 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def build_info(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), v.strip())
    return out


# build.sh's --arch (mkosi's names) against source_offer.py's --arch (Debian's).
DEBIAN_ARCH = {"x86-64": "amd64", "arm64": "arm64"}


def problems(info: dict[str, str], manifest: dict, version: str) -> list[str]:
    out: list[str] = []
    if manifest.get("complete") is not True:
        out.append("the source offer is not complete (MANIFEST.json complete is not true)")
    pairs = (
        ("release", version, manifest.get("release"), "the release"),
        ("version", info.get("version"), manifest.get("release"), "the image's version"),
        ("snapshot", info.get("snapshot"), manifest.get("snapshot"), "the archive snapshot"),
        ("sources_repo", info.get("sources_repo"), manifest.get("sources_repo"), "the sources repository"),
        (
            "architecture",
            DEBIAN_ARCH.get(info.get("architecture", ""), info.get("architecture")),
            manifest.get("architecture"),
            "the architecture",
        ),
    )
    for key, image, bundle, what in pairs:
        if not image:
            out.append(f"BUILD-INFO has no {key} (a build from before the offer was bound to it)")
        elif image != bundle:
            out.append(f"{what}: the image says {image!r}, the source offer {bundle!r}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] != "check":
        print(__doc__.strip().splitlines()[-3].strip(), file=sys.stderr)
        return 2
    info_path, manifest_path, version = Path(argv[2]), Path(argv[3]), argv[4]
    if not info_path.is_file():
        print(f"no {info_path}: the build's record of what the image's offer names", file=sys.stderr)
        return 1
    found = problems(build_info(info_path), json.loads(manifest_path.read_text()), version)
    for p in found:
        print(p, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
