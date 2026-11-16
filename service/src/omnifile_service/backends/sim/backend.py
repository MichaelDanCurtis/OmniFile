"""The simulated storage backend ("sim").

One class, two flavours (`Flavour.AZURE`, `Flavour.S3`), each obeying its
provider's verified rules on a virtual clock. See README.md in this package
for what is modelled, what is not, and where each rule comes from.

How time works: sim never sleeps and never reads the wall clock. Restore
completions, S3 expiries and in-place rehydrates are scheduled events; at the
start of every call, events due by `clock.now()` are applied in time order
(`_settle`), and their change-feed records carry the time they fell due.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import heapq
import itertools
import os
import secrets
import shutil
import tempfile
import threading
import weakref
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from ..base import (
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
    Immutable,
    ListedVersion,
    LogicalTier,
    NotRestored,
    ObjectHead,
    ObjectNotFound,
    ObjectPage,
    ObjectRef,
    ObjectVersion,
    RestoreHandle,
    RestoreSpeed,
    RestoreState,
    RestoreStatus,
    Throttled,
    TierInfo,
    UnsupportedOperation,
    VersionPage,
)
from ..copy_check import copy_mismatch
from . import capabilities as caps
from . import credentials
from .crc64 import crc64nvme, to_bytes
from .faults import CopyFault, FaultInjector, Op
from .payload import (
    CorruptPayload,
    FilePayload,
    MemoryPayload,
    Payload,
    SyntheticPayload,
    TruncatedPayload,
)

MiB = 1024 * 1024


class Flavour(StrEnum):
    AZURE = "azure"
    S3 = "s3"


class Redundancy(StrEnum):
    LRS = "LRS"
    GRS = "GRS"
    RA_GRS = "RA-GRS"
    ZRS = "ZRS"
    GZRS = "GZRS"
    RA_GZRS = "RA-GZRS"

    @property
    def zonal(self) -> bool:
        """Smart tier needs these; Archive is not allowed on them [V]."""
        return self in (Redundancy.ZRS, Redundancy.GZRS, Redundancy.RA_GZRS)


@dataclass(frozen=True)
class AccountConfig:
    """An Azure storage account."""

    name: str
    redundancy: Redundancy = Redundancy.LRS
    #: Smart as the account's default access tier (GA, zonal accounts only) [V].
    smart_default: bool = False
    versioning: bool = True
    #: Version-level immutability support (needs versioning) [W].
    version_level_worm: bool = False

    def __post_init__(self) -> None:
        if self.smart_default and not self.redundancy.zonal:
            raise ValueError("Smart tier needs ZRS, GZRS or RA-GZRS")
        if self.version_level_worm and not self.versioning:
            raise ValueError("version-level WORM needs versioning")


@dataclass(frozen=True)
class SimTimings:
    """How long things take on the virtual clock. Defaults are the providers'
    stated upper bounds, so tests see the pessimistic case."""

    azure_standard: timedelta = timedelta(hours=15)
    azure_high_min: timedelta = timedelta(hours=1)
    azure_high_bytes_per_hour: int = caps.AZ_HIGH_PRIORITY_BYTES_PER_HOUR
    #: Above 10 GB Azure promises nothing; sim adds this rate on top of 15 h.
    azure_standard_extra_bytes_per_hour: int = caps.AZ_HIGH_PRIORITY_BYTES_PER_HOUR
    s3: Mapping[tuple[str, RestoreSpeed], timedelta] = field(
        default_factory=lambda: {
            (caps.S3_GLACIER, RestoreSpeed.EXPEDITED): timedelta(minutes=5),
            (caps.S3_GLACIER, RestoreSpeed.STANDARD): timedelta(hours=5),
            (caps.S3_GLACIER, RestoreSpeed.BULK): timedelta(hours=12),
            (caps.S3_DEEP_ARCHIVE, RestoreSpeed.STANDARD): timedelta(hours=12),
            (caps.S3_DEEP_ARCHIVE, RestoreSpeed.BULK): timedelta(hours=48),
            (caps.IT_ARCHIVE_ACCESS, RestoreSpeed.EXPEDITED): timedelta(minutes=5),
            (caps.IT_ARCHIVE_ACCESS, RestoreSpeed.STANDARD): timedelta(hours=5),
            (caps.IT_ARCHIVE_ACCESS, RestoreSpeed.BULK): timedelta(hours=12),
            (caps.IT_DEEP_ARCHIVE_ACCESS, RestoreSpeed.STANDARD): timedelta(hours=12),
            (caps.IT_DEEP_ARCHIVE_ACCESS, RestoreSpeed.BULK): timedelta(hours=48),
        }
    )


@dataclass(frozen=True)
class EarlyDeletionFee:
    """A minimum-duration charge sim recorded (it never blocks the operation)."""

    ref: ObjectRef
    native_class: str
    size: int
    tier_since: datetime
    at: datetime
    minimum_days: int

    @property
    def days_charged(self) -> float:
        """Days of the minimum that were not served and get billed anyway."""
        served = (self.at - self.tier_since).total_seconds() / 86400
        return max(0.0, self.minimum_days - served)


@dataclass(frozen=True)
class ComputedChecksum:
    crc64nvme: bytes
    md5: bytes


class Conflict(BackendError):
    """The object is busy (e.g. a rehydrate is pending on it)."""


class ChecksumMismatch(BackendError):
    """The caller's checksum does not match the bytes received."""


# --- internal state -------------------------------------------------------


@dataclass
class _Pending:
    """An Azure rehydrate in flight on this version (copy target or in place)."""

    to_class: str
    done_at: datetime
    restore_id: str | None = None


@dataclass
class _Version:
    vid: str
    payload: Payload | None  # None: S3 delete marker
    created_at: datetime
    native_class: str
    tier_since: datetime
    metadata: dict[str, str]
    explicit_tier: bool
    block_crcs: list[tuple[int, int]]
    crc64: int | None
    md5: bytes | None
    etag: str
    retention_until: datetime | None = None
    retention_locked: bool = False
    legal_hold: bool = False
    pending: _Pending | None = None
    # S3 restore state
    restore_id: str | None = None
    restored_until: datetime | None = None
    restore_expired: bool = False
    it_tier: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def delete_marker(self) -> bool:
        return self.payload is None

    @property
    def size(self) -> int:
        return self.payload.size if self.payload is not None else 0


@dataclass
class _Key:
    name: str
    versions: list[_Version] = field(default_factory=list)  # oldest first
    #: Azure: False after the base blob is deleted with versioning on (every
    #: version becomes a previous version). S3 uses delete markers instead.
    has_current: bool = True


@dataclass
class _Container:
    name: str  # canonical: "account/container" (Azure) or bucket (S3)
    account: str
    versioning: bool
    worm: bool  # version-level immutability enabled
    policy_days: int | None = None  # container default time-based retention
    policy_locked: bool = False
    archive_class: str = caps.S3_GLACIER
    keys: dict[str, _Key] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.name.split("/", 1)[-1]


@dataclass
class _Restore:
    rid: str
    container: str
    key: str
    vid: str
    speed: RestoreSpeed
    size: int
    requested_at: datetime
    done_at: datetime
    eta: datetime | None
    days: int | None
    fail: str | None
    it: bool = False
    state: RestoreState = RestoreState.IN_PROGRESS
    expires_at: datetime | None = None
    target: ObjectRef | None = None
    detail: str | None = None


def _next_midnight(t: datetime) -> datetime:
    """S3 rounds a restore's expiry up to the next midnight UTC."""
    t = t.astimezone(UTC)
    midnight = datetime.combine(t.date(), time(0), tzinfo=UTC)
    return midnight if midnight == t else midnight + timedelta(days=1)


class SimBackend:
    """A `StorageBackend` that obeys Azure's or S3's rules on a virtual clock.

    Construct with `SimBackend.azure_two_account`, `SimBackend.azure_worm` or
    `SimBackend.s3`, or directly for a custom layout.
    """

    def __init__(
        self,
        clock: Clock,
        flavour: Flavour = Flavour.AZURE,
        *,
        backend_id: int = 1,
        accounts: Sequence[AccountConfig] = (),
        default_account: str | None = None,
        archive_account: str | None = None,
        recall_container: str | None = None,
        timings: SimTimings | None = None,
        feed_lag: timedelta = timedelta(0),
        feed_page_size: int = 1000,
        list_page_size: int = 1000,
        read_chunk_size: int = 4 * MiB,
        spill_threshold: int | None = None,
        auto_create_containers: bool = True,
        s3_versioning: bool = True,
        s3_credential_mode: CredentialMode = CredentialMode.S3_ROLES_ANYWHERE,
        region: str = "sim-region-1",
        restore_rate_limit: int = caps.S3_RESTORE_REQUESTS_PER_SECOND,
    ) -> None:
        self._clock = clock
        self.flavour = Flavour(flavour)
        self._id = backend_id
        self._accounts = {a.name: a for a in accounts}
        if self.flavour is Flavour.AZURE:
            if not self._accounts:
                raise ValueError("an Azure sim needs at least one account")
            self._default_account = default_account or next(iter(self._accounts))
            if self._default_account not in self._accounts:
                raise ValueError(f"unknown default account {self._default_account}")
            if archive_account is not None:
                arch = self._accounts[archive_account]
                if arch.redundancy.zonal:
                    raise ValueError("the archive account can't be zonal: no Archive on ZRS")
        else:
            self._default_account = "aws"
        self._archive_account = archive_account
        self._recall_container = recall_container
        self.timings = timings or SimTimings()
        self.feed_lag = feed_lag
        self.feed_page_size = feed_page_size
        self.list_page_size = list_page_size
        self.read_chunk_size = read_chunk_size
        self._spill_threshold = spill_threshold
        self._spill_dir: str | None = None
        self._auto_create = auto_create_containers
        self._s3_versioning = s3_versioning
        self._s3_credential_mode = s3_credential_mode
        self.region = region
        self.restore_rate_limit = restore_rate_limit

        self.faults = FaultInjector()
        self.early_deletion_fees: list[EarlyDeletionFee] = []
        self._containers: dict[str, _Container] = {}
        self._changes: list[ChangeRecord] = []
        self._restores: dict[str, _Restore] = {}
        self._events: list[tuple[datetime, int, str, str]] = []
        self._seq = itertools.count(1)
        self._lock = threading.RLock()
        self._secret = secrets.token_bytes(32)
        self._priority_free_at: dict[str, datetime] = {}
        self._restore_window: tuple[int, int] = (0, 0)

        if self.flavour is Flavour.AZURE:
            primary = self._accounts[self._default_account]
            self._caps = caps.azure_capabilities(
                smart=primary.smart_default,
                worm=any(a.version_level_worm for a in self._accounts.values()),
                in_place_archive=not primary.redundancy.zonal,
            )
            self._tiers = caps.AZURE_TIERS
        else:
            self._caps = caps.s3_capabilities()
            self._tiers = caps.S3_TIERS
        self._native = caps.native_info(self._tiers)
        if self._recall_container:
            self._ensure_container(self._recall_container, worm=False)

    # --- constructors -------------------------------------------------

    @classmethod
    def azure_two_account(cls, clock: Clock, **kw: Any) -> SimBackend:
        """Non-WORM layout [A]: a ZRS live account with Smart as default, and
        an LRS archive account that archive moves copy into."""
        accounts = [
            AccountConfig("live", Redundancy.ZRS, smart_default=True),
            AccountConfig("archive", Redundancy.LRS),
        ]
        kw.setdefault("recall_container", "live/recall")
        return cls(
            clock,
            Flavour.AZURE,
            accounts=accounts,
            default_account="live",
            archive_account="archive",
            **kw,
        )

    @classmethod
    def azure_worm(cls, clock: Clock, **kw: Any) -> SimBackend:
        """WORM layout [W]: one LRS account with versioning and version-level
        WORM; tiering in place; recalls into a non-WORM recall container."""
        accounts = [AccountConfig("worm", Redundancy.LRS, version_level_worm=True)]
        kw.setdefault("recall_container", "worm/recall")
        return cls(
            clock,
            Flavour.AZURE,
            accounts=accounts,
            default_account="worm",
            **kw,
        )

    @classmethod
    def s3(cls, clock: Clock, **kw: Any) -> SimBackend:
        return cls(clock, Flavour.S3, **kw)

    # --- StorageBackend properties --------------------------------------

    @property
    def backend_id(self) -> int:
        return self._id

    @property
    def kind(self) -> BackendKind:
        return BackendKind.SIM

    @property
    def clock(self) -> Clock:
        return self._clock

    def capabilities(self) -> Capabilities:
        return self._caps

    # --- setup helpers (sim only) ---------------------------------------

    def create_container(
        self,
        container: str,
        *,
        versioning: bool | None = None,
        worm: bool | None = None,
        retention_days: int | None = None,
        policy_locked: bool = False,
        archive_class: str | None = None,
    ) -> str:
        """Create a container (bucket) and return its canonical name.

        `worm` enables version-level immutability (Azure only; needs the
        account's support and versioning). `retention_days` adds a container
        default time-based policy applied to every version.
        """
        with self._lock:
            name = self._canonical(container)
            if name in self._containers:
                raise BackendError(f"container {name} exists")
            c = self._ensure_container(container, worm=worm, versioning=versioning)
            if archive_class is not None:
                if archive_class not in (caps.S3_GLACIER, caps.S3_DEEP_ARCHIVE):
                    raise ValueError("archive_class is GLACIER or DEEP_ARCHIVE")
                c.archive_class = archive_class
            if retention_days is not None:
                self.set_container_policy(name, retention_days, locked=policy_locked)
            return name

    def set_container_policy(self, container: str, retention_days: int, *, locked: bool) -> None:
        """Set a container's default retention. A locked policy can be extended,
        never shortened, and never unlocked [W]."""
        with self._lock:
            c = self._get_container(container)
            if self.flavour is not Flavour.AZURE:
                raise UnsupportedOperation("S3 Object Lock is not modelled")
            if c.policy_locked:
                if not locked:
                    raise Immutable("a locked policy can't be unlocked")
                if c.policy_days is not None and retention_days < c.policy_days:
                    raise Immutable("a locked policy can be extended, never shortened")
            c.policy_days = retention_days
            c.policy_locked = locked

    def delete_container_policy(self, container: str) -> None:
        with self._lock:
            c = self._get_container(container)
            if c.policy_locked:
                raise Immutable("a locked policy can't be deleted")
            c.policy_days = None

    async def put_bytes(
        self,
        container: str,
        key: str,
        data: bytes,
        tier: LogicalTier | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        """`put` of bytes in hand: what crosses the sim RPC (`sim_http`),
        where a stream cannot."""

        async def one() -> AsyncIterator[bytes]:
            yield data

        return await self.put(container, key, one(), tier, dict(metadata or {}))

    def put_synthetic(
        self,
        container: str,
        key: str,
        size: int,
        tier: LogicalTier | None = None,
        metadata: Mapping[str, str] | None = None,
        *,
        seed: int = 0,
    ) -> ObjectRef:
        """Store an object of `size` bytes without holding them (a pattern is
        generated on read). No checksums are recorded: computing CRC64 over
        gigabytes in pure Python is too slow; use `compute_checksum` if needed."""
        with self._lock:
            self._settle()
            return self._commit(
                container,
                key,
                SyntheticPayload(size, seed),
                tier,
                dict(metadata or {}),
                block_crcs=[],
                crc=None,
                md5=None,
            )

    def it_archive(self, ref: ObjectRef, *, deep: bool = False) -> bool:
        """Move an S3 Intelligent-Tiering object into its opt-in Archive Access
        (or Deep Archive Access) tier, as S3 would after 90/180 days without
        access. Objects under 128 KB are never tiered: returns False [V]."""
        with self._lock:
            self._settle()
            c, _, v = self._resolve(ref)
            if self.flavour is not Flavour.S3 or v.native_class != caps.S3_INTELLIGENT_TIERING:
                raise UnsupportedOperation("only S3 Intelligent-Tiering objects")
            if v.size < caps.S3_IT_MIN_MONITORED:
                return False
            v.it_tier = caps.IT_DEEP_ARCHIVE_ACCESS if deep else caps.IT_ARCHIVE_ACCESS
            v.tier_since = self._now()
            self._emit(ChangeKind.TIER_CHANGED, c, ref.key, v, detail={"it_tier": v.it_tier})
            return True

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        """Azure: the blocks as `put` staged them, named as the Azure
        backend's `put` names them on the wire (base64 of the zero-padded
        index), with their CRCs. S3 keeps no block list."""
        await self._gate(Op.HEAD)
        if self.flavour is Flavour.S3:
            raise UnsupportedOperation("S3 has no committed block list")
        with self._lock:
            self._settle()
            _, _, v = self._resolve(ref)
            if v.payload is None:
                raise ObjectNotFound(f"{ref.container}/{ref.key} is a delete marker")
            return [
                CommittedBlock(base64.b64encode(f"{i:08d}".encode()).decode(), n, to_bytes(c))
                for i, (n, c) in enumerate(v.block_crcs)
            ]

    def block_checksums(self, ref: ObjectRef) -> list[tuple[int, bytes]]:
        """[(block_length, CRC64-NVME)] as uploaded (Azure x-ms-content-crc64 per Put Block)."""
        with self._lock:
            _, _, v = self._resolve(ref)
            return [(n, to_bytes(c)) for n, c in v.block_crcs]

    async def compute_checksum(self, ref: ObjectRef) -> ComputedChecksum:
        """Checksum the stored bytes, whatever their class.

        S3: Batch Operations Compute checksum, which works on every class
        without a restore [V]. Azure: a read of an online blob (archived blobs
        raise `NotRestored`). Either way, the decision-24 verifier's view of
        what is really stored, so injected corruption shows up here.
        """
        await self._gate(Op.COMPUTE_CHECKSUM)
        with self._lock:
            self._settle()
            c, _, v = self._resolve(ref)
            if self.flavour is Flavour.AZURE and not self._readable(v):
                raise NotRestored(f"{c.name}/{ref.key} is archived")
            payload = v.payload
        assert payload is not None
        crc = 0
        md5 = hashlib.md5()
        for pos in range(0, payload.size, self.read_chunk_size):
            chunk = payload.read(pos, self.read_chunk_size)
            crc = crc64nvme(chunk, crc)
            md5.update(chunk)
        return ComputedChecksum(to_bytes(crc), md5.digest())

    def validate_credential(
        self, cred: EdgeCredential | str, ref: ObjectRef, access: EdgeAccess
    ) -> None:
        """Raise `CredentialRejected` unless the credential grants `access` to
        exactly `ref` (and its version, for reads) at the current time."""
        url = cred if isinstance(cred, str) else cred.url
        if url is None:
            raise credentials.CredentialRejected("credential carries no URL")
        with self._lock:
            host, cont = self._credential_scope(ref.container)
        credentials.validate_url(
            self._secret, url, host, cont, ref.key, access, self._now(), ref.version_id
        )

    def priority_queue_clear_at(self, container: str) -> datetime:
        """When the account's High-priority rehydrate pipeline drains (Azure)."""
        with self._lock:
            account = self._account_of(container)
            return max(self._now(), self._priority_free_at.get(account, self._now()))

    def close(self) -> None:
        """Remove spilled payload files."""
        if self._spill_dir:
            shutil.rmtree(self._spill_dir, ignore_errors=True)
            self._spill_dir = None

    # --- StorageBackend: data path --------------------------------------

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
        await self._gate(Op.PUT)
        chunks = [bytes(chunk) async for chunk in stream if chunk]
        data = b"".join(chunks)
        block_crcs = [(len(chunk), _crc(chunk)) for chunk in chunks]
        whole = _crc(data)
        if crc64nvme is not None and crc64nvme != to_bytes(whole):
            raise ChecksumMismatch(f"{container}/{key}: CRC64-NVME mismatch")
        with self._lock:
            self._settle()
            return self._commit(
                container,
                key,
                self._store(data),
                tier,
                dict(metadata),
                block_crcs=block_crcs,
                crc=whole,
                md5=hashlib.md5(data).digest(),
            )

    async def get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be >= 0")
        await self._gate(Op.GET_RANGE)
        with self._lock:
            self._settle()
            c, _, v = self._resolve(ref)
            if not self._readable(v):
                raise NotRestored(f"{c.name}/{ref.key} is archived and not restored")
            payload = v.payload
        assert payload is not None
        end = min(offset + length, payload.size)
        pos = offset
        while pos < end:
            n = min(self.read_chunk_size, end - pos)
            yield payload.read(pos, n)
            pos += n

    async def head(self, ref: ObjectRef) -> ObjectHead:
        await self._gate(Op.HEAD)
        with self._lock:
            self._settle()
            c, k, v = self._resolve(ref)
            return self._head(c, k, v)

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        await self._gate(Op.LIST)
        with self._lock:
            self._settle()
            c = self._containers.get(self._canonical(container))
            if c is None:
                return ObjectPage([], None)
            names = sorted(
                n
                for n, k in c.keys.items()
                if n.startswith(prefix) and (cursor is None or n > cursor) and self._current(k)
            )
            page = names[: self.list_page_size]
            items = []
            for n in page:
                k = c.keys[n]
                cur = self._current(k)
                assert cur is not None
                items.append(self._head(c, k, cur))
            more = len(names) > len(page)
            return ObjectPage(items, page[-1] if more else None)

    async def list_all_versions(
        self, container: str, prefix: str, cursor: str | None
    ) -> VersionPage:
        """Every stored version (not delete markers) of every key under
        `prefix`, including keys with no current version. Paged by key."""
        await self._gate(Op.LIST)
        with self._lock:
            self._settle()
            c = self._containers.get(self._canonical(container))
            if c is None:
                return VersionPage([], None)
            names = sorted(
                n for n in c.keys if n.startswith(prefix) and (cursor is None or n > cursor)
            )
            page = names[: self.list_page_size]
            items = []
            for n in page:
                k = c.keys[n]
                cur = self._current(k)
                for v in k.versions:
                    if v.delete_marker:
                        continue
                    items.append(ListedVersion(self._head(c, k, v), v is cur))
            more = len(names) > len(page)
            return VersionPage(items, page[-1] if more else None)

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        await self._gate(Op.LIST_VERSIONS)
        with self._lock:
            self._settle()
            c = self._containers.get(self._canonical(ref.container))
            k = c.keys.get(ref.key) if c else None
            if c is None or k is None:
                return []
            current = k.versions[-1] if k.has_current and k.versions else None
            return [
                ObjectVersion(
                    ref=self._ref(c, k.name, v),
                    is_current=v is current,
                    is_delete_marker=v.delete_marker,
                    size=v.size,
                    last_modified=v.created_at,
                    native_class=v.native_class,
                )
                for v in reversed(k.versions)
            ]

    async def copy_object(
        self,
        src: ObjectRef,
        dst_container: str,
        dst_key: str,
        tier: LogicalTier | None = None,
    ) -> ObjectRef:
        """Azure Copy Blob / Put Blob from URL; S3 CopyObject.

        The source must be readable: copying an archived blob is a rehydrate,
        which is `restore()`. Works across accounts. Injected copy faults land
        damaged bytes while the recorded checksums stay the source's.
        """
        await self._gate(Op.COPY_OBJECT)
        with self._lock:
            self._settle()
            c, _, v = self._resolve(src)
            if not self._readable(v):
                raise NotRestored(f"{c.name}/{src.key} is archived; restore it first")
            return self._copy_version(v, dst_container, dst_key, tier=tier)

    # --- StorageBackend: tiers ------------------------------------------

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        await self._gate(Op.SET_CLASS)
        with self._lock:
            self._settle()
            c, k, v = self._resolve(ref)
            target = self._native_for(c, tier, native_class)
            if self.flavour is Flavour.AZURE:
                return self._azure_set_class(c, k, v, tier, target)
            return self._s3_set_class(c, k, v, target)

    # --- StorageBackend: restore ----------------------------------------

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        await self._gate(Op.RESTORE)
        with self._lock:
            self._settle()
            c, k, v = self._resolve(ref)
            if self.flavour is Flavour.AZURE:
                return self._azure_restore(c, k, v, ref, speed)
            return self._s3_restore(c, k, v, ref, speed, days)

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        await self._gate(Op.RESTORE_STATUS)
        with self._lock:
            self._settle()
            r = self._restores.get(handle.provider_ref)
            if r is None:
                raise ObjectNotFound(f"no restore {handle.provider_ref}")
            return RestoreStatus(
                state=r.state,
                eta=r.eta if r.state is RestoreState.IN_PROGRESS else None,
                expires_at=r.expires_at,
                target=r.target,
                detail=r.detail,
            )

    # --- StorageBackend: delete and WORM --------------------------------

    async def delete(self, ref: ObjectRef) -> None:
        await self._gate(Op.DELETE)
        with self._lock:
            self._settle()
            c = self._containers.get(self._canonical(ref.container))
            k = c.keys.get(ref.key) if c else None
            if c is None or k is None:
                raise ObjectNotFound(f"{ref.container}/{ref.key}")
            if ref.version_id is not None:
                v = self._find_version(c, k, ref.version_id)
                self._delete_version(c, k, v)
                return
            cur = self._current(k)
            if cur is None:
                raise ObjectNotFound(f"{c.name}/{ref.key}")
            if c.versioning:
                # Versioned: the current version becomes a previous version (a
                # tombstone); nothing is destroyed, so WORM allows it [W].
                if self.flavour is Flavour.AZURE:
                    k.has_current = False
                else:
                    now = self._now()
                    marker = _Version(
                        vid=self._new_vid(),
                        payload=None,
                        created_at=now,
                        native_class=cur.native_class,
                        tier_since=now,
                        metadata={},
                        explicit_tier=False,
                        block_crcs=[],
                        crc64=None,
                        md5=None,
                        etag="",
                    )
                    k.versions.append(marker)
                self._emit(ChangeKind.DELETED, c, k.name, cur, detail={"tombstone": "true"})
            else:
                self._delete_version(c, k, cur)

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        await self._gate(Op.SET_RETENTION)
        with self._lock:
            self._settle()
            c, _, v = self._resolve(ref)
            self._require_worm(c)
            if v.retention_until is not None and v.retention_locked:
                if until < v.retention_until:
                    raise Immutable("a locked retention can be extended, never shortened")
                if not locked:
                    raise Immutable("a locked retention can't be unlocked")
            v.retention_until = until
            v.retention_locked = locked

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        await self._gate(Op.SET_LEGAL_HOLD)
        with self._lock:
            self._settle()
            c, _, v = self._resolve(ref)
            self._require_worm(c)
            v.legal_hold = on

    # --- StorageBackend: tags and metadata -----------------------------

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        """Azure Set Blob Tags / S3 PutObjectTagging: replaces the tag set.
        Allowed on archived and immutable versions (tags are not part of the
        protected data) [A]."""
        await self._gate(Op.SET_TAGS)
        limit = self._caps.max_tags
        if len(tags) > limit:
            raise BackendError(f"at most {limit} tags per object")
        for k, val in tags.items():
            if not 1 <= len(k) <= 128 or len(val) > 256:
                raise BackendError("tag keys are 1-128 characters, values at most 256")
        with self._lock:
            self._settle()
            _, _, v = self._resolve(ref)
            v.tags = dict(tags)

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        await self._gate(Op.GET_TAGS)
        with self._lock:
            self._settle()
            _, _, v = self._resolve(ref)
            return dict(v.tags)

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        """Azure Set Blob Metadata / S3 CopyObject onto itself.

        Archived objects refuse it (Azure can't change metadata in Archive;
        S3 can't copy an unrestored object) [A]. A version under retention or
        legal hold refuses it. On a versioned container the change makes a
        new current version with the same bytes, class and tags, as both
        providers do; otherwise it is in place.
        """
        await self._gate(Op.SET_METADATA)
        with self._lock:
            self._settle()
            c, k, v = self._resolve(ref)
            archived = (
                v.native_class == caps.AZ_ARCHIVE
                if self.flavour is Flavour.AZURE
                else not self._readable(v)
            )
            if archived or v.pending is not None:
                raise UnsupportedOperation(f"{c.name}/{k.name}: no metadata change while archived")
            if self._protected(c, v):
                raise Immutable(f"{c.name}/{k.name}@{v.vid} is under retention or legal hold")
            if not c.versioning:
                v.metadata = dict(metadata)
                self._emit(ChangeKind.CREATED, c, k.name, v, detail={"metadata_only": "true"})
                return self._ref(c, k.name, v)
            assert v.payload is not None
            new = self._commit(
                c.name,
                k.name,
                v.payload,
                None,
                dict(metadata),
                block_crcs=list(v.block_crcs),
                crc=v.crc64,
                md5=v.md5,
                native=v.native_class,
                explicit=v.explicit_tier,
                overwrite_detail={"metadata_only": "true"},
                tags=v.tags,
            )
            nv = self._current(k)
            assert nv is not None
            nv.tier_since = v.tier_since  # same bytes, same tier clock
            return new

    # --- StorageBackend: credentials and changes ------------------------

    async def edge_credential(
        self,
        ref: ObjectRef,
        access: EdgeAccess,
        ttl: timedelta,
        *,
        if_match: str | None = None,
    ) -> EdgeCredential:
        await self._gate(Op.EDGE_CREDENTIAL)
        with self._lock:
            expires = self._now() + ttl
            host, cont = self._credential_scope(ref.container)
            version = ref.version_id if access is EdgeAccess.READ else None
            if self.flavour is Flavour.AZURE:
                url = credentials.make_url(
                    self._secret, host, cont, ref.key, access, expires, version
                )
                return EdgeCredential(
                    CredentialMode.AZURE_USER_DELEGATION_SAS, access, expires, url=url
                )
            details = {
                "bucket": cont,
                "key": ref.key,
                "version": version or "",
                "region": self.region,
                "endpoint": f"bucket.vpce-sim.s3.{self.region}.vpce.sim.invalid",
            }
            if self._s3_credential_mode is CredentialMode.S3_PRESIGNED_URL:
                url = credentials.make_url(
                    self._secret, host, cont, ref.key, access, expires, version
                )
                return EdgeCredential(
                    CredentialMode.S3_PRESIGNED_URL, access, expires, url=url, details=details
                )
            # Roles Anywhere: the edge signs with its own session; no secret here [V].
            return EdgeCredential(
                CredentialMode.S3_ROLES_ANYWHERE, access, expires, url=None, details=details
            )

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        """Ordered and cursor-paged. A record becomes visible `feed_lag` after
        it happened, and delivery stops at the first invisible record, so the
        order is never broken."""
        await self._gate(Op.READ_CHANGES)
        with self._lock:
            self._settle()
            name = self._canonical(container)
            if cursor is None:
                return ChangePage([], str(len(self._changes)), caught_up=True)
            try:
                pos = int(cursor)
            except ValueError:
                raise BackendError(f"bad change-feed cursor {cursor!r}") from None
            if not 0 <= pos <= len(self._changes):
                raise BackendError(f"bad change-feed cursor {cursor!r}")
            now = self._now()
            out: list[ChangeRecord] = []
            while pos < len(self._changes) and len(out) < self.feed_page_size:
                rec = self._changes[pos]
                if rec.at + self.feed_lag > now:
                    break
                if rec.ref.container == name:
                    out.append(rec)
                pos += 1
            caught_up = pos == len(self._changes) or self._changes[pos].at + self.feed_lag > now
            return ChangePage(out, str(pos), caught_up=caught_up)

    # --- internals: names, lookups --------------------------------------

    def _now(self) -> datetime:
        return self._clock.now()

    def _new_vid(self) -> str:
        n = next(self._seq)
        if self.flavour is Flavour.AZURE:
            # Azure version ids are timestamps; the sequence keeps them unique.
            return f"{self._now().strftime('%Y-%m-%dT%H:%M:%S')}.{n:07d}Z"
        return f"s3v{n:012d}"

    def _canonical(self, container: str) -> str:
        if self.flavour is Flavour.S3:
            return container
        if "/" in container:
            return container
        return f"{self._default_account}/{container}"

    def _account_of(self, container: str) -> str:
        return self._canonical(container).split("/", 1)[0]

    def _credential_scope(self, container: str) -> tuple[str, str]:
        name = self._canonical(container)
        if self.flavour is Flavour.AZURE:
            account, short = name.split("/", 1)
            return f"{account}.blob.core.sim.invalid", short
        return f"{name}.s3.{self.region}.sim.invalid", name

    def _ensure_container(
        self, container: str, *, worm: bool | None = None, versioning: bool | None = None
    ) -> _Container:
        name = self._canonical(container)
        c = self._containers.get(name)
        if c is not None:
            return c
        if self.flavour is Flavour.AZURE:
            account_name, _, short = name.partition("/")
            if not short or "/" in short:
                raise BackendError(f"bad Azure container {name!r}: use account/container")
            account = self._accounts.get(account_name)
            if account is None:
                raise ObjectNotFound(f"no storage account {account_name}")
            ver = account.versioning if versioning is None else versioning
            if ver and not account.versioning:
                raise UnsupportedOperation("versioning is an account setting on Azure")
            w = account.version_level_worm if worm is None else worm
            if w and not (account.version_level_worm and ver):
                raise UnsupportedOperation("version-level WORM needs account support + versioning")
            c = _Container(name, account_name, ver, w)
        else:
            if worm:
                raise UnsupportedOperation("S3 Object Lock is not modelled")
            ver = self._s3_versioning if versioning is None else versioning
            c = _Container(name, "aws", ver, False)
        self._containers[name] = c
        return c

    def _get_container(self, container: str, *, create: bool = False) -> _Container:
        name = self._canonical(container)
        c = self._containers.get(name)
        if c is None:
            if not (create and self._auto_create):
                raise ObjectNotFound(f"no container {name}")
            c = self._ensure_container(container)
        return c

    @staticmethod
    def _current(k: _Key) -> _Version | None:
        if not k.has_current or not k.versions:
            return None
        v = k.versions[-1]
        return None if v.delete_marker else v

    def _find_version(self, c: _Container, k: _Key, vid: str) -> _Version:
        for v in k.versions:
            if v.vid == vid:
                return v
        raise ObjectNotFound(f"{c.name}/{k.name}@{vid}")

    def _resolve(self, ref: ObjectRef) -> tuple[_Container, _Key, _Version]:
        c = self._containers.get(self._canonical(ref.container))
        k = c.keys.get(ref.key) if c else None
        if c is None or k is None:
            raise ObjectNotFound(f"{ref.container}/{ref.key}")
        if ref.version_id is None:
            v = self._current(k)
            if v is None:
                raise ObjectNotFound(f"{c.name}/{ref.key}")
            return c, k, v
        v = self._find_version(c, k, ref.version_id)
        if v.delete_marker:
            raise ObjectNotFound(f"{c.name}/{ref.key}@{ref.version_id} is a delete marker")
        return c, k, v

    def _ref(self, c: _Container, key: str, v: _Version | None) -> ObjectRef:
        vid = v.vid if (v is not None and c.versioning) else None
        return ObjectRef(self._id, c.name, key, vid)

    def _native_for(self, c: _Container, tier: LogicalTier, native: str | None) -> str:
        infos = self._tiers.get(tier)
        if not infos:
            raise UnsupportedOperation(f"{self.flavour} has no {tier} tier")
        if native is not None:
            if native not in {i.native_class for i in infos}:
                raise UnsupportedOperation(f"{native} is not a {tier} class")
            return native
        if self.flavour is Flavour.S3 and tier is LogicalTier.ARCHIVE:
            return c.archive_class
        return infos[0].native_class

    def _info(self, native: str) -> TierInfo:
        return self._native[native]

    # --- internals: state checks -----------------------------------------

    def _effective_retention(self, c: _Container, v: _Version) -> datetime | None:
        candidates = [v.retention_until] if v.retention_until else []
        if c.policy_days is not None:
            candidates.append(v.created_at + timedelta(days=c.policy_days))
        return max(candidates) if candidates else None

    def _protected(self, c: _Container, v: _Version) -> bool:
        if v.delete_marker:
            return False
        until = self._effective_retention(c, v)
        return v.legal_hold or (until is not None and until > self._now())

    def _require_worm(self, c: _Container) -> None:
        if not self._caps.worm:
            raise UnsupportedOperation("this backend has no WORM")
        if not c.worm:
            raise UnsupportedOperation(f"{c.name} has no version-level immutability")

    def _readable(self, v: _Version) -> bool:
        if v.delete_marker or v.pending is not None:
            return False
        if self.flavour is Flavour.AZURE:
            return v.native_class != caps.AZ_ARCHIVE
        if v.native_class in (caps.S3_GLACIER, caps.S3_DEEP_ARCHIVE):
            return v.restored_until is not None and self._now() < v.restored_until
        if v.native_class == caps.S3_INTELLIGENT_TIERING:
            return v.it_tier is None
        return True

    def _restore_state(self, v: _Version) -> tuple[RestoreState, datetime | None]:
        if v.pending is not None:
            return RestoreState.IN_PROGRESS, None
        if self.flavour is Flavour.AZURE:
            if v.native_class == caps.AZ_ARCHIVE:
                return RestoreState.ARCHIVED, None
            return RestoreState.NOT_NEEDED, None
        in_progress = (
            v.restore_id is not None
            and self._restores[v.restore_id].state is RestoreState.IN_PROGRESS
        )
        if v.native_class in (caps.S3_GLACIER, caps.S3_DEEP_ARCHIVE):
            if in_progress:
                return RestoreState.IN_PROGRESS, None
            if v.restored_until is not None and self._now() < v.restored_until:
                return RestoreState.COMPLETED, v.restored_until
            if v.restore_expired:
                return RestoreState.EXPIRED, None
            return RestoreState.ARCHIVED, None
        if v.native_class == caps.S3_INTELLIGENT_TIERING and v.it_tier is not None:
            return (RestoreState.IN_PROGRESS if in_progress else RestoreState.ARCHIVED), None
        return RestoreState.NOT_NEEDED, None

    def _head(self, c: _Container, k: _Key, v: _Version) -> ObjectHead:
        state, expires = self._restore_state(v)
        info = self._info(v.native_class)
        return ObjectHead(
            ref=self._ref(c, k.name, v),
            size=v.size,
            native_class=v.native_class,
            logical_tier=info.logical,
            restore_state=state,
            restore_expires_at=expires,
            # Azure checks CRC64 per request but stores no whole-blob value [V].
            crc64nvme=(
                to_bytes(v.crc64) if self.flavour is Flavour.S3 and v.crc64 is not None else None
            ),
            md5=v.md5,
            etag=v.etag,
            last_modified=v.created_at,
            retention_until=self._effective_retention(c, v),
            legal_hold=v.legal_hold,
            metadata=dict(v.metadata),
        )

    # --- internals: writes -----------------------------------------------

    def _store(self, data: bytes) -> Payload:
        if self._spill_threshold is None or len(data) <= self._spill_threshold:
            return MemoryPayload(data)
        if self._spill_dir is None:
            self._spill_dir = tempfile.mkdtemp(prefix="omnifile-sim-")
            weakref.finalize(self, shutil.rmtree, self._spill_dir, True)
        return FilePayload(os.path.join(self._spill_dir, f"{next(self._seq)}.bin"), data)

    def _class_for_put(self, c: _Container, tier: LogicalTier | None) -> tuple[str, bool]:
        """(native class, explicit?) for a new object."""
        if self.flavour is Flavour.S3:
            return self._native_for(c, tier or LogicalTier.HOT, None), tier is not None
        account = self._accounts[c.account]
        if tier is None:
            # No x-ms-access-tier: the account default; Smart on a Smart account [V].
            return (caps.AZ_SMART if account.smart_default else caps.AZ_HOT), False
        if tier is LogicalTier.SMART:
            if not account.redundancy.zonal or LogicalTier.SMART not in self._caps.tiers:
                raise UnsupportedOperation("Smart tier needs a ZRS/GZRS/RA-GZRS Smart account")
            return caps.AZ_SMART, False
        if tier is LogicalTier.ARCHIVE and account.redundancy.zonal:
            raise UnsupportedOperation(f"Archive is not supported on {account.redundancy}")
        return self._native_for(c, tier, None), True

    def _commit(
        self,
        container: str,
        key: str,
        payload: Payload,
        tier: LogicalTier | None,
        metadata: dict[str, str],
        *,
        block_crcs: list[tuple[int, int]],
        crc: int | None,
        md5: bytes | None,
        native: str | None = None,
        explicit: bool | None = None,
        pending: _Pending | None = None,
        overwrite_detail: Mapping[str, str] | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        c = self._get_container(container, create=True)
        if native is None:
            native, explicit = self._class_for_put(c, tier)
        now = self._now()
        k = c.keys.get(key)
        current = self._current(k) if k else None
        if k is not None and not c.versioning and k.versions:
            old = k.versions[-1]
            if self._protected(c, old):
                # "For a write to succeed, you must either have versioning
                # enabled or have neither a legal hold nor a time-based
                # retention policy" [W].
                raise Immutable(f"{c.name}/{key} is immutable and versioning is off")
            self._record_early_fee(c, key, old)
            self._drop_payload(old)
            k.versions.clear()
        if k is None:
            k = c.keys[key] = _Key(key)
        n = next(self._seq)
        v = _Version(
            vid=self._new_vid(),
            payload=payload,
            created_at=now,
            native_class=native,
            tier_since=now,
            metadata=metadata,
            explicit_tier=bool(explicit),
            block_crcs=block_crcs,
            crc64=crc,
            md5=md5,
            etag=f'"0x{n:016X}"',
            pending=pending,
            tags=dict(tags or {}),
        )
        k.versions.append(v)
        k.has_current = True
        detail = dict(overwrite_detail or {})
        if current is not None:
            detail["overwrite"] = "true"  # Azure reports overwrites as BlobCreated
        self._emit(ChangeKind.CREATED, c, key, v, detail=detail)
        return self._ref(c, key, v)

    def _copy_version(
        self,
        v: _Version,
        dst_container: str,
        dst_key: str,
        *,
        tier: LogicalTier | None = None,
        native: str | None = None,
        explicit: bool | None = None,
        pending: _Pending | None = None,
    ) -> ObjectRef:
        assert v.payload is not None
        payload: Payload = v.payload
        fault = self.faults.take_copy_fault()
        if fault is CopyFault.CORRUPT and payload.size:
            payload = CorruptPayload(payload, payload.size // 2)
        elif fault is CopyFault.PARTIAL:
            payload = TruncatedPayload(payload, payload.size // 2)
        detail = {"copy_fault": str(fault)} if fault else None
        return self._commit(
            dst_container,
            dst_key,
            payload,
            tier,
            dict(v.metadata),
            block_crcs=list(v.block_crcs),
            crc=v.crc64,
            md5=v.md5,
            native=native,
            explicit=explicit,
            pending=pending,
            overwrite_detail=detail,
            tags=v.tags,
        )

    def _delete_version(self, c: _Container, k: _Key, v: _Version) -> None:
        if self._protected(c, v):
            raise Immutable(f"{c.name}/{k.name}@{v.vid} is under retention or legal hold")
        was_current = self._current(k) is v
        self._record_early_fee(c, k.name, v)
        self._emit(ChangeKind.DELETED, c, k.name, v)
        k.versions.remove(v)
        self._drop_payload(v)
        if not k.versions:
            del c.keys[k.name]
        elif was_current and self.flavour is Flavour.AZURE:
            k.has_current = False

    def _drop_payload(self, v: _Version) -> None:
        if isinstance(v.payload, FilePayload) and not self._payload_shared(v.payload):
            v.payload.remove()

    def _payload_shared(self, p: Payload) -> bool:
        count = 0
        for c in self._containers.values():
            for k in c.keys.values():
                for v in k.versions:
                    if v.payload is p:
                        count += 1
        return count > 1

    def _record_early_fee(self, c: _Container, key: str, v: _Version) -> None:
        if v.delete_marker:
            return
        info = self._info(v.native_class)
        if not info.minimum_days:
            return
        now = self._now()
        if now < v.tier_since + timedelta(days=info.minimum_days):
            self.early_deletion_fees.append(
                EarlyDeletionFee(
                    ref=self._ref(c, key, v),
                    native_class=v.native_class,
                    size=v.size,
                    tier_since=v.tier_since,
                    at=now,
                    minimum_days=info.minimum_days,
                )
            )

    def _change_class(self, c: _Container, k: _Key, v: _Version, native: str) -> None:
        if v.native_class == native:
            return
        self._record_early_fee(c, k.name, v)
        v.native_class = native
        v.tier_since = self._now()
        self._emit(ChangeKind.TIER_CHANGED, c, k.name, v)

    def _emit(
        self,
        kind: ChangeKind,
        c: _Container,
        key: str,
        v: _Version | None,
        *,
        at: datetime | None = None,
        detail: Mapping[str, str] | None = None,
    ) -> None:
        self._changes.append(
            ChangeRecord(
                kind=kind,
                ref=self._ref(c, key, v),
                at=at or self._now(),
                native_class=v.native_class if v else None,
                size=v.size if v else None,
                detail=dict(detail or {}),
            )
        )

    # --- internals: tiering ---------------------------------------------

    def _azure_set_class(
        self, c: _Container, k: _Key, v: _Version, tier: LogicalTier, target: str
    ) -> ObjectRef:
        account = self._accounts[c.account]
        if v.pending is not None:
            raise Conflict(f"{c.name}/{k.name}: a rehydrate is pending")
        if v.native_class == target:
            return self._ref(c, k.name, v)
        if target == caps.AZ_SMART:
            if not account.redundancy.zonal:
                raise UnsupportedOperation("Smart tier needs ZRS, GZRS or RA-GZRS")
            if v.explicit_tier:
                # "blobs given an explicit tier can't be tiered back to smart" [W][V]
                raise UnsupportedOperation("a blob with an explicit tier can't return to Smart")
            self._change_class(c, k, v, target)
            return self._ref(c, k.name, v)
        if target == caps.AZ_ARCHIVE and account.redundancy.zonal:
            return self._azure_archive_move(c, k, v)
        if v.native_class == caps.AZ_ARCHIVE:
            # In-place rehydrate (Set Blob Tier out of Archive): takes hours and
            # can incur the 180-day early-deletion fee, which is why recalls
            # copy instead [A].
            self._record_early_fee(c, k.name, v)
            v.pending = _Pending(target, self._now() + self.timings.azure_standard)
            v.explicit_tier = True
            self._schedule(v.pending.done_at, "inplace", f"{c.name}\0{k.name}\0{v.vid}")
            return self._ref(c, k.name, v)
        # Set Blob Tier in place; allowed on immutable versions [W].
        v.explicit_tier = True
        self._change_class(c, k, v, target)
        return self._ref(c, k.name, v)

    def _azure_archive_move(self, c: _Container, k: _Key, v: _Version) -> ObjectRef:
        """Two-account layout: copy into the archive account with
        x-ms-access-tier=Archive, then delete the original [A]."""
        if self._archive_account is None:
            raise UnsupportedOperation(
                f"Archive is not supported on {self._accounts[c.account].redundancy} and no "
                "archive account is configured"
            )
        if self._protected(c, v):
            raise Immutable("WORM data tiers in place; copy-and-delete can't run on it [W]")
        dst = f"{self._archive_account}/{c.short}"
        new_ref = self._copy_version(v, dst, k.name, native=caps.AZ_ARCHIVE, explicit=True)
        # Get Blob Properties works on Archive blobs: the copy is checked by
        # the rule every delete of an original uses (`copy_check`), from what
        # Azure would report (length, Content-MD5). A copy that fails, or
        # can't be checked (no Content-MD5), is removed and the original kept.
        nc, nk, nv = self._resolve(new_ref)
        why = copy_mismatch(size=v.size, crc64nvme=None, md5=v.md5,
                            copy=self._head(nc, nk, nv), read_back=False)  # fmt: skip
        if why is not None:
            self._delete_version(nc, nk, nv)
            raise BackendError(
                f"archive copy of {c.name}/{k.name} does not match the original "
                f"({why}; {nv.size} of {v.size} bytes); the original is kept"
            )
        self._delete_version(c, k, v)
        return new_ref

    def _s3_set_class(self, c: _Container, k: _Key, v: _Version, target: str) -> ObjectRef:
        if v.native_class == target and v.it_tier is None:
            return self._ref(c, k.name, v)
        archived = v.native_class in (caps.S3_GLACIER, caps.S3_DEEP_ARCHIVE) or v.it_tier
        if archived and not self._readable(v):
            raise UnsupportedOperation(
                "S3 can't change the class of an archived object; restore it first"
            )
        if v.native_class in (caps.S3_GLACIER, caps.S3_DEEP_ARCHIVE):
            v.restored_until = None
            v.restore_id = None
        self._change_class(c, k, v, target)
        return self._ref(c, k.name, v)

    # --- internals: restore ---------------------------------------------

    def _azure_restore(
        self, c: _Container, k: _Key, v: _Version, ref: ObjectRef, speed: RestoreSpeed
    ) -> RestoreHandle:
        if speed not in (RestoreSpeed.STANDARD, RestoreSpeed.HIGH):
            raise UnsupportedOperation(f"Azure rehydrate priority is Standard or High, not {speed}")
        if v.native_class != caps.AZ_ARCHIVE:
            raise UnsupportedOperation(f"{c.name}/{k.name} is not archived")
        if v.pending is not None:
            raise Conflict(f"{c.name}/{k.name}: a rehydrate is pending")
        now = self._now()
        t = self.timings
        size = v.size
        if speed is RestoreSpeed.HIGH:
            # One priority pipeline per account at 10 GiB/h; requests beyond it
            # queue behind the ones already accepted [A].
            start = max(now, self._priority_free_at.get(c.account, now))
            transfer = timedelta(hours=size / t.azure_high_bytes_per_hour)
            self._priority_free_at[c.account] = start + transfer
            done = start + max(t.azure_high_min, transfer)
        else:
            extra = max(0, size - caps.AZ_NO_PROMISE_ABOVE)
            done = (
                now
                + t.azure_standard
                + timedelta(hours=extra / t.azure_standard_extra_bytes_per_hour)
            )
        eta = done if size <= caps.AZ_NO_PROMISE_ABOVE else None  # never promise above 10 GB
        rid = f"rh-{next(self._seq)}"
        recall = self._recall_container or f"{c.account}/recall"
        self._ensure_container(recall, worm=False)
        tkey = f"{c.name}/{k.name}" + (f"@{ref.version_id}" if ref.version_id else "")
        target = self._copy_version(
            v,
            recall,
            tkey,
            native=caps.AZ_ARCHIVE,
            explicit=True,
            pending=_Pending(caps.AZ_HOT, done, rid),
        )
        r = _Restore(
            rid,
            c.name,
            k.name,
            v.vid,
            speed,
            size,
            now,
            done,
            eta,
            None,
            self.faults.take_restore_fault(),
            target=target,
        )
        self._restores[rid] = r
        self._schedule(done, "restore", rid)
        self._emit(
            ChangeKind.RESTORE_STARTED,
            c,
            k.name,
            v,
            detail={"provider_ref": rid, "priority": str(speed), "target": _fmt(target)},
        )
        return RestoreHandle(self._ref(c, k.name, v), speed, rid, now, None, target)

    def _s3_restore(
        self,
        c: _Container,
        k: _Key,
        v: _Version,
        ref: ObjectRef,
        speed: RestoreSpeed,
        days: int | None,
    ) -> RestoreHandle:
        if speed not in (RestoreSpeed.EXPEDITED, RestoreSpeed.STANDARD, RestoreSpeed.BULK):
            raise UnsupportedOperation(f"S3 restores are Expedited, Standard or Bulk, not {speed}")
        it = v.native_class == caps.S3_INTELLIGENT_TIERING and v.it_tier is not None
        if it:
            source = v.it_tier
            if days is not None:
                raise UnsupportedOperation("Intelligent-Tiering archive restores take no Days")
        elif v.native_class in (caps.S3_GLACIER, caps.S3_DEEP_ARCHIVE):
            source = v.native_class
            if days is None or days < 1:
                raise UnsupportedOperation("RestoreObject from Glacier needs Days >= 1")
        else:
            raise UnsupportedOperation(f"{v.native_class} needs no restore (InvalidObjectState)")
        assert source is not None
        deep = source in (caps.S3_DEEP_ARCHIVE, caps.IT_DEEP_ARCHIVE_ACCESS)
        if speed is RestoreSpeed.EXPEDITED:
            if deep:
                raise UnsupportedOperation("no Expedited restore from Deep Archive")
            if v.size >= caps.S3_EXPEDITED_MAX_BYTES:
                raise UnsupportedOperation("Expedited restore is only for objects under 250 MB")
        self._take_restore_token()
        now = self._now()
        if v.restore_id is not None:
            prior = self._restores[v.restore_id]
            if prior.state is RestoreState.IN_PROGRESS:
                # AWS answers 409 RestoreAlreadyInProgress; sim hands back the restore.
                return self._s3_handle(c, k, v, prior)
            if not it and prior.state is RestoreState.COMPLETED and v.restored_until:
                # Restoring a restored copy again just moves its expiry [V].
                assert days is not None
                v.restored_until = _next_midnight(now + timedelta(days=days))
                prior.expires_at = v.restored_until
                prior.days = days
                self._schedule(v.restored_until, "expire", prior.rid)
                return self._s3_handle(c, k, v, prior)
        done = now + self.timings.s3[(source, speed)]
        rid = f"rs-{next(self._seq)}"
        r = _Restore(
            rid,
            c.name,
            k.name,
            v.vid,
            speed,
            v.size,
            now,
            done,
            done,
            days,
            self.faults.take_restore_fault(),
            it=it,
        )
        self._restores[rid] = r
        v.restore_id = rid
        v.restore_expired = False
        self._schedule(done, "restore", rid)
        self._emit(
            ChangeKind.RESTORE_STARTED,
            c,
            k.name,
            v,
            detail={"provider_ref": rid, "tier": str(speed), "days": str(days or "")},
        )
        return self._s3_handle(c, k, v, r)

    def _s3_handle(self, c: _Container, k: _Key, v: _Version, r: _Restore) -> RestoreHandle:
        return RestoreHandle(self._ref(c, k.name, v), r.speed, r.rid, r.requested_at, r.days)

    def _take_restore_token(self) -> None:
        """1,000 restore requests/s per account; ThrottlingException above [V]."""
        now = self._now()
        second = int(now.timestamp())
        window, count = self._restore_window
        if window != second:
            window, count = second, 0
        if count >= self.restore_rate_limit:
            retry = datetime.fromtimestamp(second + 1, UTC) - now
            raise Throttled("S3 restore request rate exceeded", retry_after=retry)
        self._restore_window = (window, count + 1)

    # --- internals: events ----------------------------------------------

    def _schedule(self, due: datetime, kind: str, key: str) -> None:
        heapq.heappush(self._events, (due, next(self._seq), kind, key))

    def _settle(self) -> None:
        now = self._now()
        while self._events and self._events[0][0] <= now:
            due, _, kind, key = heapq.heappop(self._events)
            if kind == "restore":
                self._complete_restore(self._restores[key], due)
            elif kind == "expire":
                self._expire_restore(self._restores[key], due)
            elif kind == "inplace":
                self._complete_inplace(key, due)

    def _lookup(self, container: str, key: str, vid: str) -> tuple[_Container, _Key, _Version]:
        c = self._containers.get(container)
        k = c.keys.get(key) if c else None
        if c is None or k is None:
            raise ObjectNotFound(f"{container}/{key}")
        return c, k, self._find_version(c, k, vid)

    def _complete_restore(self, r: _Restore, due: datetime) -> None:
        if r.state is not RestoreState.IN_PROGRESS or r.done_at != due:
            return
        try:
            c, k, v = self._lookup(r.container, r.key, r.vid)
        except ObjectNotFound:
            r.state, r.detail = RestoreState.FAILED, "source deleted"
            return
        if self.flavour is Flavour.AZURE:
            self._complete_azure_restore(r, c, k, v, due)
            return
        if r.fail:
            r.state, r.detail = RestoreState.FAILED, r.fail
            v.restore_id = None
            self._emit(ChangeKind.RESTORE_FAILED, c, k.name, v, at=due, detail={"reason": r.fail})
            return
        r.state = RestoreState.COMPLETED
        if r.it:
            # Back to Frequent Access; no Days, no expiry, no "Restore Expired" [V].
            v.it_tier = None
            v.tier_since = due
            self._emit(ChangeKind.RESTORE_COMPLETED, c, k.name, v, at=due)
            return
        assert r.days is not None
        v.restored_until = _next_midnight(due + timedelta(days=r.days))
        r.expires_at = v.restored_until
        self._schedule(v.restored_until, "expire", r.rid)
        self._emit(
            ChangeKind.RESTORE_COMPLETED,
            c,
            k.name,
            v,
            at=due,
            detail={"expires_at": v.restored_until.isoformat()},
        )

    def _complete_azure_restore(
        self, r: _Restore, c: _Container, k: _Key, v: _Version, due: datetime
    ) -> None:
        target = None
        if r.target is not None:
            try:
                tc, tk, tv = self._resolve(r.target)
                target = (tc, tk, tv)
            except ObjectNotFound:
                target = None
        if r.fail or target is None:
            r.state = RestoreState.FAILED
            r.detail = r.fail or "rehydrate target deleted"
            if target is not None:
                tc, tk, tv = target
                tk.versions.remove(tv)
                if not tk.versions:
                    del tc.keys[tk.name]
                self._emit(ChangeKind.DELETED, tc, tk.name, tv, at=due)
            self._emit(ChangeKind.RESTORE_FAILED, c, k.name, v, at=due, detail={"reason": r.detail})
            return
        tc, tk, tv = target
        tv.pending = None
        tv.native_class = caps.AZ_HOT
        tv.tier_since = due
        r.state = RestoreState.COMPLETED
        self._emit(ChangeKind.TIER_CHANGED, tc, tk.name, tv, at=due)
        self._emit(
            ChangeKind.RESTORE_COMPLETED,
            c,
            k.name,
            v,
            at=due,
            detail={"provider_ref": r.rid, "target": _fmt(r.target)},
        )

    def _expire_restore(self, r: _Restore, due: datetime) -> None:
        try:
            c, k, v = self._lookup(r.container, r.key, r.vid)
        except ObjectNotFound:
            return
        if v.restore_id != r.rid or v.restored_until != due:
            return  # extended or superseded
        v.restored_until = None
        v.restore_expired = True
        r.state = RestoreState.EXPIRED
        self._emit(ChangeKind.RESTORE_EXPIRED, c, k.name, v, at=due)

    def _complete_inplace(self, key: str, due: datetime) -> None:
        container, name, vid = key.split("\0")
        try:
            c, k, v = self._lookup(container, name, vid)
        except ObjectNotFound:
            return
        if v.pending is None or v.pending.done_at != due:
            return
        target = v.pending.to_class
        v.pending = None
        v.native_class = target
        v.tier_since = due
        self._emit(ChangeKind.TIER_CHANGED, c, k.name, v, at=due)

    # --- internals: faults ----------------------------------------------

    async def _gate(self, op: Op) -> None:
        with self._lock:
            delay = self.faults.check(op, self._now())
        if delay.virtual:
            advance = getattr(self._clock, "advance", None)
            if advance is None:
                raise TypeError("virtual latency needs a clock with advance()")
            advance(delay.virtual)
        if delay.real_seconds:
            await asyncio.sleep(delay.real_seconds)


def _crc(data: bytes) -> int:
    return crc64nvme(data)


def _fmt(ref: ObjectRef | None) -> str:
    if ref is None:
        return ""
    return f"{ref.container}/{ref.key}" + (f"@{ref.version_id}" if ref.version_id else "")
