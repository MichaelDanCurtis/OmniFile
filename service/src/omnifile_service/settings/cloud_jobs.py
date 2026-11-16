"""The worker's side of `cloud_tasks.py`: the ``cloud_task`` queue.

- ``aws_fetch`` (cloud-connect.md 2): AssumeRoleWithWebIdentity for the new
  stack's role with the worker's own web identity, then reads only: the
  buckets (ListBuckets, and per bucket its location, versioning and whether
  EventBridge is on), the event queue's attributes, and without private
  networking the stack's outputs. Credentials are never recorded. The
  account's settings entry is built from what it found (`aws_connect`).
- ``mover_deploy`` (section 3): PUT the Storage Mover and its project with
  the worker's managed identity, which Connect Azure gave the Mover Deployer
  role on that resource group, and read them back.
- ``source_scan`` (section 4): one batch of the source per message
  (`migrate.scan`), each queueing the next until done or capped.

Each task ends ``done``, ``capped`` or ``failed`` with a plain detail.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..migrate import scan as scanning
from ..worker.jobs import Job, QueueJob
from ..worker.queue import Message, outside, send
from . import aws_connect
from .arm import Arm, ArmError
from .cloud_tasks import QUEUE

log = logging.getLogger("omnifile_service.settings")

MOVER_API = "2026-05-01"
#: An ARM deployment takes minutes, and a scan's read fetches a batch: more than a
#: message's minute. A row lock used to keep a second holder out during them.
CALL_HIDE_SECONDS = 3600

#: (params, env) -> AwsFetchResult; raises on refusal.
Fetch = Callable[[Mapping[str, Any], Mapping[str, str]], Awaitable[dict[str, Any]]]


class Failed(Exception):
    pass


# --- aws_fetch -----------------------------------------------------------------------------


def candidate(params: Mapping[str, Any], env: Mapping[str, str]):
    """The S3Config the fetch uses: the new account as the stack made it,
    checked by the operator settings' own rules (https, interface endpoints with
    private networking, no loopback). Raises Failed."""
    from ..backends.s3.config import AwsDeployment, ConfigError, S3Config

    account, region, dep = params["account_id"], params["region"], params["deployment_id"]
    n = aws_connect.names(account, dep, region)
    entry: dict[str, Any] = {
        "region": region,
        "role_arn": n["role_arn"],
        # The deployment's mode, as the confirmed entry carries it (review of #64).
        "private_network": aws_connect.private_network(env),
    }
    eps = {k: v for k, v in dict(params.get("endpoints") or {}).items() if v}
    if eps:
        entry["endpoints"] = eps
    aud = aws_connect.audience(env)
    if aud is None:
        raise Failed("the deployment has no web-identity audience")
    try:
        d = AwsDeployment.from_mapping(
            {account: entry},
            web_identity_audience=aud,
            private_network=aws_connect.private_network(env),
        )
        return S3Config.for_backend({"account_id": account, "region": region}, deployment=d)
    except ConfigError as e:
        raise Failed(f"those settings would be refused: {e}") from None


async def fetch_with(clients, params: Mapping[str, Any], env: Mapping[str, str]) -> dict:
    """`clients`: s3, sqs and (or None) cloudformation clients, already holding
    the role's session. Only reads."""
    s3, sqs, cfn = clients
    account, region, dep = params["account_id"], params["region"], params["deployment_id"]
    n = aws_connect.names(account, dep, region)
    problems: list[str] = []
    outputs: dict[str, str] = {}
    stack = None
    if cfn is not None:
        try:
            got = await cfn.describe_stacks(StackName=n["stack_name"])
            s = (got.get("Stacks") or [{}])[0]
            outputs = {o["OutputKey"]: str(o.get("OutputValue") or "")
                       for o in s.get("Outputs") or []}  # fmt: skip
            stack = {"status": s.get("StackStatus"), "name": n["stack_name"]}
        except Exception as e:  # noqa: BLE001 - recorded, not fatal
            problems.append(f"Couldn't read the stack {n['stack_name']}: {type(e).__name__}")
    in_stack = {b for b in (outputs.get("SourceBuckets") or "").split(",") if b}
    listed = await s3.list_buckets()
    buckets = []
    for b in (listed.get("Buckets") or [])[:200]:
        name = str(b.get("Name") or "")
        if not name or name == n["ops_bucket"]:
            continue
        row: dict[str, Any] = {"name": name, "region": None, "versioning": None,
                               "eventbridge": None, "in_stack": name in in_stack}  # fmt: skip
        if len(buckets) < 100 or name in in_stack:
            try:
                loc = await s3.get_bucket_location(Bucket=name)
                row["region"] = loc.get("LocationConstraint") or "us-east-1"
                v = await s3.get_bucket_versioning(Bucket=name)
                row["versioning"] = v.get("Status") or "Off"
                nc = await s3.get_bucket_notification_configuration(Bucket=name)
                row["eventbridge"] = "EventBridgeConfiguration" in nc
            except Exception:  # noqa: BLE001 - a bucket the role may not read
                pass
        buckets.append(row)
    for b in buckets:
        if b["in_stack"] and b["eventbridge"] is False:
            problems.append(f"EventBridge is off on {b['name']}: turn it on in its Properties.")
    queue_url = outputs.get("EventQueueUrl") or n["queue_url"]
    ok = True
    try:
        await sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    except Exception as e:  # noqa: BLE001
        ok = False
        problems.append(f"The event queue didn't answer: {type(e).__name__}")
    outputs.setdefault("AccountId", account)
    outputs.setdefault("Region", region)
    outputs.setdefault("DeploymentId", dep)
    entry, more = aws_connect.entry_from(
        account, region, dep, outputs, env, params.get("endpoints") or None
    )
    return {
        "account_id": account,
        "region": region,
        "role_arn": n["role_arn"],
        "buckets": buckets,
        "queue": {"url": queue_url, "ok": ok},
        "stack": stack,
        "entry": entry or {},
        "outputs": outputs,
        "problems": problems + more,
    }


def fetch_from_env() -> Fetch:  # pragma: no cover - needs real AWS (VERIFY RW-37)
    async def fetch(params: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
        from contextlib import AsyncExitStack

        from aiobotocore.config import AioConfig

        from ..backends.s3.credentials import entra_token_provider, session_for

        config = candidate(params, env)
        assert config.web_identity_audience is not None
        session = session_for(
            config, token_provider=entra_token_provider(None, config.web_identity_audience)
        )
        common = {"region_name": config.region, "config": AioConfig(retries={"max_attempts": 3}),
                  "verify": config.ca_bundle or True}  # fmt: skip
        async with AsyncExitStack() as stack:
            s3 = await stack.enter_async_context(
                session.create_client("s3", endpoint_url=config.endpoint_url, **common)
            )
            sqs = await stack.enter_async_context(
                session.create_client("sqs", endpoint_url=config.sqs_endpoint_url, **common)
            )
            cfn = None
            if not config.private_network:
                cfn = await stack.enter_async_context(
                    session.create_client("cloudformation", **common)
                )
            return await fetch_with((s3, sqs, cfn), params, env)

    return fetch


# --- mover_deploy --------------------------------------------------------------------------


def mover_config_value(p: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "subscription_id": p["subscription_id"],
            "resource_group": p["resource_group"],
            "storage_mover": p["name"],
            "project": p["project"],
            "source_endpoints": {"<bucket>": "<its source endpoint>"},
            "target_endpoints": {"<account>/<container>": "<its target endpoint>"},
        },
        indent=2,
    )


async def deploy_with(arm: Arm, p: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    base = (
        f"/subscriptions/{p['subscription_id']}/resourceGroups/{p['resource_group']}"
        f"/providers/Microsoft.StorageMover/storageMovers/{p['name']}"
    )
    try:
        await arm.put(base, MOVER_API, {
            "location": p["location"],
            "properties": {"description": "OmniFile migrations (made from the portal)."},
        })  # fmt: skip
        await arm.put(f"{base}/projects/{p['project']}", MOVER_API, {
            "properties": {"description": "OmniFile migration jobs."},
        })  # fmt: skip
        sm = await arm.get(base, MOVER_API)
    except ArmError as e:
        hint = ""
        if e.status == 403:
            hint = " The worker's identity needs the Mover Deployer grant from Connect Azure."
        raise Failed(f"Azure refused it: {e}.{hint}") from None
    state = (sm.get("properties") or {}).get("provisioningState")
    return (
        f"Created Storage Mover {p['name']} and project {p['project']} in {p['resource_group']}.",
        {
            "storage_mover": p["name"],
            "project": p["project"],
            "resource_group": p["resource_group"],
            "subscription_id": p["subscription_id"],
            "provisioning_state": state,
            "config_value": mover_config_value(p),
        },
    )


def mover_arm_from_env(env: Mapping[str, str]) -> Arm:  # pragma: no cover - real Azure
    import httpx

    from ..notify.tokens import ManagedIdentity
    from .arm import HttpArm
    from .ports import ARM_SCOPE

    return HttpArm(
        httpx.AsyncClient(trust_env=False, follow_redirects=False),
        ManagedIdentity(ARM_SCOPE, env.get("AZURE_CLIENT_ID") or None),
    )


# --- the queue -------------------------------------------------------------------------------


@dataclass
class CloudTasks:
    fetch: Fetch
    #: The worker's ARM client (its managed identity), or None (not configured).
    arm: Arm | None
    #: backend_id -> StorageBackend (the worker's registry).
    backend_for: Callable[[int], Any]
    env: Mapping[str, str]

    async def handle(self, msg: Message, conn: psycopg.AsyncConnection, now: datetime) -> None:
        cur = conn.cursor(row_factory=dict_row)
        try:
            task_id = int(msg.body["task_id"])
        except (KeyError, TypeError, ValueError):
            return
        t = await (
            await cur.execute(
                "SELECT * FROM catalog.cloud_task WHERE task_id = %s FOR UPDATE", (task_id,)
            )
        ).fetchone()
        if t is None or t["state"] not in ("pending", "running"):
            return
        if t["kind"] == "source_scan":
            await self._scan_step(conn, t, now, msg)
            return
        try:
            # An ARM deployment takes minutes: the row's lock is let go for it, so its
            # transaction id doesn't hold the edge feed back.
            detail, result = await outside(
                msg, "run", lambda: self._run(t), hide_for=CALL_HIDE_SECONDS
            )
            state = "done"
        except Failed as e:
            detail, result, state = str(e), {}, "failed"
        except Exception as e:  # noqa: BLE001 - every failure is the task's answer
            log.warning("cloud task %s failed: %s", t["kind"], type(e).__name__)
            detail, result, state = f"{type(e).__name__}: {str(e)[:300]}", {}, "failed"
        await self._finish(conn, t, state, detail, result, now)

    async def _run(self, t: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        p = dict(t["params"] or {})
        if t["kind"] == "aws_fetch":
            got = await self.fetch(p, self.env)
            n = len(got.get("buckets") or [])
            return f"Assumed {got['role_arn']} with the worker's web identity; {n} buckets.", got
        if t["kind"] == "mover_deploy":
            if self.arm is None:
                raise Failed("the worker has no Azure Resource Manager client configured")
            return await deploy_with(self.arm, p)
        raise Failed(f"unknown task {t['kind']}")

    async def _finish(self, conn, t, state, detail, result, now, progress=None) -> None:
        await conn.execute(
            "UPDATE catalog.cloud_task SET state = %s, detail = %s, result = %s,"
            " progress = coalesce(%s, progress), updated_at = %s, finished_at = %s"
            " WHERE task_id = %s",
            (state, detail, Jsonb(result), None if progress is None else Jsonb(progress), now,
             now, t["task_id"]),
        )  # fmt: skip
        await conn.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, details)"
            " VALUES ('system:settings', 'settings.cloud_task', %s, %s)",
            (
                "allow" if state in ("done", "capped") else "deny",
                Jsonb({"task_id": str(t["task_id"]), "kind": t["kind"], "state": state,
                       "identity_id": str(t["identity_id"])}),
            ),
        )  # fmt: skip

    async def _scan_step(
        self, conn, t: Mapping[str, Any], now: datetime, msg: Message | None = None
    ) -> None:
        from ..sources.model import SourceError
        from ..sources.run import reader_for

        p = dict(t["params"] or {})
        prog = dict(t["progress"] or {})
        state = dict(prog.get("_state") or {})
        tally = scanning.Tally.from_json(state.get("tally"))
        caps = scanning.caps_of(p)
        try:
            backend = self.backend_for(int(p["backend_id"]))
            reader = reader_for(backend, p["bucket"], p.get("prefix") or "", p.get("inventory"))
            got = await outside(
                msg,
                "scan",
                lambda: reader.read(state.get("resume"), scanning.BATCH),
                hide_for=CALL_HIDE_SECONDS,
            )
        except (SourceError, KeyError, ValueError) as e:
            await self._finish(conn, t, "failed", str(e), {}, now)
            return
        except Exception as e:  # noqa: BLE001 - a backend error ends the scan, plainly
            await self._finish(conn, t, "failed", f"{type(e).__name__}: {str(e)[:300]}", {}, now)
            return
        tally.add(got.objects, p.get("prefix") or "")
        batches = int(prog.get("batches") or 0) + 1
        elapsed = (now - t["created_at"]).total_seconds()
        prog = {
            "objects": tally.objects,
            "bytes": tally.bytes,
            "batches": batches,
            "elapsed_s": round(max(elapsed, 0.0), 1),
            "_state": {"tally": tally.to_json(),
                       "resume": None if got.resume is None else dict(got.resume)},
        }  # fmt: skip
        done = got.resume is None
        capped = not done and (
            tally.objects >= caps["max_objects"] or elapsed >= caps["max_seconds"]
        )
        if done or capped:
            prices = await _s3_prices(conn)
            res = scanning.result(tally, prices)
            res["list_cost_usd"] = str(scanning.list_cost(tally.objects))
            why = (
                f"Scanned {tally.objects:,} objects."
                if done
                else f"Stopped at the cap after {tally.objects:,} objects"
                f" ({'objects' if tally.objects >= caps['max_objects'] else 'time'}); the"
                " totals are of what was seen."
            )
            prog["_state"] = {}
            await self._finish(conn, t, "done" if done else "capped", why, res, now, prog)
            return
        await conn.execute(
            "UPDATE catalog.cloud_task SET state = 'running', progress = %s, updated_at = %s"
            " WHERE task_id = %s",
            (Jsonb(prog), now, t["task_id"]),
        )
        await send(conn, QUEUE, {"task_id": str(t["task_id"])})


async def _s3_prices(conn) -> Any:
    from ..policy.cost import PriceTableError, load_price_table

    cur = conn.cursor(row_factory=dict_row)
    row = await (
        await cur.execute(
            "SELECT body FROM catalog.price_table WHERE backend_kind = 's3' AND region = ''"
        )
    ).fetchone()
    if row is None:
        return None
    try:
        return load_price_table("s3", row["body"])
    except PriceTableError:
        return None


def cloud_jobs(worker: Any) -> list[Job]:
    import os

    env = dict(os.environ)
    # Dev mode has no Azure: a deployment says the worker has no ARM client.
    arm = None if env.get("OMNIFILE_DEV_MODE") == "1" else mover_arm_from_env(env)
    tasks = CloudTasks(fetch_from_env(), arm, lambda bid: worker.registry.get(bid), env)

    async def handle(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await tasks.handle(msg, conn, worker.clock.now())

    return [QueueJob(QUEUE, handle)]
