"""docs/contracts/control-api.md and service/openapi/control-api.yaml must agree."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "contracts" / "control-api.md"
SPEC = ROOT / "service" / "openapi" / "control-api.yaml"


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/"))


def _spec_operations() -> set[tuple[str, str]]:
    ops: set[tuple[str, str]] = set()
    current = None
    for line in SPEC.read_text().splitlines():
        if m := re.match(r"^  (/(?:v0|internal)/\S+):$", line):
            current = _norm(m[1])
        elif current and (m := re.match(r"^    (get|post|put|patch|delete):$", line)):
            ops.add((m[1].upper(), current))
    return ops


def _doc_operations() -> set[tuple[str, str]]:
    text = DOC.read_text()
    return {
        (m[1], _norm(m[2]))
        for m in re.finditer(r"`(GET|POST|PUT|PATCH|DELETE) (/(?:v0|internal)/[^\s`?]+)", text)
    }


def test_doc_and_spec_list_the_same_endpoints() -> None:
    doc, spec = _doc_operations(), _spec_operations()
    assert not doc - spec, f"in control-api.md only: {sorted(doc - spec)}"
    assert not spec - doc, f"in control-api.yaml only: {sorted(spec - doc)}"


def test_the_spec_has_the_core_endpoints() -> None:
    ops = _spec_operations()
    for op in [
        ("POST", "/v0/enrol"),
        ("GET", "/v0/edges/{}/feed"),
        ("POST", "/v0/edges/{}/changes"),
        ("POST", "/v0/edges/{}/uploads"),
        ("POST", "/v0/edges/{}/credentials"),
        ("POST", "/v0/edges/{}/recalls"),
        ("GET", "/v0/edges/{}/asof"),
        ("GET", "/v0/edges/{}/digest"),
    ]:
        assert op in ops, op
