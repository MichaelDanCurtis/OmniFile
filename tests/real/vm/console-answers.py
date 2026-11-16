#!/usr/bin/env python3
"""A guide on=edge-console step: type the wizard's answers on the appliance's serial console.

    console-answers.py --domain NAME     (stdin: the answers, one per line, in order)

The guide's KVM import gives the appliance a serial console (`--console pty`); the wizard
(omnifile-setup) asks on it as on tty1 (VERIFY on the first run that it offers itself on
ttyS0; if it asks on tty1 only, this sends the answers with `virsh send-key` instead). Each
answer is sent once the console has printed a question (a line ending in ':' or '?') after
the previous one. The console's output is kept in /srv/ofreal/console-<domain>.log. Secret
answers are never echoed to that log by the wizard.
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import subprocess
import sys
import time

PROMPT = re.compile(rb"[:?]\s*$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    answers = sys.stdin.read().split("\n")
    if answers and answers[-1] == "":
        answers.pop()
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["virsh", "console", "--force", a.domain],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    log = open(f"/srv/ofreal/console-{a.domain}.log", "ab")
    buf, sent, deadline = b"", 0, time.time() + a.timeout
    try:
        while sent < len(answers) and time.time() < deadline:
            r, _, _ = select.select([master], [], [], 5)
            if r:
                chunk = os.read(master, 4096)
                log.write(chunk)
                buf = (buf + chunk)[-4096:]
            line = buf.rstrip(b"\r\n").rsplit(b"\n", 1)[-1]
            if PROMPT.search(line):
                os.write(master, answers[sent].encode() + b"\r")
                sent += 1
                buf = b""
        print(json.dumps({"ok": sent == len(answers), "answered": sent, "of": len(answers)}))
        return 0 if sent == len(answers) else 1
    finally:
        proc.terminate()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
