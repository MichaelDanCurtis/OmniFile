"""Helpers for the API client tests."""

from __future__ import annotations

import itertools
from typing import Any

from .conftest import seed

GOOD = {
    "mode": "scheduled",
    "clock": "last_read",
    "warm_day": 30,
    "cold_day": 120,
    "archive_day": 365,
    "stub_at": "archive",
    "resident_mb": 4,
    "restub_after_hours": 24,
    "notify": {
        "requester": True,
        "owners": False,
        "watchers_group_sid": None,
        "email_fallback": True,
    },
}


_RIDS: dict[tuple[str, str], int] = {}
_RID_COUNTER = itertools.count(4000)


def rid_for(domain: str, key: str) -> int:
    """A RID for ``key`` in ``domain``: the same on every call, never shared by two keys.

    A counter, not ``hash(key)``: Python randomises string hashes per process, and
    a hash modulo a small range can give two people one SID.
    """
    return _RIDS.setdefault((domain, key), next(_RID_COUNTER))


def person(
    sql, w, key: str, *, roles: str = "{}", seen: str = "now()", groups: list[str] | None = None
) -> int:
    """A person in the tenant the catalog knows (as if they had signed in)."""
    sid = f"{w.domain}-{rid_for(w.domain, key)}"
    (iid,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, display_name, upn, app_roles,"
        f" app_roles_seen_at, group_sids) VALUES (%s, 'user', %s, %s, %s, {seen}, %s)"
        " ON CONFLICT (sid) DO UPDATE SET app_roles = EXCLUDED.app_roles,"
        " app_roles_seen_at = EXCLUDED.app_roles_seen_at, group_sids = EXCLUDED.group_sids"
        " RETURNING identity_id",
        (sid, key.title(), f"{key}@contoso.test", roles, groups or []),
    ).fetchone()
    return int(iid)


def register(admin, app_id: str, owner: int, scopes: list[str], name: str | None = None):
    r = admin.post(
        "/v1/settings/api-clients",
        {
            "app_id": app_id,
            "display_name": name or f"Client {app_id}",
            "scopes": scopes,
            "owner_identity_id": str(owner),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def world(web, sql):
    w = seed(sql)
    return w, web.browser(w.admin)


def propose(web, w, token: str) -> Any:
    return web.call(
        "POST",
        "/v1/policies/proposals",
        token=token,
        json={"dataset_id": str(w.dataset_id), "body": GOOD, "rationale": "from the API"},
    )
