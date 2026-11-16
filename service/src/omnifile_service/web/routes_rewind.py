"""Restore and download old versions (portal-api.md 5.4, decision 30).

Both are catalog requests the worker carries out (`worker/portal_jobs.py`,
queue `portal`): `web` has no storage role.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..api.validate import rfc3339
from .access import load_scope
from .context import Ctx, WebResult, audit, client_addr
from .problems import WebError, forbidden, invalid, not_found
from .recalls import archived, recall_options, start_or_join, version_location
from .routes_browse import price_table_or_none, readable_version

PORTAL_QUEUE = "portal"


TARGETS = ("overwrite", "restored", "restored_timestamped")
MAX_NAME_BYTES = 255


def restored_name(name: str, target: str, at: datetime) -> str:
    """Decision 34: `<stem>-restored<ext>`, or
    `<stem>-restored-<YYYY-MM-DD-HHMMSS>Z<ext>` (UTC, no colons: SMB-safe).
    The extension starts at the last dot; a name whose only dot leads it
    (`.bashrc`) has none. The stem is shortened to keep 255 bytes."""
    dot = name.rfind(".")
    stem, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    suffix = "-restored"
    if target == "restored_timestamped":
        suffix += "-" + at.astimezone(UTC).strftime("%Y-%m-%d-%H%M%S") + "Z"
    if len((suffix + ext).encode()) >= MAX_NAME_BYTES:
        # An extension too long to keep: it is part of the stem, which is shortened.
        stem, ext = stem + ext, ""
    room = MAX_NAME_BYTES - len((suffix + ext).encode())
    stem = stem.encode()[:room].decode(errors="ignore") or "_"
    return stem + suffix + ext


def restore_json(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "restore_id": str(r["restore_id"]),
        "file_id": str(r["file_id"]),
        "version_id": str(r["version_id"]),
        "target": r["target"],
        "name": r["name"],
        "state": r["state"],
        "recall_id": None if r["recall_id"] is None else str(r["recall_id"]),
        "result_file_id": None if r["result_file_id"] is None else str(r["result_file_id"]),
        "result_version_id": (
            None if r["result_version_id"] is None else str(r["result_version_id"])
        ),
        "failure": r["failure"],
        "requested_at": rfc3339(r["requested_at"]),
        "updated_at": rfc3339(r["updated_at"]),
    }


def download_json(d: dict[str, Any], recall_opts: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "download_id": str(d["download_id"]),
        "file_id": str(d["file_id"]),
        "version_id": str(d["version_id"]),
        "state": d["state"],
        "url": d["url"] if d["state"] == "ready" else None,
        "expires_at": rfc3339(d["expires_at"]),
        "recall_options": recall_opts,
        "failure": d["failure"],
    }


async def _send(db, body: dict[str, Any]) -> None:
    await db.execute("SELECT pgmq.send(%s, %s::jsonb)", (PORTAL_QUEUE, json.dumps(body)))


async def restore_version(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"target"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a restore field")
    target = b.get("target")
    if target not in TARGETS:
        raise invalid("target", "must be overwrite, restored or restored_timestamped")
    name = None
    now = ctx.now
    file_id, version_id = ctx.path_id("file_id"), ctx.path_id("version_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        scope = (await load_scope(db, who, file_id, include_deleted=True)).require_read()
        v = await readable_version(db, ctx, scope, version_id)
        roles = scope.roles
        if not (roles.owner or roles.writer):
            raise forbidden("restoring needs the share's Owners or Writers group")
        if who.identity_id is None:
            raise forbidden("restoring needs an AD identity")
        if v["kind"] == "tombstone" or v["state"] != "committed":
            raise WebError("version_not_restorable", f"a {v['kind']} ({v['state']}) version")
        loc = await version_location(db, file_id, version_id)
        if loc["location_id"] is None:
            raise WebError("version_not_restorable", "this version's bytes are gone")
        if target == "overwrite" and scope.file["deleted_at"] is not None:
            raise WebError(
                "version_not_restorable", "the file was deleted: restore it as a new file"
            )
        if target != "overwrite":
            name = restored_name(scope.file["name"], target, now)
            taken = await (
                await db.execute(
                    "SELECT 1 FROM catalog.file WHERE parent_id = %s AND name = %s"
                    " AND deleted_at IS NULL",
                    (scope.file["parent_id"], name),
                )
            ).fetchone()
            if taken is not None:
                raise WebError("name_taken", f"{name!r} already exists in that folder")
        state, recall_id = "requested", None
        if archived(loc, now):
            recall, _ = await start_or_join(
                db, file=scope.file, loc=loc, priority="standard", due_by=None,
                # A restore is of this version, whatever the file holds by then: it
                # holds the recall it waits on (the row inserted below is counted).
                identity_id=who.identity_id, app_id=None, now=now, hold=True,
            )  # fmt: skip
            state, recall_id = "waiting_recall", recall["recall_id"]
        r = await (
            await db.execute(
                "INSERT INTO catalog.version_restore (dataset_id, file_id, version_id, target,"
                " name, state, recall_id, requested_by_identity_id, requested_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    scope.dataset_id,
                    file_id,
                    version_id,
                    target,
                    name,
                    state,
                    recall_id,
                    who.identity_id,
                    now,
                    now,
                ),
            )  # fmt: skip
        ).fetchone()
        await _send(db, {"kind": "version_restore", "restore_id": str(r["restore_id"])})
        await audit(
            db, who, "version.restore", dataset_id=scope.dataset_id,
            share_id=scope.share["share_id"] if scope.share else None, file_id=file_id,
            version_id=version_id, client_addr=client_addr(ctx.request),
            details={"restore_id": str(r["restore_id"]), "target": target,
                     "name": name or scope.file["name"]},
        )  # fmt: skip
    return WebResult(202, restore_json(r))


async def get_version_restore(ctx: Ctx) -> WebResult:
    who = ctx.who
    async with ctx.svc.catalog.read() as db:
        r = await (
            await db.execute(
                "SELECT * FROM catalog.version_restore WHERE restore_id = %s",
                (ctx.path_id("restore_id"),),
            )
        ).fetchone()
        mine = r is not None and r["requested_by_identity_id"] == who.identity_id
        if r is None or not (mine or who.has("ArchiveAdmin")):
            raise not_found("restore")
        if not mine:
            # An ArchiveAdmin sees the request, but names only what they can read.
            scope = await load_scope(db, who, r["file_id"], include_deleted=True)
            if not scope.readable:
                r = {**r, "name": None}
    return WebResult(200, restore_json(r))


async def request_download(ctx: Ctx) -> WebResult:
    who = ctx.who
    b = ctx.body
    extra = set(b) - {"disposition"}
    if extra:
        raise invalid(sorted(extra)[0], "is not a download field")
    disposition = b.get("disposition", "attachment")
    if disposition not in ("attachment", "inline"):
        raise invalid("disposition", "must be attachment or inline")
    now = ctx.now
    file_id, version_id = ctx.path_id("file_id"), ctx.path_id("version_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        scope = (await load_scope(db, who, file_id, include_deleted=True)).require_read()
        v = await readable_version(db, ctx, scope, version_id)
        if who.identity_id is None:
            raise forbidden("downloading needs an AD identity")
        if v["kind"] == "tombstone" or v["state"] != "committed":
            raise WebError("version_not_restorable", f"a {v['kind']} version has no bytes")
        loc = await version_location(db, file_id, version_id)
        if loc["location_id"] is None:
            raise WebError("version_not_restorable", "this version's bytes are gone")
        opts = None
        state = "preparing"
        if archived(loc, now):
            state = "needs_recall"
            opts = await recall_options(db, loc, now, await price_table_or_none(ctx, db, loc))
        d = await (
            await db.execute(
                "INSERT INTO catalog.download_request (dataset_id, file_id, version_id,"
                " disposition, state, requested_by_identity_id, requested_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    scope.dataset_id,
                    file_id,
                    version_id,
                    disposition,
                    state,
                    who.identity_id,
                    now,
                    now,
                ),
            )  # fmt: skip
        ).fetchone()
        if state == "preparing":
            await _send(db, {"kind": "download", "download_id": str(d["download_id"])})
        await audit(
            db, who, "version.download", dataset_id=scope.dataset_id,
            share_id=scope.share["share_id"] if scope.share else None, file_id=file_id,
            version_id=version_id, client_addr=client_addr(ctx.request),
            details={"download_id": str(d["download_id"]), "state": state},
        )  # fmt: skip
    return WebResult(202, download_json(d, opts))


async def get_download(ctx: Ctx) -> WebResult:
    who = ctx.who
    now = ctx.now
    async with ctx.svc.catalog.read() as db:
        d = await (
            await db.execute(
                "SELECT * FROM catalog.download_request WHERE download_id = %s",
                (ctx.path_id("download_id"),),
            )
        ).fetchone()
        if d is None or who.identity_id is None or d["requested_by_identity_id"] != who.identity_id:
            raise not_found("download")
        # The requester must still be able to read it.
        scope = (await load_scope(db, who, d["file_id"], include_deleted=True)).require_read()
        await readable_version(db, ctx, scope, d["version_id"])
    if d["state"] == "ready" and d["expires_at"] is not None and d["expires_at"] <= now:
        d = {**d, "state": "expired", "url": None}
    return WebResult(200, download_json(d))
