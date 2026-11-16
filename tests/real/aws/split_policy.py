#!/usr/bin/env python3
"""Split harness-policy.json into the managed policies the owner attaches.

IAM allows 2,048 characters of inline policy on a user and 6,144 in one managed policy
(whitespace not counted). harness-policy.json is about 8,500, so it is attached as managed
policies: harness-policy-part<N>.json, the same statements in the same order. A Deny in one
policy applies whichever policy allows, so the parts together decide as the one file does.

    python3 tests/real/aws/split_policy.py          # rewrite the parts
    python3 tests/real/aws/split_policy.py --check  # exit 1 if the parts are stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "harness-policy.json"
#: IAM's limit for one managed policy, whitespace not counted.
LIMIT = 6144
#: What a part may reach: room to add a short statement without a new part.
ROOM = 5900


def size(doc: dict) -> int:
    return len(json.dumps(doc, separators=(",", ":")))


def parts(policy: dict) -> list[dict]:
    out: list[dict] = []
    for st in policy["Statement"]:
        if out and size({**out[-1], "Statement": [*out[-1]["Statement"], st]}) <= ROOM:
            out[-1]["Statement"].append(st)
        else:
            out.append({"Version": policy["Version"], "Statement": [st]})
    for p in out:
        if size(p) > LIMIT:
            raise SystemExit(f"one statement alone is over {LIMIT} characters")
    return out


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


def path(n: int) -> Path:
    return HERE / f"harness-policy-part{n}.json"


def main(argv: list[str]) -> int:
    want = [render(p) for p in parts(json.loads(SOURCE.read_text()))]
    have = sorted(HERE.glob("harness-policy-part*.json"))
    if "--check" in argv:
        ok = [p.read_text() for p in have] == want and have == [
            path(n) for n in range(1, len(want) + 1)
        ]
        if not ok:
            print("the parts are stale: run tests/real/aws/split_policy.py", file=sys.stderr)
        return 0 if ok else 1
    for p in have:
        p.unlink()
    for n, text in enumerate(want, 1):
        path(n).write_text(text)
        print(f"{path(n).name}: {size(json.loads(text))} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
