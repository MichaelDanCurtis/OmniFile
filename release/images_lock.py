#!/usr/bin/env python3
"""Check that an images lock (deploy/images/images.lock.json) is this commit's build.

    release/images_lock.py LOCK COMMIT

Exits 1, naming each image, when an image's source_commit isn't COMMIT: built at another commit, or from a dirty
tree (deploy/images/build.sh then writes "<commit>+dirty"). A release is built from one commit, so its images must
be too (release/runbook.sh, step 2).
"""

from __future__ import annotations

import json
import sys


def stale(lock: dict, commit: str) -> dict[str, str]:
    images = lock.get("images") or {}
    if not images:
        return {"(none)": "the lock lists no images"}
    return {n: str(i.get("source_commit")) for n, i in sorted(images.items()) if i.get("source_commit") != commit}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    bad = stale(json.load(open(argv[0])), argv[1])
    for n, c in bad.items():
        print(f"images_lock: {n} was built from {c}, not {argv[1]}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
