"""The stub handshake: prepare -> (edge punches) -> commit, or skip. Section 4.3.

`prepare` and `commit` run the same checks, each in one transaction with the
file row locked:
- `version_id` is still the file's content version (a rename or chmod since
  doesn't matter: they carry the content version forward);
- the file is resident on the edge;
- that version's primary location is committed and verified;
- no active keep_online hold, and no active pin to `hot`, on the file or any
  folder above it (decision 29: a hot pin keeps the bytes online).
The lease (`context.make_lease`) binds the resident size `prepare` chose and
expires after 10 minutes.
"""

from __future__ import annotations

from datetime import timedelta

from .context import Ctx, check_lease, make_lease
from .data import owned_file
from .idempotency import Result
from .problems import ApiError
from .validate import opt_str, req_enum, req_id, req_int, req_str, rfc3339

LEASE_TTL = timedelta(minutes=10)

BLOCKERS = """
WITH RECURSIVE up(file_id, parent_id) AS (
    SELECT file_id, parent_id FROM catalog.file WHERE file_id = %(f)s
    UNION
    SELECT p.file_id, p.parent_id FROM catalog.file p JOIN up ON p.file_id = up.parent_id
)
SELECT 'keep_online hold ' || h.hold_id AS why
  FROM catalog.hold h
 WHERE h.file_id IN (SELECT file_id FROM up) AND h.kind = 'keep_online'
   AND h.released_at IS NULL AND (h.until IS NULL OR h.until > now())
UNION ALL
SELECT 'hot tier pin ' || t.pin_id
  FROM catalog.tier_pin t
 WHERE t.file_id IN (SELECT file_id FROM up) AND t.logical_tier = 'hot'
   AND t.released_at IS NULL AND (t.until IS NULL OR t.until > now())
LIMIT 1
"""


async def check_stubbable(db, f: dict, version_id: int) -> None:
    if f["deleted_at"] is not None or f["kind"] != "file":
        raise ApiError("stub_not_allowed", "not a live file")
    if f["content_version_id"] != version_id:
        raise ApiError(
            "version_conflict",
            f"version {version_id} is not the file's content version ({f['content_version_id']})",
        )
    if f["state"] != "resident":
        raise ApiError("stub_not_allowed", f"the file is {f['state']}, not resident")
    loc = await (
        await db.execute(
            "SELECT verified FROM catalog.location WHERE version_id = %s AND role = 'primary'"
            " AND state = 'committed'",
            (version_id,),
        )
    ).fetchone()
    if loc is None or not loc["verified"]:
        raise ApiError("stub_not_allowed", "the version's primary copy is not verified")
    blocker = await (await db.execute(BLOCKERS, {"f": f["file_id"]})).fetchone()
    if blocker is not None:
        raise ApiError("stub_not_allowed", blocker["why"])


async def prepare_stub(ctx: Ctx) -> Result:
    file_id = ctx.path_id("file_id")
    version_id = req_id(ctx.body, "version_id")
    async with ctx.svc.catalog.read() as db:
        f = await owned_file(db, ctx.edge_id, file_id)
        await check_stubbable(db, f, version_id)
    wanted = f["stub_resident_bytes"]
    if wanted is None:
        wanted = ctx.svc.settings.default_resident_bytes
    if f["size"] <= wanted:
        raise ApiError("stub_not_allowed", "the file is no larger than its resident region")
    lease, expires = make_lease(
        ctx.svc.settings.lease_key,
        ctx.edge_id,
        file_id,
        version_id,
        wanted,
        ctx.svc.clock() + LEASE_TTL,
    )
    return Result(
        200,
        {
            "lease_id": lease,
            "resident_bytes": wanted,
            "lease_expires_at": rfc3339(expires),
        },
    )


async def commit_stub(ctx: Ctx) -> Result:
    file_id = ctx.path_id("file_id")
    lease_id = req_str(ctx.body, "lease_id", max_len=64)
    version_id = req_id(ctx.body, "version_id")
    resident = req_int(ctx.body, "resident_bytes", minimum=0)
    check_lease(
        ctx.svc.settings.lease_key,
        lease_id,
        ctx.edge_id,
        file_id,
        version_id,
        resident,
        ctx.svc.clock(),
    )
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        f = await owned_file(db, ctx.edge_id, file_id, lock=True)
        if (
            f["state"] == "stub"
            and f["content_version_id"] == version_id
            and f["resident_bytes"] == resident
        ):
            return Result(204)  # already committed
        await check_stubbable(db, f, version_id)
        await db.execute(
            "UPDATE catalog.file SET state = 'stub', resident_bytes = %s,"
            " stub_version_id = NULL, stub_resident_bytes = NULL WHERE file_id = %s",
            (resident, file_id),
        )
    return Result(204)


async def skip_stub(ctx: Ctx) -> Result:
    file_id = ctx.path_id("file_id")
    version_id = req_id(ctx.body, "version_id")
    reason = req_enum(
        ctx.body, "reason", ("version_mismatch", "open_for_write", "lease_refused", "local_error")
    )
    detail = opt_str(ctx.body, "detail")
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        f = await owned_file(db, ctx.edge_id, file_id, lock=True)
        # The desired state is cleared; the policy sweep asks again later.
        await db.execute(
            "UPDATE catalog.file SET stub_version_id = NULL, stub_resident_bytes = NULL"
            " WHERE file_id = %s AND stub_version_id = %s",
            (file_id, version_id),
        )
        await db.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id,"
            " version_id, details) VALUES (%s, 'stub.skip', 'info', %s, %s, %s,"
            " jsonb_build_object('reason', %s::text, 'detail', %s::text))",
            (ctx.edge.principal, f["dataset_id"], file_id, version_id, reason, detail),
        )
    return Result(204)


async def fill_done(ctx: Ctx) -> Result:
    """An online fetch filled a stub on the edge (no recall: the bytes were
    readable by range GET). The reverse of `commit`: the catalog shows the file
    `resident` again once every byte is local (control-api.md section 4.4).
    Idempotent: a repeat, or a report for a file already resident, is 204."""
    file_id = ctx.path_id("file_id")
    version_id = req_id(ctx.body, "version_id")
    resident = req_int(ctx.body, "resident_bytes", minimum=0)
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        f = await owned_file(db, ctx.edge_id, file_id, lock=True)
        if f["content_version_id"] != version_id:
            raise ApiError("version_conflict", f"file {file_id} has another content version")
        await db.execute(
            "UPDATE catalog.file SET"
            " resident_bytes = greatest(resident_bytes, least(%(n)s, size)),"
            " state = CASE WHEN %(n)s >= size THEN 'resident' ELSE state END"
            " WHERE file_id = %(f)s AND state IN ('stub', 'recalling', 'remote')",
            {"n": resident, "f": file_id},
        )
    return Result(204)
