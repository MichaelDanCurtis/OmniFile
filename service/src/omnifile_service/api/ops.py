"""Heartbeat (section 4.7), certificate renewal (section 2) and read reports (4.7)."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ..enrol.ca import renew_after
from .clientauth import ROLES_ANYWHERE_PROOF_CONTEXT, verify_renewal_proof
from .context import Ctx
from .db import id_str
from .feed import count_after
from .idempotency import Result
from .problems import ApiError
from .validate import bad, cursor, parse_id, req, req_enum, req_str, rfc3339, timestamp

MAX_READS = 10_000


# The heartbeat's gauges (control-api.md section 4.7), kept in catalog.edge.health.
HEALTH_GAUGES = (
    "landing_used_bytes",
    "landing_capacity_bytes",
    "cache_used_bytes",
    "outbox_depth",
    "fills_running",
    "recalls_waiting",
    "data_free_bytes",
    "data_capacity_bytes",
    "fsal_connections",
    "recovery_used",
    # Decision 67: this edge's defaults for a share with no quiet period of its
    # own. Present at all, they also say the edge applies each share's own.
    "quiet_default_s",
    "quiet_worm_default_s",
    # How long the edge has failed to read its shares' quiet periods (#80 re-check).
    "quiet_stale_s",
    # 1: this edge knows the recall state `superseded` (migration 0059). An edge
    # that doesn't say so is sent `cancelled` in its place (api/feed.py).
    "recall_superseded",
)


def _gauge(b: dict[str, Any], key: str) -> int | None:
    v = b.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 2**63:
        raise bad(f"{key} must be a non-negative integer")
    return v


async def heartbeat(ctx: Ctx) -> Result:
    b = ctx.body
    version = req_str(b, "version", max_len=64)
    feed_cursor = b.get("feed_cursor")
    after = cursor(feed_cursor, "feed_cursor") if feed_cursor is not None else None
    kernel = b.get("kernel")
    if kernel is not None and (not isinstance(kernel, str) or len(kernel) > 128):
        raise bad("kernel must be a string of at most 128 characters")
    guard_state = b.get("guard_state")
    if guard_state is not None:
        guard_state = req_enum(b, "guard_state", ("armed", "degraded", "down"))
    gauges = {k: v for k in HEALTH_GAUGES if (v := _gauge(b, k)) is not None}
    async with ctx.svc.catalog.tx(ctx.edge.principal) as db:
        lag = await count_after(db, ctx.edge_id, after) if after is not None else None
        if lag is not None:
            gauges["feed_lag_events"] = lag
        # A fill running now is the evidence that this edge still works on
        # its `downloading` recalls (worker/recall_timeout.py, migration 0039).
        await db.execute(
            "UPDATE catalog.edge SET last_seen_at = now(), last_heartbeat_at = now(),"
            " software_version = %s, kernel = %s, guard_state = %s, health = %s,"
            " last_fill_at = CASE WHEN %s THEN now() ELSE last_fill_at END"
            " WHERE edge_id = %s",
            (
                version,
                kernel,
                guard_state,
                json.dumps(gauges),
                gauges.get("fills_running", 0) > 0,
                ctx.edge_id,
            ),
        )
        # The service's reachable window (migration 0040): a heartbeat after
        # a gap of over 3 minutes starts a new one.
        await db.execute(
            "INSERT INTO catalog.heartbeat_window (id, started_at, last_at)"
            " VALUES (true, now(), now()) ON CONFLICT (id) DO UPDATE SET"
            " started_at = CASE WHEN catalog.heartbeat_window.last_at"
            "   < now() - interval '3 minutes' THEN now()"
            "   ELSE catalog.heartbeat_window.started_at END,"
            " last_at = greatest(catalog.heartbeat_window.last_at, now())"
        )
        # Bumped by any change to this edge's shares or datasets.
        row = await (
            await db.execute(
                "SELECT coalesce((SELECT sum(entity_version) FROM catalog.share"
                "                  WHERE edge_id = %(e)s), 0)"
                "     + coalesce((SELECT sum(entity_version) FROM catalog.dataset"
                "                  WHERE edge_id = %(e)s), 0) AS v, now() AS now",
                {"e": ctx.edge_id},
            )
        ).fetchone()
    out = {
        "server_time": rfc3339(row["now"]),
        "min_edge_version": ctx.svc.settings.min_edge_version,
        "config_version": id_str(int(row["v"])),
    }
    if lag is not None:
        out["feed_lag_events"] = lag
    if ctx.edge.cert_not_after is not None:
        out["certificate_renew_after"] = rfc3339(renew_after(ctx.edge.cert_not_after))
    return Result(200, out)


async def renew_certificate(ctx: Ctx) -> Result:
    csr_pem = req_str(ctx.body, "csr_pem", max_len=16_384)
    proof = req(ctx.body, "proof")
    if not isinstance(proof, dict):
        raise bad("proof must be an object")
    if ctx.edge.cert is None:
        raise ApiError("certificate_invalid", "no presented certificate to check the proof against")
    # Proof of possession of the CURRENT key: the forwarded certificate is
    # public data, so it alone must not be enough to get a new one.
    verify_renewal_proof(
        ctx.edge.cert,
        ctx.edge_id,
        csr_pem,
        req_str(proof, "signed_at", max_len=64),
        req_str(proof, "signature", max_len=1024),
        ctx.svc.clock(),
    )
    if ctx.svc.signer is None:
        raise ApiError("unavailable", "certificate renewal is not configured", retry_after_s=60)
    assert ctx.idempotency_key is not None
    issued = await ctx.svc.signer.sign(
        ctx.edge_id, csr_pem, ctx.edge.cert_sha256.hex(), ctx.idempotency_key
    )
    out = {
        "edge_id": str(ctx.edge_id),
        "cert_pem": issued["cert_pem"],
        "chain_pem": issued["chain_pem"],
        "ca_pem": issued["ca_pem"],
        "not_after": issued["not_after"],
    }
    if ctx.svc.settings.api_url:
        out["api_url"] = ctx.svc.settings.api_url
    return Result(201, out)


async def roles_anywhere_certificate(ctx: Ctx) -> Result:
    """`POST aws-certificate` (decision 50; control-api.md section 2): the
    edge's IAM Roles Anywhere certificate, signed by `enrol` with the edge
    CA. The CSR's self-signature proves the edge holds the AWS key; a proof
    signed with the presented mTLS certificate's key (as for a renewal, under
    its own context) proves this request is the edge's. The AWS key must be
    a key of its own, never the mTLS one, and is pinned at the first
    certificate (`enrol`). A revoked edge never gets here (`clientauth`)."""
    csr_pem = req_str(ctx.body, "csr_pem", max_len=16_384)
    proof = req(ctx.body, "proof")
    if not isinstance(proof, dict):
        raise bad("proof must be an object")
    extra = set(ctx.body) - {"csr_pem", "proof"}
    if extra:
        raise bad(f"unexpected fields: {', '.join(sorted(extra))}")
    if ctx.edge.cert is None:
        raise ApiError("certificate_invalid", "no presented certificate to check the proof against")
    verify_renewal_proof(
        ctx.edge.cert,
        ctx.edge_id,
        csr_pem,
        req_str(proof, "signed_at", max_len=64),
        req_str(proof, "signature", max_len=1024),
        ctx.svc.clock(),
        ROLES_ANYWHERE_PROOF_CONTEXT,
    )
    try:
        key = x509.load_pem_x509_csr(csr_pem.encode()).public_key()
    except ValueError:
        raise bad("csr_pem is not a PEM PKCS#10 request") from None
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    mtls = ctx.edge.cert.public_key()
    if isinstance(key, ec.EllipticCurvePublicKey) and key.public_bytes(
        der, spki
    ) == mtls.public_bytes(der, spki):
        raise bad("the Roles Anywhere key must be a key of its own, not the mTLS key")
    if ctx.svc.signer is None:
        raise ApiError("unavailable", "certificate signing is not configured", retry_after_s=60)
    assert ctx.idempotency_key is not None
    issued = await ctx.svc.signer.sign(
        ctx.edge_id,
        csr_pem,
        ctx.edge.cert_sha256.hex(),
        ctx.idempotency_key,
        purpose="roles_anywhere",
    )
    return Result(
        201,
        {
            "edge_id": str(ctx.edge_id),
            "cert_pem": issued["cert_pem"],
            "chain_pem": issued["chain_pem"],
            "ca_pem": issued["ca_pem"],
            "not_after": issued["not_after"],
            "renew_after": issued["renew_after"],
        },
    )


async def report_reads(ctx: Ctx) -> Result:
    reads = req(ctx.body, "reads")
    if not isinstance(reads, list) or len(reads) > MAX_READS:
        raise bad(f"reads must be a list of at most {MAX_READS}")
    latest: dict[int, object] = {}
    for i, item in enumerate(reads):
        if not isinstance(item, dict):
            raise bad(f"reads[{i}] must be an object")
        fid = parse_id(req(item, "file_id"), f"reads[{i}].file_id")
        at = timestamp(req(item, "last_read_at"), f"reads[{i}].last_read_at")
        if fid not in latest or at > latest[fid]:  # type: ignore[operator]
            latest[fid] = at
    if not latest:
        return Result(200, {"accepted": 0, "unknown_file_ids": []})
    ids = list(latest)
    times = [latest[i] for i in ids]
    async with ctx.svc.catalog.tx(ctx.edge.principal) as db:
        # Later of stored and reported; never in the future (a skewed edge clock).
        # Only the service-side clock changes: no feed event, and (migration 0005)
        # no entity_version bump, so the reconciliation digest is untouched.
        rows = await (
            await db.execute(
                """
                UPDATE catalog.file f
                   SET last_read_at = greatest(f.last_read_at, least(r.at, now()))
                  FROM unnest(%s::bigint[], %s::timestamptz[]) AS r(file_id, at),
                       catalog.dataset d
                 WHERE f.file_id = r.file_id AND d.dataset_id = f.dataset_id
                   AND d.edge_id = %s
                RETURNING f.file_id
                """,
                (ids, times, ctx.edge_id),
            )
        ).fetchall()
    known = {r["file_id"] for r in rows}
    unknown = [str(i) for i in ids if i not in known]
    return Result(200, {"accepted": len(known), "unknown_file_ids": unknown})


MAX_AUDIT = 5000
_ACTION = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,5}$")
_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
#: An edge's clock may run ahead a little; a record dated further ahead is refused.
AUDIT_SKEW = timedelta(minutes=10)


async def send_audit(ctx: Ctx) -> Result:
    """`POST audit` (control-api.md §4.7): the edge's batched records, stored in
    catalog.audit_log as actor `edge:<id>`. A record may name only this edge's
    files: another `file_id` is kept in `details.claimed_file_id`, never on the
    row, so an edge cannot write into another dataset's history. The dataset
    and share come from the file, never from the edge."""
    records = req(ctx.body, "records")
    if not isinstance(records, list) or len(records) > MAX_AUDIT:
        raise bad(f"records must be a list of at most {MAX_AUDIT}")
    latest = datetime.now(UTC) + AUDIT_SKEW
    rows: list[dict[str, Any]] = []
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            raise bad(f"records[{i}] must be an object")
        at = timestamp(req(r, "at"), f"records[{i}].at")
        if at > latest:
            raise bad(f"records[{i}].at is in the future")
        action = req_str(r, "action", max_len=64)
        if not _ACTION.match(action):
            raise bad(f"records[{i}].action must be dotted lower-case words")
        decision = req_enum(r, "decision", ("allow", "deny", "info"))
        sid = r.get("sid")
        if sid is not None and not (isinstance(sid, str) and _SID.match(sid)):
            raise bad(f"records[{i}].sid is not a SID")
        uid = r.get("uid")
        if uid is not None and (not isinstance(uid, int) or isinstance(uid, bool) or uid < 0):
            raise bad(f"records[{i}].uid must be a non-negative integer")
        proof = r.get("proof")
        if proof is not None and proof not in ("claimed", "proven"):
            raise bad(f"records[{i}].proof must be claimed or proven")
        trust = r.get("trust_level")
        if trust is not None and trust not in ("proven", "host_trusted"):
            raise bad(f"records[{i}].trust_level must be proven or host_trusted")
        fid = r.get("file_id")
        fid = None if fid is None else parse_id(fid, f"records[{i}].file_id")
        addr = r.get("client_addr")
        if addr is not None:
            try:
                addr = str(ipaddress.ip_address(addr))
            except (TypeError, ValueError):
                raise bad(f"records[{i}].client_addr is not an IP address") from None
        details = r.get("details", {})
        if not isinstance(details, dict):
            raise bad(f"records[{i}].details must be an object")
        details = dict(details)
        if uid is not None:
            details["uid"] = uid
        rows.append(
            {
                "at": at,
                "action": action,
                "decision": decision,
                "sid": sid,
                "proof": proof,
                "trust": trust,
                "file_id": fid,
                "addr": addr,
                "details": details,
            }
        )
    if not rows:
        return Result(204)
    async with ctx.svc.catalog.tx(ctx.edge.principal) as db:
        named = sorted({r["file_id"] for r in rows if r["file_id"] is not None})
        owned: dict[int, dict[str, Any]] = {}
        if named:
            for f in await (
                await db.execute(
                    "SELECT f.file_id, f.dataset_id,"
                    "       (SELECT s.share_id FROM catalog.share s"
                    "         WHERE s.dataset_id = f.dataset_id AND s.edge_id = d.edge_id"
                    "         ORDER BY s.share_id LIMIT 1) AS share_id"
                    "  FROM catalog.file f JOIN catalog.dataset d ON d.dataset_id = f.dataset_id"
                    " WHERE f.file_id = ANY(%s) AND d.edge_id = %s",
                    (named, ctx.edge_id),
                )
            ).fetchall():
                owned[f["file_id"]] = f
        actor = f"edge:{ctx.edge_id}"
        for r in rows:
            f = owned.get(r["file_id"]) if r["file_id"] is not None else None
            if r["file_id"] is not None and f is None:
                r["details"]["claimed_file_id"] = str(r["file_id"])
            await db.execute(
                "INSERT INTO catalog.audit_log (at, actor, actor_sid, proof, trust_level,"
                " action, decision, dataset_id, share_id, file_id, client_addr, details)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    r["at"],
                    actor,
                    r["sid"],
                    r["proof"],
                    r["trust"],
                    r["action"],
                    r["decision"],
                    f and f["dataset_id"],
                    f and f["share_id"],
                    f and f["file_id"],
                    r["addr"],
                    json.dumps(r["details"]),
                ),
            )
    return Result(204)
