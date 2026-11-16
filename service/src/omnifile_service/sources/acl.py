"""A source's AD groups as the ACLs of its folders and files (access-control.md
section 5: S3 objects carry no ACLs and must never be world-readable).

Stored like edge ACLs (`api.acls`): content-addressed per dataset, with
`read_sids` / `deny_sids` derived by the service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import psycopg

from ..api.acls import access_sets, canonical_json, entry_canonical

DIR_MODE = 0o770
FILE_MODE = 0o660


@dataclass(frozen=True)
class Groups:
    owners: str
    writers: str | None = None
    readers: str | None = None


def _entries(g: Groups, *, folder: bool) -> list[dict[str, Any]]:
    full, read = ("rwx", "r-x") if folder else ("rw-", "r--")
    access = [
        {"scope": "access", "tag": "owner", "perms": full},
        {"scope": "access", "tag": "owning_group", "perms": full},
        {"scope": "access", "tag": "mask", "perms": full},
        {"scope": "access", "tag": "other", "perms": "---"},
    ]
    if g.writers:
        access.append({"scope": "access", "tag": "group", "sid": g.writers, "perms": full})
    if g.readers:
        access.append({"scope": "access", "tag": "group", "sid": g.readers, "perms": read})
    entries = access + ([{**e, "scope": "default"} for e in access] if folder else [])
    by_text = {entry_canonical(e): e for e in entries}
    return [by_text[k] for k in sorted(by_text)]


def acl_body(g: Groups, *, folder: bool) -> dict[str, Any]:
    entries = _entries(g, folder=folder)
    mode = DIR_MODE if folder else FILE_MODE
    sha = hashlib.sha256(canonical_json(None, g.owners, mode, entries).encode()).digest()
    read, deny = access_sets(None, g.owners, entries)
    return {"sha256": sha, "group_sid": g.owners, "mode": mode, "entries": entries,
            "read": read, "deny": deny}  # fmt: skip


async def ensure_acl(conn: psycopg.AsyncConnection, dataset_id: int, body: dict[str, Any]) -> int:
    cur = await conn.execute(
        """
        INSERT INTO catalog.acl
            (dataset_id, sha256, owner_sid, group_sid, mode, entries, read_sids, deny_sids)
        VALUES (%s, %s, NULL, %s, %s, %s::jsonb, %s::catalog.sid[], %s::catalog.sid[])
        ON CONFLICT (dataset_id, sha256) DO NOTHING
        RETURNING acl_id
        """,
        (dataset_id, body["sha256"], body["group_sid"], body["mode"],
         json.dumps(body["entries"]), body["read"], body["deny"]),
    )  # fmt: skip
    row = await cur.fetchone()
    if row is None:
        cur = await conn.execute(
            "SELECT acl_id FROM catalog.acl WHERE dataset_id = %s AND sha256 = %s",
            (dataset_id, body["sha256"]),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def source_acls(conn: psycopg.AsyncConnection, dataset_id: int, g: Groups) -> tuple[int, int]:
    """(folder acl_id, file acl_id)."""
    return (
        await ensure_acl(conn, dataset_id, acl_body(g, folder=True)),
        await ensure_acl(conn, dataset_id, acl_body(g, folder=False)),
    )
