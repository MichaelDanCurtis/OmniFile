"""Test people get stable, distinct SIDs whatever PYTHONHASHSEED is.

`person()` used to derive a SID from `hash(key)`. Python randomises string
hashes per process, so two people in one test could share a SID, and the
second `person()` then updated the first one's row (and kept its display name).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .support import rid_for

KEYS = ["nora", "stale", "sasha", "ivan", "mia", "owner", "reader", "writer", "admin", "bob"]
SEEDS = ["1314", "1109", "237", "0", "7"]


def test_a_rid_is_the_same_for_one_person_and_never_shared() -> None:
    rid_of = rid_for
    domain = "S-1-5-21-1-2-3"
    rids = {k: rid_of(domain, k) for k in KEYS}
    assert len(set(rids.values())) == len(KEYS)
    assert rids == {k: rid_of(domain, k) for k in KEYS}


@pytest.mark.parametrize("seed", SEEDS)
def test_the_owner_test_passes_under_any_hash_seed(seed: str) -> None:
    """Seeds 1314 and 1109 gave two people one SID before the fix."""
    if not os.environ.get("OMNIFILE_TEST_DATABASE_URL"):
        pytest.skip("OMNIFILE_TEST_DATABASE_URL is not set")
    node = (
        Path(__file__).parent
        / "test_clients.py::test_a_proposal_is_refused_when_its_owner_cannot_see_the_dataset"
    )
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(node)],
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-500:]
