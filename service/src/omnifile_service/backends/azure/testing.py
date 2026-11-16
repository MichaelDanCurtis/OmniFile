"""Wiring for the real-Azure contract lane (test strategy layer 3).

`contract_factories()` builds backends against a real Azure dev subscription
for `service/tests/backend_contract`, opted in with ``OMNIFILE_TEST_AZURE=1``:

    OMNIFILE_TEST_AZURE_LIVE_URL     https://<zrs-smart-account>.blob.core.windows.net
    OMNIFILE_TEST_AZURE_ARCHIVE_URL  https://<lrs-archive-account>.blob.core.windows.net
    OMNIFILE_TEST_AZURE_WORM_URL     optional: an LRS account with versioning and
                                     version-level immutability

Auth is `DefaultAzureCredential` (an `az login` session or a CI workload
identity) with Storage Blob Data Owner and Storage Blob Delegator on the
accounts. Containers are created on first use. Nothing here creates
accounts: the short-lived resources come from the scheduled lane's IaC.

Two adaptations, because the contract suite calls ``asyncio.run`` per
operation and passes a hand-moved clock:

- `LoopThreadBackend` runs the backend on its own event-loop thread. The
  SDK's aiohttp sessions are bound to one loop, and the suite's loops are
  short-lived.
- The suite's `ManualClock` is moved to wall time when the backend is
  built, because Azure checks SAS start and expiry against real time.
  Contract tests that advance the clock to "finish" a rehydrate can't pass
  against real Azure (a rehydrate takes hours); the lane runs them on a long
  schedule or skips them by name.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterable, AsyncIterator, Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ..base import (
    BackendKind,
    Capabilities,
    ChangePage,
    Clock,
    CommittedBlock,
    EdgeAccess,
    EdgeCredential,
    LogicalTier,
    ObjectHead,
    ObjectPage,
    ObjectRef,
    ObjectVersion,
    RestoreHandle,
    RestoreSpeed,
    RestoreStatus,
    StorageBackend,
)
from .backend import AzureBlobBackend
from .config import AzureAccount, AzureBlobConfig

ENV_ENABLE = "OMNIFILE_TEST_AZURE"


class LoopThreadBackend:
    """A `StorageBackend` proxy that runs every call on one private event
    loop in a daemon thread, so callers on any loop share one SDK client."""

    def __init__(self, build: Callable[[], StorageBackend]) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        async def make() -> StorageBackend:
            return build()

        self._inner = asyncio.run_coroutine_threadsafe(make(), self._loop).result()

    async def _call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    @property
    def inner(self) -> StorageBackend:
        return self._inner

    @property
    def backend_id(self) -> int:
        return self._inner.backend_id

    @property
    def kind(self) -> BackendKind:
        return self._inner.kind

    @property
    def clock(self) -> Clock:
        return self._inner.clock

    def capabilities(self) -> Capabilities:
        return self._inner.capabilities()

    async def put(
        self,
        container: str,
        key: str,
        stream: AsyncIterable[bytes],
        tier: LogicalTier | None,
        metadata: Mapping[str, str],
        *,
        crc64nvme: bytes | None = None,
    ) -> ObjectRef:
        data = b"".join([c async for c in stream])

        async def one() -> AsyncIterator[bytes]:
            yield data

        return await self._call(
            self._inner.put(container, key, one(), tier, metadata, crc64nvme=crc64nvme)
        )

    async def get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        async def collect() -> bytes:
            return b"".join([c async for c in self._inner.get_range(ref, offset, length)])

        data = await self._call(collect())
        if data:
            yield data

    async def head(self, ref: ObjectRef) -> ObjectHead:
        return await self._call(self._inner.head(ref))

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        return await self._call(self._inner.committed_blocks(ref))

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        return await self._call(self._inner.list(container, prefix, cursor))

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        return await self._call(self._inner.list_versions(ref))

    async def list_all_versions(self, container: str, prefix: str, cursor: str | None):
        return await self._call(self._inner.list_all_versions(container, prefix, cursor))

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        return await self._call(self._inner.set_class(ref, tier, native_class=native_class))

    async def copy_object(
        self, src: ObjectRef, dst_container: str, dst_key: str, tier: LogicalTier | None = None
    ) -> ObjectRef:
        return await self._call(self._inner.copy_object(src, dst_container, dst_key, tier))

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        return await self._call(self._inner.restore(ref, speed, days))

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        return await self._call(self._inner.restore_status(handle))

    async def delete(self, ref: ObjectRef) -> None:
        await self._call(self._inner.delete(ref))

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        await self._call(self._inner.set_retention(ref, until, locked=locked))

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        await self._call(self._inner.set_legal_hold(ref, on))

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        await self._call(self._inner.set_tags(ref, tags))

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        return await self._call(self._inner.get_tags(ref))

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        return await self._call(self._inner.set_metadata(ref, metadata))

    async def edge_credential(
        self,
        ref: ObjectRef,
        access: EdgeAccess,
        ttl: timedelta,
        *,
        if_match: str | None = None,
    ) -> EdgeCredential:
        return await self._call(self._inner.edge_credential(ref, access, ttl, if_match=if_match))

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        return await self._call(self._inner.read_changes(container, cursor))


def _sync_to_wall_time(clock: Clock) -> None:
    advance = getattr(clock, "advance", None)
    gap = datetime.now(UTC) - clock.now()
    if advance is not None and gap > timedelta(0):
        advance(gap)


def contract_factories() -> dict[str, Callable[[Clock], StorageBackend]]:
    """Factories for the contract registry, or {} when not opted in."""
    if os.environ.get(ENV_ENABLE) != "1":
        return {}
    live = os.environ["OMNIFILE_TEST_AZURE_LIVE_URL"]
    archive = os.environ["OMNIFILE_TEST_AZURE_ARCHIVE_URL"]
    worm = os.environ.get("OMNIFILE_TEST_AZURE_WORM_URL")

    def name(url: str) -> str:
        return url.split("://", 1)[-1].split(".", 1)[0]

    def two_account(clock: Clock) -> StorageBackend:
        _sync_to_wall_time(clock)
        config = AzureBlobConfig(
            accounts={
                name(live): AzureAccount(name(live), live),
                name(archive): AzureAccount(name(archive), archive),
            },
            layout="two_account",
            primary_account=name(live),
            archive_account=name(archive),
            smart=True,
            auto_create_containers=True,
        )
        return LoopThreadBackend(lambda: AzureBlobBackend(9001, config, clock=clock))

    factories: dict[str, Callable[[Clock], StorageBackend]] = {"azure": two_account}
    if worm:

        def worm_account(clock: Clock) -> StorageBackend:
            _sync_to_wall_time(clock)
            config = AzureBlobConfig(
                accounts={name(worm): AzureAccount(name(worm), worm)},
                layout="single_account",
                primary_account=name(worm),
                worm=True,
                auto_create_containers=True,
            )
            return LoopThreadBackend(lambda: AzureBlobBackend(9002, config, clock=clock))

        factories["azure-worm"] = worm_account
    return factories
