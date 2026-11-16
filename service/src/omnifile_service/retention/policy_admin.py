"""The container-level WORM policy (decision 31).

Version-level WORM has two layers: a time-based policy on the container (the
default every new version gets) and per-version retention and holds
(`StorageBackend.set_retention` / `set_legal_hold`). The container policy is
a management-plane object on Azure (ARM: `blobContainers/immutabilityPolicies`
create, lock, extend), not a data-plane call, so it is not part of the
`StorageBackend` contract. This module is that management seam.

The lifecycle: create **unlocked** when an ArchiveAdmin enables WORM (the
validation window, at most 24 h), **lock** when the window ends, then only
**extend**. An unlocked policy may be deleted (the admin cancelled during
the window); a locked one never.

On Azure, `AzureContainerPolicy` drives the management SDK
(`azure-mgmt-storage`, `StorageManagementClient.blob_containers`) with the
worker's managed identity, which needs a role that may write immutability
policies on the WORM account (Storage Account Contributor). ARM is public,
and that is fine (decision 22). Every change after the first is conditional
on the policy's ETag, as ARM requires for lock and extend: a policy changed
by someone else meanwhile fails the call (412) and the worker retries.

VERIFY on real Azure (pilot 4): what Get returns for a container with no
policy (a 404 and a zero-day policy are both read as none), and that a
default policy on a container with version-level immutability is this same
`immutabilityPolicies/default` resource.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..backends.base import (
    BackendError,
    BackendKind,
    Immutable,
    StorageBackend,
    UnsupportedOperation,
)

log = logging.getLogger(__name__)


class ContainerPolicyAdmin(Protocol):
    async def set_unlocked(self, container: str, days: int) -> None: ...

    async def lock(self, container: str, days: int) -> None:
        """Lock the policy at `days` (creating it first if needed)."""
        ...

    async def extend(self, container: str, days: int) -> None:
        """Extend a locked policy. Shortening raises `Immutable`."""
        ...

    async def delete_unlocked(self, container: str) -> None: ...


class SimContainerPolicy:
    """sim's container policy (`SimBackend.set_container_policy`), in process
    or over sim_http's RPC (`worker.backends.SimHttpBackend`, where the calls
    are coroutines)."""

    def __init__(self, sim: StorageBackend) -> None:
        self._sim = sim

    @staticmethod
    async def _done(result: object) -> None:
        if inspect.isawaitable(result):
            await result

    async def set_unlocked(self, container: str, days: int) -> None:
        await self._done(self._sim.set_container_policy(container, days, locked=False))  # type: ignore[attr-defined]

    async def lock(self, container: str, days: int) -> None:
        await self._done(self._sim.set_container_policy(container, days, locked=True))  # type: ignore[attr-defined]

    async def extend(self, container: str, days: int) -> None:
        await self._done(self._sim.set_container_policy(container, days, locked=True))  # type: ignore[attr-defined]

    async def delete_unlocked(self, container: str) -> None:
        await self._done(self._sim.delete_container_policy(container))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Azure: the management SDK
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagementConfig:
    """Where the binding's storage accounts are in ARM: `catalog.backend.config`
    ``management``::

        {"subscription_id": "...", "resource_group": "rg-omnifile",
         "account_groups": {"wormacct": "rg-other"},
         "managed_identity_client_id": "..."}
    """

    subscription_id: str
    resource_group: str
    #: Accounts in another resource group than `resource_group`.
    account_groups: Mapping[str, str] = field(default_factory=dict)
    managed_identity_client_id: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ManagementConfig:
        raw = raw or {}
        sub, rg = raw.get("subscription_id"), raw.get("resource_group")
        if not sub or not rg:
            raise UnsupportedOperation(
                "the backend's config has no management.subscription_id and"
                " management.resource_group, so its container policies can't be set"
            )
        return cls(
            subscription_id=str(sub),
            resource_group=str(rg),
            account_groups=dict(raw.get("account_groups") or {}),
            managed_identity_client_id=raw.get("managed_identity_client_id"),
        )

    def group_of(self, account: str) -> str:
        return self.account_groups.get(account, self.resource_group)


@dataclass(frozen=True)
class _Policy:
    days: int
    locked: bool
    etag: str


def _days(days: int) -> Any:
    from azure.mgmt.storage.models import ImmutabilityPolicy, ImmutabilityPolicyProperty

    return ImmutabilityPolicy(
        properties=ImmutabilityPolicyProperty(immutability_period_since_creation_in_days=days)
    )


class AzureContainerPolicy:
    """ARM immutability policies through `blob_containers`, the management
    SDK's operations group (`azure.mgmt.storage.aio`), or anything with its
    methods (tests use a fake that checks every call)."""

    def __init__(self, blob_containers: Any, config: ManagementConfig) -> None:
        self._ops = blob_containers
        self._config = config

    def _where(self, container: str) -> dict[str, str]:
        account, sep, name = container.partition("/")
        if not sep or not account or not name:
            raise BackendError(f"{container!r} is not 'account/container'")
        return {"resource_group_name": self._config.group_of(account), "account_name": account,
                "container_name": name}  # fmt: skip

    async def _call(self, method: str, container: str, **kw: Any) -> Any:
        from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

        try:
            return await getattr(self._ops, method)(**self._where(container), **kw)
        except ResourceNotFoundError:
            raise
        except HttpResponseError as e:
            if e.status_code == 412:
                raise BackendError(
                    f"{container}: its immutability policy changed meanwhile ({method})"
                ) from None
            raise BackendError(f"{container}: {method} answered {e.status_code}: {e.message}"
                               ) from None  # fmt: skip

    async def _get(self, container: str) -> _Policy | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            p = await self._call("get_immutability_policy", container)
        except ResourceNotFoundError:
            return None
        days = p.immutability_period_since_creation_in_days
        if not days:  # VERIFY: a container with no policy may answer 0 days
            return None
        state = getattr(p.state, "value", p.state)  # an enum, or its string
        return _Policy(int(days), str(state or "").lower() == "locked", str(p.etag))

    @staticmethod
    def _if(etag: str) -> dict[str, Any]:
        from azure.core import MatchConditions

        return {"etag": etag, "match_condition": MatchConditions.IfNotModified}

    async def _put(self, container: str, days: int, current: _Policy | None) -> str:
        """Create or change an unlocked policy; returns its new ETag."""
        kw = self._if(current.etag) if current is not None else {}
        p = await self._call(
            "create_or_update_immutability_policy", container, parameters=_days(days), **kw
        )
        return str(p.etag)

    async def set_unlocked(self, container: str, days: int) -> None:
        cur = await self._get(container)
        if cur is not None and cur.locked:
            raise Immutable(f"{container}: the policy is locked ({cur.days} days); it can't be"
                            " unlocked")  # fmt: skip
        if cur is None or cur.days != days:
            await self._put(container, days, cur)

    async def lock(self, container: str, days: int) -> None:
        cur = await self._get(container)
        if cur is not None and cur.locked:
            await self._extend(container, cur, days)
            return
        etag = cur.etag if cur is not None else None
        if cur is None or cur.days != days:
            etag = await self._put(container, days, cur)
        await self._call("lock_immutability_policy", container, **self._if(str(etag)))

    async def extend(self, container: str, days: int) -> None:
        cur = await self._get(container)
        if cur is None or not cur.locked:
            await self.lock(container, days)
            return
        await self._extend(container, cur, days)

    async def _extend(self, container: str, cur: _Policy, days: int) -> None:
        if days < cur.days:
            raise Immutable(f"{container}: a locked policy of {cur.days} days can't be"
                            f" shortened to {days}")  # fmt: skip
        if days > cur.days:
            await self._call("extend_immutability_policy", container, parameters=_days(days),
                             **self._if(cur.etag))  # fmt: skip

    async def delete_unlocked(self, container: str) -> None:
        cur = await self._get(container)
        if cur is None:
            return
        if cur.locked:
            raise Immutable(f"{container}: a locked policy is never deleted")
        await self._call("delete_immutability_policy", container, **self._if(cur.etag))


#: One management client per subscription and identity, for the process.
_clients: dict[tuple[str, str | None], Any] = {}


def _blob_containers(config: ManagementConfig) -> Any:
    key = (config.subscription_id, config.managed_identity_client_id)
    if key not in _clients:
        from azure.identity.aio import DefaultAzureCredential
        from azure.mgmt.storage.aio import StorageManagementClient

        cred = DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            managed_identity_client_id=config.managed_identity_client_id,
        )
        _clients[key] = StorageManagementClient(cred, config.subscription_id)
    return _clients[key].blob_containers


def policy_admin_for(
    backend: StorageBackend, config: Mapping[str, Any] | None = None
) -> ContainerPolicyAdmin:
    """The container-policy seam for a binding: sim's, or ARM's for Azure
    (`config` is the backend's row config; its ``management`` says where the
    accounts are). Raises `UnsupportedOperation` when it can't be set."""
    if hasattr(backend, "set_container_policy"):
        return SimContainerPolicy(backend)
    if backend.kind is not BackendKind.AZURE_BLOB:
        raise UnsupportedOperation(f"a {backend.kind} backend has no container WORM policy")
    mgmt = ManagementConfig.from_mapping((config or {}).get("management"))
    return AzureContainerPolicy(_blob_containers(mgmt), mgmt)
