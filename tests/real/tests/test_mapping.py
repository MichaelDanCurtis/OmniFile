"""Every line of the three real-* checklists has an id, and every id is mapped to an
automated check, a step of an exit test, or a recorded manual step."""

from __future__ import annotations

import re

from harness import docs
from harness.checks import load_all
from harness.m6exit import STEPS as M6_STEPS

TENANT_EXIT = {"stub-read", "card-sent", "card-seen", "click-recall", "card-follows", "policy-cost"}


def test_every_checkbox_line_has_an_id_in_order():
    for prefix, doc in docs.DOCS.items():
        found, missing = docs.lines(doc)
        assert missing == [], f"{doc.name}: lines {missing} have no id"
        ids = [ln.id for ln in found]
        assert ids == [f"{prefix}-{i:02d}" for i in range(1, len(ids) + 1)], doc.name


def test_every_line_is_mapped_and_nothing_else_is():
    lines = docs.all_lines()
    reg = load_all()
    assert set(lines) - set(reg) == set(), "lines without a check"
    assert set(reg) - set(lines) == set(), "checks for lines no doc has"


def test_each_mapping_is_complete():
    m6 = {name for name, _, _ in M6_STEPS}
    for cid, c in load_all().items():
        assert c.cloud == {"RA": "azure", "RW": "aws", "RT": "tenant"}[cid[:2]]
        if c.kind == "auto":
            assert callable(c.fn), cid
        elif c.kind == "exit":
            step = c.exit_step
            if step.startswith("azure:"):
                assert step.split(":", 1)[1] in m6, cid
            else:
                assert step in (m6 if c.cloud == "azure" else TENANT_EXIT), (cid, step)
        else:
            assert c.kind == "manual" and c.steps and c.needs, cid


def test_the_counts():
    reg = load_all()
    lines = docs.all_lines()
    assert len(lines) == len(reg) == 145
    kinds = {}
    for c in reg.values():
        kinds[(c.cloud, c.kind)] = kinds.get((c.cloud, c.kind), 0) + 1
    assert sum(v for (cl, _), v in kinds.items() if cl == "azure") == 60
    assert sum(v for (cl, _), v in kinds.items() if cl == "aws") == 40
    assert sum(v for (cl, _), v in kinds.items() if cl == "tenant") == 45


def test_ticking_replaces_an_earlier_pass_note():
    text = "- [ ] RA-01 **x.** y.\n- [ ] RA-02 **z.**\n"
    once, done = docs.tick(text, {"RA-01": "rr-1: a.json"})
    assert done == ["RA-01"]
    assert once.startswith("- [x] RA-01 **x.** y. (passed in run rr-1: a.json)\n")
    twice, _ = docs.tick(once, {"RA-01": "rr-2: b.json"})
    assert twice.count("passed in run") == 1 and "rr-2" in twice
    assert re.search(r"^- \[ \] RA-02", twice, re.M)
