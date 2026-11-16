"""Minimal in-memory backend: the reference for the basic contract.

Hot tier only, versioning on, no archive, no WORM. It exists so the contract
suite runs before the simulated backend (sim) and the real ones do; it is not
a substitute for sim.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from omnifile_service.backends.base import (
    BackendError,
    BackendKind,
    Capabilities,
    ChangeKind,
    ChangePage,
    ChangeRecord,
    Clock,
    CommittedBlock,
    CredentialMode,
    EdgeAccess,
    EdgeCredential,
    LogicalTier,
    ObjectHead,
    ObjectNotFound,
    ObjectPage,
    ObjectRef,
    ObjectVersion,
    PrivateNetworkOption,
    RestoreHandle,
    RestoreSpeed,
    RestoreState,
    RestoreStatus,
    TierInfo,
    UnsupportedOperation,
)

PAGE = 2  # small, so pagination is exercised


@dataclass
class _Version:
    version_id: str
    data: bytes
    last_modified: datetime
    deleted: bool = False


class MemoryBackend:
    def __init__(self, clock: Clock, backend_id: int = 1) -> None:
        self._clock = clock
        self._id = backend_id
        self._objects: dict[tuple[str, str], list[_Version]] = {}
        self._changes: list[ChangeRecord] = []
        self._tags: dict[tuple[str, str], dict[str, str]] = {}
        self._metadata: dict[tuple[str, str], dict[str, str]] = {}
        self._seq = 0

    @property
    def backend_id(self) -> int:
        return self._id

    @property
    def kind(self) -> BackendKind:
        return BackendKind.SIM

    @property
    def clock(self) -> Clock:
        return self._clock

    def _ref(self, container: str, key: str, version_id: str | None = None) -> ObjectRef:
        return ObjectRef(self._id, container, key, version_id)

    def _find(self, ref: ObjectRef) -> _Version:
        versions = self._objects.get((ref.container, ref.key), [])
        if ref.version_id is None:
            if versions and not versions[-1].deleted:
                return versions[-1]
        else:
            for v in versions:
                if v.version_id == ref.version_id and not v.deleted:
                    return v
        raise ObjectNotFound(f"{ref.container}/{ref.key}@{ref.version_id}")

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
        if tier not in (None, LogicalTier.HOT):
            raise UnsupportedOperation(f"memory backend has no {tier} tier")
        data = b"".join([chunk async for chunk in stream])
        self._seq += 1
        version = _Version(f"v{self._seq}", data, self._clock.now())
        self._objects.setdefault((container, key), []).append(version)
        self._metadata[(container, key)] = dict(metadata)
        ref = self._ref(container, key, version.version_id)
        self._changes.append(
            ChangeRecord(ChangeKind.CREATED, ref, version.last_modified, "hot", len(data))
        )
        return ref

    async def get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        data = self._find(ref).data[offset : offset + length]
        for i in range(0, len(data), 4):
            yield data[i : i + 4]

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        self._find(ref)
        raise UnsupportedOperation("the memory backend keeps no block lists")

    async def head(self, ref: ObjectRef) -> ObjectHead:
        v = self._find(ref)
        return ObjectHead(
            ref=self._ref(ref.container, ref.key, v.version_id),
            size=len(v.data),
            native_class="hot",
            logical_tier=LogicalTier.HOT,
            restore_state=RestoreState.NOT_NEEDED,
            md5=hashlib.md5(v.data).digest(),
            metadata=dict(self._metadata.get((ref.container, ref.key), {})),
            last_modified=v.last_modified,
        )

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        keys = sorted(
            k
            for (c, k), vs in self._objects.items()
            if c == container and k.startswith(prefix) and not vs[-1].deleted
        )
        start = int(cursor) if cursor else 0
        page = keys[start : start + PAGE]
        items = [await self.head(self._ref(container, k)) for k in page]
        nxt = start + PAGE
        return ObjectPage(items, str(nxt) if nxt < len(keys) else None)

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        versions = self._objects.get((ref.container, ref.key), [])
        out = [
            ObjectVersion(
                ref=self._ref(ref.container, ref.key, v.version_id),
                is_current=(i == len(versions) - 1),
                is_delete_marker=v.deleted,
                size=len(v.data),
                last_modified=v.last_modified,
                native_class="hot",
            )
            for i, v in enumerate(versions)
        ]
        return list(reversed(out))

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        raise UnsupportedOperation("memory backend has only the hot tier")

    async def copy_object(
        self,
        src: ObjectRef,
        dst_container: str,
        dst_key: str,
        tier: LogicalTier | None = None,
    ) -> ObjectRef:
        data = self._find(src).data

        async def one() -> AsyncIterator[bytes]:
            yield data

        return await self.put(dst_container, dst_key, one(), tier, {})

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        raise UnsupportedOperation("nothing to restore")

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        return RestoreStatus(RestoreState.NOT_NEEDED)

    async def delete(self, ref: ObjectRef) -> None:
        self._find(ref)
        self._seq += 1
        marker = _Version(f"v{self._seq}", b"", self._clock.now(), deleted=True)
        self._objects[(ref.container, ref.key)].append(marker)
        self._changes.append(
            ChangeRecord(
                ChangeKind.DELETED, self._ref(ref.container, ref.key), marker.last_modified
            )
        )

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        raise UnsupportedOperation("no WORM")

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        raise UnsupportedOperation("no WORM")

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        self._find(ref)
        if len(tags) > self.capabilities().max_tags:
            raise BackendError("too many tags")
        self._tags[(ref.container, ref.key)] = dict(tags)

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        self._find(ref)
        return dict(self._tags.get((ref.container, ref.key), {}))

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        self._find(ref)
        self._metadata[(ref.container, ref.key)] = dict(metadata)
        return ref

    async def edge_credential(
        self,
        ref: ObjectRef,
        access: EdgeAccess,
        ttl: timedelta,
        *,
        if_match: str | None = None,
    ) -> EdgeCredential:
        return EdgeCredential(
            CredentialMode.AZURE_USER_DELEGATION_SAS,
            access,
            self._clock.now() + ttl,
            url=f"memory://{ref.container}/{ref.key}?sig=test",
        )

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        start = int(cursor) if cursor is not None else len(self._changes)
        records = [r for r in self._changes[start:] if r.ref.container == container]
        return ChangePage(records, str(len(self._changes)), caught_up=True)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=BackendKind.SIM,
            tiers={LogicalTier.HOT: [TierInfo(LogicalTier.HOT, "hot", None, False)]},
            restore_options=[],
            throughput_caps=[],
            private_network=[PrivateNetworkOption("none", ("TLSv1.2", "TLSv1.3"))],
            credential_modes=[CredentialMode.AZURE_USER_DELEGATION_SAS],
            versioning=True,
        )
