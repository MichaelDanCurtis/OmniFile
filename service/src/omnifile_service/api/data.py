"""Uploads (intent, complete, abort) and credentials: control-api.md sections 3 and 4.2.

- `upload_id` is the pending content version's id (so `upload_id ==
  version_id` in the intent's response). The intent's `op_id` is stored as
  `file_version.edge_op_id`, so a replayed intent finds its version.
- Object keys never derive from paths: `<dataset key_prefix><file_id>/<version_id>`.
- Uploads never set a tier (`set_tier: false`): on the Smart live account a
  blob with an explicit tier never enters Smart.
- A new intent for a file drops that file's older pending upload: the edge
  wrote the file again before the first upload finished.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ..backends.base import EdgeAccess
from ..catalog.blob_lock import blob_lock_key
from .blockcheck import combined_crc, parse_blocks
from .context import Ctx
from .dataplane import Target, quoted_etag
from .db import id_str
from .idempotency import Result
from .problems import ApiError
from .rows import default_tier
from .validate import (
    bad,
    hex_bytes,
    nanos,
    opt_id,
    opt_str,
    parse_id,
    req,
    req_enum,
    req_id,
    req_int,
    req_str,
    rfc3339,
)

OWNED_FILE = """
SELECT f.*, d.backend_id AS ds_backend_id, d.key_prefix, d.storage_layout,
       b.kind AS backend_kind, b.config AS backend_config
  FROM catalog.file f
  JOIN catalog.dataset d ON d.dataset_id = f.dataset_id
  JOIN catalog.backend b ON b.backend_id = d.backend_id
 WHERE f.file_id = %s AND d.edge_id = %s
"""


async def owned_file(db, edge_id: int, file_id: int, *, lock: bool = False) -> dict[str, Any]:
    row = await (
        await db.execute(OWNED_FILE + (" FOR UPDATE OF f" if lock else ""), (file_id, edge_id))
    ).fetchone()
    if row is None:
        raise ApiError("not_found", f"file {file_id} is not on this edge")
    return row


def upload_container(config: dict[str, Any]) -> str:
    """Where new blobs go: `config.container` ("account/container"), else the
    live account's `data` container."""
    if config.get("container"):
        return config["container"]
    return f"{config.get('live_account', 'live')}/{config.get('data_container', 'data')}"


def file_ref_id(value: Any, field: str) -> int:
    if not isinstance(value, dict):
        raise bad(f"{field} must be a file ref")
    if "file_id" in value:
        return parse_id(value["file_id"], f"{field}.file_id")
    if "local" in value:
        raise bad(f"{field}: send the create through /changes first, then refer to its file_id")
    raise bad(f"{field} must be a file ref")


def upload_response(row: dict[str, Any], target: dict[str, Any], block_size: int) -> dict:
    return {
        "action": "upload",
        "upload_id": id_str(row["version_id"]),
        "version_id": id_str(row["version_id"]),
        "target": target,
        "block_size": block_size,
        "set_tier": False,
    }


async def start_upload(ctx: Ctx) -> Result:
    b = ctx.body
    op_id = req_str(b, "op_id", max_len=128)
    file_id = file_ref_id(req(b, "file"), "file")
    size = req_int(b, "size", minimum=0)
    mtime = nanos(req(b, "mtime_ns"), "mtime_ns")
    blake3 = hex_bytes(req(b, "blake3"), "blake3", 32)
    crc = hex_bytes(req(b, "crc64nvme"), "crc64nvme", 8)
    block_size = req_int(b, "block_size", minimum=1)
    has_base = "base_version_id" in b
    base = opt_id(b, "base_version_id")

    async with ctx.svc.catalog.tx(
        ctx.edge.principal, origin_edge_id=ctx.edge_id, origin_op_id=op_id
    ) as db:
        f = await owned_file(db, ctx.edge_id, file_id, lock=True)
        prior = await (
            await db.execute(
                "SELECT v.version_id, v.file_id, v.state, l.container, l.object_key"
                "  FROM catalog.file_version v"
                "  LEFT JOIN catalog.location l ON l.version_id = v.version_id"
                "       AND l.role = 'primary' AND l.state IN ('pending', 'committed')"
                " WHERE v.edge_id = %s AND v.edge_op_id = %s",
                (ctx.edge_id, op_id),
            )
        ).fetchone()
        if prior is not None:
            if prior["file_id"] != file_id:
                raise bad(f"op_id {op_id} was used for another file")
            if prior["state"] != "pending":
                return Result(201, {"action": "dedupe", "version_id": id_str(prior["version_id"])})
            pending = prior
        else:
            pending = None
            if f["deleted_at"] is not None:
                raise ApiError("not_found", f"file {file_id} is deleted")
            if f["kind"] != "file":
                raise bad(f"file {file_id} is a {f['kind']}, not a file")
            if has_base and base != f["content_version_id"]:
                raise ApiError(
                    "version_conflict",
                    f"base_version_id is {base}, the current version is {f['content_version_id']}",
                )
            await drop_pending(db, file_id, ctx.edge_id)
            dup = await find_duplicate(db, f["dataset_id"], blake3, size)
            if dup is not None:
                vid = await commit_dedupe(db, ctx, f, dup, op_id, size, mtime, blake3, crc)
                return Result(201, {"action": "dedupe", "version_id": id_str(vid)})
            pending = await create_pending(db, ctx, f, op_id, size, mtime, blake3, crc)

    plane = ctx.svc.planes.get(f["ds_backend_id"], f["backend_kind"], f["backend_config"] or {})
    target = Target(f["ds_backend_id"], pending["container"], pending["object_key"])
    cred = await plane.credential(target, EdgeAccess.WRITE)
    return Result(201, upload_response(pending, cred, block_size))


async def drop_pending(db, file_id: int, edge_id: int) -> None:
    olds = await (
        await db.execute(
            "SELECT version_id FROM catalog.file_version"
            " WHERE file_id = %s AND state = 'pending' AND kind = 'content'",
            (file_id,),
        )
    ).fetchall()
    for o in olds:
        await db.execute(
            "DELETE FROM catalog.location WHERE version_id = %s AND state = 'pending'",
            (o["version_id"],),
        )
        await db.execute(
            "DELETE FROM catalog.file_version WHERE version_id = %s AND state = 'pending'",
            (o["version_id"],),
        )


async def find_duplicate(db, dataset_id: int, blake3: bytes, size: int) -> dict | None:
    """A committed, verified, online copy of the same bytes in the same dataset.

    Its blob's lock is held shared until this transaction ends, so a tier move
    either finished before the row below was read or sees the new reference. A
    blob being moved right now is not used: the file uploads its own copy."""
    dup = await (
        await db.execute(
            """
            SELECT l.*
              FROM catalog.file f2
              JOIN catalog.location l ON l.version_id = f2.content_version_id
                   AND l.role = 'primary' AND l.state = 'committed' AND l.verified
             WHERE f2.dataset_id = %s AND f2.blake3 = %s AND f2.size = %s
               AND f2.deleted_at IS NULL
               AND coalesce(l.logical_tier, 'hot') <> 'archive'
             LIMIT 1
            """,
            (dataset_id, blake3, size),
        )
    ).fetchone()
    if dup is None:
        return None
    key = blob_lock_key(dup["backend_id"], dup["container"], dup["object_key"])
    got = await (
        await db.execute("SELECT pg_try_advisory_xact_lock_shared(hashtext(%s)) AS ok", (key,))
    ).fetchone()
    if not got["ok"]:
        return None
    return await (
        await db.execute(
            "SELECT * FROM catalog.location WHERE location_id = %s AND state = 'committed'"
            " AND role = 'primary' AND verified AND coalesce(logical_tier, 'hot') <> 'archive'",
            (dup["location_id"],),
        )
    ).fetchone()


async def insert_version(db, ctx: Ctx, f, op_id, size, mtime, blake3, crc) -> int:
    row = await (
        await db.execute(
            """
            INSERT INTO catalog.file_version
                (file_id, dataset_id, parent_id, name, kind, state, size, mtime_ns,
                 blake3, crc64nvme, uid, gid, mode, acl_id, edge_id, edge_op_id)
            VALUES (%s, %s, %s, %s, 'content', 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING version_id
            """,
            (
                f["file_id"],
                f["dataset_id"],
                f["parent_id"],
                f["name"],
                size,
                mtime,
                blake3,
                crc,
                f["uid"],
                f["gid"],
                f["mode"],
                f["acl_id"],
                ctx.edge_id,
                op_id,
            ),
        )
    ).fetchone()
    return row["version_id"]


async def create_pending(db, ctx: Ctx, f, op_id, size, mtime, blake3, crc) -> dict:
    vid = await insert_version(db, ctx, f, op_id, size, mtime, blake3, crc)
    container = upload_container(f["backend_config"] or {})
    key = f"{f['key_prefix']}{f['file_id']}/{vid}"
    await db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, size) VALUES (%s, %s, %s, %s, %s, 'primary', 'pending', %s)",
        (f["file_id"], vid, f["ds_backend_id"], container, key, size),
    )
    if f["origin"] != "annex":  # an annexed file is never on the edge (annex.md 6.3)
        await db.execute(
            "UPDATE catalog.file SET state = 'uploading', stub_version_id = NULL,"
            " stub_resident_bytes = NULL WHERE file_id = %s",
            (f["file_id"],),
        )
    return {"version_id": vid, "container": container, "object_key": key}


async def commit_dedupe(db, ctx: Ctx, f, dup, op_id, size, mtime, blake3, crc) -> int:
    """Record a reference to the existing blob and commit the version at once."""
    vid = await insert_version(db, ctx, f, op_id, size, mtime, blake3, crc)
    await db.execute(
        """
        INSERT INTO catalog.location
            (file_id, version_id, backend_id, container, object_key, backend_version_id,
             role, state, logical_tier, storage_class, size, crc64nvme, etag, verified,
             verified_at, verified_by, committed_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s, %s, true,
                now(), %s, now())
        """,
        (
            f["file_id"],
            vid,
            dup["backend_id"],
            dup["container"],
            dup["object_key"],
            dup["backend_version_id"],
            dup["logical_tier"],
            dup["storage_class"],
            size,
            dup["crc64nvme"],
            dup["etag"],
            dup["verified_by"],
        ),
    )
    await commit_version(
        db, f["file_id"], vid, size, mtime, blake3, crc, remote=f["origin"] == "annex"
    )
    return vid


async def commit_version(
    db, file_id: int, vid: int, size: int, mtime, blake3, crc, *, remote: bool = False
) -> dict:
    """pending -> committed; the catalog's triggers supersede the old version,
    move the file's content version and refresh its location summary.

    `remote`: an annexed file (origin `annex`), whose bytes were read from a
    NAS and are never on the edge: it stays `remote`, with nothing resident,
    so nothing ever stubs or fills it there (annex.md section 6.3)."""
    v = await (
        await db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = now()"
            " WHERE version_id = %s RETURNING committed_at, retention_until",
            (vid,),
        )
    ).fetchone()
    await db.execute(
        "UPDATE catalog.file SET state = %s, size = %s, resident_bytes = %s,"
        " mtime_ns = %s, blake3 = %s, crc64nvme = %s, written_at = now(),"
        " stub_version_id = NULL, stub_resident_bytes = NULL"
        " WHERE file_id = %s",
        (
            "remote" if remote else "resident",
            size,
            0 if remote else size,
            mtime,
            blake3,
            crc,
            file_id,
        ),
    )
    return v


PENDING_UPLOAD = """
SELECT v.version_id, v.file_id, v.state, v.size, v.mtime_ns, v.blake3, v.crc64nvme,
       v.committed_at, v.retention_until, v.edge_op_id,
       l.location_id, l.backend_id, l.container, l.object_key,
       d.storage_layout, b.kind AS backend_kind, b.config AS backend_config,
       f.deleted_at, f.origin
  FROM catalog.file_version v
  JOIN catalog.file f ON f.file_id = v.file_id
  JOIN catalog.dataset d ON d.dataset_id = v.dataset_id
  LEFT JOIN catalog.location l ON l.version_id = v.version_id AND l.role = 'primary'
       AND l.state IN ('pending', 'committed')
  LEFT JOIN catalog.backend b ON b.backend_id = l.backend_id
 WHERE v.version_id = %s AND v.kind = 'content' AND d.edge_id = %s
"""


async def load_upload(db, edge_id: int, upload_id: int, *, lock: bool = False) -> dict:
    row = await (
        await db.execute(
            PENDING_UPLOAD + (" FOR UPDATE OF v" if lock else ""), (upload_id, edge_id)
        )
    ).fetchone()
    if row is None:
        raise ApiError("not_found", f"no upload {upload_id} on this edge")
    return row


def committed_response(v: dict) -> Result:
    return Result(
        200,
        {
            "version_id": id_str(v["version_id"]),
            "committed_at": rfc3339(v["committed_at"]),
            "retention_until": rfc3339(v["retention_until"]),
        },
    )


async def complete_upload(ctx: Ctx) -> Result:
    upload_id = ctx.path_id("upload_id")
    b = ctx.body
    crc = hex_bytes(req(b, "crc64nvme"), "crc64nvme", 8)
    blake3 = hex_bytes(req(b, "blake3"), "blake3", 32)
    size = req_int(b, "size", minimum=0)
    opt_str(b, "etag")  # informational: the service records the ETag it verified
    backend_version_id = opt_str(b, "backend_version_id")

    async with ctx.svc.catalog.read() as db:
        u = await load_upload(db, ctx.edge_id, upload_id)
    if u["state"] == "committed":
        return committed_response(u)  # idempotent by upload_id
    if u["location_id"] is None:
        raise ApiError("not_found", f"upload {upload_id} has no pending location")
    if (size, blake3, crc) != (u["size"], bytes(u["blake3"]), bytes(u["crc64nvme"])):
        raise bad("size, blake3 or crc64nvme differ from the upload intent")
    blocks = parse_blocks(req(b, "blocks"), size)
    if combined_crc(blocks) != int.from_bytes(crc, "big"):
        raise bad("the block CRCs do not combine to crc64nvme")

    # Outside any transaction: the blob version the edge committed must exist
    # with the right size and exactly the declared blocks (blockcheck.py).
    # With `backend_version_id` (from Put Block List), that version is the one
    # checked, so a later overwrite through the write SAS can't change it.
    plane = ctx.svc.planes.get(u["backend_id"], u["backend_kind"], u["backend_config"] or {})
    target = Target(u["backend_id"], u["container"], u["object_key"], backend_version_id)
    head = await plane.head(target)
    if head is None:
        raise bad("the blob does not exist: commit the block list before completing")
    if head.size != size:
        raise bad(f"the blob holds {head.size} bytes, not {size}")
    version = head.version_id or backend_version_id
    if backend_version_id is not None and version != backend_version_id:
        raise bad("backend_version_id is not the blob version that was checked")
    committed = await plane.committed_blocks(
        Target(u["backend_id"], u["container"], u["object_key"], version)
    )
    if [(c.block_id, c.size) for c in committed] != [(i, n) for i, n, _ in blocks]:
        raise bad("the committed block list differs from the declared blocks")
    # A backend that lists block CRCs must agree with the declared ones, and
    # then the upload is verified now. Azure lists none, so the CRCs would be
    # the edge's word alone: the location is committed unverified and the
    # `verify` queue reads it back (worker/verify.py) before anything may stub.
    for c, (_, _, declared) in zip(committed, blocks, strict=True):
        if c.crc64nvme is not None and c.crc64nvme != declared.to_bytes(8, "big"):
            raise bad(f"block {c.block_id} holds other bytes than declared")
    verified_now = bool(committed) and all(c.crc64nvme is not None for c in committed)

    async with ctx.svc.catalog.tx(
        ctx.edge.principal, origin_edge_id=ctx.edge_id, origin_op_id=u["edge_op_id"]
    ) as db:
        u = await load_upload(db, ctx.edge_id, upload_id, lock=True)
        if u["state"] == "committed":
            return committed_response(u)
        if u["deleted_at"] is not None:
            raise ApiError("version_conflict", "the file was deleted while it uploaded")
        tier = default_tier(u["storage_layout"])
        await db.execute(
            """
            UPDATE catalog.location
               SET state = 'committed', committed_at = now(), verified = %s,
                   verified_at = CASE WHEN %s THEN now() END,
                   verified_by = CASE WHEN %s THEN 'upload_block_crc' END, crc64nvme = %s,
                   etag = %s, backend_version_id = %s, logical_tier = %s, storage_class = %s,
                   tier_changed_at = now(), size = %s
             WHERE location_id = %s
            """,
            (
                verified_now,
                verified_now,
                verified_now,
                crc,
                head.etag,
                version,
                tier,
                "Smart" if tier == "smart" else "Hot",
                size,
                u["location_id"],
            ),
        )
        if not verified_now:
            await db.execute(
                "SELECT pgmq.send('verify', %s::jsonb)",
                (json.dumps({"location_id": str(u["location_id"])}),),
            )
        v = await commit_version(
            db,
            u["file_id"],
            upload_id,
            size,
            u["mtime_ns"],
            blake3,
            crc,
            remote=u["origin"] == "annex",
        )
    return committed_response({"version_id": upload_id, **v})


async def abort_upload(ctx: Ctx) -> Result:
    upload_id = ctx.path_id("upload_id")
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        try:
            u = await load_upload(db, ctx.edge_id, upload_id, lock=True)
        except ApiError:
            return Result(204)  # already gone
        if u["state"] != "pending":
            raise ApiError("version_conflict", f"upload {upload_id} is already committed")
        await drop_pending(db, u["file_id"], ctx.edge_id)
        await db.execute(
            "UPDATE catalog.file SET"
            " state = CASE WHEN content_version_id IS NULL THEN 'landing' ELSE 'resident' END,"
            " resident_bytes = CASE WHEN content_version_id IS NULL THEN 0 ELSE size END"
            " WHERE file_id = %s AND state = 'uploading'",
            (u["file_id"],),
        )
    return Result(204)


async def mint_credential(ctx: Ctx) -> Result:
    b = ctx.body
    access = req_enum(b, "access", ("read", "write"))
    async with ctx.svc.catalog.read() as db:
        if access == "write":
            u = await load_upload(db, ctx.edge_id, req_id(b, "upload_id"))
            if u["state"] != "pending" or u["location_id"] is None:
                raise ApiError("version_conflict", "the upload is no longer pending")
            target = Target(u["backend_id"], u["container"], u["object_key"])
            kind, config = u["backend_kind"], u["backend_config"]
        else:
            file_id, version_id = req_id(b, "file_id"), req_id(b, "version_id")
            await owned_file(db, ctx.edge_id, file_id)
            loc = await readable_location(db, file_id, version_id)
            target = Target(
                loc["backend_id"], loc["container"], loc["object_key"], loc["backend_version_id"]
            )
            kind, config = loc["kind"], loc["config"]
    plane = ctx.svc.planes.get(target.backend_id, kind, config or {})
    if access == "write":
        return Result(200, await plane.credential(target, EdgeAccess.WRITE))
    return Result(200, await read_credential(ctx, loc, plane, target))


#: The actor of catalog changes the service makes on its own while serving a
#: read (not the edge's: the feed carries them back to it).
S3_CRC_ACTOR = "system:s3-crc"


async def read_credential(ctx: Ctx, loc: dict, plane, target: Target) -> dict:
    """The read credential for `loc` (a location row with its `crc64nvme`).

    An S3 read (a Roles Anywhere pointer, or the presigned fallback)
    carries the object's full CRC-64/NVME, and the pointer the location's
    ETag (quoted, for If-Match): an S3 range comes with
    no checksum, so the edge verifies the whole file against it before it
    exposes a byte (control-api.md section 3). A file seeded from a listing
    or an inventory has none in the catalog; the object's own, from HEAD
    with checksum mode, is recorded on the location (and on the file, so the
    feed carries it to the edge) and sent. An object S3 stores no CRC for
    gets a credential without one, and the edge refuses to fill it."""
    if target.etag is None and loc.get("etag"):
        # The catalog's ETag pins the read: a presigned GET signs it as
        # If-Match (a replaced object answers 412, never mixed bytes).
        target = replace(target, etag=str(loc["etag"]))
    cred = await plane.credential(target, EdgeAccess.READ)
    if cred.get("mode") not in ("s3_presigned_url", "s3_roles_anywhere"):
        return cred
    if cred["mode"] == "s3_roles_anywhere" and loc.get("etag"):
        # The edge pins unversioned reads with If-Match, and refuses chunk
        # reads that have neither a version nor an ETag.
        cred = {**cred, "etag": quoted_etag(str(loc["etag"]))}
    crc = loc.get("crc64nvme")
    if crc is None:
        head = await plane.head(target)
        crc = head.crc64nvme if head is not None else None
        if crc is not None:
            async with ctx.svc.catalog.tx(S3_CRC_ACTOR) as db:
                await db.execute(
                    "UPDATE catalog.location SET crc64nvme = %s, updated_at = now()"
                    " WHERE location_id = %s AND crc64nvme IS NULL",
                    (crc, loc["location_id"]),
                )
                await db.execute(
                    "UPDATE catalog.file SET crc64nvme = %s"
                    " WHERE file_id = %s AND content_version_id = %s AND crc64nvme IS NULL",
                    (crc, loc["file_id"], loc["version_id"]),
                )
    if crc is not None:
        cred = {**cred, "crc64nvme": bytes(crc).hex()}
    return cred


async def readable_location(db, file_id: int, version_id: int) -> dict:
    """The primary location of a committed content version (a metadata or
    rename version resolves to the content it carries)."""
    loc = await (
        await db.execute(
            """
            SELECT l.*, b.kind, b.config
              FROM catalog.file_version v
              JOIN catalog.location l
                   ON l.version_id = CASE WHEN v.kind = 'content' THEN v.version_id
                                          ELSE v.content_version_id END
                  AND l.role = 'primary' AND l.state = 'committed'
              JOIN catalog.backend b ON b.backend_id = l.backend_id
             WHERE v.version_id = %s AND v.file_id = %s AND v.state = 'committed'
            """,
            (version_id, file_id),
        )
    ).fetchone()
    if loc is None:
        raise ApiError("not_found", f"version {version_id} of file {file_id} has no stored bytes")
    return loc
