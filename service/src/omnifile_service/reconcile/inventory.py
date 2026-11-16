"""Where reconcile gets "what the backend holds".

`InventorySource` is the abstraction: an async stream of `InventoryItem`s
for one container, as of `snapshot_at`.

- **Non-WORM accounts: Blob Inventory** (native-features rule). Azure writes
  a daily CSV/Parquet report of every blob into a container; reading it costs
  one blob read per report file instead of a LIST call per 5,000 blobs
  (`blob_inventory.py`). It is used when the backend's config names the
  rule (`inventory`, see `choose_inventory`) and its newest run is complete
  and recent; configure the rule with `includeBlobVersions`, since reconcile
  matches every catalog location with the exact version it names. Otherwise
  `choose_inventory` falls back to `ListingInventory`, which yields the same
  items, and logs why.
- **WORM accounts: listings.** Version-level immutability blocks Blob
  Inventory on the whole account [W], so reconcile catches the change feed
  up and then lists (`ListingInventory`).
- **sim** has no inventory; `ListingInventory` over sim emulates one (sim's
  `list_all_versions` returns the fields an inventory row carries: name,
  version, current or not, size, class, retention, hold).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..backends.base import (
    ObjectHead,
    ObjectNotFound,
    ObjectRef,
    StorageBackend,
    VersionListing,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InventoryItem:
    """One stored object version."""

    key: str
    version_id: str | None
    size: int
    native_class: str
    retention_until: datetime | None = None
    legal_hold: bool = False
    is_current: bool = True


def _item(h: ObjectHead, is_current: bool) -> InventoryItem:
    return InventoryItem(
        key=h.ref.key,
        version_id=h.ref.version_id,
        size=h.size,
        native_class=h.native_class,
        retention_until=h.retention_until,
        legal_hold=h.legal_hold,
        is_current=is_current,
    )


class InventorySource(Protocol):
    method: str  # "inventory" | "listing"
    #: True when `items` yields every stored version, including those of keys
    #: with no current version. When False, reconcile asks `probe` about the
    #: keys it found missing.
    complete_versions: bool

    @property
    def snapshot_at(self) -> datetime: ...

    def items(self, container: str) -> AsyncIterator[InventoryItem]: ...

    async def probe(self, container: str, keys: Sequence[str]) -> list[InventoryItem]: ...


class ListingInventory:
    """A paged listing read as an inventory, with every version.

    With `VersionListing` (sim, Azure ``include=versions``) one paged call
    yields every stored version. Otherwise it lists current versions and asks
    `list_versions` for each key's older ones; versions of keys that have no
    current version are found by `probe`.
    """

    method = "listing"

    def __init__(
        self, backend: StorageBackend, *, prefix: str = "", detail: str | None = None
    ) -> None:
        self._backend = backend
        self._prefix = prefix
        self._snapshot_at = backend.clock.now()
        self.complete_versions = isinstance(backend, VersionListing)
        #: Why a listing (no report configured, or none usable), for the run.
        self.detail = detail

    @property
    def snapshot_at(self) -> datetime:
        return self._snapshot_at

    async def items(self, container: str) -> AsyncIterator[InventoryItem]:
        self._snapshot_at = self._backend.clock.now()
        cursor: str | None = None
        if isinstance(self._backend, VersionListing):
            while True:
                vpage = await self._backend.list_all_versions(container, self._prefix, cursor)
                for lv in vpage.items:
                    yield _item(lv.head, lv.is_current)
                if vpage.cursor is None:
                    return
                cursor = vpage.cursor
        while True:
            page = await self._backend.list(container, self._prefix, cursor)
            for h in page.items:
                yield _item(h, True)
                for it in await self._older(h.ref, h.ref.version_id):
                    yield it
            if page.cursor is None:
                return
            cursor = page.cursor

    async def _older(self, ref: ObjectRef, current: str | None) -> list[InventoryItem]:
        out = []
        try:
            versions = await self._backend.list_versions(ref)
        except ObjectNotFound:
            return []
        for v in versions:
            if v.is_delete_marker or v.ref.version_id in (None, current):
                continue
            out.append(_item(await self._backend.head(v.ref), False))
        return out

    async def probe(self, container: str, keys: Sequence[str]) -> list[InventoryItem]:
        if self.complete_versions:
            return []
        out: list[InventoryItem] = []
        for key in keys:
            ref = ObjectRef(self._backend.backend_id, container, key)
            out += await self._older(ref, None)
        return out


class PrefixedInventory:
    """Listings of several prefixes of one container, read as one inventory
    (a bucket's source prefixes)."""

    method = "listing"

    def __init__(self, backend: StorageBackend, prefixes: Sequence[str]) -> None:
        self._parts = [ListingInventory(backend, prefix=p) for p in prefixes] or [
            ListingInventory(backend)
        ]
        self.complete_versions = self._parts[0].complete_versions
        self.detail = f"source prefixes: {', '.join(prefixes) or '(all)'}"[:1000]

    @property
    def snapshot_at(self) -> datetime:
        return self._parts[0].snapshot_at  # the first listing started first

    async def items(self, container: str) -> AsyncIterator[InventoryItem]:
        for part in self._parts:
            async for it in part.items(container):
                yield it

    async def probe(self, container: str, keys: Sequence[str]) -> list[InventoryItem]:
        return await self._parts[0].probe(container, keys)


def inventory_for(backend: StorageBackend, why: str = "no inventory configured") -> InventorySource:
    """The listing inventory for a non-WORM account, and why it lists."""
    return ListingInventory(backend, detail=why)


async def choose_inventory(
    backend: StorageBackend,
    container: str,
    config: Mapping[str, Any] | None,
    now: datetime,
) -> InventorySource:
    """A non-WORM container's inventory: its account's newest usable Blob
    Inventory report, or a listing when there is none (logged with why).

    `config` is the backend's row config. Its ``inventory`` names the rule::

        {"inventory": {"container": "inventory", "rule": "omnifile-reconcile",
                       "max_age_hours": 48,
                       "accounts": {"archive": {"container": "inv", "rule": "r"}}}}

    `container` and `rule` apply to every account of the binding (each
    account has its own inventory policy, writing into a container of its
    own); `accounts` overrides them per account. Accounts with version-level
    immutability can't run Blob Inventory [W], so a WORM backend never uses one.
    """
    from .blob_inventory import BlobInventory, InventoryError, latest_report

    spec = dict((config or {}).get("inventory") or {})
    if not spec:
        return inventory_for(backend)
    if backend.capabilities().worm:
        why = "version-level WORM accounts have no Blob Inventory"
        log.warning("backend %s: %s; reconciling %s from a listing", backend.backend_id, why,
                    container)  # fmt: skip
        return inventory_for(backend, why)
    account = container.split("/", 1)[0] if "/" in container else ""
    spec.update((spec.get("accounts") or {}).get(account) or {})
    dest, rule = spec.get("container"), spec.get("rule")
    if not dest or not rule:
        why = "the inventory config needs container and rule"
        log.warning("backend %s: %s", backend.backend_id, why)
        return inventory_for(backend, why)
    destination = f"{account}/{dest}" if account and "/" not in str(dest) else str(dest)
    default = getattr(backend, "_default_tier", None)
    try:
        report = await latest_report(
            backend,
            destination=destination,
            rule=str(rule),
            container=container,
            versioned=backend.capabilities().versioning,
            now=now,
            max_age=timedelta(hours=float(spec.get("max_age_hours", 48))),
        )
    except InventoryError as e:
        log.warning("reconcile %s: no usable Blob Inventory report (%s); listing instead",
                    container, e)  # fmt: skip
        return inventory_for(backend, f"no usable Blob Inventory report: {e}"[:1000])
    return BlobInventory(
        backend,
        report,
        default_tier=spec.get("default_tier") or (default(account) if callable(default) else None),
    )
