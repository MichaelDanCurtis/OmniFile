"""Namespace changes from the edge outbox: control-api.md section 4.1.

One batch is one transaction; each op runs in its own savepoint, so a refused
op leaves the others applied. Every op leaves a `file_version` row carrying
its `op_id` (`edge_op_id`), which is how a replayed op is recognised as a
`duplicate` (decision 30: every change is a version):

| op       | file row                       | version     |
|----------|--------------------------------|-------------|
| create   | new row (`landing` for files)  | `metadata`  |
| setattr  | attributes                     | `metadata`  |
| rename   | parent and name                | `rename`    |
| unlink   | `deleted_at`                   | `tombstone` |
| acl      | `acl_id`                       | `metadata`  |

`size` is not a setattr attribute: content and its size arrive through
uploads. A `rename` with `replace` over an existing name tombstones the
target first. Conflicts (`expected_entity_version` is stale, or the name is
taken) also emit a `file.conflict` feed event carrying the catalog's state.

An op may name its `actor` (optional, additive): who made the change on the
edge, shaped like a recall's requester (section 4.4) plus `via = unknown`
(the edge's reconcile pass found it after the fact, or could not resolve the
session; no identity then). The versions the op writes record it
(`file_version.actor_*`, migration 0030), with `proof` derived here as for
recalls and never taken from the edge (the edge's word can only lower it),
and the catalog identity of its SID or uid: so a tombstone says who deleted
the file (the cutover report's `deleted_by`). Ops without `actor` record
nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg

from .context import Ctx
from .data import owned_file
from .db import id_str, map_db_error
from .idempotency import Result
from .problems import ApiError
from .recalls import _inet, derive_proof, resolve_identity
from .validate import (
    bad,
    hex_bytes,
    nanos,
    opt_int,
    parse_id,
    req,
    req_bool,
    req_enum,
    req_str,
    sid,
)

MAX_OPS = 1000
OPS = ("create", "setattr", "rename", "unlink", "acl")
LocalKey = tuple[str, str, str]
ACTOR_VIA = ("nfs", "smb", "local", "unknown")
ACTOR_SEC = ("sys", "krb5", "krb5i", "krb5p", "ntlm", "kerberos")


class Rejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class Conflict(Exception):
    def __init__(self, file_id: int, detail: str) -> None:
        super().__init__(detail)
        self.file_id, self.detail = file_id, detail


def parse_actor(value: Any) -> dict | None:
    """An op's `actor` (optional). `via = unknown` carries no identity: a uid,
    SID or flavour sent with it is ignored."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise bad("actor must be an object")
    via = req_enum(value, "via", ACTOR_VIA)
    addr = value.get("client_addr")
    if addr is not None and (not isinstance(addr, str) or len(addr) > 64):
        raise bad("actor.client_addr must be a short string")
    uid = opt_int(value, "uid", minimum=0)
    if uid is not None and uid >= 0xFFFFFFFF:
        raise bad("actor.uid must be a uid (0xFFFFFFFF means unknown: leave it out)")
    out: dict[str, Any] = {
        "via": via,
        # Advisory only: the service derives proof itself (derive_proof).
        "edge_proof": (
            req_enum(value, "proof", ("claimed", "proven"))
            if value.get("proof") is not None
            else None
        ),
        "uid": uid,
        "sid": sid(value["sid"], "actor.sid") if value.get("sid") is not None else None,
        "sec": req_enum(value, "sec", ACTOR_SEC) if value.get("sec") is not None else None,
        "client_addr": _inet(addr),
    }
    if via == "unknown":
        out.update(uid=None, sid=None, sec=None, edge_proof=None, client_addr=None)
    return out


def valid_name(name: Any) -> str:
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\x00" in name
        or len(name.encode()) > 255
    ):
        raise Rejected("name_invalid", f"invalid name {name!r}")
    return name


class Batch:
    def __init__(self, ctx: Ctx, db: psycopg.AsyncConnection) -> None:
        self.ctx = ctx
        self.db = db
        self.local: dict[LocalKey, int] = {}
        self.failed_local: set[LocalKey] = set()
        #: The current op's actor (parse_actor), recorded on its versions,
        #: and what it resolved to per dataset: (proof, identity_id).
        self.actor: dict | None = None
        self.actor_by_dataset: dict[int, tuple[str, int | None]] = {}

    # --- refs -----------------------------------------------------------

    @staticmethod
    def local_key(ref: dict) -> LocalKey:
        loc = ref["local"]
        if not isinstance(loc, dict):
            raise bad("local must be an object")
        try:
            return (str(loc["fsid"]), str(loc["ino"]), str(loc["ino_gen"]))
        except KeyError as e:
            raise bad(f"local ref lacks {e.args[0]}") from None

    def resolve(self, ref: Any, field: str) -> int:
        if not isinstance(ref, dict):
            raise bad(f"{field} must be a file ref")
        if "file_id" in ref:
            return parse_id(ref["file_id"], f"{field}.file_id")
        if "local" in ref:
            key = self.local_key(ref)
            if key in self.local:
                return self.local[key]
            if key in self.failed_local:
                raise Rejected("dependency_failed", f"{field}: the op that created it failed")
            raise Rejected("dependency_failed", f"{field}: unknown local ref; use its file_id")
        raise bad(f"{field} must be a file ref")

    async def file(self, file_id: int, *, live: bool = True) -> dict:
        try:
            f = await owned_file(self.db, self.ctx.edge_id, file_id, lock=True)
        except ApiError:
            raise Rejected("not_found", f"file {file_id} is not on this edge") from None
        if live and f["deleted_at"] is not None:
            raise Rejected("not_found", f"file {file_id} is deleted")
        return f

    async def directory(self, file_id: int, dataset_id: int | None = None) -> dict:
        d = await self.file(file_id)
        if d["kind"] != "dir":
            raise Rejected("invalid_request", f"{file_id} is not a directory")
        if dataset_id is not None and d["dataset_id"] != dataset_id:
            raise Rejected("invalid_request", "cannot move between datasets")
        return d

    async def acl_id(self, dataset_id: int, sha_hex: Any, field: str) -> int:
        sha = hex_bytes(sha_hex, field, 32)
        row = await (
            await self.db.execute(
                "SELECT acl_id FROM catalog.acl WHERE dataset_id = %s AND sha256 = %s",
                (dataset_id, sha),
            )
        ).fetchone()
        if row is None:
            raise Rejected("acl_unknown", f"ACL {sha_hex} is not known; send it to /acls first")
        return row["acl_id"]

    async def attrs(self, dataset_id: int, attrs: Any) -> dict[str, Any]:
        if attrs is None:
            return {}
        if not isinstance(attrs, dict):
            raise bad("attrs must be an object")
        out: dict[str, Any] = {}
        if "mtime_ns" in attrs:
            out["mtime_ns"] = nanos(attrs["mtime_ns"], "attrs.mtime_ns")
        for name, hi in (("uid", 2**63 - 1), ("gid", 2**63 - 1), ("mode", 4095)):
            if name in attrs:
                v = attrs[name]
                if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
                    raise bad(f"attrs.{name} is out of range")
                out[name] = v
        if "owner_sid" in attrs:
            out["owner_sid"] = sid(attrs["owner_sid"], "attrs.owner_sid")
        if "acl_sha256" in attrs:
            out["acl_id"] = await self.acl_id(dataset_id, attrs["acl_sha256"], "attrs.acl_sha256")
        if "size" in attrs:
            v = attrs["size"]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise bad("attrs.size must be a non-negative integer")
            out["size"] = v
        return out

    # --- versions -------------------------------------------------------

    async def actor_columns(self, file_id: int) -> tuple:
        """`(via, uid, sid, sec, proof, client_addr, identity_id)` for the
        current op's actor: proof derived per dataset (decision 28), never
        the edge's word; all NULL when the op names none."""
        a = self.actor
        if a is None:
            return (None,) * 7
        if a["via"] == "unknown":
            return ("unknown", None, None, None, "unknown", None, None)
        row = await (
            await self.db.execute(
                "SELECT dataset_id FROM catalog.file WHERE file_id = %s", (file_id,)
            )
        ).fetchone()
        ds = row["dataset_id"]
        if ds not in self.actor_by_dataset:
            proof = await derive_proof(self.db, self.ctx.edge_id, ds, a)
            identity_id, _ = await resolve_identity(self.db, self.ctx, a)
            self.actor_by_dataset[ds] = (proof, identity_id)
        proof, identity_id = self.actor_by_dataset[ds]
        return (a["via"], a["uid"], a["sid"], a["sec"], proof, a["client_addr"], identity_id)

    async def version(self, file_id: int, kind: str, op_id: str) -> int:
        actor = await self.actor_columns(file_id)
        row = await (
            await self.db.execute(
                """
                INSERT INTO catalog.file_version
                    (file_id, dataset_id, parent_id, name, kind, state, committed_at, size,
                     mtime_ns, uid, gid, mode, acl_id, edge_id, edge_op_id,
                     actor_via, actor_uid, actor_sid, actor_sec, actor_proof,
                     actor_client_addr, actor_identity_id)
                SELECT f.file_id, f.dataset_id, f.parent_id, f.name, %s, 'committed', now(),
                       f.size, f.mtime_ns, f.uid, f.gid, f.mode, f.acl_id, %s, %s,
                       %s, %s, %s, %s, %s, %s::inet, %s
                  FROM catalog.file f WHERE f.file_id = %s
                RETURNING version_id
                """,
                (kind, self.ctx.edge_id, op_id, *actor, file_id),
            )
        ).fetchone()
        return row["version_id"]

    async def applied(self, op_id: str, file_id: int, version_id: int) -> dict:
        row = await (
            await self.db.execute(
                "SELECT entity_version FROM catalog.file WHERE file_id = %s", (file_id,)
            )
        ).fetchone()
        return {
            "op_id": op_id,
            "status": "applied",
            "file_id": id_str(file_id),
            "version_id": id_str(version_id),
            "entity_version": id_str(row["entity_version"]),
        }

    async def name_taken(self, parent_id: int, name: str) -> dict | None:
        return await (
            await self.db.execute(
                "SELECT file_id, kind FROM catalog.file"
                " WHERE parent_id = %s AND name = %s AND deleted_at IS NULL",
                (parent_id, name),
            )
        ).fetchone()

    # --- ops ------------------------------------------------------------

    def check_expected(self, f: dict, op: dict) -> None:
        if op.get("expected_entity_version") is None:
            return
        expected = parse_id(op["expected_entity_version"], "expected_entity_version")
        if expected != f["entity_version"]:
            raise Conflict(
                f["file_id"],
                f"entity_version is {f['entity_version']}, the op expected {expected}",
            )

    async def create(self, op: dict, op_id: str) -> dict:
        parent_id = self.resolve(req(op, "parent"), "parent")
        parent = await self.directory(parent_id)
        name = valid_name(req(op, "name"))
        kind = req_enum(op, "kind", ("file", "dir", "symlink"))
        target = req_str(op, "symlink_target") if kind == "symlink" else None
        attrs = await self.attrs(parent["dataset_id"], op.get("attrs"))
        existing = await self.name_taken(parent_id, name)
        if existing is not None:
            raise Conflict(existing["file_id"], f"{name!r} already exists in {parent_id}")
        size = attrs.get("size", 0) if kind == "file" else (len(target.encode()) if target else 0)
        row = await (
            await self.db.execute(
                """
                INSERT INTO catalog.file
                    (dataset_id, parent_id, name, kind, symlink_target, size, mtime_ns, uid, gid,
                     mode, owner_sid, acl_id, state, resident_bytes, origin)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'edge')
                RETURNING file_id
                """,
                (
                    parent["dataset_id"],
                    parent_id,
                    name,
                    kind,
                    target,
                    size,
                    attrs.get("mtime_ns"),
                    attrs.get("uid"),
                    attrs.get("gid"),
                    attrs.get("mode"),
                    attrs.get("owner_sid"),
                    attrs.get("acl_id"),
                    "landing" if kind == "file" else "resident",
                    0 if kind == "file" else size,
                ),
            )
        ).fetchone()
        file_id = row["file_id"]
        vid = await self.version(file_id, "metadata", op_id)
        if isinstance(op.get("file"), dict) and "local" in op["file"]:
            self.local[self.local_key(op["file"])] = file_id
        return await self.applied(op_id, file_id, vid)

    async def setattr(self, op: dict, op_id: str) -> dict:
        f = await self.file(self.resolve(req(op, "file"), "file"))
        self.check_expected(f, op)
        attrs = await self.attrs(f["dataset_id"], op.get("attrs"))
        attrs.pop("size", None)  # content (and its size) comes through uploads
        if attrs:
            cols = ", ".join(f"{k} = %({k})s" for k in attrs)
            await self.db.execute(
                f"UPDATE catalog.file SET {cols} WHERE file_id = %(file_id)s",
                {**attrs, "file_id": f["file_id"]},
            )
        vid = await self.version(f["file_id"], "metadata", op_id)
        return await self.applied(op_id, f["file_id"], vid)

    async def acl(self, op: dict, op_id: str) -> dict:
        f = await self.file(self.resolve(req(op, "file"), "file"))
        self.check_expected(f, op)
        acl_id = await self.acl_id(f["dataset_id"], req(op, "acl_sha256"), "acl_sha256")
        await self.db.execute(
            "UPDATE catalog.file SET acl_id = %s WHERE file_id = %s", (acl_id, f["file_id"])
        )
        vid = await self.version(f["file_id"], "metadata", op_id)
        return await self.applied(op_id, f["file_id"], vid)

    async def tombstone(self, f: dict, op_id: str) -> int:
        if f["parent_id"] is None:
            raise Rejected("invalid_request", "the dataset root cannot be removed")
        # deleted_at first: the catalog refuses a non-empty directory (OF001).
        await self.db.execute(
            "UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (f["file_id"],)
        )
        return await self.version(f["file_id"], "tombstone", op_id)

    async def unlink(self, op: dict, op_id: str) -> dict:
        f = await self.file(self.resolve(req(op, "file"), "file"))
        self.check_expected(f, op)
        vid = await self.tombstone(f, op_id)
        return await self.applied(op_id, f["file_id"], vid)

    async def rename(self, op: dict, op_id: str) -> dict:
        f = await self.file(self.resolve(req(op, "file"), "file"))
        self.check_expected(f, op)
        if f["parent_id"] is None:
            raise Rejected("invalid_request", "the dataset root cannot be renamed")
        new_parent = await self.directory(
            self.resolve(req(op, "new_parent"), "new_parent"), f["dataset_id"]
        )
        new_name = valid_name(req(op, "new_name"))
        replace = req_bool(op, "replace", default=False)
        if f["kind"] == "dir":
            loop = await (
                await self.db.execute(
                    """
                    WITH RECURSIVE up(file_id, parent_id) AS (
                        SELECT file_id, parent_id FROM catalog.file WHERE file_id = %s
                        UNION
                        SELECT p.file_id, p.parent_id FROM catalog.file p
                          JOIN up ON p.file_id = up.parent_id)
                    SELECT 1 FROM up WHERE file_id = %s
                    """,
                    (new_parent["file_id"], f["file_id"]),
                )
            ).fetchone()
            if loop is not None:
                raise Rejected("invalid_request", "a directory cannot move below itself")
        existing = await self.name_taken(new_parent["file_id"], new_name)
        if existing is not None and existing["file_id"] != f["file_id"]:
            if not replace:
                raise Conflict(existing["file_id"], f"{new_name!r} already exists")
            victim = await self.file(existing["file_id"])
            await self.tombstone(victim, f"{op_id}#replaced")
        await self.db.execute(
            "UPDATE catalog.file SET parent_id = %s, name = %s WHERE file_id = %s",
            (new_parent["file_id"], new_name, f["file_id"]),
        )
        vid = await self.version(f["file_id"], "rename", op_id)
        return await self.applied(op_id, f["file_id"], vid)

    # --- driver ---------------------------------------------------------

    async def duplicate(self, op: dict, op_id: str) -> dict | None:
        row = await (
            await self.db.execute(
                "SELECT v.version_id, v.file_id, f.entity_version FROM catalog.file_version v"
                " JOIN catalog.file f ON f.file_id = v.file_id"
                " WHERE v.edge_id = %s AND v.edge_op_id = %s",
                (self.ctx.edge_id, op_id),
            )
        ).fetchone()
        if row is None:
            return None
        if op.get("op") == "create" and isinstance(op.get("file"), dict) and "local" in op["file"]:
            self.local[self.local_key(op["file"])] = row["file_id"]
        return {
            "op_id": op_id,
            "status": "duplicate",
            "file_id": id_str(row["file_id"]),
            "version_id": id_str(row["version_id"]),
            "entity_version": id_str(row["entity_version"]),
        }

    async def conflict_event(self, file_id: int, op_id: str, reason: str) -> None:
        await self.db.execute(
            "SELECT catalog.feed_conflict(%s, %s, %s, %s)",
            (self.ctx.edge_id, file_id, op_id, reason),
        )

    async def apply(self, op: Any, index: int) -> dict:
        if not isinstance(op, dict):
            raise bad(f"ops[{index}] must be an object")
        op_id = req_str(op, "op_id", max_len=128)
        kind = req_enum(op, "op", OPS)
        self.actor = parse_actor(op.get("actor"))
        self.actor_by_dataset = {}  # per op: proof depends on the actor and the dataset
        await self.db.execute("SELECT set_config('omnifile.origin_op_id', %s, true)", (op_id,))
        dup = await self.duplicate(op, op_id)
        if dup is not None:
            return dup
        try:
            async with self.db.transaction():  # a savepoint per op
                return await getattr(self, kind)(op, op_id)
        except Rejected as exc:
            self.forget_local(op, kind)
            return {"op_id": op_id, "status": "rejected", "code": exc.code, "detail": exc.detail}
        except Conflict as exc:
            self.forget_local(op, kind)
            await self.conflict_event(exc.file_id, op_id, exc.detail)
            return {
                "op_id": op_id,
                "status": "conflict",
                "code": "version_conflict",
                "detail": exc.detail,
                "file_id": id_str(exc.file_id),
            }
        except psycopg.Error as exc:
            self.forget_local(op, kind)
            err = map_db_error(exc)
            if err.status >= 500:
                raise err from exc
            return {"op_id": op_id, "status": "rejected", "code": err.code, "detail": err.detail}

    def forget_local(self, op: dict, kind: str) -> None:
        if kind == "create" and isinstance(op.get("file"), dict) and "local" in op["file"]:
            try:
                self.failed_local.add(self.local_key(op["file"]))
            except ApiError:
                pass


async def send_changes(ctx: Ctx) -> Result:
    ops = req(ctx.body, "ops")
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
        raise bad(f"ops must be a list of 1 to {MAX_OPS} ops")
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        batch = Batch(ctx, db)
        results = [await batch.apply(op, i) for i, op in enumerate(ops)]
    return Result(200, {"results": results})
