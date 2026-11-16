"""Settings, Storage Mover: its agents and projects, read-only, through ARM.

The configuration is the worker's (``OMNIFILE_STORAGE_MOVER``, a JSON
`migrate.storage_mover.StorageMoverConfig`), given to `web` too. The reads are
GETs of the Storage Mover, its agents and its projects at the API version the
migration engine uses, with `web`'s managed identity (it needs Reader on the
Storage Mover). Nothing is created or changed here.

Before ``OMNIFILE_STORAGE_MOVER`` is set, a Storage Mover the portal deployed
(docs/design/cloud-connect.md section 3) is read instead, so the page can show
its agent registering.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..migrate.storage_mover import API_VERSION, StorageMoverConfig
from .ports import ProbeError


def _base(c: StorageMoverConfig) -> str:
    return (
        f"/subscriptions/{c.subscription_id}/resourceGroups/{c.resource_group}"
        f"/providers/Microsoft.StorageMover/storageMovers/{c.storage_mover}"
    )


#: Downloading and registering the on-premises agent (VERIFY RA-48: the link and the
#: console's wording, against Microsoft's agent guide).
AGENT_STEPS = [
    "Download the Storage Mover agent image from Microsoft's agent deployment guide:"
    " https://learn.microsoft.com/azure/storage-mover/agent-deploy",
    "Create a VM from it on Hyper-V or VMware, near the data (the guide lists the sizes:"
    " at least 8 vCPUs and 16 GiB of memory), with outbound HTTPS to Azure.",
    "Sign in to the VM's console with the guide's default account and change its password.",
    "Choose Register, and give the subscription, resource group and Storage Mover name"
    " shown here, and the tenant. Sign in with an account that may register agents there.",
    "This page shows the agent as Online once it has registered (it checks every 15 s).",
]


def _deployed_config(deployed: Mapping[str, Any]) -> StorageMoverConfig:
    return StorageMoverConfig(
        subscription_id=str(deployed["subscription_id"]),
        resource_group=str(deployed["resource_group"]),
        storage_mover=str(deployed["name"]),
        project=str(deployed["project"]),
        source_endpoints={},
        target_endpoints={},
    )


async def mover_settings(
    probes, env: Mapping[str, str], deployed: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "configured": False,
        "storage_mover": None,
        "agents": [],
        "projects": [],
        "endpoints": {"source": {}, "target": {}},
        "error": None,
    }
    raw = env.get("OMNIFILE_STORAGE_MOVER")
    if not raw and deployed is None:
        out["error"] = (
            "No Storage Mover is configured (OMNIFILE_STORAGE_MOVER): migrations start no copies."
        )
        return out
    if raw == "sim":
        out["configured"] = True
        out["error"] = "The simulated mover (development): nothing to read."
        return out
    if raw:
        try:
            c = StorageMoverConfig.from_mapping(json.loads(raw))
        except (ValueError, TypeError) as e:
            out["error"] = f"OMNIFILE_STORAGE_MOVER is not a Storage Mover config: {e}"
            return out
        out["configured"] = True
    else:
        assert deployed is not None
        c = _deployed_config(deployed)
    out["storage_mover"] = {
        "name": c.storage_mover,
        "resource_group": c.resource_group,
        "subscription_id": c.subscription_id,
        "provisioning_state": None,
    }
    out["endpoints"] = {"source": dict(c.source_endpoints), "target": dict(c.target_endpoints)}
    if probes is None:
        out["error"] = "Live checks are off (OMNIFILE_SETTINGS_PROBES): status isn't read."
        return out
    base = _base(c)
    try:
        sm = await probes.arm_get(base, c.api_version or API_VERSION)
        out["storage_mover"]["provisioning_state"] = (sm.get("properties") or {}).get(
            "provisioningState"
        )
        agents = await probes.arm_get(f"{base}/agents", c.api_version or API_VERSION)
        for a in agents.get("value") or []:
            p = a.get("properties") or {}
            out["agents"].append(
                {
                    "name": a.get("name"),
                    "status": p.get("agentStatus"),
                    "last_status_update": p.get("lastStatusUpdate"),
                    "arc_resource_id": p.get("arcResourceId"),
                    "version": p.get("agentVersion"),
                }
            )
        projects = await probes.arm_get(f"{base}/projects", c.api_version or API_VERSION)
        for pr in projects.get("value") or []:
            p = pr.get("properties") or {}
            out["projects"].append(
                {
                    "name": pr.get("name"),
                    "description": p.get("description"),
                    "provisioning_state": p.get("provisioningState"),
                    "in_use": pr.get("name") == c.project,
                }
            )
    except ProbeError as e:
        out["error"] = (
            f"Azure Resource Manager {e}. The web app's identity needs Reader on the Storage Mover."
        )
    return out
