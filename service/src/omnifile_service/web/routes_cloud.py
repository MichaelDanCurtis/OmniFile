"""Connect Azure, Connect AWS, Storage Mover deployment and source scans
(docs/design/cloud-connect.md; portal-api.md 5.20). ArchiveAdmin, signed in,
never an app. Every change is audited.

The work is in `settings.azure_connect`, `settings.aws_connect`,
`settings.cloud_tasks` (run by the worker, `settings.cloud_jobs`) and
`migrate.scan`; `web.azure_connect` holds the delegated sign-in.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.responses import Response

from ..api.validate import parse_id
from ..migrate import scan as scanning
from ..settings import aws_connect, cloud_tasks, mover
from ..settings import azure_connect as az
from . import azure_connect as signin
from .context import Ctx, WebResult, audit
from .problems import WebError, invalid, not_found
from .routes_settings import admin_only, identity_of

_LOCATION = re.compile(r"^[a-z0-9]{3,30}$")
_MOVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,62}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _env(ctx: Ctx) -> dict[str, str]:
    return ctx.svc.settings_env


def _fields(body: dict[str, Any], allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        raise invalid(sorted(extra)[0], "is not a field")


def _precondition(message: str) -> WebError:
    return WebError("dependency_failed", message)


async def _azure(ctx: Ctx) -> dict[str, Any]:
    async with ctx.svc.catalog.read() as db:
        return await az.settings_json(db, _env(ctx), ctx.svc.settings.tenant_id, ctx.now)


# --- Connect Azure -------------------------------------------------------------------------


async def get_azure(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    return WebResult(200, await _azure(ctx))


async def azure_sign_in(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    identity_of(who)
    b = ctx.body
    _fields(b, {"purpose", "grant_ids"})
    purpose = b.get("purpose")
    if purpose not in ("list", "grant", "remove"):
        raise invalid("purpose", "list, grant or remove")
    items: list[dict[str, Any]] = []
    if purpose == "grant":
        s = await _azure(ctx)
        if not s["plan"]:
            raise _precondition("choose a subscription and resource groups first")
        missing = [i for i in s["plan"] if i["state"] == "missing_identity"]
        if missing:
            raise _precondition(
                "the deployment doesn't name the service identities"
                " (OMNIFILE_SERVICE_PRINCIPAL_IDS: web=…,worker=…)"
            )
        items = [{k: i[k] for k in ("key", "purpose", "role_name", "role_definition_id", "scope",
                                    "principal", "principal_id", "actions")}
                 for i in s["plan"]]  # fmt: skip
    elif purpose == "remove":
        raw = b.get("grant_ids")
        if not isinstance(raw, list) or not raw or len(raw) > 50:
            raise invalid("grant_ids", "one to 50 grant ids")
        ids = [parse_id(str(g), "grant_ids") for g in raw]
        async with ctx.svc.catalog.read() as db:
            active = {g["grant_id"] for g in await az.active_grants(db)}
        if any(i not in active for i in ids):
            raise not_found("active grant")
        items = [{"grant_id": str(i)} for i in ids]
    elif b.get("grant_ids") is not None:
        raise invalid("grant_ids", "only for remove")
    op, url, cookie = await signin.start(ctx.svc, who, purpose, items, ctx.now)
    from ..api.validate import rfc3339

    return WebResult(
        201,
        {"op_id": str(op["op_id"]), "purpose": purpose, "authorize_url": url,
         "expires_at": rfc3339(op["expires_at"])},
        headers={"set-cookie": cookie},
    )  # fmt: skip


async def put_azure_choice(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    b = ctx.body
    _fields(b, {"subscription_id", "resource_groups", "mover_resource_group"})
    if not isinstance(b.get("resource_groups"), list):
        raise invalid("resource_groups", "a list of resource group names")
    tenant = ctx.svc.settings.tenant_id
    async with ctx.svc.catalog.tx(who.actor) as db:
        c = await az.connection(db)
        try:
            choice = az.check_choice(b, (c or {}).get("listing"), tenant)
        except az.ConnectError as e:
            raise WebError("invalid_request", str(e)) from None
        await az.upsert(db, tenant, choice=choice, chosen_at=ctx.now, chosen_by=me)
        await audit(db, who, "settings.azure_choice", details=choice)
    return WebResult(200, await _azure(ctx))


async def discover_azure(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    identity_of(who)
    _fields(ctx.body, set())
    if ctx.svc.probes is None:
        raise _precondition(
            "live checks are off (OMNIFILE_SETTINGS_PROBES), so the service can't read Azure"
        )
    async with ctx.svc.catalog.read() as db:
        grants = await az.active_grants(db)
    scopes = az.granted_scopes(grants)
    if not scopes:
        raise _precondition("grant the service its Discovery Reader roles first")
    found = await az.discover(
        ctx.svc.probes, scopes, ctx.now, service_vnet_id=_env(ctx).get("OMNIFILE_SERVICE_VNET_ID")
    )
    async with ctx.svc.catalog.tx(who.actor) as db:
        await az.upsert(db, ctx.svc.settings.tenant_id, discovered=found, discovered_at=ctx.now)
        await audit(
            db, who, "settings.azure_discover",
            details={"scopes": scopes, "storage_accounts": len(found["storage_accounts"]),
                     "errors": found["errors"]},
        )  # fmt: skip
    return WebResult(200, await _azure(ctx))


async def confirm_azure(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    b = ctx.body
    _fields(b, {"storage_accounts", "virtual_networks", "private_endpoints"})
    async with ctx.svc.catalog.tx(who.actor) as db:
        c = await az.connection(db)
        found = (c or {}).get("discovered")
        if not found:
            raise _precondition("discover first")
        known = {
            "storage_accounts": {a["id"]: a for a in found["storage_accounts"]},
            "virtual_networks": {v["id"]: v for v in found["virtual_networks"]},
            "private_endpoints": {p["id"]: p for p in found["private_endpoints"]},
        }
        confirmed: dict[str, list[str]] = {}
        for field, table in known.items():
            ids = b.get(field) or []
            if not isinstance(ids, list):
                raise invalid(field, "a list of resource ids")
            for i in ids:
                if i not in table:
                    raise invalid(field, f"{i} wasn't discovered")
                if field == "storage_accounts" and not table[i]["private"]:
                    raise invalid(
                        field,
                        f"{table[i]['name']} isn't reachable privately (decision 22): give it a"
                        " blob private endpoint and turn public access off first",
                    )
            confirmed[field] = list(dict.fromkeys(ids))
        await az.upsert(
            db, ctx.svc.settings.tenant_id, confirmed=confirmed, confirmed_at=ctx.now,
            confirmed_by=me,
        )  # fmt: skip
        await audit(db, who, "settings.azure_confirm", details=confirmed)
    return WebResult(200, await _azure(ctx))


# --- Connect AWS ---------------------------------------------------------------------------


async def _connections(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT c.*, coalesce(i.display_name, i.upn) AS by_name"
                "  FROM catalog.aws_connection c"
                "  LEFT JOIN catalog.identity i ON i.identity_id = c.confirmed_by"
                " ORDER BY c.confirmed_at DESC LIMIT 20"
            )
        ).fetchall()
    return [aws_connect.connection_json(r, _env(ctx), r["by_name"]) for r in rows]


async def get_aws_connect(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    region = ctx.query("region")
    if region is not None and not aws_connect.REGION.match(region):
        raise invalid("region", "an AWS region, e.g. eu-west-1")
    dep = ctx.query("deployment_id")
    if dep is not None and not aws_connect.DEPLOYMENT_ID.match(dep):
        raise invalid("deployment_id", "2 to 20 lowercase letters, digits and -")
    return WebResult(
        200,
        aws_connect.connect_json(
            _env(ctx), ctx.svc.settings.tenant_id, region, dep, await _connections(ctx)
        ),
    )


async def get_aws_template(ctx: Ctx) -> Response:
    admin_only(ctx.who)
    return Response(
        aws_connect.template_text(),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="omnifile-connect.yaml"'},
    )


def _account_fields(b: dict[str, Any]) -> tuple[str, str, str]:
    a, r, d = b.get("account_id"), b.get("region"), b.get("deployment_id")
    if not isinstance(a, str) or not aws_connect.ACCOUNT.match(a):
        raise invalid("account_id", "the 12-digit AWS account id")
    if not isinstance(r, str) or not aws_connect.REGION.match(r):
        raise invalid("region", "an AWS region, e.g. eu-west-1")
    if not isinstance(d, str) or not aws_connect.DEPLOYMENT_ID.match(d):
        raise invalid("deployment_id", "2 to 20 lowercase letters, digits and -")
    return a, r, d


async def fetch_aws(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    b = ctx.body
    _fields(b, {"account_id", "region", "deployment_id", "endpoints"})
    account, region, dep = _account_fields(b)
    eps = b.get("endpoints") or {}
    if not isinstance(eps, dict) or set(eps) - {"s3", "sts", "sqs"}:
        raise invalid("endpoints", "s3, sts and sqs only")
    params: dict[str, Any] = {"account_id": account, "region": region, "deployment_id": dep}
    clean = {k: v.strip() for k, v in eps.items() if isinstance(v, str) and v.strip()}
    if clean.get("s3"):
        clean["s3_control"] = clean["s3"].replace("://bucket.", "://control.", 1)
    if clean:
        params["endpoints"] = clean
    # The same rules as the operator's settings, before the worker sends anything.
    from ..settings.cloud_jobs import Failed, candidate

    try:
        candidate(params, _env(ctx))
    except Failed as e:
        raise WebError("invalid_request", str(e)) from None
    async with ctx.svc.catalog.tx(who.actor) as db:
        t = await cloud_tasks.queue_task(db, "aws_fetch", me, params, ctx.now)
        await audit(db, who, "settings.aws_fetch", details={"task_id": str(t["task_id"]), **params})
    return WebResult(202, cloud_tasks.task_json(t))


async def parse_aws_outputs(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    b = ctx.body
    _fields(b, {"text"})
    text = b.get("text")
    if not isinstance(text, str) or len(text) > 20000:
        raise invalid("text", "the stack's outputs, at most 20,000 characters")
    outputs = aws_connect.parse_outputs(text)
    entry, problems = aws_connect.entry_from(None, None, None, outputs, _env(ctx))
    buckets = [x for x in (outputs.get("SourceBuckets") or "").split(",") if _BUCKET.match(x)]
    return WebResult(
        200,
        {"account_id": outputs.get("AccountId"), "region": outputs.get("Region"),
         "deployment_id": outputs.get("DeploymentId"), "outputs": outputs, "entry": entry,
         "buckets": buckets, "problems": problems},
    )  # fmt: skip


async def confirm_aws(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    b = ctx.body
    _fields(b, {"account_id", "region", "deployment_id", "outputs", "buckets"})
    account, region, dep = _account_fields(b)
    outputs = b.get("outputs")
    if not isinstance(outputs, dict) or not all(isinstance(v, str) for v in outputs.values()):
        raise invalid("outputs", "the stack's outputs, as strings")
    outputs = {k: v for k, v in outputs.items() if k in aws_connect.OUTPUTS}
    buckets = b.get("buckets")
    if not isinstance(buckets, list) or not all(
        isinstance(x, str) and _BUCKET.match(x) for x in buckets
    ):
        raise invalid("buckets", "S3 bucket names")
    entry, problems = aws_connect.entry_from(account, region, dep, outputs, _env(ctx))
    if entry is None or problems:
        raise WebError("invalid_request", "; ".join(problems) or "no settings entry")
    from psycopg.types.json import Jsonb

    async with ctx.svc.catalog.tx(who.actor) as db:
        row = await (
            await db.execute(
                "INSERT INTO catalog.aws_connection (account_id, region, deployment_id, entry,"
                " outputs, buckets, confirmed_by, confirmed_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (account_id) DO UPDATE SET region = EXCLUDED.region,"
                " deployment_id = EXCLUDED.deployment_id, entry = EXCLUDED.entry,"
                " outputs = EXCLUDED.outputs, buckets = EXCLUDED.buckets,"
                " confirmed_by = EXCLUDED.confirmed_by, confirmed_at = EXCLUDED.confirmed_at"
                " RETURNING *",
                (account, region, dep, Jsonb(entry), Jsonb(outputs), sorted(set(buckets)), me,
                 ctx.now),
            )
        ).fetchone()  # fmt: skip
        await audit(
            db, who, "settings.aws_confirm",
            details={"account_id": account, "region": region, "deployment_id": dep,
                     "entry": entry, "buckets": sorted(set(buckets))},
        )  # fmt: skip
    return WebResult(200, aws_connect.connection_json(row, _env(ctx), who.display_name))


# --- Storage Mover deployment --------------------------------------------------------------


async def mover_extras(ctx: Ctx) -> dict[str, Any]:
    """`deployment`, `can_deploy` and `agent_steps` for GET /v1/settings/storage-mover."""
    async with ctx.svc.catalog.read() as db:
        grants = await az.active_grants(db)
        t = await cloud_tasks.latest(db, "mover_deploy")
    scope = az.mover_scope(grants)
    can = {"ok": False, "reason": None, "subscription_id": None, "resource_group": None}
    if scope is None:
        can["reason"] = (
            "Connect Azure first, with a resource group for Storage Mover: the worker needs"
            " its Mover Deployer grant there."
        )
    else:
        parts = scope.split("/")
        can.update(ok=True, subscription_id=parts[2], resource_group=parts[4])
    dep = None
    if t is not None:
        p = dict(t["params"] or {})
        from ..api.validate import rfc3339
        from ..settings.cloud_jobs import mover_config_value

        dep = {
            "task_id": str(t["task_id"]),
            "state": t["state"],
            "detail": t["detail"],
            "subscription_id": p["subscription_id"],
            "resource_group": p["resource_group"],
            "name": p["name"],
            "project": p["project"],
            "location": p["location"],
            "created_at": rfc3339(t["created_at"]),
            "finished_at": rfc3339(t["finished_at"]),
            "config_value": mover_config_value(p),
            "registration": {
                "tenant_id": ctx.svc.settings.tenant_id,
                "subscription_id": p["subscription_id"],
                "resource_group": p["resource_group"],
                "storage_mover": p["name"],
            },
        }
    return {"deployment": dep, "can_deploy": can, "agent_steps": mover.AGENT_STEPS}


async def deploy_mover(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    b = ctx.body
    _fields(b, {"name", "project", "location"})
    for f, rx in (("name", _MOVER_NAME), ("project", _MOVER_NAME), ("location", _LOCATION)):
        if not isinstance(b.get(f), str) or not rx.match(b[f]):
            raise invalid(f, "letters, digits, - and _ (3 to 63)" if f != "location"
                          else "an Azure region name, e.g. westeurope")  # fmt: skip
    extras = await mover_extras(ctx)
    can = extras["can_deploy"]
    if not can["ok"]:
        raise _precondition(can["reason"])
    params = {"subscription_id": can["subscription_id"], "resource_group": can["resource_group"],
              "name": b["name"], "project": b["project"], "location": b["location"]}  # fmt: skip
    async with ctx.svc.catalog.tx(who.actor) as db:
        t = await cloud_tasks.queue_task(db, "mover_deploy", me, params, ctx.now)
        await audit(db, who, "settings.mover_deploy",
                    details={"task_id": str(t["task_id"]), **params})  # fmt: skip
    return WebResult(202, cloud_tasks.task_json(t))


async def get_task(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    tid = ctx.path_id("task_id")
    async with ctx.svc.catalog.read() as db:
        t = await cloud_tasks.get_task(db, tid)
    if t is None or t["kind"] == "source_scan":
        raise not_found("task")
    return WebResult(200, cloud_tasks.task_json(t))


# --- source scans --------------------------------------------------------------------------


def scan_json(t: dict[str, Any]) -> dict[str, Any]:
    from ..api.validate import rfc3339

    p = dict(t["params"] or {})
    prog = dict(t["progress"] or {})
    res = dict(t["result"] or {})
    return {
        "scan_id": str(t["task_id"]),
        "state": t["state"],
        "detail": t["detail"],
        "backend_id": str(p["backend_id"]),
        "bucket": p["bucket"],
        "prefix": p.get("prefix") or "",
        "reader": (p.get("inventory") or {}).get("kind") or "listing",
        "caps": scanning.caps_of(p),
        "progress": {
            "objects": int(prog.get("objects") or 0),
            "bytes": int(prog.get("bytes") or 0),
            "batches": int(prog.get("batches") or 0),
            "elapsed_s": float(prog.get("elapsed_s") or 0),
        },
        "result": {k: v for k, v in res.items() if k != "list_cost_usd"} or None,
        "created_at": rfc3339(t["created_at"]),
        "finished_at": rfc3339(t["finished_at"]),
    }


async def start_scan(ctx: Ctx) -> WebResult:
    who = ctx.who
    admin_only(who)
    me = identity_of(who)
    b = ctx.body
    _fields(b, {"backend_id", "bucket", "prefix", "inventory", "max_objects", "max_seconds"})
    bid = parse_id(str(b.get("backend_id") or ""), "backend_id")
    bucket = b.get("bucket")
    if not isinstance(bucket, str) or not _BUCKET.match(bucket):
        raise invalid("bucket", "an S3 bucket name")
    prefix = b.get("prefix")
    if not isinstance(prefix, str) or len(prefix) > 1024 or (prefix and not prefix.endswith("/")):
        raise invalid("prefix", "empty, or folders each ending in '/'")
    inv = b.get("inventory")
    if inv is not None:
        if not isinstance(inv, dict) or inv.get("kind") not in ("s3_inventory", "listing"):
            raise invalid("inventory", "s3_inventory or listing")
        if inv["kind"] == "listing":
            inv = None
        else:
            if set(inv) - {"kind", "bucket", "manifest_key"} or not all(
                isinstance(inv.get(k), str) and inv[k] for k in ("bucket", "manifest_key")
            ):
                raise invalid("inventory", "kind, bucket and manifest_key")
    caps = {}
    for f, lo, hi in (("max_objects", 1000, 10_000_000), ("max_seconds", 10, 1800)):
        v = b.get(f)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise invalid(f, f"{lo} to {hi}")
        caps[f] = v
    async with ctx.svc.catalog.tx(who.actor) as db:
        row = await (
            await db.execute(
                "SELECT kind, config FROM catalog.backend WHERE backend_id = %s", (bid,)
            )
        ).fetchone()
        if row is None:
            raise not_found("backend")
        if row["kind"] not in ("s3", "sim"):
            raise invalid("backend_id", "a scan reads an S3 backend")
        buckets = (row["config"] or {}).get("buckets")
        if isinstance(buckets, list) and buckets and bucket not in buckets:
            raise invalid("bucket", f"backend {bid} isn't set up for bucket {bucket}")
        params = {"backend_id": bid, "bucket": bucket, "prefix": prefix, "inventory": inv, **caps}
        t = await cloud_tasks.queue_task(db, "source_scan", me, params, ctx.now)
        await audit(db, who, "source.scan", details={"scan_id": str(t["task_id"]), **params})
    return WebResult(202, scan_json(t))


async def get_scan(ctx: Ctx) -> WebResult:
    admin_only(ctx.who)
    sid = ctx.path_id("scan_id")
    async with ctx.svc.catalog.read() as db:
        t = await cloud_tasks.get_task(db, sid, "source_scan")
    if t is None:
        raise not_found("scan")
    return WebResult(200, scan_json(t))
