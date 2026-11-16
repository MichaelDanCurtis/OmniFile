"""Every line of docs/milestones/real-{azure,aws,tenant}-checks.md, by id, and how it is
proven: an automated check function, a step of the exit test, or a recorded manual step.

    RA-nn  real-azure-checks.md
    RW-nn  real-aws-checks.md
    RT-nn  real-tenant-checks.md

tests/real/tests/test_mapping.py fails if a line of those docs has no id, an id has no entry
here, or an entry names an id no doc has.

Statuses a check ends with:
    pass     proven on this run; collect.py ticks the line
    fail     disproven, or the check broke; the run exits 1
    partial  part proven (for example the immediate answer of a restore that takes hours);
             not ticked, the evidence says what is left
    manual   a person does it (manual steps below); ticked only by `collect.py --record`
    pending  depends on something this run didn't have (another cloud's run, a build)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

STATUSES = ("pass", "fail", "partial", "manual", "pending")


class CheckFailed(Exception):
    pass


class Partial(Exception):
    """Raised by a check that proved part of its line; the message says what is left."""

    def __init__(self, left: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(left)
        self.evidence = evidence or {}


class Pending(Exception):
    pass


@dataclass
class Check:
    id: str
    cloud: str
    title: str
    kind: str  # auto | exit | manual
    fn: Callable[..., dict[str, Any]] | None = None
    exit_step: str | None = None
    steps: list[str] = field(default_factory=list)
    needs: str = ""


REGISTRY: dict[str, Check] = {}


def _cloud(cid: str) -> str:
    return {"RA": "azure", "RW": "aws", "RT": "tenant"}[cid[:2]]


def _add(c: Check) -> Check:
    if c.id in REGISTRY:
        raise ValueError(f"{c.id} registered twice")
    REGISTRY[c.id] = c
    return c


def auto(cid: str, title: str):
    """An automated check: fn(env) -> evidence dict, or raises CheckFailed / Partial / Pending."""

    def deco(fn):
        _add(Check(cid, _cloud(cid), title, "auto", fn=fn))
        return fn

    return deco


def by_exit(cid: str, title: str, step: str) -> None:
    """Proven by a step of the exit test (its status and evidence are the line's)."""
    _add(Check(cid, _cloud(cid), title, "exit", exit_step=step))


def manual(cid: str, title: str, needs: str, steps: list[str]) -> None:
    """A recorded manual step: what the owner does, and what to keep as evidence."""
    if not steps:
        raise ValueError(f"{cid}: a manual check needs its steps")
    _add(Check(cid, _cloud(cid), title, "manual", steps=steps, needs=needs))


def load_all() -> dict[str, Check]:
    from . import aws, azure, tenant  # noqa: F401  (registers)

    return REGISTRY
