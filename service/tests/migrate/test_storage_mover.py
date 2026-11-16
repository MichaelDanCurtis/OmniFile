"""The Azure Storage Mover client against a fake ARM (migrate/storage_mover.py)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from omnifile_service.backends.base import BackendError
from omnifile_service.migrate import MoverJob
from omnifile_service.migrate.storage_mover import (
    API_VERSION,
    AzureStorageMover,
    StorageMoverConfig,
    from_environment,
)

CONFIG = {
    "subscription_id": "sub-1",
    "resource_group": "rg-omnifile",
    "storage_mover": "mover-1",
    "project": "migration",
    "source_endpoints": {"acme-seismic": "s3-acme-seismic"},
    "target_endpoints": {"live/data": "blob-live-data"},
}
PROJECT = (
    "/subscriptions/sub-1/resourceGroups/rg-omnifile/providers/Microsoft.StorageMover"
    "/storageMovers/mover-1/projects/migration"
)
JOB = MoverJob("omnifile-migration-5", 7, "acme-seismic", "raw/", 1, "live/data",
               "migrated/acme-seismic/raw/")  # fmt: skip


class FakeArm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.run_status = "Running"
        self.job: dict | None = {}
        self.job_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.url.params["api-version"] == API_VERSION
        assert request.headers["authorization"] == "Bearer tok"
        path = request.url.path
        if request.method == "PUT" and "/jobDefinitions/" in path:
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[1], **(body or {})})
        if request.method == "POST" and path.endswith("/startJob"):
            run = f"{path.rsplit('/startJob', 1)[0]}/jobRuns/run-17"
            return httpx.Response(200, json={"jobRunResourceId": run})
        if request.method == "GET" and "/jobRuns/" in path:
            return httpx.Response(200, json={"properties": {
                "status": self.run_status, "itemsScanned": 10, "itemsTransferred": 8,
                "itemsFailed": 0, "itemsUnsupported": 2, "itemsNoTransferNeeded": 0,
                "error": {"code": "Boom", "message": "the target refused"}
                if self.run_status == "Failed" else None}})  # fmt: skip
        if request.method == "GET" and "/jobDefinitions/" in path:
            if self.job is None:
                return httpx.Response(404, json={"error": {"code": "NotFound"}})
            return httpx.Response(self.job_status, json=self.job)
        return httpx.Response(404, json={"error": {"code": "NotFound"}})


class Tok:
    async def token(self) -> str:
        return "tok"


def _mover(arm: FakeArm) -> AzureStorageMover:
    http = httpx.AsyncClient(transport=httpx.MockTransport(arm))
    return AzureStorageMover(StorageMoverConfig.from_mapping(CONFIG), Tok(), http=http)


def test_a_job_is_defined_run_and_followed() -> None:
    arm = FakeArm()
    mover = _mover(arm)

    async def go():  # type: ignore[no-untyped-def]
        name = await mover.ensure_job(JOB)
        run_id = await mover.start_run(name)
        running = await mover.run_status(name, run_id)
        arm.run_status = "Succeeded"
        done = await mover.run_status(name, run_id)
        await mover.aclose()
        return name, run_id, running, done

    name, run_id, running, done = asyncio.run(go())
    assert (name, run_id) == ("omnifile-migration-5", "run-17")
    method, path, body = arm.calls[0]
    assert (method, path) == ("PUT", f"{PROJECT}/jobDefinitions/omnifile-migration-5")
    assert body == {"properties": {
        "description": "OmniFile migration of acme-seismic/raw/", "copyMode": "Additive",
        "sourceName": "s3-acme-seismic", "sourceSubpath": "raw/",
        "targetName": "blob-live-data", "targetSubpath": "migrated/acme-seismic/raw/"}}  # fmt: skip
    assert arm.calls[1][1] == f"{PROJECT}/jobDefinitions/omnifile-migration-5/startJob"
    assert running.state == "running"
    assert done.state == "succeeded" and done.counters == {
        "scanned": 10,
        "transferred": 8,
        "failed": 0,
        "unsupported": 2,
        "no_transfer_needed": 0,
    }


def test_failures() -> None:
    arm = FakeArm()
    arm.run_status = "Failed"
    mover = _mover(arm)

    async def go():  # type: ignore[no-untyped-def]
        run = await mover.run_status("j", "r")
        with pytest.raises(BackendError, match="no Storage Mover endpoint"):
            await mover.ensure_job(MoverJob("x", 7, "other-bucket", "", 1, "live/data", "m/"))
        with pytest.raises(BackendError, match="refused"):
            await mover._call("GET", f"https://management.azure.com{PROJECT}/nothing")
        await mover.aclose()
        return run

    run = asyncio.run(go())
    assert run.state == "failed" and "Boom the target refused" in (run.error or "")
    with pytest.raises(ValueError, match="project"):
        StorageMoverConfig.from_mapping({k: v for k, v in CONFIG.items() if k != "project"})
    assert from_environment(None, None) is None


def test_batch_operations_need_the_control_endpoint_with_private_networking() -> None:
    from omnifile_service.backends.s3 import BatchOps, S3Config

    acct = "123456789012"
    base = {
        "region": "eu-west-1",
        "account_id": acct,
        "endpoint_url": "https://bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com",
        "sts_endpoint_url": "https://vpce-1.sts.eu-west-1.vpce.amazonaws.com",
        "role_arn": f"arn:aws:iam::{acct}:role/omnifile-service",
        "web_identity_audience": "api://omnifile-aws",
    }
    batch = BatchOps(acct, f"arn:aws:iam::{acct}:role/b", "m")
    with pytest.raises(ValueError, match="control_endpoint_url"):
        S3Config(**base, batch=batch)
    ctl = "https://control.vpce-1.s3.eu-west-1.vpce.amazonaws.com"
    assert S3Config(**base, batch=BatchOps(**{**batch.__dict__, "control_endpoint_url": ctl})).batch


def test_a_jobs_running_run_is_found_from_its_definition() -> None:
    """A crashed step started this run and never recorded it: it is not started twice."""
    arm = FakeArm()
    mover = _mover(arm)
    arm.job = {"properties": {"latestJobRunName": "run-17", "latestJobRunStatus": "Running"}}

    async def go() -> tuple[str | None, str | None, str | None]:
        running = await mover.running_run("j")
        arm.job = {"properties": {"latestJobRunName": "run-17", "latestJobRunStatus": "Succeeded"}}
        ended = await mover.running_run("j")
        arm.job = {"properties": {}}
        never = await mover.running_run("j")
        await mover.aclose()
        return running, ended, never

    assert asyncio.run(go()) == ("run-17", None, None)
    assert arm.calls[0][:2] == ("GET", f"{PROJECT}/jobDefinitions/j")


def test_a_job_with_no_definition_yet_has_no_running_run() -> None:
    """The look comes before the PUT now, so the first time there is nothing to find."""
    arm = FakeArm()
    arm.job = None
    mover = _mover(arm)

    async def go() -> str | None:
        found = await mover.running_run("j")
        await mover.aclose()
        return found

    assert asyncio.run(go()) is None


def test_another_refusal_of_the_look_is_an_error() -> None:
    """Only "not found" means no run. A 403 mustn't pass for one, or a second would start."""
    arm = FakeArm()
    arm.job_status = 403
    mover = _mover(arm)

    async def go() -> None:
        with pytest.raises(BackendError):
            await mover.running_run("j")
        await mover.aclose()

    asyncio.run(go())
