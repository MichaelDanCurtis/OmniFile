"""The real-* checklists: every checkbox line carries an id (RA-nn, RW-nn, RT-nn)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .core import REPO

DOCS = {
    "RA": REPO / "docs/milestones/real-azure-checks.md",
    "RW": REPO / "docs/milestones/real-aws-checks.md",
    "RT": REPO / "docs/milestones/real-tenant-checks.md",
}
CHECKBOX = re.compile(r"^- \[( |x)\] ")
LINE = re.compile(r"^- \[(?P<box> |x)\] (?P<id>R[AWT]-\d{2}) (?P<rest>.*)$")
PASSED = re.compile(r" \(passed in run [^)]*\)$")


@dataclass
class Line:
    id: str
    doc: Path
    lineno: int
    ticked: bool
    text: str


def lines(doc: Path) -> tuple[list[Line], list[int]]:
    """(the id'd checkbox lines, the line numbers of checkbox lines with no id)."""
    found, missing = [], []
    for n, raw in enumerate(doc.read_text().splitlines(), 1):
        if not CHECKBOX.match(raw):
            continue
        m = LINE.match(raw)
        if not m:
            missing.append(n)
            continue
        found.append(Line(m.group("id"), doc, n, m.group("box") == "x", m.group("rest")))
    return found, missing


def all_lines() -> dict[str, Line]:
    out: dict[str, Line] = {}
    for prefix, doc in DOCS.items():
        found, _ = lines(doc)
        for ln in found:
            if not ln.id.startswith(prefix):
                raise ValueError(f"{doc.name}:{ln.lineno}: {ln.id} in the wrong doc")
            if ln.id in out:
                raise ValueError(f"{ln.id} twice")
            out[ln.id] = ln
    return out


def tick(text: str, ids: dict[str, str]) -> tuple[str, list[str]]:
    """Tick each id in IDS ({id: note}) in TEXT, replacing an earlier pass note."""
    out, done = [], []
    for raw in text.splitlines():
        m = LINE.match(raw)
        if m and m.group("id") in ids:
            rest = PASSED.sub("", m.group("rest"))
            raw = f"- [x] {m.group('id')} {rest} (passed in run {ids[m.group('id')]})"
            done.append(m.group("id"))
        out.append(raw)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), done
