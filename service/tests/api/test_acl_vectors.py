"""The POSIX.1e read decisions shared with the edge (decision 28).

tests/acl/vectors/acl-access.json is also checked by the edge
(omnifile-acl `snapshot::tests::shared_access_vectors`), so the service's
`access_sets` / `catalog.acl_can_read` and the edge's cross-check are proven
to produce the same read_sids, deny_sids and answers.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnifile_service.api.acls import access_sets, can_read

VECTORS = Path(__file__).resolve().parents[3] / "tests" / "acl" / "vectors" / "acl-access.json"


def test_shared_access_vectors(sql) -> None:
    cases = json.loads(VECTORS.read_text())["cases"]
    assert len(cases) >= 10
    for c in cases:
        read, deny = access_sets(c["owner_sid"], c["group_sid"], c["entries"])
        assert read == c["read_sids"], c["name"]
        assert deny == c["deny_sids"], c["name"]
        for k in c["checks"]:
            assert can_read(read, deny, k["user"], k["groups"]) is k["can_read"], (c["name"], k)
            (got,) = sql.execute(
                "SELECT catalog.acl_can_read(%s::catalog.sid[], %s::catalog.sid[], %s, %s)",
                (read, deny, k["user"], k["groups"]),
            ).fetchone()
            assert got is k["can_read"], (c["name"], k)
