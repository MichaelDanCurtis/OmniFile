"""Reads of non-local bytes, and recalls: control-api.md section 4.4.

`POST /recalls` answers `online` with a read credential when the bytes are
readable now, or starts / joins the one active recall for that (file, content
version) and answers with its state, ETA, the wait/fail decision and the note
text. A new recall is handed to the worker in the same transaction
(PGMQ `restores` {"recall_id"}, decision 27); the worker creates the provider
restore and moves the recall along, and the edge hears of it through
`recall.state` feed events.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import replace
from datetime import datetime

from ..policy.recall import decide_on_open, plan_recall
from ..worker.backends import capabilities_for
from .context import Ctx
from .data import owned_file, read_credential, readable_location
from .dataplane import Target
from .db import id_str
from .idempotency import Result
from .problems import ApiError
from .validate import bad, req, req_enum, req_id, req_int, req_str, rfc3339, sid

ACTIVE = ("requested", "rehydrating", "copying", "downloading")


def parse_requester(value) -> dict:
    if not isinstance(value, dict):
        raise bad("requester must be an object")
    out = {
        "via": req_enum(value, "via", ("nfs", "smb", "local")),
        # Advisory only: the service derives proof itself (derive_proof).
        "edge_proof": (
            req_enum(value, "proof", ("claimed", "proven")) if "proof" in value else None
        ),
        "uid": req_int(value, "uid", minimum=0) if "uid" in value else None,
        "sid": sid(value["sid"], "requester.sid") if value.get("sid") is not None else None,
        "sec": (
            req_enum(value, "sec", ("sys", "krb5", "krb5i", "krb5p", "ntlm", "kerberos"))
            if "sec" in value
            else None
        ),
        "client_addr": value.get("client_addr"),
    }
    if out["uid"] is None and out["sid"] is None:
        raise bad("requester needs a uid or a sid")
    if out["client_addr"] is not None and not isinstance(out["client_addr"], str):
        raise bad("requester.client_addr must be a string")
    return out


KERBEROS_NFS = ("krb5", "krb5i", "krb5p")
SMB_SEC = (None, "ntlm", "kerberos")


async def derive_proof(db, edge_id: int, dataset_id: int, who: dict) -> str:
    """`proven` or `claimed`, decided here and never taken from the edge
    (decision 28): the edge's word can only lower it.

    - NFS is proven only with a Kerberos flavour, and only if every NFS share
      of the dataset on this edge is Kerberos-only (`trust_level = proven`):
      on a share that also allows sec=sys any client can claim any uid.
    - SMB is proven only if every SMB share of the dataset on this edge is
      `proven` too (a host-trusted share holds nothing that needs proof).
    - Anything else (`local`, or a flavour that doesn't fit the protocol) is
      claimed.
    """
    via, sec = who["via"], who["sec"]
    if via == "nfs" and sec in KERBEROS_NFS:
        protocols = ["nfs3", "nfs4"]
    elif via == "smb" and sec in SMB_SEC:
        protocols = ["smb"]
    else:
        return "claimed"
    row = await (
        await db.execute(
            "SELECT count(*) AS n, bool_and(trust_level = 'proven') AS proven"
            "  FROM catalog.share"
            " WHERE dataset_id = %s AND edge_id = %s AND protocols && %s::text[]",
            (dataset_id, edge_id, protocols),
        )
    ).fetchone()
    if not row or not row["n"] or not row["proven"]:
        return "claimed"
    if who["edge_proof"] == "claimed":
        return "claimed"
    return "proven"


_DOMAIN_SID = re.compile(r"^(S-1-5-21-[0-9]+-[0-9]+-[0-9]+)-([0-9]+)$")


async def check_new_sid(db, ctx: Ctx, sid_value: str) -> str | None:
    """None if an unseen requester SID may become a catalog identity, else
    the reason it may not. A SID is accepted only if it is a domain account
    (S-1-5-21-a-b-c-RID, RID 500 or >= 1000), its domain is one this edge's
    shares already use (their group SIDs) or the directory has resolved, and
    the edge is within its rate of new SIDs (`Services.new_sids`)."""
    m = _DOMAIN_SID.match(sid_value)
    if m is None:
        return "not_a_domain_account"
    rid = int(m[2])
    if rid != 500 and rid < 1000:
        return "well_known_rid"
    prefix = m[1] + "-"
    known = await (
        await db.execute(
            """
            SELECT EXISTS (
                     SELECT 1 FROM catalog.share s
                      WHERE s.edge_id = %(e)s
                        AND (starts_with(s.owners_group_sid, %(p)s)
                             OR starts_with(s.writers_group_sid, %(p)s)
                             OR starts_with(s.readers_group_sid, %(p)s)))
                OR EXISTS (
                     SELECT 1 FROM catalog.identity i
                      WHERE i.winbind_resolved_at IS NOT NULL AND starts_with(i.sid, %(p)s))
                   AS ok
            """,
            {"e": ctx.edge_id, "p": prefix},
        )
    ).fetchone()
    if not known["ok"]:
        return "unknown_domain"
    if not ctx.svc.new_sids.allow(ctx.edge_id):
        return "rate_limited"
    return None


async def online_location(db, file_id: int, version_id: int) -> dict | None:
    """Where the bytes can be read right now, or None if a restore is needed."""
    loc = await readable_location(db, file_id, version_id)
    if (loc["logical_tier"] or "hot") != "archive":
        return loc
    restored = await (
        await db.execute(
            """
            SELECT coalesce(t.location_id, l.location_id) AS location_id
              FROM catalog.restore r
              JOIN catalog.location l ON l.location_id = r.location_id
              LEFT JOIN catalog.location t ON t.location_id = r.target_location_id
                   AND t.state = 'committed'
             WHERE r.location_id = %s AND r.state = 'completed'
               AND (r.expires_at IS NULL OR r.expires_at > now())
             ORDER BY r.requested_at DESC LIMIT 1
            """,
            (loc["location_id"],),
        )
    ).fetchone()
    if restored is None:
        return None
    return await (
        await db.execute(
            "SELECT l.*, b.kind, b.config FROM catalog.location l"
            " JOIN catalog.backend b ON b.backend_id = l.backend_id WHERE l.location_id = %s",
            (restored["location_id"],),
        )
    ).fetchone()


def promise(
    kind: str,
    config: dict,
    size: int,
    now: datetime,
    priority: str = "standard",
    native_class: str | None = None,
) -> tuple[datetime | None, str]:
    """The ETA a new recall is given, and how sure it is.

    From the backend's `capabilities()` through `policy.recall.plan_recall`:
    the restore option's stated upper bound (Azure Standard "up to 15 h"; S3
    by class), and no time at all above the option's no-promise size (10 GB
    on Azure, AGENTS.md). The worker replaces it with the provider's own ETA
    once the restore has started."""
    try:
        caps = capabilities_for(kind, config or {})
    except (ValueError, KeyError, TypeError):
        return None, "none"
    if native_class:
        # The options that apply to this object's class (S3 Deep Archive's
        # Standard is 12 h, Flexible Retrieval's 3-5 h).
        fits = [o for o in caps.restore_options if native_class in o.from_classes]
        if fits:
            caps = replace(caps, restore_options=fits)
    plan = plan_recall(caps, size, priority, now)
    return plan.eta, ("provider_sla" if plan.eta is not None else "none")


NOTE_NAME_MAX = 255  # characters of the file name shown in the note


def note_safe_name(name: str) -> str:
    """The file name as it may appear in the note: control characters (C0,
    DEL, C1, and Unicode line/paragraph separators and bidi controls) become
    `\\xNN` / `\\uNNNN`, and the result is capped at `NOTE_NAME_MAX`
    characters. NFS names can hold anything but `/` and NUL."""
    out: list[str] = []
    for ch in name:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F:
            out.append(f"\\x{o:02x}")
        elif o in (0x2028, 0x2029) or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    safe = "".join(out)
    if len(safe) > NOTE_NAME_MAX:
        safe = safe[: NOTE_NAME_MAX - 1] + "\u2026"
    return safe


def note_text(name: str, eta: datetime | None, decision: str) -> str:
    lines = [f"{note_safe_name(name)} is archived. A recall has been requested."]
    if eta is not None:
        lines.append(f"Expected to be ready by {eta.strftime('%Y-%m-%d %H:%M')} UTC.")
    else:
        # Above 10 GB, or once a recall has been sent again after provider errors.
        lines.append("No recall time is promised.")
    if decision == "wait":
        lines.append("Programs reading it will wait until it is back.")
    else:
        lines.append("Reads fail until it is back; open it again then.")
    lines.append("You will get a Teams message when it is ready.")
    return "\n".join(lines) + "\n"


async def request_read(ctx: Ctx) -> Result:
    b = ctx.body
    file_id = req_id(b, "file_id")
    version_id = req_id(b, "version_id")
    on_open = req_enum(b, "on_open", ("wait", "fail", "auto"))
    who = parse_requester(req(b, "requester"))
    for f_ in ("offset", "length"):
        if f_ in b:
            req_int(b, f_, minimum=0)
    # The reader asked for this version itself (a rewind placeholder: `.snapshots`,
    # Previous Versions), not for whatever the file holds: its recall outlives the
    # file being replaced or deleted (migration 0059).
    pinned = b.get("pinned", False)
    if not isinstance(pinned, bool):
        raise bad("pinned must be true or false")

    async with ctx.svc.catalog.read() as db:
        f = await owned_file(db, ctx.edge_id, file_id)
        loc = await online_location(db, file_id, version_id)
        primary = await readable_location(db, file_id, version_id)
        size_row = await (
            await db.execute(
                "SELECT size FROM catalog.file_version WHERE version_id = %s", (version_id,)
            )
        ).fetchone()
    if loc is not None:
        plane = ctx.svc.planes.get(loc["backend_id"], loc["kind"], loc["config"] or {})
        target = Target(
            loc["backend_id"], loc["container"], loc["object_key"], loc["backend_version_id"]
        )
        cred = await read_credential(ctx, loc, plane, target)
        return Result(200, {"status": "online", "credential": cred})

    size = (size_row or {}).get("size") or f["size"]
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        now = (await (await db.execute("SELECT now() AS t")).fetchone())["t"]
        eta, confidence = promise(
            primary["kind"],
            primary["config"] or {},
            size,
            now,
            native_class=primary.get("storage_class"),
        )
        created = await (
            await db.execute(
                "INSERT INTO catalog.recall (file_id, version_id, edge_id, eta, pinned)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (file_id, version_id)"
                "   WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')"
                " DO NOTHING RETURNING recall_id, state, eta",
                (file_id, version_id, ctx.edge_id, eta, pinned),
            )
        ).fetchone()
        joined = created is None
        if joined:
            recall = await (
                await db.execute(
                    "SELECT recall_id, state, eta FROM catalog.recall"
                    " WHERE file_id = %s AND version_id = %s AND state = ANY(%s)",
                    (file_id, version_id, list(ACTIVE)),
                )
            ).fetchone()
            if recall is None:  # finished between the insert and the select
                raise ApiError("request_in_progress", "retry", retry_after_s=1)
            if recall["eta"] is None:
                confidence = "none"
            if pinned:
                await pin(db, recall["recall_id"])
        else:
            recall = created
            await db.execute(
                "SELECT pgmq.send('restores', %s::jsonb)",
                (json.dumps({"recall_id": str(recall["recall_id"])}),),
            )
        who["proof"] = await derive_proof(db, ctx.edge_id, f["dataset_id"], who)
        await add_requester(db, ctx, recall["recall_id"], who, f, version_id)
        decision = await decide(db, ctx, f["dataset_id"], on_open, recall["eta"], now)
    return Result(
        200,
        {
            "status": "recall",
            "recall_id": id_str(recall["recall_id"]),
            "state": recall["state"],
            "joined": joined,
            "eta": rfc3339(recall["eta"]),
            "eta_confidence": confidence,
            "decision": decision,
            "note": note_text(f["name"], recall["eta"], decision),
        },
    )


async def pin(db, recall_id: int) -> None:
    """Someone who joined this recall asked for its version itself: it is no
    longer ended when the file is replaced or deleted."""
    await db.execute(
        "UPDATE catalog.recall SET pinned = true WHERE recall_id = %s AND NOT pinned",
        (recall_id,),
    )


async def resolve_identity(db, ctx: Ctx, who: dict) -> tuple[int | None, str | None]:
    """The catalog identity of a requester or actor (`who`: sid, uid), and
    why its SID was refused, if it was. A SID the identity cache hasn't seen
    is added, if it passes check_new_sid; the worker resolves it (winbind,
    Graph) later, for the Teams card. One that fails gets no identity. With
    no SID, the uid's identity (`posix_id`), if the cache has one."""
    ident = None
    sid_rejected = None
    if who["sid"] is not None:
        ident = await (
            await db.execute(
                "SELECT identity_id FROM catalog.identity WHERE sid = %s", (who["sid"],)
            )
        ).fetchone()
        if ident is None:
            sid_rejected = await check_new_sid(db, ctx, who["sid"])
            if sid_rejected is None:
                ident = await (
                    await db.execute(
                        "INSERT INTO catalog.identity (sid) VALUES (%s)"
                        " ON CONFLICT (sid) DO UPDATE SET sid = EXCLUDED.sid"
                        " RETURNING identity_id",
                        (who["sid"],),
                    )
                ).fetchone()
    if ident is None and who["uid"] is not None and sid_rejected is None:
        ident = await (
            await db.execute(
                "SELECT identity_id FROM catalog.identity WHERE kind = 'user' AND posix_id = %s",
                (who["uid"],),
            )
        ).fetchone()
    return (ident["identity_id"] if ident else None), sid_rejected


async def add_requester(db, ctx: Ctx, recall_id: int, who: dict, f: dict, version_id: int) -> None:
    identity_id, sid_rejected = await resolve_identity(db, ctx, who)
    added = None
    if identity_id is not None or who["uid"] is not None:
        added = await (
            await db.execute(
                "INSERT INTO catalog.recall_requester"
                " (recall_id, identity_id, uid, via, proof, sec_flavour, client_addr)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s::inet)"
                " ON CONFLICT DO NOTHING RETURNING requester_id",
                (
                    recall_id,
                    identity_id,
                    who["uid"],
                    who["via"],
                    who["proof"],
                    who["sec"],
                    _inet(who["client_addr"]),
                ),
            )
        ).fetchone()
    if who["uid"] is not None:
        await db.execute(
            "INSERT INTO catalog.identity_observation (edge_id, uid, proof, identity_id)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (edge_id, uid, proof) DO UPDATE SET last_seen_at = now(),"
            "   identity_id = coalesce(EXCLUDED.identity_id,"
            "                          catalog.identity_observation.identity_id)",
            (ctx.edge_id, who["uid"], who["proof"], identity_id),
        )
    if added is not None and sid_rejected is None:
        await db.execute(
            "SELECT pgmq.send('notify', %s::jsonb)", (json.dumps({"recall_id": str(recall_id)}),)
        )
    details = {"recall_id": str(recall_id), "uid": who["uid"], "via": who["via"]}
    if who["edge_proof"] is not None and who["edge_proof"] != who["proof"]:
        details["edge_proof"] = who["edge_proof"]
    if sid_rejected is not None:
        details["sid_rejected"] = sid_rejected
    await db.execute(
        "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision, dataset_id,"
        " file_id, version_id, client_addr, details)"
        " VALUES (%s, %s, %s, 'recall.request', 'allow', %s, %s, %s, %s::inet, %s::jsonb)",
        (
            ctx.edge.principal,
            who["sid"],
            who["proof"],
            f["dataset_id"],
            f["file_id"],
            version_id,
            _inet(who["client_addr"]),
            json.dumps(details),
        ),
    )


def _inet(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


async def decide(db, ctx: Ctx, dataset_id: int, on_open: str, eta, now) -> str:
    """The share's open policy against the recall's ETA
    (`policy.recall.decide_on_open`): wait, fail, or (auto, the FSAL's
    waitquick) wait only if a time is promised within the share's threshold.
    No ETA (above 10 GB) under auto means fail."""
    threshold = None
    if on_open == "auto":
        row = await (
            await db.execute(
                "SELECT max(wait_threshold_minutes) AS m FROM catalog.share"
                " WHERE dataset_id = %s AND edge_id = %s AND on_open = 'auto'",
                (dataset_id, ctx.edge_id),
            )
        ).fetchone()
        threshold = row["m"] if row else None
    return decide_on_open(on_open, threshold, eta, now)


async def recall_filled(ctx: Ctx) -> Result:
    recall_id = ctx.path_id("recall_id")
    version_id = req_id(ctx.body, "version_id")
    resident = req_int(ctx.body, "resident_bytes", minimum=0)
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        r = await (
            await db.execute(
                "SELECT r.*, f.dataset_id FROM catalog.recall r"
                " JOIN catalog.file f ON f.file_id = r.file_id"
                " JOIN catalog.dataset d ON d.dataset_id = f.dataset_id"
                " WHERE r.recall_id = %s AND d.edge_id = %s FOR UPDATE OF r",
                (recall_id, ctx.edge_id),
            )
        ).fetchone()
        if r is None:
            raise ApiError("not_found", f"no recall {recall_id} on this edge")
        if r["version_id"] != version_id:
            raise ApiError("version_conflict", f"recall {recall_id} is for another version")
        if r["state"] == "available":
            return Result(204)  # idempotent by recall_id
        # A recall the edge reported failed whose bytes then arrived (a
        # later fill finished, or the failure report was delayed behind it):
        # the file is resident, so the recall is available after all, and
        # the catalog must not stay `stub`.
        # The same for one the worker failed because its edge went quiet
        # (worker/recall_timeout.py): the edge came back with the bytes.
        edge_failed = r["state"] == "failed" and (r["failure"] or "").startswith(
            ("edge fill failed:", "edge unreachable:", "edge not filling:")
        )
        if r["state"] not in ACTIVE and not edge_failed:
            raise ApiError("version_conflict", f"recall {recall_id} is {r['state']}")
        await db.execute(
            "UPDATE catalog.recall SET state = 'available', completed_at = now(),"
            " failure = NULL, updated_at = now() WHERE recall_id = %s",
            (recall_id,),
        )
        await db.execute(
            "UPDATE catalog.file SET"
            " resident_bytes = least(%(n)s, size),"
            " state = CASE WHEN %(n)s >= size THEN 'resident' ELSE 'stub' END"
            " WHERE file_id = %(f)s AND content_version_id = %(v)s"
            "   AND state IN ('stub', 'recalling', 'remote')",
            {"n": resident, "f": r["file_id"], "v": version_id},
        )
        await db.execute(
            "SELECT pgmq.send('notify', %s::jsonb)", (json.dumps({"recall_id": str(recall_id)}),)
        )
    return Result(204)


async def recall_failed(ctx: Ctx) -> Result:
    """The edge could not fill a `downloading` recall (its disk full, the
    placeholder gone or replaced, the bytes failing their CRC): the recall
    becomes `failed`, with the edge's reason, and users are told. A reader
    who opens the file again starts a new recall. Before this, a fill that
    failed left the recall `downloading` for good, and every later read
    joined it and got EIO (WP3's chaos soak).

    A recall that ended `superseded` (its file was replaced or deleted,
    migration 0059) stays so: the fill found another file under the name,
    which is no failure."""
    recall_id = ctx.path_id("recall_id")
    version_id = req_id(ctx.body, "version_id")
    reason = req_str(ctx.body, "reason", max_len=500)
    async with ctx.svc.catalog.tx(ctx.edge.principal, origin_edge_id=ctx.edge_id) as db:
        r = await (
            await db.execute(
                "SELECT r.*, f.dataset_id FROM catalog.recall r"
                " JOIN catalog.file f ON f.file_id = r.file_id"
                " JOIN catalog.dataset d ON d.dataset_id = f.dataset_id"
                " WHERE r.recall_id = %s AND d.edge_id = %s FOR UPDATE OF r",
                (recall_id, ctx.edge_id),
            )
        ).fetchone()
        if r is None:
            raise ApiError("not_found", f"no recall {recall_id} on this edge")
        if r["version_id"] != version_id:
            raise ApiError("version_conflict", f"recall {recall_id} is for another version")
        if r["state"] in ("failed", "cancelled", "superseded"):
            return Result(204)  # idempotent by recall_id
        if r["state"] != "downloading":
            raise ApiError("version_conflict", f"recall {recall_id} is {r['state']}")
        await db.execute(
            "UPDATE catalog.recall SET state = 'failed', failure = %s, completed_at = now(),"
            " updated_at = now() WHERE recall_id = %s",
            (f"edge fill failed: {reason}", recall_id),
        )
        await db.execute(
            "SELECT pgmq.send('notify', %s::jsonb)", (json.dumps({"recall_id": str(recall_id)}),)
        )
    return Result(204)
