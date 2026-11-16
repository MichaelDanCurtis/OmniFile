#!/usr/bin/env python3
"""A stand-in for `az` in the guide tools' tests (test_tools.py). Never calls Azure.

It logs each call's arguments, one JSON list per line, to $FAKE_AZ_LOG, and answers from
$FAKE_AZ_ANSWERS, a JSON object mapping a space-joined prefix of the arguments to
{"stdout": "...", "rc": 0}. The longest matching prefix wins; no match answers "" with rc 0.
"""

import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_AZ_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
answers = json.loads(os.environ.get("FAKE_AZ_ANSWERS", "{}"))
joined = " ".join(args)
best = None
for prefix in answers:
    if joined.startswith(prefix) and (best is None or len(prefix) > len(best)):
        best = prefix
if best is None:
    sys.exit(0)
a = answers[best]
sys.stdout.write(a.get("stdout", ""))
sys.exit(a.get("rc", 0))
