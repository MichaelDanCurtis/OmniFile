"""The storage backend contract (docs/ARCHITECTURE.md section 3).

Every provider (Azure Blob, Amazon S3, later GCS) implements `StorageBackend`.
Policies speak in logical tiers (hot, warm, cold, archive, smart); a backend
maps them to its own classes and describes its limits in `Capabilities`, so the
policy editor can warn in the provider's own terms.

Facts quoted in the docstrings come from the verification pass,
docs/research/2026-09-26-verification.md ("[V]"), the WORM note,
docs/design/tiers-versioning-worm.md ("[W]"), or AGENTS.md ("[A]"). A backend's
`capabilities()` must match those sources; when a provider changes, update the
research note first, then the backend.

All I/O methods are async. `get_range` is a plain method returning an async
iterator, so implementations can be written as async generators.

Time: a backend takes a `Clock` and uses it for every timestamp and ETA it
produces, so the simulated backend can run on virtual time and tests never
sleep.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Clock(Protocol):
    """Source of "now" for a backend. Always timezone-aware UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class BackendKind(StrEnum):
    AZURE_BLOB = "azure_blob"
    S3 = "s3"
    SIM = "sim"  # the simulated backend used in tests (BUILD-PLAN "Test strategy")


class LogicalTier(StrEnum):
    """The tiers policies use. Backends map each to one or more native classes."""

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    ARCHIVE = "archive"
    SMART = "smart"


class RestoreSpeed(StrEnum):
    """Restore priorities across providers.

    Azure offers STANDARD and HIGH (rehydrate priority). S3 offers EXPEDITED,
    STANDARD and BULK. A backend lists what it supports in
    `Capabilities.restore_options`; asking for anything else raises
    `UnsupportedOperation`.
    """

    EXPEDITED = "expedited"
    STANDARD = "standard"
    BULK = "bulk"
    HIGH = "high"


class RestoreState(StrEnum):
    NOT_NEEDED = "not_needed"  # readable now (online tier, or Glacier Instant Retrieval)
    ARCHIVED = "archived"  # archived, no restore requested and no live restored copy
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    EXPIRED = "expired"  # S3 temporary copy past its `Days`
    FAILED = "failed"


class CredentialMode(StrEnum):
    """How the edge reaches objects without holding long-lived secrets [A]."""

    AZURE_ARC_MANAGED_IDENTITY = "azure_arc_managed_identity"
    AZURE_USER_DELEGATION_SAS = "azure_user_delegation_sas"  # minted per blob by the service
    S3_ROLES_ANYWHERE = "s3_roles_anywhere"  # a pointer; the edge signs (Roles Anywhere, TPM key)
    S3_PRESIGNED_URL = "s3_presigned_url"  # one object version, read only


class EdgeAccess(StrEnum):
    READ = "read"
    WRITE = "write"  # create + write one object, for upload


class BackendError(Exception):
    """Base class for backend failures the caller can act on."""


class ObjectNotFound(BackendError):
    """No such object or version."""


class Immutable(BackendError):
    """WORM retention or a legal hold forbids this change [W]."""


class UnsupportedOperation(BackendError):
    """The backend cannot do this (e.g. Expedited restore from Deep Archive)."""


class WriteRefused(UnsupportedOperation):
    """A write the backend refuses on principle: on S3, any change to an
    object outside OmniFile's own areas (a migration source is the
    client's, and read-only to OmniFile; catalog-seeding.md section 10)."""


class NotRestored(BackendError):
    """A ranged read hit an archived object that has no live restored copy."""


class Throttled(BackendError):
    """The provider throttled the call; retry after `retry_after`."""

    def __init__(self, message: str, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ObjectRef:
    """One object, or one version of it.

    `container` is "account/container" for Azure and the bucket for S3.
    `version_id` is the Azure blob version id or the S3 VersionId; None means
    the current version. On versioned WORM accounts every committed file
    version has its own `version_id` [W].
    """

    backend_id: int
    container: str
    key: str
    version_id: str | None = None


@dataclass(frozen=True)
class CommittedBlock:
    """One block of an object's committed block list (Azure Get Block List)."""

    block_id: str
    size: int
    #: The block's CRC64-NVME (big-endian) when the provider reports it.
    #: Azure's Get Block List returns names and sizes only, so Azure gives
    #: None: each block's CRC was checked by Azure at Put Block against the
    #: uploader's x-ms-content-crc64, and the uploader declares those CRCs [V].
    crc64nvme: bytes | None = None


@dataclass(frozen=True)
class ObjectHead:
    ref: ObjectRef
    size: int
    native_class: str
    logical_tier: LogicalTier
    restore_state: RestoreState
    restore_expires_at: datetime | None = None
    #: Full-object CRC64-NVME when the provider has one. S3 stores a
    #: full-object CRC64NVME; Azure checks CRC64 per request and nothing says
    #: it stores a whole-blob value, so Azure returns None and the catalog's
    #: value (combined from the per-block CRCs) is used [V].
    crc64nvme: bytes | None = None
    md5: bytes | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    retention_until: datetime | None = None
    legal_hold: bool = False
    #: User metadata as written by `put`, including the POSIX attributes
    #: file-owner / file-group / file-permissions [A]. Empty if none.
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObjectPage:
    items: Sequence[ObjectHead]
    cursor: str | None  # None: last page


@dataclass(frozen=True)
class ListedVersion:
    """One stored version in a versions-aware listing (`VersionListing`)."""

    head: ObjectHead  # ref.version_id set on versioned containers
    is_current: bool


@dataclass(frozen=True)
class VersionPage:
    items: Sequence[ListedVersion]
    cursor: str | None  # None: last page


@dataclass(frozen=True)
class ObjectVersion:
    ref: ObjectRef  # with version_id set
    is_current: bool
    is_delete_marker: bool
    size: int
    last_modified: datetime
    native_class: str


class ChangeKind(StrEnum):
    """Provider change events, normalised.

    Azure: the Blob change feed (BlobCreated, BlobDeleted, and from schema
    v4 BlobTierChanged and BlobAsyncOperationInitiated), read as blobs over
    the private endpoint [A]. S3: EventBridge "Object Restore Initiated /
    Completed / Expired" pulled from SQS, plus the S3 Metadata journal for
    object changes [V].
    """

    CREATED = "created"
    DELETED = "deleted"
    TIER_CHANGED = "tier_changed"
    RESTORE_STARTED = "restore_started"
    RESTORE_COMPLETED = "restore_completed"
    RESTORE_EXPIRED = "restore_expired"
    RESTORE_FAILED = "restore_failed"


@dataclass(frozen=True)
class ChangeRecord:
    kind: ChangeKind
    ref: ObjectRef
    at: datetime
    native_class: str | None = None
    size: int | None = None
    detail: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangePage:
    records: Sequence[ChangeRecord]
    #: Opaque; pass it back to continue. Never None: an empty page returns
    #: the same position, so the caller can poll from it.
    cursor: str
    #: True when the provider has nothing newer right now.
    caught_up: bool


@dataclass(frozen=True)
class RestoreHandle:
    """A started restore.

    Azure: the target of a Copy Blob rehydrate (`target`), never Set Blob Tier
    on the original, which would incur the 180-day early-deletion fee [A].
    On WORM shares the target is in the non-WORM recall container [W].
    S3: a RestoreObject or Batch Operations job (`provider_ref`); the restored
    copy is temporary for `days` and billed on top of the archived copy [V].
    """

    ref: ObjectRef
    speed: RestoreSpeed
    provider_ref: str
    requested_at: datetime
    days: int | None = None
    target: ObjectRef | None = None


@dataclass(frozen=True)
class RestoreStatus:
    state: RestoreState
    eta: datetime | None = None
    #: S3 Flexible Retrieval / Deep Archive restores expire after `Days`.
    #: Intelligent-Tiering archive restores take no `Days` and never expire,
    #: so this is None for them [V].
    expires_at: datetime | None = None
    target: ObjectRef | None = None
    detail: str | None = None


@dataclass(frozen=True)
class EdgeCredential:
    """What the edge needs to reach one object.

    Azure: a blob URL carrying a user-delegation SAS scoped to that blob,
    short-lived (the service mints it; the edge holds no account secret).
    S3: no secret at all. The edge signs with its own Roles Anywhere session,
    so `url` is None and `details` carries bucket, key, version, region and
    the interface-endpoint name.
    """

    mode: CredentialMode
    access: EdgeAccess
    expires_at: datetime | None
    url: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TierInfo:
    """One native class behind a logical tier.

    A logical tier can have several (S3 archive: Glacier Flexible Retrieval
    or Deep Archive); the first is the default.
    """

    logical: LogicalTier
    native_class: str
    minimum_days: int | None  # early-deletion / minimum storage duration
    needs_restore: bool  # a read needs a restore first
    min_billable_bytes: int | None = None  # e.g. 128 KB for S3 Standard-IA / Glacier IR [V]
    per_object_overhead_bytes: int | None = None  # e.g. +40 KB for Glacier/Deep Archive [V]
    note: str | None = None


@dataclass(frozen=True)
class RestoreOption:
    speed: RestoreSpeed
    from_classes: frozenset[str]  # native classes this speed applies to
    typical_hours: tuple[float, float]  # (low, high) as the provider states it
    max_object_bytes: int | None = None  # S3 Expedited: under 250 MB [V]
    #: Above this size the backend makes no time promise. Azure's "up to
    #: 15 h" holds only under 10 GB, so Azure sets 10 GB [A].
    no_promise_above_bytes: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class ThroughputCap:
    """A provider limit the service must budget for.

    Examples: Azure priority rehydrate, 10 GiB/hour per storage account [A];
    S3, 1,000 restore requests/s per account, shared by Batch Operations and
    single restores (sharing INFERRED) [V].
    """

    name: str
    limit: float
    unit: str  # e.g. "GiB/hour", "requests/s"
    scope: str  # e.g. "storage account", "AWS account"
    note: str | None = None


@dataclass(frozen=True)
class PrivateNetworkOption:
    """How data-plane traffic stays private (decision 22).

    Azure: a Private Endpoint per storage account. S3: a PrivateLink interface
    endpoint, reachable over Direct Connect or VPN; interface endpoints do
    not support TLS 1.3, so clients must allow TLS 1.2 [V].
    """

    kind: str
    tls_versions: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True)
class Capabilities:
    """Everything the policy editor and scheduler need to know about a backend."""

    kind: BackendKind
    tiers: Mapping[LogicalTier, Sequence[TierInfo]]
    restore_options: Sequence[RestoreOption]
    throughput_caps: Sequence[ThroughputCap]
    private_network: Sequence[PrivateNetworkOption]
    credential_modes: Sequence[CredentialMode]
    #: Integrity algorithm end to end. Azure CRC64 and S3 CRC64NVME are the
    #: same CRC-64/NVME: polynomial 0xad93d23594c93659 (reflected form
    #: 0x9a6c9329ac4bc9b5), refin/refout true, init and xorout all ones [V].
    checksum: str = "crc64nvme"
    #: Object versioning is available (decision 30).
    versioning: bool = False
    #: Version-level WORM: per-version retention and legal hold (decision 31).
    worm: bool = False
    #: The archive tier is reached by an in-place class change. Azure WORM
    #: layouts need this, because immutability forbids copy-and-delete [W].
    in_place_archive: bool = False
    #: Smart tier and Archive live in different accounts. Azure: Smart needs
    #: ZRS/GZRS/RA-GZRS and Archive is not allowed there [V], so archive is a
    #: server-side copy into an LRS/GRS account, then a delete [A].
    archive_needs_separate_account: bool = False
    #: Setting an explicit tier removes an object from Smart for good. Azure:
    #: a blob with an explicit tier cannot go back to Smart, and uploads to a
    #: Smart account must not set x-ms-access-tier at all [V].
    explicit_tier_leaves_smart: bool = False
    #: Index tags per object: Azure blob index tags and S3 object tagging
    #: both allow 10. Holds and lifecycle state live in tags, because tags
    #: can be set on Archive-tier blobs and metadata cannot [A].
    max_tags: int = 10
    #: User metadata can be changed while the object is archived. False for
    #: Azure Archive [A], so POSIX metadata must be complete before archive.
    metadata_on_archive: bool = False


@runtime_checkable
class StorageBackend(Protocol):
    """One provider binding. Implementations must be safe to call concurrently."""

    @property
    def backend_id(self) -> int: ...

    @property
    def kind(self) -> BackendKind: ...

    @property
    def clock(self) -> Clock:
        """The clock behind every timestamp and ETA this backend returns."""
        ...

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
        """Write an object (block / multipart upload) and return its ref.

        `tier=None` means "the account default". On an Azure Smart account
        that is the only correct choice: a blob uploaded with an explicit tier
        never enters Smart [V]. POSIX attributes go in `metadata` as
        file-owner / file-group / file-permissions (S3:
        x-amz-meta-file-*, File Gateway compatible) [A]. Metadata cannot be
        changed once a blob is in Archive, so it is complete at write.

        Bulk uploads come from the edge with a SAS, not through here; the
        service uses `put` for small objects and tests.
        """
        ...

    def get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        """Stream `length` bytes from `offset` (Azure x-ms-range, S3 Range).

        `ref.version_id` selects a specific version (rewind, decision 30);
        None reads the current one. Reading past the end yields what exists.

        Raises `NotRestored` for an archived object without a live restored
        copy. Glacier Instant Retrieval needs no restore [V].
        """
        ...

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        """The committed block list of the object, or of `ref.version_id`, in
        order (Azure Get Block List, `blocklisttype=committed`). Upload
        `complete` checks it against the blocks the edge declares
        (control-api.md section 4.2), so a block list committed by anyone
        else with the write SAS is refused.

        Raises `ObjectNotFound`, or `UnsupportedOperation` on providers with
        no block lists (S3: multipart parts are not kept as a block list).
        """
        ...

    async def head(self, ref: ObjectRef) -> ObjectHead:
        """Size, class, restore state, checksums, retention. One round trip.

        An archived object with no restore under way reports
        `RestoreState.ARCHIVED`.
        """
        ...

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        """One page of objects under `prefix`.

        For reconciliation only. Listings shown to users come from the
        catalog; never LIST on a directory read.
        """
        ...

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        """Every version of `ref.key`, newest first (decision 30).

        Version-level WORM blocks Azure Blob Inventory on that account, so
        reconciliation of WORM accounts uses listings and the change feed [W].
        """
        ...

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        """Move an object to `tier`. Returns where it now lives.

        `native_class` picks one of the tier's `TierInfo` classes (S3 archive:
        GLACIER or DEEP_ARCHIVE); None takes the first, the default.

        Azure, two-account layout: Smart -> archive is a server-side copy into
        the archive account with x-ms-access-tier=Archive, then a delete of
        the original, so the returned ref differs [A]. The original goes only
        if the copy passes `copy_check` (its length and Content-MD5; nothing
        reads it back here); otherwise `BackendError`, and the original stays. Azure, single-account
        (WORM) layout: an in-place Set Blob Tier; immutable blobs allow it [W].
        Any explicit tier takes a blob out of Smart permanently [V].
        Minimum durations (Azure Cool 30, Cold 90, Archive 180 days; S3
        Standard-IA 30, Glacier IR 90, Flexible Retrieval 90, Deep Archive
        180) are the caller's to check, from `capabilities()` [A].
        """
        ...

    async def copy_object(
        self,
        src: ObjectRef,
        dst_container: str,
        dst_key: str,
        tier: LogicalTier | None = None,
    ) -> ObjectRef:
        """Server-side copy within the backend (Azure Copy Blob / Put Blob
        from URL; S3 CopyObject).

        Used for the non-WORM archive move into the archive account, rehydrate
        copies into the recall container [W], and staging copies. `tier=None`
        keeps the destination account's default (Smart on a Smart account).
        """
        ...

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        """Start a restore of an archived object.

        Azure: Copy Blob out of Archive (Standard or High priority). High
        priority shares a 10 GiB/hour per-account cap; don't offer High where
        it cannot be faster [A].
        S3: RestoreObject with `days`. Expedited (1-5 min) only under 250 MB
        and never for Deep Archive; Standard 3-5 h (Flexible) or within 12 h
        (Deep Archive); Bulk 5-12 h or within 48 h. 1,000 restore requests/s
        per account; above that, `Throttled` [V]. Intelligent-Tiering archive
        restores take no `days` [V].
        """
        ...

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        """Poll a restore. The service is the only poller; no push events.

        Azure status normally comes from the Blob change feed; this call is
        for the single file a user is waiting on [A]. S3 completion normally
        arrives as EventBridge "Object Restore Completed" through SQS [V].
        """
        ...

    async def delete(self, ref: ObjectRef) -> None:
        """Delete an object or version.

        Raises `Immutable` under WORM retention or legal hold: blobs "can be
        created and read, but not modified or deleted" [W]. Raises
        `ObjectNotFound` if there is nothing to delete.
        """
        ...

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        """Set or extend a version's retention (version-level WORM) [W].

        Locked (compliance mode) by default. `locked=False` is only for the
        short validation window before a share's policy is locked.

        A locked policy can be extended, never shortened: shortening raises
        `Immutable`. Raises `UnsupportedOperation` when `capabilities().worm`
        is False.
        """
        ...

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        """Set or clear a version's legal hold [W]."""
        ...

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        """Replace the object's index tags (Azure Set Blob Tags; S3
        PutObjectTagging).

        Allowed on archived objects [A]. More than `capabilities().max_tags`
        raises `BackendError`.
        """
        ...

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        """The object's index tags (empty if none)."""
        ...

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        """Replace user metadata, e.g. the POSIX stamp after a Storage Mover
        copy (decision 24). Returns the ref, which may carry a new version_id
        on versioned backends (S3 rewrites the object with CopyObject).

        Raises `UnsupportedOperation` on an archived object unless
        `capabilities().metadata_on_archive`; Azure cannot change metadata in
        Archive [A]. Raises `Immutable` where WORM forbids the change (VERIFY
        Azure behaviour on version-level WORM in pilot 4).
        """
        ...

    async def edge_credential(
        self,
        ref: ObjectRef,
        access: EdgeAccess,
        ttl: timedelta,
        *,
        if_match: str | None = None,
    ) -> EdgeCredential:
        """Credential for the edge to read or upload exactly this object.

        Azure: a user-delegation SAS on that blob only (read: `r`; write:
        `cw`), valid for `ttl`. S3: a pointer (bucket, key, version, region,
        endpoint) for the edge's Roles Anywhere session; no secret [A][V].

        `if_match`: the catalog's quoted ETag of the object, for reads. An S3
        presigned GET signs it (`If-Match` in `X-Amz-SignedHeaders`, returned
        in `details["if_match"]`), so the edge must send it and a replaced
        object answers 412. Other backends ignore it.
        """
        ...

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        """Read provider change events after `cursor` (None: from now).

        Ordered and durable on the provider side; the catalog-sync and
        recall-status stream. No push: Event Grid cannot deliver into a
        private network, so the service always pulls [A].
        """
        ...

    def capabilities(self) -> Capabilities:
        """Static facts about this backend. Cheap; safe to call per request."""
        ...


@runtime_checkable
class VersionListing(Protocol):
    """Optional: list every stored version under a prefix, in pages.

    Reconcile needs it on versioned containers: `list` returns current
    versions only, so it can't see an overwritten version or the versions of
    a deleted (tombstoned) blob, which WORM keeps. Azure: List Blobs with
    ``include=versions``; sim: every non-delete-marker version. Backends
    without it are reconciled with `list` plus `list_versions` per key.
    """

    async def list_all_versions(
        self, container: str, prefix: str, cursor: str | None
    ) -> VersionPage: ...
