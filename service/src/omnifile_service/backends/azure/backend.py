"""The real Azure Blob backend, on the official async SDK.

Rules it follows, each with its source ([A] AGENTS.md, [V] verification
note, [W] WORM note, [C] control-api.md):

- **Auth:** Entra only (`DefaultAzureCredential`: the service's managed
  identity in Azure, a developer login locally). No shared keys anywhere [A].
  Roles needed: Storage Blob Data Owner (immutability and tags need Owner,
  not Contributor) and Storage Blob Delegator.
- **put:** staged blocks, each with ``x-ms-content-crc64`` (CRC-64/NVME,
  little-endian, base64) so Azure checks every block on receipt [A][V]; the
  whole-object CRC is combined from the block CRCs and checked against the
  caller's before the commit. On a Smart account the commit sets **no tier**,
  or the blob never enters Smart [V]. MD5 is sent as ``x-ms-blob-content-md5``
  so `head()` can return it.
- **POSIX metadata:** callers use the logical names ``file-owner``,
  ``file-group``, ``file-permissions``; Azure metadata names must be C#
  identifiers, so on the wire they are ``file_owner``, ``file_group``,
  ``file_permissions`` [A]. Values use the edge's format (``omnifile-blob``
  ``PosixMeta``): decimal uid and gid, permissions as 4-digit octal
  (``0644``); `to_azure_metadata` normalises them and refuses anything else.
- **Archive, two-account layout:** Copy Blob into the archive account with
  ``x-ms-access-tier: Archive``, wait for it, then delete the original [A],
  only once the copy passes `copy_check` (length and Content-MD5; an
  original without a Content-MD5 is kept). The tiering worker doesn't use
  this: it copies Hot, reads back, archives in place, then checks again.
  **WORM / single-account layout:** Set Blob Tier in place [W].
- **Restore:** Copy Blob out of Archive into the non-WORM recall container
  of the archived blob's own account, with Standard or High priority; never Set Blob Tier
  on the original (180-day early-deletion fee) [A][W].
- **Async Copy Blob source auth:** the source URL carries a short-lived
  user-delegation SAS, because ``x-ms-copy-source-authorization`` is only for
  synchronous copies (SDK: "Source authorization tokens are only supported
  for sync copy").
- **Change feed:** `changefeed.py` [A].
- **Edge credentials:** user-delegation SAS, 60 min; ``cache_until`` =
  expiry - 5 min in `details` [C].

Not modelled here: the 10 GiB/h priority budget (the service budgets it from
`capabilities()`), lifecycle rules, Blob Inventory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.storage.blob import (
    BlobBlock,
    BlobImmutabilityPolicyMode,
    ContentSettings,
    ImmutabilityPolicy,
    RehydratePriority,
    StandardBlobTier,
)
from azure.storage.blob.aio import BlobClient, BlobServiceClient, ContainerClient
from azure.storage.extensions import checksums

from ..base import (
    BackendError,
    BackendKind,
    Capabilities,
    ChangePage,
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
    SystemClock,
    UnsupportedOperation,
    VersionPage,
)
from ..copy_check import copy_mismatch
from .capabilities import (
    AZ_ARCHIVE,
    AZ_HOT,
    AZ_NO_PROMISE_ABOVE,
    AZ_SMART,
    LOGICAL_TO_NATIVE,
    NATIVE_TO_LOGICAL,
    ONLINE_TIERS,
    capabilities_for,
)
from .changefeed import FEED_CONTAINER, ChangeFeedReader
from .config import AzureBlobConfig
from .errors import ChecksumMismatch, translate
from .sas import DelegationKeyCache, blob_sas_url, cache_until

#: Logical POSIX metadata names [A] -> Azure's spelling (C# identifiers).
POSIX_TO_AZURE = {
    "file-owner": "file_owner",
    "file-group": "file_group",
    "file-permissions": "file_permissions",
}
AZURE_TO_POSIX = {v: k for k, v in POSIX_TO_AZURE.items()}
_CSHARP_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Restore promise per priority, while the blob is under 10 GB [A].
STANDARD_REHYDRATE = timedelta(hours=15)
HIGH_REHYDRATE = timedelta(hours=1)
#: Source SAS lifetime for server-side copies.
COPY_SOURCE_TTL = timedelta(hours=2)

Sleep = Callable[[float], Awaitable[None]]


def posix_metadata(uid: int, gid: int, mode: int) -> dict[str, str]:
    """The POSIX stamp in the edge's format (`omnifile-blob` `PosixMeta`):
    uid and gid in decimal, the permission bits as 4-digit octal (``0644``).
    Logical names; `to_azure_metadata` maps them to Azure's spelling."""
    return {
        "file-owner": str(_posix_id(uid, "file-owner")),
        "file-group": str(_posix_id(gid, "file-group")),
        "file-permissions": f"{mode & 0o7777:04o}",
    }


def _posix_id(value: int | str, name: str) -> int:
    text = str(value)
    if not text.isascii() or not text.isdigit():
        raise BackendError(f"{name} must be a decimal id, not {value!r}")
    n = int(text)
    if n > 0xFFFFFFFF:
        raise BackendError(f"{name} {n} does not fit in 32 bits")
    return n


def _posix_mode(value: str) -> str:
    text = value[2:] if value[:2] in ("0o", "0O") else value
    if not text or len(text) > 6 or any(c not in "01234567" for c in text):
        raise BackendError(f"file-permissions must be octal permission bits, not {value!r}")
    return f"{int(text, 8) & 0o7777:04o}"


def normalize_posix(metadata: Mapping[str, str]) -> dict[str, str]:
    """Bring POSIX values (logical names) into the edge's format: decimal uid
    and gid, 4-digit octal permissions. Anything else passes through. A value
    the edge could not parse back is refused rather than stored."""
    out = dict(metadata)
    for name in ("file-owner", "file-group"):
        if name in out:
            out[name] = str(_posix_id(out[name], name))
    if "file-permissions" in out:
        out["file-permissions"] = _posix_mode(out["file-permissions"])
    return out


def to_azure_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in normalize_posix(metadata).items():
        name = POSIX_TO_AZURE.get(k, k)
        if not _CSHARP_IDENT.match(name):
            raise BackendError(f"metadata name {k!r} is not valid on Azure (C# identifier)")
        out[name] = v
    return out


def from_azure_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    return {AZURE_TO_POSIX.get(k, k): v for k, v in (metadata or {}).items()}


def crc64_le_b64(crc: int) -> str:
    return base64.b64encode(crc.to_bytes(8, "little")).decode()


def _block_id(index: int) -> str:
    # Same length for every block of a blob, as Azure requires.
    return f"{index:08d}"


class AzureBlobBackend:
    """`StorageBackend` over Azure Blob Storage. Safe to call concurrently."""

    def __init__(
        self,
        backend_id: int,
        config: AzureBlobConfig,
        *,
        credential: Any = None,
        clock: Clock | None = None,
        transport: Any = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._id = backend_id
        self.config = config
        self._clock = clock or SystemClock()
        self._sleep = sleep
        self._owns_credential = credential is None
        if credential is None:
            from azure.identity.aio import DefaultAzureCredential

            # Managed identity in Azure; developer login locally. Never keys.
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._credential = credential
        kwargs: dict[str, Any] = dict(config.extra_client_kwargs)
        if config.api_version:
            kwargs["api_version"] = config.api_version
        if transport is not None:
            kwargs["transport"] = transport
        if config.connection_verify is not None:
            kwargs["connection_verify"] = config.connection_verify
        self._services: dict[str, BlobServiceClient] = {
            name: BlobServiceClient(acct.url, credential=credential, **kwargs)
            for name, acct in config.accounts.items()
        }
        self._keys = DelegationKeyCache(config.delegation_key_lifetime)
        self._caps = capabilities_for(config)
        self._created: set[str] = set()

    # --- properties -------------------------------------------------------

    @property
    def backend_id(self) -> int:
        return self._id

    @property
    def kind(self) -> BackendKind:
        return BackendKind.AZURE_BLOB

    @property
    def clock(self) -> Clock:
        return self._clock

    def capabilities(self) -> Capabilities:
        return self._caps

    async def close(self) -> None:
        for svc in self._services.values():
            await svc.close()
        if self._owns_credential and hasattr(self._credential, "close"):
            await self._credential.close()

    async def __aenter__(self) -> AzureBlobBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- naming -----------------------------------------------------------

    def _split(self, container: str) -> tuple[str, str]:
        account, sep, name = container.partition("/")
        if not sep:
            return self.config.primary_account, container
        if account not in self._services:
            raise ObjectNotFound(f"account {account!r} is not part of this binding")
        return account, name

    def _qualified(self, account: str, container: str) -> str:
        return f"{account}/{container}"

    def _blob(self, account: str, container: str, key: str) -> BlobClient:
        return self._services[account].get_blob_client(container, key)

    def _container(self, account: str, container: str) -> ContainerClient:
        return self._services[account].get_container_client(container)

    def _is_smart_account(self, account: str) -> bool:
        return self.config.smart and account == self.config.primary_account

    def _default_tier(self, account: str) -> str:
        return AZ_SMART if self._is_smart_account(account) else AZ_HOT

    @asynccontextmanager
    async def _errors(self, what: str, *, archived_means: type[BackendError] = NotRestored):
        try:
            yield
        except BackendError:
            raise
        except Exception as e:  # noqa: BLE001 - every SDK error is translated
            raise translate(e, what, archived_means=archived_means) from e

    async def _ensure_container(self, account: str, container: str) -> None:
        q = self._qualified(account, container)
        if not self.config.auto_create_containers or q in self._created:
            return
        try:
            await self._container(account, container).create_container()
        except ResourceExistsError:
            pass
        except Exception as e:  # noqa: BLE001
            raise translate(e, f"create container {q}") from e
        self._created.add(q)

    # --- head -------------------------------------------------------------

    def _tier_of(self, account: str, props: Any) -> tuple[str, LogicalTier]:
        native = props.blob_tier or self._default_tier(account)
        native = str(getattr(native, "value", native))
        logical = NATIVE_TO_LOGICAL.get(native.lower())
        if logical is None:
            raise BackendError(f"unknown Azure tier {native!r}")
        canonical = LOGICAL_TO_NATIVE[logical]
        return canonical, logical

    def _recall_key(self, account: str, container: str, key: str, version: str | None) -> str:
        return f"{account}/{container}/{key}" + (f"@{version}" if version else "")

    async def _props(self, account: str, container: str, key: str, version: str | None) -> Any:
        async with self._errors(f"head {account}/{container}/{key}"):
            return await self._blob(account, container, key).get_blob_properties(version_id=version)

    def _head_from(
        self, ref: ObjectRef, account: str, props: Any, state: RestoreState
    ) -> ObjectHead:
        native, logical = self._tier_of(account, props)
        md5 = props.content_settings.content_md5 if props.content_settings else None
        policy = props.immutability_policy
        return ObjectHead(
            ref=ref,
            size=int(props.size),
            native_class=native,
            logical_tier=logical,
            restore_state=state,
            restore_expires_at=None,
            crc64nvme=None,  # Azure stores no whole-blob CRC64 [V]
            md5=bytes(md5) if md5 else None,
            etag=props.etag,
            last_modified=props.last_modified,
            retention_until=policy.expiry_time if policy else None,
            legal_hold=bool(props.has_legal_hold),
            metadata=from_azure_metadata(props.metadata),
        )

    @staticmethod
    def _pending_rehydrate(props: Any) -> bool:
        status = (props.archive_status or "").lower()
        return status.startswith("rehydrate-pending")

    async def _recall_state(
        self, account: str, container: str, key: str, version: str | None
    ) -> RestoreState:
        """State of an archived blob: a rehydrate copy in the recall
        container tells whether a restore is under way or done."""
        tkey = self._recall_key(account, container, key, version)
        recall_account = account
        try:
            props = await self._blob(
                recall_account, self.config.recall_container, tkey
            ).get_blob_properties()
        except ResourceNotFoundError:
            return RestoreState.ARCHIVED
        except Exception as e:  # noqa: BLE001
            raise translate(e, f"head recall copy {tkey}") from e
        return self._copy_state(props)[0]

    def _copy_state(self, props: Any) -> tuple[RestoreState, str | None]:
        copy = props.copy
        status = (copy.status or "").lower() if copy else ""
        if status in ("failed", "aborted"):
            return RestoreState.FAILED, copy.status_description
        if status == "pending" or self._pending_rehydrate(props):
            return RestoreState.IN_PROGRESS, None
        native = str(getattr(props.blob_tier, "value", props.blob_tier) or "")
        if native == AZ_ARCHIVE:
            return RestoreState.IN_PROGRESS, None
        return RestoreState.COMPLETED, None

    async def head(self, ref: ObjectRef) -> ObjectHead:
        account, container = self._split(ref.container)
        props = await self._props(account, container, ref.key, ref.version_id)
        qref = ObjectRef(
            self._id,
            self._qualified(account, container),
            ref.key,
            ref.version_id,
        )
        native, _ = self._tier_of(account, props)
        if self._pending_rehydrate(props):
            state = RestoreState.IN_PROGRESS
        elif native == AZ_ARCHIVE:
            if container == self.config.recall_container:
                state = self._copy_state(props)[0]
            else:
                state = await self._recall_state(account, container, ref.key, ref.version_id)
        else:
            state = RestoreState.NOT_NEEDED
        return self._head_from(qref, account, props, state)

    # --- put / read -------------------------------------------------------

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
        account, cname = self._split(container)
        native_tier: str | None = None
        if tier is not None:
            if tier not in self._caps.tiers:
                raise UnsupportedOperation(f"tier {tier} is not offered by this binding")
            if tier is LogicalTier.SMART:
                native_tier = None  # Smart is the default; never set it explicitly [V]
            elif tier is LogicalTier.ARCHIVE and self.config.layout == "two_account":
                # Archive is not allowed on the zonal live account [V]:
                # write straight into the archive account.
                account = self.config.archive_account  # type: ignore[assignment]
                native_tier = AZ_ARCHIVE
            else:
                native_tier = LOGICAL_TO_NATIVE[tier]
        azure_meta = to_azure_metadata(metadata)
        await self._ensure_container(account, cname)
        blob = self._blob(account, cname, key)
        block_size = self.config.block_size
        blocks: list[BlobBlock] = []
        total_crc = checksums.crc64.compute(b"", 0)
        md5 = hashlib.md5(usedforsecurity=False)
        buf = bytearray()

        async def stage(data: bytes) -> None:
            nonlocal total_crc
            crc = checksums.crc64.compute(data, 0)
            total_crc = checksums.crc64.compute(data, total_crc)
            md5.update(data)
            bid = _block_id(len(blocks))
            async with self._errors(f"put block {account}/{cname}/{key}"):
                await blob.stage_block(
                    bid,
                    data,
                    length=len(data),
                    transactional_content_crc64=crc.to_bytes(8, "little"),
                )
            blocks.append(BlobBlock(bid))

        async for chunk in stream:
            buf += chunk
            while len(buf) >= block_size:
                await stage(bytes(buf[:block_size]))
                del buf[:block_size]
        if buf or not blocks:
            await stage(bytes(buf))
        if crc64nvme is not None and crc64nvme != total_crc.to_bytes(8, "big"):
            # Nothing is committed: uncommitted blocks expire on their own.
            raise ChecksumMismatch(
                f"{account}/{cname}/{key}: CRC64-NVME {total_crc:016x} != {crc64nvme.hex()}"
            )
        async with self._errors(f"commit {account}/{cname}/{key}"):
            resp = await blob.commit_block_list(
                blocks,
                metadata=azure_meta,
                content_settings=ContentSettings(content_md5=bytearray(md5.digest())),
                standard_blob_tier=StandardBlobTier(native_tier) if native_tier else None,
            )
        return ObjectRef(self._id, self._qualified(account, cname), key, resp.get("version_id"))

    def get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        return self._get_range(ref, offset, length)

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        """Get Block List (`GET ?comp=blocklist&blocklisttype=committed`).

        Through the SDK's generated operation, not `BlobClient.get_block_list`:
        that one has no version argument, and it returns block ids
        base64-DECODED when they happen to be UTF-8 (raw otherwise), while the
        contract compares the wire names the uploader sent. `versionid` is a
        query parameter the REST operation takes from version 2019-12-12 [A].
        VERIFY on a real account that a version's list comes back, not the
        current blob's. Azure lists names and sizes only: no per-block CRC
        (`CommittedBlock.crc64nvme` is None)."""
        account, container = self._split(ref.container)
        blob = self._blob(account, container, ref.key)
        params = {"versionid": ref.version_id} if ref.version_id else {}
        async with self._errors(f"block list {account}/{container}/{ref.key}"):
            listing = await blob._client.block_blob.get_block_list(  # noqa: SLF001
                list_type="committed", params=params
            )
        return [CommittedBlock(b.name, int(b.size)) for b in listing.committed_blocks or []]

    async def _get_range(self, ref: ObjectRef, offset: int, length: int) -> AsyncIterator[bytes]:
        if length <= 0:
            return
        account, container = self._split(ref.container)
        blob = self._blob(account, container, ref.key)
        what = f"read {account}/{container}/{ref.key}"
        try:
            downloader = await blob.download_blob(
                offset=offset, length=length, version_id=ref.version_id
            )
        except HttpResponseError as e:
            if e.status_code == 416:  # offset at or past the end: nothing exists there
                return
            raise translate(e, what) from e
        except Exception as e:  # noqa: BLE001
            raise translate(e, what) from e
        async with self._errors(what):
            async for chunk in downloader.chunks():
                yield chunk

    # --- listing ----------------------------------------------------------

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        account, cname = self._split(container)
        items: list[ObjectHead] = []
        async with self._errors(f"list {account}/{cname}/{prefix}"):
            pages = (
                self._container(account, cname)
                .list_blobs(name_starts_with=prefix, include=["metadata"])
                .by_page(continuation_token=cursor)
            )
            async for page in pages:
                async for b in page:
                    native, _ = self._tier_of(account, b)
                    state = (
                        RestoreState.IN_PROGRESS
                        if self._pending_rehydrate(b)
                        else RestoreState.ARCHIVED
                        if native == AZ_ARCHIVE
                        else RestoreState.NOT_NEEDED
                    )
                    ref = ObjectRef(self._id, self._qualified(account, cname), b.name, b.version_id)
                    items.append(self._head_from(ref, account, b, state))
                break
            next_cursor = pages.continuation_token
        return ObjectPage(items, next_cursor or None)

    async def list_all_versions(
        self, container: str, prefix: str, cursor: str | None
    ) -> VersionPage:
        """List Blobs with ``include=versions`` (plus immutability and legal
        hold, for WORM reconcile): every stored version, current or not, so a
        deleted blob's retained versions are seen. VERIFY against real Azure
        (pilot 4): that `is_current_version` is unset on accounts without
        versioning, which this treats as current."""
        account, cname = self._split(container)
        items: list[ListedVersion] = []
        async with self._errors(f"list versions {account}/{cname}/{prefix}"):
            pages = (
                self._container(account, cname)
                .list_blobs(
                    name_starts_with=prefix,
                    include=["metadata", "versions", "immutabilitypolicy", "legalhold"],
                )
                .by_page(continuation_token=cursor)
            )
            async for page in pages:
                async for b in page:
                    native, _ = self._tier_of(account, b)
                    state = (
                        RestoreState.IN_PROGRESS
                        if self._pending_rehydrate(b)
                        else RestoreState.ARCHIVED
                        if native == AZ_ARCHIVE
                        else RestoreState.NOT_NEEDED
                    )
                    ref = ObjectRef(self._id, self._qualified(account, cname), b.name, b.version_id)
                    current = b.is_current_version
                    items.append(
                        ListedVersion(
                            self._head_from(ref, account, b, state),
                            True if current is None else bool(current),
                        )
                    )
                break
            next_cursor = pages.continuation_token
        return VersionPage(items, next_cursor or None)

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        account, cname = self._split(ref.container)
        out: list[ObjectVersion] = []
        async with self._errors(f"list versions {account}/{cname}/{ref.key}"):
            async for b in self._container(account, cname).list_blobs(
                name_starts_with=ref.key, include=["versions"]
            ):
                if b.name != ref.key:
                    continue
                native, _ = self._tier_of(account, b)
                out.append(
                    ObjectVersion(
                        ref=ObjectRef(
                            self._id, self._qualified(account, cname), b.name, b.version_id
                        ),
                        is_current=bool(b.is_current_version),
                        is_delete_marker=False,  # Azure has none; a delete leaves no current one
                        size=int(b.size),
                        last_modified=b.last_modified,
                        native_class=native,
                    )
                )
        if not out:
            raise ObjectNotFound(f"{account}/{cname}/{ref.key}")
        # Version ids are RFC 3339 timestamps, so they sort by time.
        out.sort(key=lambda v: (v.is_current, v.ref.version_id or ""), reverse=True)
        return out

    # --- copies -----------------------------------------------------------

    async def _source_url(self, account: str, container: str, key: str, version: str | None) -> str:
        now = self._clock.now()
        expiry = now + COPY_SOURCE_TTL
        svc = self._services[account]
        async with self._errors(f"user delegation key {account}"):
            udk = await self._keys.get(account, svc, now, expiry)
        return blob_sas_url(
            endpoint=svc.url,
            account=account,
            container=container,
            blob=key,
            key=udk,
            access=EdgeAccess.READ,
            start=now - timedelta(minutes=5),
            expiry=expiry,
            version_id=version,
        )

    async def _wait_copy(self, blob: BlobClient, what: str) -> Any:
        delay = self.config.copy_poll_initial
        waited = 0.0
        while True:
            async with self._errors(what):
                props = await blob.get_blob_properties()
            status = (props.copy.status or "success").lower() if props.copy else "success"
            if status == "success":
                return props
            if status in ("failed", "aborted"):
                raise BackendError(f"{what}: copy {status}: {props.copy.status_description}")
            if waited >= self.config.copy_timeout:
                raise BackendError(f"{what}: copy still pending after {waited:.0f}s")
            await self._sleep(delay)
            waited += delay
            delay = min(delay * 2, self.config.copy_poll_max)

    async def _start_copy(
        self,
        src: tuple[str, str, str, str | None],
        dst: tuple[str, str, str],
        *,
        tier: str | None,
        priority: RestoreSpeed | None = None,
    ) -> tuple[BlobClient, dict[str, Any]]:
        s_acct, s_cont, s_key, s_ver = src
        d_acct, d_cont, d_key = dst
        await self._ensure_container(d_acct, d_cont)
        source_url = await self._source_url(s_acct, s_cont, s_key, s_ver)
        async with self._errors(f"tags {s_acct}/{s_cont}/{s_key}"):
            tags = await self._blob(s_acct, s_cont, s_key).get_blob_tags(version_id=s_ver)
        target = self._blob(d_acct, d_cont, d_key)
        kwargs: dict[str, Any] = {}
        if tier:
            kwargs["standard_blob_tier"] = StandardBlobTier(tier)
        if priority is not None:
            kwargs["rehydrate_priority"] = (
                RehydratePriority.HIGH
                if priority is RestoreSpeed.HIGH
                else RehydratePriority.STANDARD
            )
        if tags:
            # Async Copy Blob does not copy tags; set them on the copy.
            kwargs["tags"] = dict(tags)
        async with self._errors(f"copy to {d_acct}/{d_cont}/{d_key}"):
            resp = await target.start_copy_from_url(source_url, **kwargs)
        return target, resp

    async def copy_object(
        self,
        src: ObjectRef,
        dst_container: str,
        dst_key: str,
        tier: LogicalTier | None = None,
    ) -> ObjectRef:
        s_acct, s_cont = self._split(src.container)
        d_acct, d_cont = self._split(dst_container)
        props = await self._props(s_acct, s_cont, src.key, src.version_id)
        native, _ = self._tier_of(s_acct, props)
        if native == AZ_ARCHIVE:
            raise NotRestored(f"{s_acct}/{s_cont}/{src.key} is archived; use restore()")
        dest_tier: str | None = None
        if tier is not None and tier is not LogicalTier.SMART:
            if tier not in self._caps.tiers:
                raise UnsupportedOperation(f"tier {tier} is not offered by this binding")
            dest_tier = LOGICAL_TO_NATIVE[tier]
            if dest_tier == AZ_ARCHIVE and self._is_smart_account(d_acct):
                raise UnsupportedOperation("Archive is not allowed on the zonal live account")
        target, resp = await self._start_copy(
            (s_acct, s_cont, src.key, src.version_id), (d_acct, d_cont, dst_key), tier=dest_tier
        )
        what = f"copy to {d_acct}/{d_cont}/{dst_key}"
        if (resp.get("copy_status") or "").lower() != "success":
            await self._wait_copy(target, what)
        return ObjectRef(self._id, self._qualified(d_acct, d_cont), dst_key, resp.get("version_id"))

    # --- tiers ------------------------------------------------------------

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        infos = self._caps.tiers.get(tier)
        if not infos:
            raise UnsupportedOperation(f"tier {tier} is not offered by this binding")
        natives = [i.native_class for i in infos]
        native = native_class or natives[0]
        if native not in natives:
            raise UnsupportedOperation(f"{native} is not a class of {tier}")
        account, container = self._split(ref.container)
        props = await self._props(account, container, ref.key, ref.version_id)
        current, _ = self._tier_of(account, props)
        if tier is LogicalTier.SMART:
            if current == AZ_SMART:
                return ObjectRef(
                    self._id, self._qualified(account, container), ref.key, ref.version_id
                )
            # An explicit tier can never go back to Smart [V].
            raise UnsupportedOperation(
                f"{account}/{container}/{ref.key} has an explicit tier; it can't return to Smart"
            )
        if (
            native == AZ_ARCHIVE
            and self.config.layout == "two_account"
            and account != self.config.archive_account
        ):
            return await self._archive_move(ref, account, container, props)
        rehydrate = (
            {"rehydrate_priority": RehydratePriority.STANDARD}
            if current == AZ_ARCHIVE and native != AZ_ARCHIVE
            else {}
        )
        async with self._errors(f"set tier {account}/{container}/{ref.key}"):
            await self._blob(account, container, ref.key).set_standard_blob_tier(
                StandardBlobTier(native), version_id=ref.version_id, **rehydrate
            )
        return ObjectRef(self._id, self._qualified(account, container), ref.key, ref.version_id)

    async def _archive_move(
        self, ref: ObjectRef, account: str, container: str, props: Any
    ) -> ObjectRef:
        """Two-account archive: copy into the archive account as Archive, then
        delete the original [A]. WORM data can't take this path [W]."""
        policy = props.immutability_policy
        now = self._clock.now()
        if props.has_legal_hold or (policy and policy.expiry_time and policy.expiry_time > now):
            raise Immutable(
                f"{account}/{container}/{ref.key}: retained or on legal hold; "
                "archive in place on a WORM account instead"
            )
        archive = self.config.archive_account
        assert archive is not None
        target, resp = await self._start_copy(
            (account, container, ref.key, ref.version_id),
            (archive, container, ref.key),
            tier=AZ_ARCHIVE,
        )
        what = f"archive copy {archive}/{container}/{ref.key}"
        if (resp.get("copy_status") or "").lower() != "success":
            await self._wait_copy(target, what)
        # Get Blob Properties works on Archive blobs: the copy is checked
        # against the original by the rule every delete of an original uses
        # (`copy_check`): the length, and the Content-MD5 (Azure keeps no
        # whole-blob CRC64 [V]). Nothing reads the copy back on this path, so
        # an original without a Content-MD5 is never deleted here: the
        # tiering worker's phased move (copy Hot, read back, then Archive) is
        # the way for it.
        dest_ref = ObjectRef(self._id, self._qualified(archive, container), ref.key,
                             resp.get("version_id"))  # fmt: skip
        dest = await self._props(archive, container, ref.key, resp.get("version_id"))
        src_md5 = props.content_settings.content_md5 if props.content_settings else None
        why = copy_mismatch(
            size=int(props.size),
            crc64nvme=None,
            md5=bytes(src_md5) if src_md5 else None,
            copy=self._head_from(dest_ref, archive, dest, RestoreState.ARCHIVED),
            read_back=False,
        )
        if why is not None:
            async with self._errors(f"remove bad {what}"):
                await self._blob(archive, container, ref.key).delete_blob(
                    version_id=resp.get("version_id")
                )
            detail = ("can't be checked: the original has no Content-MD5" if why == "unverified"
                      else f"does not match the original ({why}; {dest.size} of {props.size}"
                      " bytes)")  # fmt: skip
            raise BackendError(f"{what} {detail}; the original is kept")
        async with self._errors(f"delete after archive {account}/{container}/{ref.key}"):
            await self._blob(account, container, ref.key).delete_blob(version_id=ref.version_id)
        return ObjectRef(
            self._id, self._qualified(archive, container), ref.key, resp.get("version_id")
        )

    # --- restore ----------------------------------------------------------

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        if speed not in (RestoreSpeed.STANDARD, RestoreSpeed.HIGH):
            raise UnsupportedOperation(f"Azure rehydrate priority is Standard or High, not {speed}")
        account, container = self._split(ref.container)
        props = await self._props(account, container, ref.key, ref.version_id)
        native, _ = self._tier_of(account, props)
        if native != AZ_ARCHIVE:
            raise UnsupportedOperation(f"{account}/{container}/{ref.key} is not archived")
        # The recall container is in the archived blob's own account (the
        # archive account, or the WORM account) and has no immutability
        # policy [W]. Cross-account rehydrate copies exist (REST 2021-02-12+),
        # but the copy target sits in Archive tier until rehydrated, and
        # whether a ZRS (Smart) account accepts that is unverified, so the
        # copy stays in one LRS/GRS account. sim uses live/recall: see the
        # M1 report.
        recall_account = account
        recall = self.config.recall_container
        tkey = self._recall_key(account, container, ref.key, ref.version_id)
        target_ref = ObjectRef(self._id, self._qualified(recall_account, recall), tkey)
        now = self._clock.now()
        # Start-or-join: a copy already running into the recall container.
        try:
            existing = await self._blob(recall_account, recall, tkey).get_blob_properties()
        except ResourceNotFoundError:
            existing = None
        except Exception as e:  # noqa: BLE001
            raise translate(e, f"head recall copy {tkey}") from e
        if existing is not None and self._copy_state(existing)[0] in (
            RestoreState.IN_PROGRESS,
            RestoreState.COMPLETED,
        ):
            copy_id = existing.copy.id if existing.copy else None
            return RestoreHandle(
                ref=ref,
                speed=speed,
                provider_ref=copy_id or tkey,
                # The rehydrate copy was created when the copy started.
                requested_at=existing.creation_time or existing.last_modified or now,
                target=target_ref,
            )
        _, resp = await self._start_copy(
            (account, container, ref.key, ref.version_id),
            (recall_account, recall, tkey),
            tier=AZ_HOT,
            priority=speed,
        )
        return RestoreHandle(
            ref=ref,
            speed=speed,
            provider_ref=resp.get("copy_id") or tkey,
            requested_at=now,
            target=target_ref,
        )

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        if handle.target is None:
            raise ObjectNotFound(f"restore {handle.provider_ref} has no target")
        account, container = self._split(handle.target.container)
        try:
            props = await self._blob(account, container, handle.target.key).get_blob_properties()
        except ResourceNotFoundError:
            return RestoreStatus(
                RestoreState.FAILED, target=handle.target, detail="rehydrate copy is gone"
            )
        except Exception as e:  # noqa: BLE001
            raise translate(e, f"restore status {handle.provider_ref}") from e
        state, detail = self._copy_state(props)
        eta = None
        if state is RestoreState.IN_PROGRESS and int(props.size or 0) <= AZ_NO_PROMISE_ABOVE:
            took = HIGH_REHYDRATE if handle.speed is RestoreSpeed.HIGH else STANDARD_REHYDRATE
            eta = max(handle.requested_at + took, self._clock.now())
        return RestoreStatus(state, eta=eta, expires_at=None, target=handle.target, detail=detail)

    # --- delete and WORM --------------------------------------------------

    async def delete(self, ref: ObjectRef) -> None:
        account, container = self._split(ref.container)
        async with self._errors(f"delete {account}/{container}/{ref.key}"):
            await self._blob(account, container, ref.key).delete_blob(version_id=ref.version_id)

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        if not self._caps.worm:
            raise UnsupportedOperation("this binding has no version-level WORM")
        account, container = self._split(ref.container)
        props = await self._props(account, container, ref.key, ref.version_id)
        policy = props.immutability_policy
        mode = str(getattr(policy.policy_mode, "value", policy.policy_mode) or "") if policy else ""
        if (
            policy
            and policy.expiry_time
            and mode.lower() == "locked"
            and until < policy.expiry_time
        ):
            raise Immutable(f"{account}/{container}/{ref.key}: a locked retention only grows")
        new = ImmutabilityPolicy(
            expiry_time=until,
            policy_mode=BlobImmutabilityPolicyMode.Locked
            if locked
            else BlobImmutabilityPolicyMode.Unlocked,
        )
        async with self._errors(f"set retention {account}/{container}/{ref.key}"):
            await self._blob(account, container, ref.key).set_immutability_policy(
                new, version_id=ref.version_id
            )

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        if not self._caps.worm:
            raise UnsupportedOperation("this binding has no version-level WORM")
        account, container = self._split(ref.container)
        async with self._errors(f"legal hold {account}/{container}/{ref.key}"):
            await self._blob(account, container, ref.key).set_legal_hold(
                on, version_id=ref.version_id
            )

    # --- tags and metadata ------------------------------------------------

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        if len(tags) > self._caps.max_tags:
            raise BackendError(f"{len(tags)} tags; Azure allows {self._caps.max_tags}")
        account, container = self._split(ref.container)
        async with self._errors(f"set tags {account}/{container}/{ref.key}"):
            await self._blob(account, container, ref.key).set_blob_tags(
                dict(tags), version_id=ref.version_id
            )

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        account, container = self._split(ref.container)
        async with self._errors(f"get tags {account}/{container}/{ref.key}"):
            return dict(
                await self._blob(account, container, ref.key).get_blob_tags(
                    version_id=ref.version_id
                )
                or {}
            )

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        account, container = self._split(ref.container)
        props = await self._props(account, container, ref.key, ref.version_id)
        native, _ = self._tier_of(account, props)
        if native == AZ_ARCHIVE:
            raise UnsupportedOperation("Azure can't change metadata of an archived blob [A]")
        policy = props.immutability_policy
        if props.has_legal_hold or (
            policy and policy.expiry_time and policy.expiry_time > self._clock.now()
        ):
            raise Immutable(f"{account}/{container}/{ref.key}: retained or on legal hold")
        async with self._errors(
            f"set metadata {account}/{container}/{ref.key}", archived_means=UnsupportedOperation
        ):
            # Set Blob Metadata acts on the current blob; on a versioned
            # account it makes a new current version.
            resp = await self._blob(account, container, ref.key).set_blob_metadata(
                to_azure_metadata(metadata)
            )
        return ObjectRef(
            self._id,
            self._qualified(account, container),
            ref.key,
            resp.get("version_id") or ref.version_id,
        )

    # --- edge credentials -------------------------------------------------

    async def edge_credential(
        self,
        ref: ObjectRef,
        access: EdgeAccess,
        ttl: timedelta,
        *,
        if_match: str | None = None,
    ) -> EdgeCredential:
        account, container = self._split(ref.container)
        now = self._clock.now()
        expires = now + ttl
        svc = self._services[account]
        async with self._errors(f"user delegation key {account}"):
            udk = await self._keys.get(account, svc, now, expires)
        version = ref.version_id if access is EdgeAccess.READ else None
        url = blob_sas_url(
            endpoint=self.config.accounts[account].edge_url,
            account=account,
            container=container,
            blob=ref.key,
            key=udk,
            access=access,
            start=now - timedelta(minutes=5),
            expiry=expires,
            version_id=version,
        )
        return EdgeCredential(
            mode=CredentialMode.AZURE_USER_DELEGATION_SAS,
            access=access,
            expires_at=expires,
            url=url,
            details={
                "cache_until": cache_until(expires).isoformat(),
                "account": account,
                "container": container,
                "key": ref.key,
                **({"version_id": version} if version else {}),
            },
        )

    # --- change feed ------------------------------------------------------

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        account, cname = self._split(container)
        reader = ChangeFeedReader(
            self._services[account].get_container_client(FEED_CONTAINER),
            account=account,
            backend_id=self._id,
            page_size=self.config.feed_page_size,
        )
        async with self._errors(f"change feed {account}"):
            return await reader.read(cname, cursor, self._clock.now())


__all__ = [
    "AzureBlobBackend",
    "from_azure_metadata",
    "normalize_posix",
    "posix_metadata",
    "to_azure_metadata",
    "ONLINE_TIERS",
]
