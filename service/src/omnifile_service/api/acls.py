"""ACL snapshots and scans from the edge: control-api.md section 4.6 (decision 28,
access-control.md section 6).

The catalog keeps each distinct ACL once per dataset (`catalog.acl`,
content-addressed by `sha256`), as SIDs. Every file and folder row points at
one (`file.acl_id`, set by the `changes` ops `acl` / `attrs.acl_sha256`), so
a folder's snapshot and any file whose ACL differs from it are both just
pointers. The edge's `omnifile-acl` crate produces the bodies
(`snapshot::AclsRequest`).

- `POST …/acls`: stores the ACLs the service doesn't know yet and returns
  `{acl_ids: {sha256: acl_id}}`. The service recomputes each `sha256` over
  the canonical form, so an edge can't file one body under another's hash,
  and derives `read_sids` / `deny_sids` itself (never from the edge), in
  POSIX.1e access-check order (`access_sets`, `catalog.acl_can_read`).
- `POST …/acl-scans` and `…/acl-scans/{scan_id}/complete` bracket a nightly
  full scan or an on-change batch (`catalog.acl_scan`).

An edge may only touch datasets bound to it (one edge per dataset, v1).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .context import Ctx
from .db import id_str
from .idempotency import Result
from .problems import ApiError
from .validate import bad, req, req_enum, req_id, req_int, sid

MAX_ACLS = 1000
MAX_ENTRIES = 1000
SCOPES = ("access", "default")
TAGS = ("owner", "owning_group", "user", "group", "mask", "other")
EVERYONE = "S-1-1-0"
_PERMS = re.compile(r"^[r-][w-][x-]$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def entry_canonical(e: dict[str, Any]) -> str:
    """One entry as `omnifile-acl` writes it (keys sorted, no whitespace)."""
    if e.get("sid") is not None:
        return (
            f'{{"perms":"{e["perms"]}","scope":"{e["scope"]}",'
            f'"sid":"{e["sid"]}","tag":"{e["tag"]}"}}'
        )
    return f'{{"perms":"{e["perms"]}","scope":"{e["scope"]}","tag":"{e["tag"]}"}}'


def canonical_json(owner: str | None, group: str | None, mode: int, entries: list[dict]) -> str:
    """`AclSnapshot::canonical_json` (edge/crates/omnifile-acl/src/snapshot.rs):
    entries sorted by their canonical text, then group_sid, mode, owner_sid,
    absent SIDs omitted."""
    s = '{"entries":[' + ",".join(entry_canonical(e) for e in entries) + "]"
    if group is not None:
        s += f',"group_sid":"{group}"'
    s += f',"mode":{mode}'
    if owner is not None:
        s += f',"owner_sid":"{owner}"'
    return s + "}"


def _perm_bits(p: str) -> int:
    return (4 if p[0] == "r" else 0) | (2 if p[1] == "w" else 0) | (1 if p[2] == "x" else 0)


def access_sets(
    owner: str | None, group: str | None, entries: list[dict], need: int = 4
) -> tuple[list[str], list[str]]:
    """(read_sids, deny_sids) under POSIX.1e access-check order (migration
    0008): the owner entry decides for the owner; a named-user entry decides
    for that user, even when it denies; else any matching group entry
    (owning group or named group) granting r grants, and a matching one that
    doesn't denies; else `other` (Everyone, S-1-1-0). The mask limits named
    users and every group entry, never the owner or `other`.
    `catalog.acl_can_read` evaluates the two sets for a user. `need` is the
    permission bits that must all be granted: 4 (r) for reads, 3 (w and x)
    for adding a file to a folder (`catalog.acl_can_write`)."""
    access = [e for e in entries if e["scope"] == "access"]
    mask = next((_perm_bits(e["perms"]) for e in access if e["tag"] == "mask"), None)

    def reads(e: dict, masked: bool) -> bool:
        bits = _perm_bits(e["perms"])
        if masked and mask is not None:
            bits &= mask
        return bits & need == need

    users: dict[str, bool] = {}  # step 1-2 decisions, owner first
    for e in access:
        if e["tag"] == "owner" and owner is not None:
            users[owner] = reads(e, masked=False)
    for e in access:
        if e["tag"] == "user" and e["sid"] not in users:
            users[e["sid"]] = reads(e, masked=True)
    grant_groups: set[str] = set()
    deny_groups: set[str] = set()
    for e in access:
        who = group if e["tag"] == "owning_group" else e.get("sid") if e["tag"] == "group" else None
        if who is None:
            continue
        (grant_groups if reads(e, masked=True) else deny_groups).add(who)
    other = any(reads(e, masked=False) for e in access if e["tag"] == "other")
    read = {u for u, ok in users.items() if ok} | grant_groups | ({EVERYONE} if other else set())
    deny = {u for u, ok in users.items() if not ok} | (deny_groups - grant_groups)
    return sorted(read), sorted(deny)


def read_sids(owner: str | None, group: str | None, entries: list[dict]) -> list[str]:
    """The `read_sids` prefilter (see `access_sets`)."""
    return access_sets(owner, group, entries)[0]


def can_read(read: list[str], deny: list[str], user: str, groups: list[str]) -> bool:
    """`catalog.acl_can_read` in Python, for callers outside SQL and tests."""
    if user in deny:
        return False
    if user in read:
        return True
    if set(groups) & (set(read) - {EVERYONE}):
        return True
    if set(groups) & set(deny):
        return False
    return EVERYONE in read


def parse_acl(value: Any, i: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad(f"acls[{i}] must be an object")
    sha = req(value, "sha256")
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise bad(f"acls[{i}].sha256 must be 64 lowercase hex digits")
    owner = sid(value["owner_sid"], f"acls[{i}].owner_sid") if value.get("owner_sid") else None
    group = sid(value["group_sid"], f"acls[{i}].group_sid") if value.get("group_sid") else None
    mode = req_int(value, "mode", minimum=0)
    if mode > 0o7777:
        raise bad(f"acls[{i}].mode must be at most 4095")
    raw = req(value, "entries")
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
        raise bad(f"acls[{i}].entries must be a list of at most {MAX_ENTRIES}")
    entries: dict[str, dict[str, Any]] = {}
    for j, e in enumerate(raw):
        where = f"acls[{i}].entries[{j}]"
        if not isinstance(e, dict):
            raise bad(f"{where} must be an object")
        scope = req_enum(e, "scope", SCOPES)
        tag = req_enum(e, "tag", TAGS)
        perms = req(e, "perms")
        if not isinstance(perms, str) or not _PERMS.match(perms):
            raise bad(f"{where}.perms must look like rwx, r-x or ---")
        named = tag in ("user", "group")
        esid = e.get("sid")
        if named != (esid is not None):
            raise bad(f"{where}: user and group entries need a sid, the others must not have one")
        out = {"scope": scope, "tag": tag, "perms": perms}
        if esid is not None:
            out["sid"] = sid(esid, f"{where}.sid")
        entries[entry_canonical(out)] = out
    ordered = [entries[k] for k in sorted(entries)]
    computed = hashlib.sha256(canonical_json(owner, group, mode, ordered).encode()).hexdigest()
    if computed != sha:
        raise bad(f"acls[{i}].sha256 is not the hash of its canonical form ({computed})")
    return {
        "sha256": bytes.fromhex(sha),
        "owner_sid": owner,
        "group_sid": group,
        "mode": mode,
        "entries": ordered,
        "access": access_sets(owner, group, ordered),
    }


async def own_dataset(db, edge_id: int, dataset_id: int) -> None:
    row = await (
        await db.execute(
            "SELECT 1 FROM catalog.dataset WHERE dataset_id = %s AND edge_id = %s",
            (dataset_id, edge_id),
        )
    ).fetchone()
    if row is None:
        raise ApiError("not_found", f"dataset {dataset_id} is not on this edge")


async def put_acls(ctx: Ctx) -> Result:
    dataset_id = req_id(ctx.body, "dataset_id")
    raw = req(ctx.body, "acls")
    if not isinstance(raw, list) or len(raw) > MAX_ACLS:
        raise bad(f"acls must be a list of at most {MAX_ACLS}")
    acls = [parse_acl(a, i) for i, a in enumerate(raw)]
    ids: dict[str, str] = {}
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        await own_dataset(db, ctx.edge_id, dataset_id)
        for a in acls:
            # Content-addressed: a known hash keeps its row (the body is
            # identical by construction, since the hash was checked).
            row = await (
                await db.execute(
                    """
                    INSERT INTO catalog.acl
                        (dataset_id, sha256, owner_sid, group_sid, mode, entries, read_sids,
                         deny_sids)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::catalog.sid[],
                            %s::catalog.sid[])
                    ON CONFLICT (dataset_id, sha256) DO NOTHING
                    RETURNING acl_id
                    """,
                    (
                        dataset_id,
                        a["sha256"],
                        a["owner_sid"],
                        a["group_sid"],
                        a["mode"],
                        json.dumps(a["entries"]),
                        a["access"][0],
                        a["access"][1],
                    ),
                )
            ).fetchone()
            if row is None:
                row = await (
                    await db.execute(
                        "SELECT acl_id FROM catalog.acl WHERE dataset_id = %s AND sha256 = %s",
                        (dataset_id, a["sha256"]),
                    )
                ).fetchone()
            ids[a["sha256"].hex()] = id_str(row["acl_id"])
    return Result(200, {"acl_ids": ids})


async def start_scan(ctx: Ctx) -> Result:
    dataset_id = req_id(ctx.body, "dataset_id")
    kind = req_enum(ctx.body, "kind", ("full", "on_change"))
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        await own_dataset(db, ctx.edge_id, dataset_id)
        row = await (
            await db.execute(
                "INSERT INTO catalog.acl_scan (edge_id, dataset_id, kind) VALUES (%s, %s, %s)"
                " RETURNING acl_scan_id",
                (ctx.edge_id, dataset_id, kind),
            )
        ).fetchone()
    return Result(201, {"scan_id": id_str(row["acl_scan_id"])})


async def complete_scan(ctx: Ctx) -> Result:
    scan_id = ctx.path_id("scan_id")
    seen = req_int(ctx.body, "entries_seen", minimum=0)
    changed = req_int(ctx.body, "entries_changed", minimum=0)
    if changed > seen:
        raise bad("entries_changed cannot exceed entries_seen")
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        row = await (
            await db.execute(
                "SELECT completed_at, entries_seen, entries_changed FROM catalog.acl_scan"
                " WHERE acl_scan_id = %s AND edge_id = %s FOR UPDATE",
                (scan_id, ctx.edge_id),
            )
        ).fetchone()
        if row is None:
            raise ApiError("not_found", f"no ACL scan {scan_id} on this edge")
        if row["completed_at"] is not None:
            if (row["entries_seen"], row["entries_changed"]) == (seen, changed):
                return Result(204)  # idempotent by scan_id
            raise ApiError("version_conflict", f"ACL scan {scan_id} is already complete")
        await db.execute(
            "UPDATE catalog.acl_scan SET completed_at = now(), entries_seen = %s,"
            " entries_changed = %s WHERE acl_scan_id = %s",
            (seen, changed, scan_id),
        )
    return Result(204)
