"""`World.user` gives each person one RID, the same on every call and never shared.

It used `hash(key)`, which Python randomises per process (see
apiclients/test_stable_sids.py).
"""

from __future__ import annotations

from .conftest import _rid_for

KEYS = ["nora", "stale", "sasha", "ivan", "mia", "bob", "carol", "dave", "erin", "frank"]


def test_a_rid_is_the_same_for_one_person_and_never_shared() -> None:
    domain = "S-1-5-21-9-8-7"
    rids = {k: _rid_for(domain, k) for k in KEYS}
    assert len(set(rids.values())) == len(KEYS)
    assert rids == {k: _rid_for(domain, k) for k in KEYS}
