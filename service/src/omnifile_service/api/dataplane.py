"""What the api needs from storage: edge credentials for one object, and a HEAD.

The api never moves bytes. It mints a credential for exactly one object
(control-api.md section 3) and, at upload `complete`, checks that the blob
version exists with the right size and the declared committed blocks
(`blockcheck.py`). `DataPlane` is that seam:

- `BackendDataPlane` sits on any `backends.base.StorageBackend`
  (`edge_credential`, `head`). The real Azure backend and the in-process sim
  plug in here, through the worker's backend registry (`.get(backend_id)`).
- `SimHttpDataPlane` serves the sim Azure Blob HTTP facade
  (`backends/sim_http`) running as its own process: it signs real
  user-delegation SAS tokens with the facade's key, locally, and HEADs over
  HTTP. That is what M1's end-to-end test uses.

Which one a backend gets is in its catalog row: `config.data_plane =
"sim_http"` (with `url` and `key_file`), else the backend registry.
"""

from __future__ import annotations

import base64
import binascii
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from ..backends.base import (
    BackendError,
    CommittedBlock,
    CredentialMode,
    EdgeAccess,
    ObjectNotFound,
    ObjectRef,
    StorageBackend,
    UnsupportedOperation,
)
from ..backends.sim_http import sas
from .problems import ApiError
from .validate import rfc3339

READ_SAS_TTL = timedelta(minutes=60)  # section 3: reads survive an hour's outage
WRITE_SAS_TTL = timedelta(minutes=15)  # section 3: renewed per upload_id
CACHE_MARGIN = timedelta(minutes=5)  # cache_until = expiry - 5 min


@dataclass(frozen=True)
class Target:
    """One object: a catalog location's address."""

    backend_id: int
    container: str  # Azure "account/container"; S3 bucket
    key: str
    version_id: str | None = None
    #: The catalog's ETag of the object (location.etag), for reads: an S3
    #: presigned GET signs it as `If-Match`, so a replaced object answers 412
    #: instead of serving another object's bytes.
    etag: str | None = None


def quoted_etag(etag: str) -> str:
    """An ETag as S3 gives it, in double quotes (an inventory row stores it
    bare): what `If-Match` takes."""
    bare = etag.strip()
    if bare.startswith("W/"):
        return bare
    return f'"{bare.strip(chr(34))}"'


@dataclass(frozen=True)
class Head:
    size: int
    etag: str | None
    version_id: str | None
    #: The full-object CRC-64/NVME the provider stores (S3; big-endian), or
    #: None. Azure stores none.
    crc64nvme: bytes | None = None


def default_ttl(access: EdgeAccess) -> timedelta:
    return WRITE_SAS_TTL if access is EdgeAccess.WRITE else READ_SAS_TTL


class DataPlane(Protocol):
    async def credential(
        self, target: Target, access: EdgeAccess, ttl: timedelta | None = None
    ) -> dict[str, Any]:
        """The wire `Credential` (control-api.yaml) for exactly this object.
        `ttl` defaults to `default_ttl(access)`."""
        ...

    async def head(self, target: Target) -> Head | None:
        """Size, ETag and version of the object (of `target.version_id` if
        set); None if it does not exist."""
        ...

    async def committed_blocks(self, target: Target) -> list[CommittedBlock]:
        """The committed block list, in order, of the object or of
        `target.version_id`. Raises `ApiError` if it can't be read."""
        ...


def _sas_credential(url: str, expires: datetime) -> dict[str, Any]:
    return {
        "mode": "azure_user_delegation_sas",
        "url": url,
        "expires_at": rfc3339(expires),
        "cache_until": rfc3339(expires - CACHE_MARGIN),
    }


def s3_pointer(d: Mapping[str, Any], target: Target) -> dict[str, Any]:
    """The `s3_roles_anywhere` credential (control-api.md section 3): only a
    pointer, no secret. The edge signs it with SigV4 from its own IAM Roles
    Anywhere session. `etag` and `crc64nvme` are added by the caller, from
    the catalog's location (`data.read_credential`)."""
    out: dict[str, Any] = {
        "mode": "s3_roles_anywhere",
        "bucket": d.get("bucket", target.container),
        "key": d.get("key", target.key),
        "region": d.get("region", ""),
        "endpoint": d.get("endpoint", ""),
        "addressing": d.get("addressing", "virtual_host"),
        "tls_max_version": d.get("tls_max_version", "1.2"),
        "requester_pays": bool(d.get("requester_pays", False)),
        "private_network": bool(d.get("private_network", True)),
    }
    version = d.get("version_id") or d.get("version") or target.version_id
    if version:
        out["version_id"] = version
    return out


class BackendDataPlane:
    """Any `StorageBackend` (Azure, or the in-process sim)."""

    def __init__(self, backend: StorageBackend) -> None:
        self.backend = backend

    def _ref(self, t: Target) -> ObjectRef:
        return ObjectRef(t.backend_id, t.container, t.key, t.version_id)

    async def credential(
        self, target: Target, access: EdgeAccess, ttl: timedelta | None = None
    ) -> dict[str, Any]:
        ttl = ttl or default_ttl(access)
        if_match = quoted_etag(target.etag) if target.etag and access is EdgeAccess.READ else None
        try:
            cred = await self.backend.edge_credential(
                self._ref(target), access, ttl, if_match=if_match
            )
        except BackendError as exc:
            raise ApiError(
                "unavailable", f"backend: {type(exc).__name__}", retry_after_s=2
            ) from exc
        if cred.mode is CredentialMode.AZURE_USER_DELEGATION_SAS and cred.url:
            expires = cred.expires_at or datetime.now(UTC) + ttl
            return _sas_credential(cred.url, expires)
        if cred.mode is CredentialMode.S3_PRESIGNED_URL and cred.url:
            expires = cred.expires_at or datetime.now(UTC) + ttl
            out: dict[str, Any] = {
                "mode": "s3_presigned_url",
                "url": cred.url,
                "expires_at": rfc3339(expires),
                "cache_until": rfc3339(expires - CACHE_MARGIN),
            }
            signed = cred.details.get("if_match")
            if signed:
                # A signed header: the edge sends exactly this as If-Match.
                out["if_match"] = signed
            return out
        if cred.mode is CredentialMode.S3_ROLES_ANYWHERE:
            return s3_pointer(cred.details, target)
        raise ApiError("internal", f"credential mode {cred.mode} is not served to edges")

    async def head(self, target: Target) -> Head | None:
        try:
            h = await self.backend.head(self._ref(target))
        except ObjectNotFound:
            return None
        except BackendError as exc:
            raise ApiError(
                "unavailable", f"backend: {type(exc).__name__}", retry_after_s=2
            ) from exc
        return Head(h.size, h.etag, h.ref.version_id, h.crc64nvme)

    async def committed_blocks(self, target: Target) -> list[CommittedBlock]:
        try:
            return list(await self.backend.committed_blocks(self._ref(target)))
        except UnsupportedOperation:
            # Fail closed: without a block list an upload can't be verified.
            raise ApiError(
                "unavailable",
                "this backend cannot list committed blocks, so uploads cannot be verified",
                retry_after_s=60,
            ) from None
        except ObjectNotFound:
            raise ApiError("invalid_request", "the blob version does not exist") from None
        except BackendError as exc:
            raise ApiError(
                "unavailable", f"backend: {type(exc).__name__}", retry_after_s=2
            ) from exc


class SimHttpDataPlane:
    """The sim Azure Blob HTTP facade, reached over HTTP like real Azure."""

    def __init__(
        self,
        base_url: str,
        key: sas.UserDelegationKey,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        key_file: str | Path | None = None,
        flavour: str = "azure_blob",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        #: ``s3``: the facade's S3 sim, read through presigned GETs (`s3get.py`).
        self.flavour = flavour
        self.http = http or httpx.AsyncClient(timeout=5.0, trust_env=False)
        self.clock = clock
        self.key_file = Path(key_file) if key_file is not None else None

    @classmethod
    def from_key_file(cls, base_url: str, key_file: str | Path, **kw: Any) -> SimHttpDataPlane:
        raw = json.loads(Path(key_file).read_text())
        return cls(base_url, user_delegation_key_from_json(raw), key_file=key_file, **kw)

    def _signing_key(self) -> sas.UserDelegationKey:
        """The facade's key; re-read from its file once it lapses (the facade
        publishes a new one there, e.g. after its virtual clock moved)."""
        if self.key_file is not None and self.clock() >= self.key.signed_expiry - CACHE_MARGIN:
            try:
                raw = json.loads(self.key_file.read_text())
                self.key = user_delegation_key_from_json(raw)
            except (OSError, ValueError, KeyError):
                pass
        return self.key

    def url(self, target: Target, permissions: str, expires: datetime, *, version: bool) -> str:
        account, _, container = target.container.partition("/")
        if not container:
            raise ApiError("internal", f"container {target.container!r} is not account/container")
        version_id = target.version_id if version else None
        query = sas.mint(
            self._signing_key(),
            account,
            container,
            target.key,
            permissions,
            expires,
            version_id=version_id,
        )
        if version_id:
            query += "&versionid=" + quote(version_id, safe="")
        return f"{self.base_url}/{account}/{quote(container)}/{quote(target.key, safe='/')}?{query}"

    def s3_url(self, target: Target, expires: datetime, if_match: str | None = None) -> str:
        from ..backends.sim_http import s3get

        return s3get.presign(
            self._signing_key(), self.base_url, target.container, target.key,
            target.version_id, expires, if_match=if_match,
        )  # fmt: skip

    async def credential(
        self, target: Target, access: EdgeAccess, ttl: timedelta | None = None
    ) -> dict[str, Any]:
        expires = self.clock() + (ttl or default_ttl(access))
        if self.flavour == "s3":
            if access is not EdgeAccess.READ:
                raise ApiError("unavailable", "edges never write to S3", retry_after_s=60)
            if_match = quoted_etag(target.etag) if target.etag else None
            out: dict[str, Any] = {
                "mode": "s3_presigned_url",
                "url": self.s3_url(target, expires, if_match),
                "expires_at": rfc3339(expires),
                "cache_until": rfc3339(expires - CACHE_MARGIN),
            }
            if if_match:
                out["if_match"] = if_match
            return out
        if access is EdgeAccess.READ:
            url = self.url(target, "r", expires, version=bool(target.version_id))
        else:
            url = self.url(target, "cw", expires, version=False)
        return _sas_credential(url, expires)

    async def head(self, target: Target) -> Head | None:
        headers = {"x-ms-version": sas.DEFAULT_SIGNED_VERSION}
        if self.flavour == "s3":
            url = self.s3_url(target, self.clock() + timedelta(minutes=5))
            headers = {"x-amz-checksum-mode": "ENABLED"}
        else:
            url = self.url(
                target, "r", self.clock() + timedelta(minutes=5), version=bool(target.version_id)
            )
        try:
            resp = await self.http.head(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ApiError("unavailable", "blob endpoint unreachable", retry_after_s=2) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ApiError(
                "unavailable",
                f"blob HEAD answered {resp.status_code} {resp.headers.get('x-ms-error-code', '')}",
                retry_after_s=2,
            )
        crc = None
        if self.flavour == "s3" and resp.headers.get("x-amz-checksum-crc64nvme"):
            try:
                crc = base64.b64decode(resp.headers["x-amz-checksum-crc64nvme"], validate=True)
            except binascii.Error:
                crc = None
            if crc is not None and len(crc) != 8:
                crc = None
        return Head(
            int(resp.headers.get("content-length", "0")),
            resp.headers.get("etag"),
            resp.headers.get("x-ms-version-id") or resp.headers.get("x-amz-version-id"),
            crc,
        )

    async def get_range(self, target: Target, offset: int, length: int):
        """Stream bytes [offset, offset + length) in 8 MiB ranged GETs (the
        worker's `verify` read-back; the api itself never moves bytes)."""
        end = offset + length
        pos = offset
        step = 8 * 1024 * 1024
        while pos < end:
            last = min(end, pos + step) - 1
            if self.flavour == "s3":
                url = self.s3_url(target, self.clock() + timedelta(minutes=5))
                headers = {"range": f"bytes={pos}-{last}"}
            else:
                url = self.url(
                    target, "r", self.clock() + timedelta(minutes=5),
                    version=bool(target.version_id),
                )  # fmt: skip
                headers = {
                    "x-ms-version": sas.DEFAULT_SIGNED_VERSION,
                    "x-ms-range": f"bytes={pos}-{last}",
                }
            try:
                resp = await self.http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise BackendError(f"blob endpoint unreachable: {exc}") from exc
            if resp.status_code == 404:
                raise ObjectNotFound(f"{target.container}/{target.key}")
            if resp.status_code == 416:  # past the end
                return
            if resp.status_code not in (200, 206):
                raise BackendError(f"Get Blob answered {resp.status_code}")
            if not resp.content:
                return
            yield resp.content
            pos += len(resp.content)
            if len(resp.content) < last - (pos - len(resp.content)) + 1:
                return  # short read: the end of the blob

    async def committed_blocks(self, target: Target) -> list[CommittedBlock]:
        url = self.url(
            target, "r", self.clock() + timedelta(minutes=5), version=bool(target.version_id)
        )
        try:
            resp = await self.http.get(
                url + "&comp=blocklist&blocklisttype=committed",
                headers={"x-ms-version": sas.DEFAULT_SIGNED_VERSION},
            )
        except httpx.HTTPError as exc:
            raise ApiError("unavailable", "blob endpoint unreachable", retry_after_s=2) from exc
        if resp.status_code == 404:
            raise ApiError("invalid_request", "the blob version does not exist")
        if resp.status_code != 200:
            raise ApiError(
                "unavailable",
                f"Get Block List answered {resp.status_code}"
                f" {resp.headers.get('x-ms-error-code', '')}",
                retry_after_s=2,
            )
        return parse_block_list(resp.content)

    async def aclose(self) -> None:
        await self.http.aclose()


def parse_block_list(body: bytes) -> list[CommittedBlock]:
    """Get Block List's `<CommittedBlocks>`, in order (names and sizes only)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ApiError("unavailable", "unreadable Get Block List response") from None
    committed = root.find("CommittedBlocks")
    out: list[CommittedBlock] = []
    for block in committed if committed is not None else []:
        name, size = block.findtext("Name"), block.findtext("Size")
        if name is None or size is None or not size.isdigit():
            raise ApiError("unavailable", "malformed Get Block List response")
        out.append(CommittedBlock(name, int(size)))
    return out


def user_delegation_key_from_json(raw: Mapping[str, str]) -> sas.UserDelegationKey:
    """The JSON `python -m omnifile_service.backends.sim_http --key-file` writes."""
    fields = {
        "signed_oid": ("SignedOid", "signed_oid"),
        "signed_tid": ("SignedTid", "signed_tid"),
        "signed_start": ("SignedStart", "signed_start"),
        "signed_expiry": ("SignedExpiry", "signed_expiry"),
        "signed_service": ("SignedService", "signed_service"),
        "signed_version": ("SignedVersion", "signed_version"),
        "value": ("Value", "value"),
    }
    out: dict[str, Any] = {}
    for name, keys in fields.items():
        value = next((raw[k] for k in keys if k in raw), None)
        if value is None:
            raise ValueError(f"user delegation key JSON lacks {keys[0]}")
        out[name] = value
    out["signed_start"] = sas.parse_time(out["signed_start"])
    out["signed_expiry"] = sas.parse_time(out["signed_expiry"])
    return sas.UserDelegationKey(**out)


class BackendLookup(Protocol):
    def get(self, backend_id: int) -> StorageBackend: ...


class DataPlanes:
    """backend_id -> DataPlane, built from catalog.backend rows on first use."""

    def __init__(
        self,
        registry: BackendLookup | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        allow_sim: bool = True,
    ) -> None:
        self.registry = registry
        #: False in production: a ``sim_http`` row is refused (dev mode only).
        self.allow_sim = allow_sim
        self._planes: dict[int, DataPlane] = {}
        #: "now" for the SAS tokens a sim_http plane signs (the dev clock).
        self.clock = clock

    def add(self, backend_id: int, plane: DataPlane) -> None:
        self._planes[backend_id] = plane

    def get(self, backend_id: int, kind: str, config: Mapping[str, Any]) -> DataPlane:
        plane = self._planes.get(backend_id)
        if plane is not None:
            return plane
        if config.get("data_plane") == "sim_http":
            if not self.allow_sim:
                raise ApiError("unavailable", f"backend {backend_id} is a sim: dev mode only")
            kw: dict[str, Any] = {"clock": self.clock} if self.clock is not None else {}
            kw["flavour"] = config.get("flavour", "azure_blob")
            plane = SimHttpDataPlane.from_key_file(config["url"], config["key_file"], **kw)
        else:
            registry = self.registry or _default_registry()
            try:
                backend = registry.get(backend_id)
            except KeyError:
                raise ApiError(
                    "unavailable", f"backend {backend_id} is not loaded; restart api"
                ) from None
            plane = BackendDataPlane(backend)
        self._planes[backend_id] = plane
        return plane

    async def aclose(self) -> None:
        for plane in self._planes.values():
            close = getattr(plane, "aclose", None)
            if close is not None:
                await close()


def _default_registry() -> BackendLookup:
    raise ApiError("unavailable", "no storage backend registry is configured for this backend")
