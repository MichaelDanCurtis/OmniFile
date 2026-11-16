"""The NAS files of tests/e2e/annex_a1_test.sh: six present at the start
(two SEG-Y surveys, documents and a binary), and a large SEG-Y file for the
kill-mid-copy step. SEG-Y files are whole traces, so they pass the edge's
structure check (edge/crates/omnifile-edge/src/annex/segy.rs)."""

from __future__ import annotations

import os
from pathlib import Path


def segy(traces: int, samples: int, seed: int) -> bytes:
    """Rev 1 SEG-Y: textual and binary headers, then `traces` traces of
    `samples` IEEE floats (format 5)."""
    text = b"C 1 CLIENT OmniFile annex A1 exit test".ljust(3200, b" ")
    binh = bytearray(400)
    binh[20:22] = samples.to_bytes(2, "big")
    binh[24:26] = (5).to_bytes(2, "big")
    trace = 240 + samples * 4
    body = bytes((seed + i) % 251 for i in range(trace))
    return text + bytes(binh) + body * traces


def write_all(root: str) -> None:
    r = Path(root)
    files = {
        "surveys/2026/raw/line_1042.segy": segy(300, 500, 1),
        "surveys/2026/raw/line_1043.segy": segy(120, 750, 2),
        "surveys/2026/index.csv": b"line,traces\n1042,300\n1043,120\n",
        "docs/notes.txt": b"Field notes for the 2026 survey.\n",
        "docs/readme.md": b"# Long-term storage\n\nCopied to Azure by an annex.\n",
        "top.bin": os.urandom(3 << 20),
    }
    for rel, data in files.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def big(path: str, mib: int) -> None:
    """A large SEG-Y file, written whole (then quiet)."""
    samples = 1000
    trace = 240 + samples * 4
    traces = (mib << 20) // trace
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    with open(tmp, "wb") as f:
        f.write(segy(0, samples, 3))
        chunk = bytes((7 + i) % 251 for i in range(trace)) * 1024
        left = traces
        while left > 0:
            n = min(left, 1024)
            f.write(chunk[: n * trace])
            left -= n
    os.chmod(tmp, 0o644)
    os.rename(tmp, p)
