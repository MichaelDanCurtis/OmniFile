"""Catalog reads shared by `api` (the invoke reply) and the worker (the full card).

Every function takes an open psycopg connection in a transaction and reads with its own
dict-row cursor, so it works on `api`'s dict-row pool and on the worker's plain connections.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..catalog.read_set import readable_by_sql
from .cards import CardState

#: An open-ended keep_online hold (`until` NULL).
FOREVER = datetime(9999, 12, 31, tzinfo=UTC)

#: The policy body's `notify` object, when it is missing (teams.md section 3.1).
NOTIFY_DEFAULTS: dict[str, Any] = {
    "requester": True,
    "owners": False,
    "watchers_group_sid": None,
    "email_fallback": True,
}


async def one(conn: psycopg.AsyncConnection, sql: str, params: Any = None) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(sql, params)
    return await cur.fetchone()


async def rows(conn: psycopg.AsyncConnection, sql: str, params: Any = None) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(sql, params)
    return await cur.fetchall()


async def recall_row(conn: psycopg.AsyncConnection, recall_id: int) -> dict | None:
    return await one(
        conn,
        """
        SELECT r.recall_id, r.state, r.file_id, r.version_id, r.priority, r.requested_at,
               r.eta, r.updated_at, r.failure, r.superseded_reason,
               r.superseded_by_version_id, f.dataset_id, f.name, f.acl_id,
               coalesce(v.size, f.size) AS size, p.body AS policy
          FROM catalog.recall r
          JOIN catalog.file f ON f.file_id = r.file_id
          LEFT JOIN catalog.file_version v ON v.version_id = r.version_id
          LEFT JOIN catalog.policy p ON p.dataset_id = f.dataset_id AND p.state = 'active'
         WHERE r.recall_id = %s
        """,
        (recall_id,),
    )


def notify_settings(policy_body: Any) -> dict[str, Any]:
    out = dict(NOTIFY_DEFAULTS)
    raw = policy_body.get("notify") if isinstance(policy_body, dict) else None
    if isinstance(raw, dict):
        for k in NOTIFY_DEFAULTS:
            if k in raw:
                out[k] = raw[k]
    return out


async def share_and_path(
    conn: psycopg.AsyncConnection, dataset_id: int, file_id: int
) -> tuple[dict | None, str]:
    """The share that exports the file most specifically, and the path within it."""
    chain = await one(
        conn,
        """
        WITH RECURSIVE up AS (
            SELECT file_id, parent_id, name, 0 AS depth FROM catalog.file WHERE file_id = %s
            UNION ALL
            SELECT f.file_id, f.parent_id, f.name, up.depth + 1
              FROM catalog.file f JOIN up ON f.file_id = up.parent_id
             WHERE up.depth < 4096)
        SELECT array_agg(name ORDER BY depth DESC) AS names,
               array_agg(file_id ORDER BY depth DESC) AS ids
          FROM up
        """,
        (file_id,),
    )
    names: list[str] = list(chain["names"] or []) if chain else []
    ids: list[int] = list(chain["ids"] or []) if chain else []
    shares = await rows(
        conn,
        "SELECT share_id, name, root_dir_id, owners_group_sid FROM catalog.share"
        " WHERE dataset_id = %s ORDER BY share_id",
        (dataset_id,),
    )
    best: dict | None = None
    best_at = -1
    for s in shares:
        root = s["root_dir_id"]
        at = 0 if root is None else (ids.index(root) if root in ids else -1)
        if at > best_at:
            best, best_at = s, at
    start = max(best_at, 0) + 1  # skip the root ('') or the share's root directory
    return best, "/".join(names[start:])


#: Cached AD groups are trusted for this long: a card send refreshes older ones, and a
#: Teams click refreshes them instead of acting on them (teams.md section 4.3, step 8).
GROUPS_FRESH = timedelta(hours=1)


async def can_read(conn: psycopg.AsyncConnection, file_id: int, identity_id: int) -> bool:
    """decision 28: the recipient's SID and cached groups against the file's ACL, and for
    an annexed file the folders above it (decision 63). A file whose ACL the service
    doesn't know is not readable (fail closed)."""
    row = await one(
        conn,
        f"""
        SELECT {readable_by_sql("f.file_id", "a", "i.sid::text", "i.group_sids::text[]")} AS ok
          FROM catalog.file f
          JOIN catalog.identity i ON i.identity_id = %s
          LEFT JOIN catalog.acl a ON a.acl_id = f.acl_id
         WHERE f.file_id = %s
        """,
        (identity_id, file_id),
    )
    return bool(row and row["ok"])


async def keep_online_until(
    conn: psycopg.AsyncConnection, file_id: int, now: datetime
) -> datetime | None:
    row = await one(
        conn,
        "SELECT max(until) AS until, bool_or(until IS NULL) AS forever FROM catalog.hold"
        " WHERE file_id = %s AND kind = 'keep_online' AND released_at IS NULL"
        "   AND (until IS NULL OR until > %s)",
        (file_id, now),
    )
    if not row:
        return None
    if row["forever"]:
        return FOREVER
    return row["until"]


async def card_state(
    conn: psycopg.AsyncConnection,
    recall_id: int,
    identity_id: int,
    *,
    now: datetime,
    portal_url: str | None,
    tracking: bool = False,
    no_access: bool = False,
) -> CardState | None:
    r = await recall_row(conn, recall_id)
    if r is None:
        return None
    share, path = await share_and_path(conn, r["dataset_id"], r["file_id"])
    hold = await keep_online_until(conn, r["file_id"], now)
    reqs = await rows(
        conn,
        "SELECT proof, host(client_addr) AS addr FROM catalog.recall_requester"
        " WHERE recall_id = %s AND identity_id = %s",
        (recall_id, identity_id),
    )
    if not reqs:
        reqs = await rows(
            conn,
            "SELECT proof, host(client_addr) AS addr FROM catalog.recall_requester"
            " WHERE recall_id = %s",
            (recall_id,),
        )
    claimed = bool(reqs) and all(q["proof"] == "claimed" for q in reqs)
    addr = next((q["addr"] for q in reqs if q["proof"] == "claimed" and q["addr"]), None)
    cancelled_by_you = False
    if r["state"] == "cancelled":
        row = await one(
            conn,
            "SELECT actor FROM catalog.audit_log WHERE action = 'recall.cancel'"
            "   AND file_id = %s AND details->>'recall_id' = %s ORDER BY at DESC LIMIT 1",
            (r["file_id"], str(recall_id)),
        )
        cancelled_by_you = bool(row and row["actor"] == f"user:{identity_id}")
    superseded_by = None
    if r["state"] == "superseded" and not no_access:
        superseded_by = await who_superseded(conn, r)
    return CardState(
        recall_id=recall_id,
        state=r["state"],
        file_name=r["name"],
        share=share["name"] if share else None,
        path=path or r["name"],
        size=int(r["size"] or 0),
        priority=r["priority"],
        requested_at=r["requested_at"],
        eta=r["eta"],
        hold_until=hold,
        tracking=tracking,
        claimed=claimed,
        claimed_from=addr if claimed else None,
        cancelled_by_recipient=cancelled_by_you,
        no_access=no_access,
        portal_url=portal_url,
        failure=r["failure"] if r["state"] == "failed" else None,
        superseded_reason=r["superseded_reason"] if r["state"] == "superseded" else None,
        superseded_by=superseded_by,
    )


async def who_superseded(conn: psycopg.AsyncConnection, r: dict) -> str | None:
    """Who replaced or deleted the file a `superseded` recall was for, to tell someone
    who can read the file (the caller checks that: a recipient with `no_access` is told
    nothing). The display name of the version's actor, and only when the edge proved who
    it was: an NFS `sec=sys` uid is claimed by the client, and a card must not name a
    person on a claim. None when the catalog has no such name."""
    if r["superseded_by_version_id"] is None:
        return None
    row = await one(
        conn,
        "SELECT i.display_name FROM catalog.file_version v"
        " JOIN catalog.identity i ON i.identity_id = v.actor_identity_id"
        " WHERE v.version_id = %s AND v.file_id = %s AND v.actor_proof = 'proven'",
        (r["superseded_by_version_id"], r["file_id"]),
    )
    return row["display_name"] if row else None
