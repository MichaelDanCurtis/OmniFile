"""Azure Storage Mover behind the `Mover` port (docs/design/migration-engine.md
sections 4 and 10, part 2).

Driven through Azure Resource Manager's REST API with the worker's managed
identity; no SDK. ARM is public and that is fine (decision 22). The customer
creates the Storage Mover, its project and its endpoints when setting up
(the S3 source endpoints through the Azure Arc multicloud connector, the
target endpoints on the dataset's storage containers, managed identity with
Storage Blob Data Contributor) [V]; this client creates job definitions on
them and runs them.

- **Job definition:** `PUT .../projects/{project}/jobDefinitions/{name}` with
  `copyMode: Additive` (never Mirror: it deletes at the target), the source
  and target endpoint names and subpaths.
- **Run:** `POST .../jobDefinitions/{name}/startJob` returns the run's
  resource id; `GET .../jobRuns/{run}` gives its status and counters. `GET`
  on the job definition names its latest run, so a run a crashed step never
  recorded is found, not started twice.

VERIFY on real Azure (real-azure-checks.md, Storage Mover): the API
version, whether cloud-to-cloud job definitions need more properties
(`job_properties` adds them without a code change), and the status and
counter names this maps.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..backends.base import BackendError
from .mover import MoverJob, MoverRun

log = logging.getLogger("omnifile_service.migrate")

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
#: The API version the research checked [V].
API_VERSION = "2026-05-01"
#: Storage Mover run states -> MoverRun.state.
RUNNING = frozenset({"Queued", "Started", "Running", "CancelRequested", "Canceling",
                     "PausedByBandwidthManagement"})  # fmt: skip
SUCCEEDED = frozenset({"Succeeded"})
#: Storage Mover's counters -> MoverRun.counters (mover.COUNTERS).
COUNTERS = {
    "itemsScanned": "scanned",
    "itemsTransferred": "transferred",
    "itemsFailed": "failed",
    "itemsUnsupported": "unsupported",
    "itemsNoTransferNeeded": "no_transfer_needed",
    "itemsExcluded": "excluded",
}


class _NotFound(BackendError):
    """ARM has no such resource."""


class TokenSource(Protocol):
    async def token(self) -> str: ...


@dataclass(frozen=True)
class StorageMoverConfig:
    subscription_id: str
    resource_group: str
    storage_mover: str
    project: str
    #: S3 bucket -> its source endpoint (an Arc multicloud connector endpoint).
    source_endpoints: Mapping[str, str]
    #: Our container ("<account>/<container>") -> its target endpoint.
    target_endpoints: Mapping[str, str]
    api_version: str = API_VERSION
    managed_identity_client_id: str | None = None
    #: More job-definition properties, should real Azure need them.
    job_properties: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> StorageMoverConfig:
        try:
            return cls(
                subscription_id=str(d["subscription_id"]),
                resource_group=str(d["resource_group"]),
                storage_mover=str(d["storage_mover"]),
                project=str(d["project"]),
                source_endpoints=dict(d.get("source_endpoints") or {}),
                target_endpoints=dict(d.get("target_endpoints") or {}),
                api_version=str(d.get("api_version") or API_VERSION),
                managed_identity_client_id=d.get("managed_identity_client_id"),
                job_properties=dict(d.get("job_properties") or {}),
            )
        except KeyError as e:
            raise ValueError(f"Storage Mover config needs {e.args[0]}") from None

    def project_url(self) -> str:
        return (
            f"{ARM}/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.StorageMover/storageMovers/{self.storage_mover}"
            f"/projects/{self.project}"
        )


class AzureStorageMover:
    """The `Mover` port over Storage Mover's ARM API."""

    def __init__(
        self,
        config: StorageMoverConfig,
        tokens: TokenSource,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, url: str, body: Any = None, *, as_given: bool = False
    ) -> dict[str, Any]:
        """`as_given`: a URL ARM gave (a nextLink), with its api-version."""
        headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
        sep = "&" if "?" in url else "?"
        full = url if as_given else f"{url}{sep}api-version={self.config.api_version}"
        try:
            r = await self._http.request(method, full, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise BackendError(f"Storage Mover: {type(e).__name__}: {e}") from None
        if r.status_code == 429 or r.status_code >= 500:
            raise BackendError(f"Storage Mover answered {r.status_code}")
        if r.status_code >= 400:
            kind = _NotFound if r.status_code == 404 else BackendError
            raise kind(f"Storage Mover refused {method} ({r.status_code}): {r.text[:500]}")
        return r.json() if r.content else {}

    def _job_url(self, name: str) -> str:
        return f"{self.config.project_url()}/jobDefinitions/{quote(name, safe='')}"

    async def ensure_job(self, job: MoverJob) -> str:
        source = self.config.source_endpoints.get(job.source_container)
        target = self.config.target_endpoints.get(job.target_container)
        if source is None or target is None:
            missing = job.source_container if source is None else job.target_container
            raise BackendError(f"no Storage Mover endpoint is configured for {missing!r}")
        props = {
            **self.config.job_properties,
            "description": f"OmniFile migration of {job.source_container}/{job.source_subpath}",
            "copyMode": "Additive",
            "sourceName": source,
            "sourceSubpath": job.source_subpath,
            "targetName": target,
            "targetSubpath": job.target_subpath,
        }
        await self._call("PUT", self._job_url(job.name), {"properties": props})
        return job.name

    async def start_run(self, name: str) -> str:
        out = await self._call("POST", f"{self._job_url(name)}/startJob")
        rid = out.get("jobRunResourceId")
        if not isinstance(rid, str) or "/jobRuns/" not in rid:
            raise BackendError(f"Storage Mover started no run: {out!r}"[:500])
        return rid.rsplit("/jobRuns/", 1)[1]

    async def run_status(self, name: str, run_id: str) -> MoverRun:
        out = await self._call("GET", f"{self._job_url(name)}/jobRuns/{quote(run_id, safe='')}")
        p = out.get("properties") or {}
        status = str(p.get("status") or "")
        state = "running" if status in RUNNING else ("succeeded" if status in SUCCEEDED
                                                      else "failed")  # fmt: skip
        counters = {ours: int(p[theirs]) for theirs, ours in COUNTERS.items()
                    if isinstance(p.get(theirs), int)}  # fmt: skip
        err = p.get("error")
        error = None
        if state == "failed":
            error = f"Storage Mover run {status or 'unknown'}"
            if isinstance(err, Mapping) and err.get("message"):
                error += f": {err.get('code', '')} {err['message']}".rstrip()
        return MoverRun(run_id, state, counters, error)

    async def running_run(self, name: str) -> str | None:
        """The job definition's latest run, if it is still going; None if there is no
        such job yet. VERIFY on real Azure (real-azure-checks.md, RA-14):
        `latestJobRunName` and `latestJobRunStatus`."""
        try:
            out = await self._call("GET", self._job_url(name))
        except _NotFound:
            return None
        p = out.get("properties") or {}
        run, status = p.get("latestJobRunName"), p.get("latestJobRunStatus")
        return run if isinstance(run, str) and run and status in RUNNING else None

    async def list_agents(self) -> list[dict[str, Any]]:
        """The Storage Mover's registered agents (`GET .../agents`), for the
        console. A cloud-to-cloud job (S3 through the Arc multicloud
        connector) runs without one, so this may well be empty. VERIFY on
        real Azure: the property names below."""
        out = await self._call("GET", self.config.project_url().rsplit("/projects/", 1)[0]
                               + "/agents")  # fmt: skip
        found: list[dict[str, Any]] = list(out.get("value") or [])
        pages = 1
        # ARM pages with `nextLink`, a full URL with its own api-version and
        # skip token. It is followed only on ARM itself, so the token never
        # goes elsewhere.
        while isinstance(out.get("nextLink"), str) and pages < 50:
            nxt = out["nextLink"]
            if not nxt.startswith(ARM + "/"):
                log.warning("Storage Mover agents: nextLink outside ARM ignored: %s", nxt[:200])
                break
            out = await self._call("GET", nxt, as_given=True)
            found += list(out.get("value") or [])
            pages += 1
        agents = []
        for a in found:
            p = a.get("properties") or {}
            err = p.get("errorDetails")
            agents.append({
                "name": str(a.get("name") or ""),
                "status": p.get("agentStatus"),
                "version": p.get("agentVersion"),
                "host": p.get("localIPAddress"),
                "arc_resource_id": p.get("arcResourceId"),
                "last_status_at": p.get("lastStatusUpdate"),
                "error": (err.get("message") if isinstance(err, Mapping) else None),
            })  # fmt: skip
        return agents


def from_environment(raw: str | None, registry: Any) -> Any:
    """The worker's mover from `OMNIFILE_STORAGE_MOVER`: a JSON
    `StorageMoverConfig`, `sim` (dev and e2e only: copies between the
    worker's own backends), or unset (no copies are started)."""
    if not raw:
        return None
    if raw == "sim":
        from .mover import SimMover

        return SimMover(registry)
    import json

    from ..notify.tokens import ManagedIdentity

    config = StorageMoverConfig.from_mapping(json.loads(raw))
    return AzureStorageMover(config, ManagedIdentity(ARM_SCOPE, config.managed_identity_client_id))


__all__ = ["API_VERSION", "AzureStorageMover", "StorageMoverConfig", "from_environment"]
