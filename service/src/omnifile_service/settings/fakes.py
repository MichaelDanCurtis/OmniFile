"""A fake `Probes` for the tests: canned DNS, ARM and Graph answers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ports import ProbeError


@dataclass
class FakeProbes:
    #: host -> addresses; a missing host doesn't resolve.
    dns: dict[str, list[str]] = field(default_factory=dict)
    #: ARM path (without api-version) -> answer, or a ProbeError to raise.
    arm: dict[str, Any] = field(default_factory=dict)
    #: Graph path -> answer, or a ProbeError to raise.
    graph: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def resolve(self, host: str) -> list[str]:
        self.calls.append(f"dns {host}")
        if host not in self.dns:
            raise ProbeError(f"{host} does not resolve")
        return list(self.dns[host])

    def _answer(self, table: dict[str, Any], path: str, what: str) -> dict[str, Any]:
        self.calls.append(f"{what} {path}")
        got = table.get(path)
        if got is None:
            raise ProbeError("answered 404 not found", 404)
        if isinstance(got, ProbeError):
            raise got
        return got

    async def arm_get(self, path: str, api_version: str) -> dict[str, Any]:
        return self._answer(self.arm, path, "arm")

    async def graph_get(self, path: str) -> dict[str, Any]:
        return self._answer(self.graph, path, "graph")


DEMO_SUBSCRIPTION = "00000000-0000-4000-8000-0000000000a1"


def demo_arm_answers(tenant_id: str) -> dict[str, Any]:
    """A small tenant for dev mode and tests: one subscription, three groups,
    and an admin who may assign roles everywhere."""
    sub = f"/subscriptions/{DEMO_SUBSCRIPTION}"
    owner = {"value": [{"actions": ["*"], "notActions": []}]}
    out: dict[str, Any] = {
        "/tenants": {"value": [
            {"tenantId": tenant_id, "displayName": "Contoso"},
            {"tenantId": "99999999-9999-4999-8999-999999999999", "displayName": "Fabrikam"},
        ]},
        "/subscriptions": {"value": [
            {"subscriptionId": DEMO_SUBSCRIPTION, "displayName": "Contoso production",
             "tenantId": tenant_id, "state": "Enabled"},
        ]},
        f"{sub}/resourcegroups": {"value": [
            {"name": "rg-storage", "location": "westeurope"},
            {"name": "rg-network", "location": "westeurope"},
            {"name": "rg-migration", "location": "westeurope"},
        ]},
    }  # fmt: skip
    for rg in ("rg-storage", "rg-network", "rg-migration"):
        out[f"{sub}/resourceGroups/{rg}/providers/Microsoft.Authorization/permissions"] = owner
    return out


def dev_arm(tenant_id: str):
    """`WebServices.arm_for` in dev mode: every token gets the same demo fake."""
    arm = FakeArm(answers=demo_arm_answers(tenant_id))
    return lambda _token: arm


@dataclass
class FakeArm:
    """A fake `settings.arm.Arm`: canned GETs, and PUTs kept so a GET after a
    PUT answers it. Every call is recorded as ``"<METHOD> <path>"``."""

    #: path -> answer, or an ArmError to raise.
    answers: dict[str, Any] = field(default_factory=dict)
    #: path -> an ArmError the next PUT or DELETE of it raises (once).
    fail_once: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    bodies: dict[str, Any] = field(default_factory=dict)

    async def get(self, path: str, api_version: str) -> dict[str, Any]:
        self.calls.append(f"GET {path}")
        got = self.answers.get(path)
        if got is None:
            from .arm import ArmError

            raise ArmError(404, "NotFound", "not found")
        if isinstance(got, Exception):
            raise got
        return got

    def _maybe_fail(self, path: str) -> None:
        err = self.fail_once.pop(path, None)
        if err is not None:
            raise err

    async def put(self, path: str, api_version: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(f"PUT {path}")
        self._maybe_fail(path)
        self.bodies[path] = body
        out = {"id": path, "name": path.rsplit("/", 1)[-1], **body}
        out["properties"] = {**(out.get("properties") or {}), "provisioningState": "Succeeded"}
        self.answers[path] = out
        return out

    async def delete_resource(self, path: str, api_version: str) -> None:
        self.calls.append(f"DELETE {path}")
        self._maybe_fail(path)
        if self.answers.pop(path, None) is None:
            from .arm import ArmError

            raise ArmError(404, "NotFound", "not found")
