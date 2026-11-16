"""Recalls into an OmniFile share (annex.md sections 4.6 and 8.4; phase A3.2).

A placement whose target is a share folder (`catalog.annex_placement`,
`target.kind = 'share'`) is carried out here, on queue ``portal``
(``{"kind": "annex_placement", "placement_id"}``), once it is `ready`: its
bytes are readable (online, or brought back from Archive by its recall).
Nothing is written to any NAS.

1. **The gates, again.** The requester can still read the annexed file
   (its captured NAS ACL, deny-first) and still add a file to the target
   folder (`annex/returns.py`); the share still exists; the file's ACL can
   be carried into a share (`share_acl_problem`). Otherwise `failed`.
2. **The copy's ACL never lets more people read the file than its NAS
   did** (decision 51). A POSIX ACL (an NFSv3 NAS) carries over as it is.
   An NT or NFSv4 ACL carries over as its read set: one `r--` entry per
   SID, the mask `r--`, the owner `r--` only if it was a reader, nobody
   else. The read set of what is written is checked to be within the
   file's before anything is copied (`acl_mismatch` otherwise). It is a
   read-only copy: writers of the share may still save a new version over
   it, as they may over any file whose ACL lets them.
3. **The name** (section 4.6, decision 34). Free: a new file. Taken:
   - `same_content`: a file of the same size and CRC is left alone
     (`already_there`);
   - `overwrite_keep_version`: a new version of that file, the old one
     kept, but only when that file's readers are all readers of the
     annexed file, the annexed file's ACL has no deny, and the requester
     may write that file. Otherwise the
     copy takes a `-restored` name (`overwrite_refused_acl`);
   - `restored_name`: `<name>-restored<ext>`, then with a UTC time; a taken
     timestamped name fails (`name_taken`), never overwritten.
4. **The bytes:** a server-side copy (`copy_object`) into the share's
   dataset, under `annex-restores/<placement_id>`, then an in-Azure read of
   the whole copy, whose CRC64-NVME must equal the catalog's (decision 24).
   Only then is the catalog file made (`origin = 'annex_restore'`, linked to
   the placement), its version committed and verified.
5. **After:** the copy is held online for the rule's hold (a Keep online
   hold, decision 33, until `hold_until`), and recalled at once for the
   requester, so the share's edge fills it and the usual Teams card says
   where it is. The source's recall, if one brought the bytes back, is
   closed. Audited as `annex.placement.placed`, with an annex event.

The source and the share's dataset must be on the same backend (a
server-side copy); otherwise the placement fails `different_backend`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..annex import returns as R
from ..api.acls import access_sets, can_read, canonical_json
from ..backends.base import NotRestored, ObjectRef, Throttled
from ..backends.sim.crc64 import to_bytes
from ..catalog.read_set import (
    Folders,
    acl_sets,
    acls_hidden_blocks,
    captured,
    folders_of,
    group_readable,
    readable_sql_pos,
)
from ..retention.common import data_container
from ..web.routes_rewind import restored_name
from .portal_jobs import COPY_HIDE_SECONDS, SOURCE_SQL, readable_ref
from .queue import Message, Retry, outside
from .verify import VERIFIED_BY, read_crc

log = logging.getLogger(__name__)

ACTOR = "system:annex_placements"
EVERYONE = "S-1-1-0"
#: A copy whose owner or group SID has no id on the share's edge: root's.
SERVICE_ID = 0


async def _one(cur, sql: str, params: Any) -> dict[str, Any] | None:
    await cur.execute(sql, params)
    return await cur.fetchone()


async def _fail(conn, p: dict[str, Any], reason: str, detail: str, now: datetime) -> str:
    await conn.execute(
        "UPDATE catalog.annex_placement SET state = 'failed', fail_reason = %s,"
        " fail_detail = left(%s, 500), done_at = %s, updated_at = %s WHERE placement_id = %s",
        (reason, detail, now, now, p["placement_id"]),
    )
    await conn.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
        " VALUES (%s, 'placement_failed', %s, %s, %s)",
        (p["annex_id"], p["file_id"], p["version_id"],
         Jsonb({"placement_id": str(p["placement_id"]), "reason": reason,
                "detail": detail[:500]})),
    )  # fmt: skip
    await conn.execute(
        "SELECT catalog.audit('annex.placement.failed', %s, NULL, %s, %s, %s::jsonb)",
        (p["dataset_id"], p["file_id"], p["version_id"],
         json.dumps({"placement_id": str(p["placement_id"]), "reason": reason})),
    )  # fmt: skip
    return "failed"


def _bits(perms: str) -> int:
    return (
        (4 if perms[0] == "r" else 0)
        | (2 if perms[1] == "w" else 0)
        | (1 if perms[2] == "x" else 0)
    )


async def copy_acl(cur, src: dict[str, Any]) -> dict[str, Any]:
    """The share copy's ACL (owner, group, mode, entries, read, deny, sha)
    from the annexed file's `catalog.acl` row `src`."""
    owner, group = src["owner_sid"], src["group_sid"]
    if src["raw_kind"] == "posix":
        entries = [e for e in src["entries"] if e.get("scope") == "access"]
        mode = int(src["mode"]) & 0o777
    else:
        read = sorted(captured(src)[0])
        kinds = {}
        if read:
            await cur.execute(
                "SELECT sid::text AS sid, kind FROM catalog.identity WHERE sid = ANY(%s)", (read,)
            )
            kinds = {r["sid"]: r["kind"] for r in await cur.fetchall()}
        entries = [{"scope": "access", "tag": "owner",
                    "perms": "r--" if owner in read else "---"},
                   {"scope": "access", "tag": "owning_group", "perms": "---"},
                   {"scope": "access", "tag": "mask", "perms": "r--"},
                   {"scope": "access", "tag": "other",
                    "perms": "r--" if EVERYONE in read else "---"}]  # fmt: skip
        for sid in read:
            if sid in (EVERYONE, owner):
                continue
            tag = "user" if kinds.get(sid) == "user" else "group"
            entries.append({"scope": "access", "tag": tag, "sid": sid, "perms": "r--"})
        mode = (_bits(entries[0]["perms"]) << 6) | (4 << 3) | _bits(entries[3]["perms"])
    entries.sort(key=lambda e: json.dumps(e, sort_keys=True, separators=(",", ":")))
    sha = hashlib.sha256(canonical_json(owner, group, mode, entries).encode()).digest()
    read_sids, deny_sids = access_sets(owner, group, entries)
    return {"owner_sid": owner, "group_sid": group, "mode": mode, "entries": entries,
            "read": read_sids, "deny": deny_sids, "sha256": sha}  # fmt: skip


def within(copy_read: list[str], src: dict[str, Any]) -> bool:
    """The copy's readers are all readers of the annexed file: every SID it
    grants, the file granted, and the file has no deny that a POSIX check
    could miss. And every one of them gets through the folders above the file
    on its NAS (decision 63, `src["folders"]`): a share's folders are no cover
    for the NAS's."""
    granted, denied, _ = captured(src)
    if src["raw_kind"] != "posix" and denied:
        return False
    folders = src.get("folders")
    if not isinstance(folders, Folders) or not folders.allow(copy_read):
        return False
    return set(copy_read) <= granted


def may_write(f: dict[str, Any], who: R.Requester) -> bool:
    """The requester may write the file a new version would replace (#68
    review): a share Writer may add files to the folder, not replace a file
    of someone else's they can't write. Its POSIX ACL, `w`."""
    if f["acl_entries"] is None or not who.sid:
        return False
    write, deny = access_sets(f["acl_owner"], f["acl_group"], f["acl_entries"], need=2)
    return can_read(write, deny, who.sid, list(who.groups))


async def _ensure_acl(cur, dataset_id: int, a: dict[str, Any]) -> int:
    row = await _one(
        cur,
        "INSERT INTO catalog.acl (dataset_id, sha256, owner_sid, group_sid, mode, entries,"
        " read_sids, deny_sids) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::catalog.sid[],"
        " %s::catalog.sid[]) ON CONFLICT (dataset_id, sha256) DO NOTHING RETURNING acl_id",
        (dataset_id, a["sha256"], a["owner_sid"], a["group_sid"], a["mode"],
         json.dumps(a["entries"]), a["read"], a["deny"]),
    )  # fmt: skip
    if row is None:
        row = await _one(
            cur,
            "SELECT acl_id FROM catalog.acl WHERE dataset_id = %s AND sha256 = %s",
            (dataset_id, a["sha256"]),
        )
    assert row is not None
    return int(row["acl_id"])


async def _folder(cur, share: dict[str, Any], folder: str, now: datetime) -> int | None:
    """The share's folder at `folder` (below its root), made as needed: each
    new folder takes its parent's ACL and owner, as a folder made in the
    share would. None if a file is in the way."""
    cur_id = share["root_dir_id"]
    if cur_id is None:
        root = await _one(
            cur,
            "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
            (share["dataset_id"],),
        )
        if root is None:
            return None
        cur_id = root["file_id"]
    for name in [x for x in folder.split("/") if x]:
        row = await _one(
            cur,
            "SELECT file_id, kind FROM catalog.file WHERE parent_id = %s AND name = %s"
            " AND deleted_at IS NULL",
            (cur_id, name),
        )
        if row is None:
            parent = await _one(cur, "SELECT * FROM catalog.file WHERE file_id = %s", (cur_id,))
            assert parent is not None
            row = await _one(
                cur,
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id, uid, gid,"
                " mode, owner_sid, origin, written_at) VALUES (%s, %s, %s, 'dir', %s, %s, %s,"
                " %s, %s, 'annex_restore', %s)"
                " ON CONFLICT (parent_id, name) WHERE deleted_at IS NULL DO NOTHING"
                " RETURNING file_id, kind",
                (share["dataset_id"], cur_id, name, parent["acl_id"], parent["uid"],
                 parent["gid"], parent["mode"], parent["owner_sid"], now),
            )  # fmt: skip
            if row is None:
                raise Retry(1.0)  # made in between: look again
        if row["kind"] != "dir":
            return None
        cur_id = row["file_id"]
    return cur_id


async def _posix_id(cur, sid: str | None, kind: str) -> int | None:
    if sid is None:
        return None
    row = await _one(
        cur, "SELECT posix_id FROM catalog.identity WHERE sid = %s AND kind = %s", (sid, kind)
    )
    return None if row is None or row["posix_id"] is None else int(row["posix_id"])


async def handle_share_placement(
    conn: psycopg.AsyncConnection, registry, placement_id: int, now: datetime,
    msg: Message | None = None,
) -> str:  # fmt: skip
    """Returns the placement's state. Raises `Retry` while it must wait.

    The copy and its read-back run `outside` the transaction: they can take hours,
    and this handler's row locks would hold the edge feed back for all of them. It
    then comes back through every gate with its locks taken again."""
    cur = conn.cursor(row_factory=dict_row)
    p = await _one(
        cur,
        "SELECT p.*, f.dataset_id FROM catalog.annex_placement p"
        " JOIN catalog.file f ON f.file_id = p.file_id"
        " WHERE p.placement_id = %s FOR UPDATE OF p",
        (placement_id,),
    )
    if p is None:
        return "gone"
    if p["target"].get("kind") != "share" or p["state"] not in ("ready", "writing", "verifying"):
        return p["state"]
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))
    ident = await _one(
        cur,
        "SELECT sid::text AS sid, group_sids::text[] AS groups FROM catalog.identity"
        " WHERE identity_id = %s",
        (p["requester_identity_id"],),
    )
    who = R.Requester(ident["sid"] if ident else None, list((ident or {}).get("groups") or []))
    # 1. The gates, again.
    srcacl = await _one(
        cur,
        f"SELECT a.*, {acl_sets('a')},"
        " a.owner_sid::text AS owner_sid, a.group_sid::text AS group_sid,"
        f" {readable_sql_pos('f.file_id', 'a')} AS can_read"
        " FROM catalog.file f JOIN catalog.acl a ON a.acl_id = f.acl_id WHERE f.file_id = %s",
        (who.sid or "S-1-0-0", list(who.groups), p["file_id"]),
    )
    if srcacl is not None:
        srcacl["folders"] = await folders_of(cur, p["file_id"])
    if srcacl is None or not srcacl["can_read"]:
        return await _fail(conn, p, "access_denied", "the requester can no longer read the file",
                           now)  # fmt: skip
    t = p["target"]
    target = R.Target("share", t["folder"], t["name"], share_id=int(t["share_id"]))
    ok, why = await R.write_check(DictDb(conn), target, who)
    if not ok:
        return await _fail(conn, p, "access_denied", why or "the requester can't write there", now)
    problem = await R.share_acl_problem(DictDb(conn), p["file_id"])
    if problem is not None:
        return await _fail(conn, p, "acl_not_representable", problem, now)
    share = await _one(cur, "SELECT * FROM catalog.share WHERE share_id = %s", (target.share_id,))
    ds = await _one(
        cur,
        "SELECT d.*, b.config AS backend_config FROM catalog.dataset d"
        " JOIN catalog.backend b ON b.backend_id = d.backend_id WHERE d.dataset_id = %s",
        (share["dataset_id"],),
    )
    src = await _one(cur, SOURCE_SQL, (p["version_id"],))
    if src is None or src["version_state"] != "committed" or src["crc64nvme"] is None:
        return await _fail(conn, p, "not_restorable", "the version or its CRC is gone", now)
    if src["backend_id"] != ds["backend_id"]:
        return await _fail(conn, p, "different_backend",
                           "the share's dataset is on another backend", now)  # fmt: skip

    # 2. The copy's ACL.
    acl = await copy_acl(cur, srcacl)
    if not within(acl["read"], srcacl):
        return await _fail(conn, p, "acl_mismatch",
                           "the copy's ACL would let more people read the file", now)  # fmt: skip
    if await acls_hidden_blocks(cur, p["file_id"]) and group_readable(
        acl["read"], srcacl["owner_sid"]
    ):
        return await _fail(conn, p, "acls_hidden_filer",
                           "the file's NAS shows no ACLs, and no ArchiveAdmin has acknowledged"
                           " it: only a copy its owner alone reads", now)  # fmt: skip

    # 3. The name, as it is now.
    parent_id = await _folder(cur, share, target.folder, now)
    if parent_id is None:
        return await _fail(conn, p, "name_taken", "a file is in the way of the folder", now)
    existing = await _one(
        cur,
        f"SELECT f.*, {acl_sets('a', 'acl_')}, a.owner_sid::text AS acl_owner,"
        " a.group_sid::text AS acl_group, a.entries AS acl_entries FROM catalog.file f"
        " LEFT JOIN catalog.acl a ON a.acl_id = f.acl_id"
        " WHERE f.parent_id = %s AND f.name = %s AND f.deleted_at IS NULL",
        (parent_id, target.name),
    )
    name, overwrite, outcome = target.name, None, "placed"
    if existing is not None and existing["file_id"] != p["placed_file_id"]:
        same = (
            existing["kind"] == "file"
            and existing["size"] == src["size"]
            and existing["crc64nvme"] is not None
            and bytes(existing["crc64nvme"]) == bytes(src["crc64nvme"])
        )
        if p["on_conflict"] == "same_content" and same:
            return await _placed(conn, cur, p, existing["file_id"], None, "already_there",
                                 share, target.folder, target.name, now)  # fmt: skip
        if (
            p["on_conflict"] == "overwrite_keep_version"
            and existing["kind"] == "file"
            and within(sorted(captured(existing, "acl_")[0]) or [EVERYONE], srcacl)
            and may_write(existing, who)
        ):
            overwrite = existing
        else:
            if p["on_conflict"] == "overwrite_keep_version":
                outcome = "overwrite_refused_acl"
            name = None
            for kind in ("restored", "restored_timestamped"):
                cand = restored_name(target.name, kind, now)
                taken = await _one(
                    cur,
                    "SELECT 1 AS x FROM catalog.file WHERE parent_id = %s AND name = %s"
                    " AND deleted_at IS NULL",
                    (parent_id, cand),
                )
                if taken is None:
                    name = cand
                    break
            if name is None:
                why = "the name and its -restored names are all taken"
                return await _fail(conn, p, "name_taken", why, now)
            if outcome == "placed":
                outcome = "restored_name"

    # 4. The bytes: copied server side, then read back in Azure.
    backend = registry.get(ds["backend_id"])
    if p["copy_key"] is None:
        ref = readable_ref(src, now)
        if ref is None:
            raise Retry(60.0)  # the recall's copy isn't live (yet, or any more)
        try:
            new = await outside(
                msg, "copy",
                lambda: backend.copy_object(
                    ref, data_container(ds["backend_config"] or {}),
                    f"{ds['key_prefix']}annex-restores/{p['placement_id']}",
                ),
                hide_for=COPY_HIDE_SECONDS,
            )  # fmt: skip
        except Throttled as e:
            raise Retry(e.retry_after.total_seconds() if e.retry_after else 30.0) from e
        except NotRestored:
            raise Retry(60.0) from None
        await conn.execute(
            "UPDATE catalog.annex_placement SET state = 'writing', copy_container = %s,"
            " copy_key = %s, copy_backend_version_id = %s, updated_at = %s"
            " WHERE placement_id = %s",
            (new.container, new.key, new.version_id, now, placement_id),
        )
    else:
        new = ObjectRef(ds["backend_id"], p["copy_container"], p["copy_key"],
                        p["copy_backend_version_id"])  # fmt: skip
    size = int(src["size"] or 0)

    async def read_back() -> tuple[int, int, Any]:
        n, crc = await read_crc(backend, new, size)
        return n, crc, await backend.head(new)

    try:
        n, crc, head = await outside(msg, "read_back", read_back, hide_for=COPY_HIDE_SECONDS)
    except Throttled as e:
        raise Retry(e.retry_after.total_seconds() if e.retry_after else 30.0) from e
    if n != size or to_bytes(crc) != bytes(src["crc64nvme"]):
        return await _fail(conn, p, "crc_mismatch",
                           f"the copy read back {n} bytes, CRC {crc:016x}, not"
                           f" {bytes(src['crc64nvme']).hex()}", now)  # fmt: skip

    # The catalog: a new file, or a new version of the one it replaces. Its
    # owner and group are the SIDs' own ids on the share's edge; a SID
    # without one gets root's, the edge's own (#68 review: never the NAS's
    # numeric ids, which may be someone else's there, who could then chmod).
    uid = await _posix_id(cur, acl["owner_sid"], "user")
    gid = await _posix_id(cur, acl["group_sid"], "group")
    uid = SERVICE_ID if uid is None else uid
    gid = SERVICE_ID if gid is None else gid
    if overwrite is not None:
        tfile = overwrite
        acl_id = overwrite["acl_id"]
    else:
        acl_id = await _ensure_acl(cur, share["dataset_id"], acl)
        tfile = await _one(
            cur,
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes, mtime_ns, uid, gid, mode, owner_sid, acl_id, origin, written_at)"
            " VALUES (%s, %s, %s, 'file', %s, %s, 0, %s, %s, %s, %s, %s, %s, 'annex_restore', %s)"
            " ON CONFLICT (parent_id, name) WHERE deleted_at IS NULL DO NOTHING RETURNING *",
            (share["dataset_id"], parent_id, name, "stub" if size > 0 else "resident", size,
             src["mtime_ns"], uid, gid, acl["mode"], acl["owner_sid"], acl_id, now),
        )  # fmt: skip
        if tfile is None:
            raise Retry(1.0)  # the name was taken in between: decide again
    tid = tfile["file_id"]
    v = await _one(
        cur,
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " size, mtime_ns, blake3, crc64nvme, uid, gid, mode, acl_id)"
        " VALUES (%s, %s, %s, %s, 'content', 'pending', %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING version_id",
        (tid, tfile["dataset_id"], tfile["parent_id"], tfile["name"], size, src["mtime_ns"],
         src["blake3"], src["crc64nvme"], tfile["uid"], tfile["gid"], tfile["mode"], acl_id),
    )  # fmt: skip
    vid = v["version_id"]
    await conn.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " backend_version_id, role, state, logical_tier, storage_class, size, crc64nvme,"
        " committed_at, tier_changed_at, verified, verified_at, verified_by, azure_read_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s, %s, %s,"
        " true, %s, %s, %s)",
        (tid, vid, ds["backend_id"], new.container, new.key, new.version_id,
         str(head.logical_tier), head.native_class, size, src["crc64nvme"], now, now, now,
         VERIFIED_BY, now),
    )  # fmt: skip
    await conn.execute(
        "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
        " WHERE version_id = %s",
        (now, vid),
    )
    await conn.execute(
        "UPDATE catalog.file SET size = %s, mtime_ns = %s, blake3 = %s, crc64nvme = %s,"
        " state = %s, resident_bytes = 0, stub_version_id = NULL, stub_resident_bytes = NULL,"
        " written_at = %s WHERE file_id = %s",
        (size, src["mtime_ns"], src["blake3"], src["crc64nvme"],
         "stub" if size > 0 else "resident", now, tid),
    )  # fmt: skip
    return await _placed(conn, cur, p, tid, vid, outcome, share, target.folder, tfile["name"], now)


async def _placed(conn, cur, p: dict[str, Any], file_id: int, version_id: int | None,
                  outcome: str, share: dict[str, Any], folder: str, name: str,
                  now: datetime) -> str:  # fmt: skip
    path = "/".join(x for x in [folder, name] if x)
    hold_until = now + timedelta(days=p["hold_days"]) if p["hold_days"] else None
    await conn.execute(
        "UPDATE catalog.annex_placement SET state = 'placed', outcome = %s, final_name = %s,"
        " final_path = %s, placed_file_id = %s, placed_version_id = %s, hold_until = %s,"
        " done_at = %s, updated_at = %s WHERE placement_id = %s",
        (outcome, name, path, file_id, version_id, hold_until, now, now, p["placement_id"]),
    )
    if hold_until is not None:
        # Keep online (decision 33): the copy stays filled for the hold.
        await conn.execute(
            "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id,"
            " until) VALUES (%s, %s, 'keep_online', %s, %s, %s)",
            (share["dataset_id"], file_id,
             f"recalled from an annex (placement {p['placement_id']})",
             p["requester_identity_id"], hold_until),
        )  # fmt: skip
    if version_id is not None:
        # Filled at once for the requester: the share's edge downloads it, and
        # the usual recall card tells them where it is.
        rc = await _one(
            cur,
            "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority)"
            " VALUES (%s, %s, %s, 'standard')"
            " ON CONFLICT (file_id, version_id)"
            "   WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')"
            " DO NOTHING RETURNING recall_id",
            (file_id, version_id, share["edge_id"]),
        )
        if rc is not None:
            await conn.execute(
                "SELECT pgmq.send('restores', %s::jsonb)",
                (json.dumps({"recall_id": str(rc["recall_id"])}),),
            )
            await conn.execute(
                "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof)"
                " VALUES (%s, %s, 'portal', 'proven') ON CONFLICT DO NOTHING",
                (rc["recall_id"], p["requester_identity_id"]),
            )
            await conn.execute(
                "SELECT pgmq.send('notify', %s::jsonb)",
                (json.dumps({"recall_id": str(rc["recall_id"])}),),
            )
    # The source's recall has done its job: no edge fills an annex file.
    if p["recall_id"] is not None:
        await conn.execute(
            "UPDATE catalog.recall SET state = 'available', completed_at = %s, updated_at = %s"
            " WHERE recall_id = %s AND state = 'downloading'",
            (now, now, p["recall_id"]),
        )
    await conn.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, file_id, version_id, details)"
        " VALUES (%s, 'placed', %s, %s, %s)",
        (p["annex_id"], p["file_id"], p["version_id"],
         Jsonb({"placement_id": str(p["placement_id"]), "outcome": outcome,
                "share_id": str(share["share_id"]), "path": path,
                "placed_file_id": str(file_id)})),
    )  # fmt: skip
    await conn.execute(
        "SELECT catalog.audit('annex.placement.placed', %s, NULL, %s, %s, %s::jsonb)",
        (share["dataset_id"], file_id, version_id,
         json.dumps({"placement_id": str(p["placement_id"]), "outcome": outcome,
                     "from_file_id": str(p["file_id"]), "from_version_id": str(p["version_id"]),
                     "share_id": str(share["share_id"]), "path": path})),
    )  # fmt: skip
    return "placed"


class DictDb:
    """A connection that answers `execute` with a dict-row cursor, as the web's
    catalog connections do (for `annex/returns.py`)."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self.conn = conn

    async def execute(self, query: Any, params: Any = None) -> psycopg.AsyncCursor:
        cur = self.conn.cursor(row_factory=dict_row)
        await cur.execute(query, params)
        return cur
