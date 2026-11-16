"""Backend selection: a `catalog.backend` row -> a `StorageBackend`.

`catalog.backend.kind` is ``azure_blob``, ``s3`` or ``sim``; `config` holds
accounts, containers and layout, never secrets (the service uses its
managed identity for Azure and IAM Roles Anywhere for S3). In dev and tests
the backend is ``sim`` (layout chosen by ``config["layout"]``); production is
``azure_blob``, plus ``s3`` for datasets migrating out of S3 (M5).

One instance per backend id, built lazily and shared: SDK clients pool
connections, and a sim instance holds state that the sim_http facade and the
service must share.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..backends.base import (
    BackendError,
    BackendKind,
    Capabilities,
    ChangePage,
    Clock,
    CommittedBlock,
    LogicalTier,
    ObjectHead,
    ObjectPage,
    ObjectRef,
    ObjectVersion,
    RestoreHandle,
    RestoreSpeed,
    RestoreStatus,
    StorageBackend,
    SystemClock,
    UnsupportedOperation,
)

SIM_LAYOUTS = ("azure_two_account", "azure_worm", "s3")


@dataclass(frozen=True)
class SimHttpConfig:
    """What the migration engine reads from a backend's ``config``
    (`migrate/staging.py`, `migrate/waves.py`): the staging bucket urgent
    recalls are copied into (the row's ``config.staging_bucket``), and no
    S3 Batch Operations (sim has none, so no waves or checksum jobs)."""

    staging_bucket: str | None = None
    batch: None = None


class SimHttpBackend:
    """The sim Azure Blob HTTP facade (`backends/sim_http`) when it runs as
    its own process, as in the end-to-end tests: the edge writes blobs into
    that process, so an in-process `SimBackend` here would never see them.

    The catalog row says so with ``config.data_plane = "sim_http"`` (the same
    switch the api's `DataPlanes` uses), plus ``url``, ``key_file`` and
    ``layout``. Reads (`get_range`, the `verify` queue's read-back) go
    through the Azure-shaped data plane with a SAS signed from the facade's
    key, like real Azure. Every other call (tier moves, restores, the change
    feed, retention, holds, tags, versions, deletes) is the facade's
    ``POST /_sim/backend`` (`backends/sim_http/rpc.py`), so the worker drives
    the same sim the edge reads and writes. `capabilities()` is the layout's.
    """

    def __init__(
        self,
        backend_id: int,
        plane: Any,
        *,
        layout: str = "azure_two_account",
        clock: Clock | None = None,
        http: Any = None,
        staging_bucket: str | None = None,
    ) -> None:
        import httpx

        from ..backends.sim import SimBackend, VirtualClock

        self.config = SimHttpConfig(staging_bucket=staging_bucket)
        self._id = backend_id
        self.plane = plane
        self.layout = layout
        self._clock = clock or SystemClock()
        self._http = http or getattr(plane, "http", None) or httpx.AsyncClient(timeout=30.0)
        self._caps = getattr(SimBackend, layout)(VirtualClock()).capabilities()

    @classmethod
    def from_config(
        cls, backend_id: int, config: Mapping[str, Any], *, clock: Clock | None = None
    ) -> SimHttpBackend:
        from ..api.dataplane import SimHttpDataPlane

        kw: dict[str, Any] = {} if clock is None else {"clock": clock.now}
        flavour = config.get("flavour", "azure_blob")
        plane = SimHttpDataPlane.from_key_file(
            config["url"], config["key_file"], flavour=flavour, **kw
        )
        layout = "s3" if flavour == "s3" else config.get("layout", "azure_two_account")
        return cls(
            backend_id, plane, layout=layout, clock=clock,
            staging_bucket=config.get("staging_bucket"),
        )  # fmt: skip

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

    async def _call(self, op: str, *args: Any, **kwargs: Any) -> Any:
        import httpx

        from ..backends.sim_http import rpc

        body = {
            "op": op,
            "args": [rpc.encode(a) for a in args],
            "kwargs": {k: rpc.encode(v) for k, v in kwargs.items()},
        }
        if self.layout == "s3":
            body["backend"] = "s3"  # the facade's S3 sim
        try:
            resp = await self._http.post(f"{self.plane.base_url}/_sim/backend", json=body)
        except httpx.HTTPError as e:
            raise BackendError(f"sim_http unreachable: {e}") from e
        if resp.status_code != 200:
            raise BackendError(f"sim_http /_sim/backend answered {resp.status_code}: {resp.text}")
        out = resp.json()
        if "error" in out:
            rpc.raise_error(out["error"])
        return rpc.decode(out.get("result"))

    async def head(self, ref: ObjectRef) -> ObjectHead:
        return await self._call("head", ref)

    async def get_range(self, ref: ObjectRef, offset: int, length: int):
        from ..api.dataplane import Target

        async for chunk in self.plane.get_range(
            Target(ref.backend_id, ref.container, ref.key, ref.version_id), offset, length
        ):
            yield chunk

    async def list(self, container: str, prefix: str, cursor: str | None) -> ObjectPage:
        return await self._call("list", container, prefix, cursor)

    async def list_versions(self, ref: ObjectRef) -> Sequence[ObjectVersion]:
        return await self._call("list_versions", ref)

    async def list_all_versions(self, container: str, prefix: str, cursor: str | None):
        return await self._call("list_all_versions", container, prefix, cursor)

    async def set_class(
        self, ref: ObjectRef, tier: LogicalTier, *, native_class: str | None = None
    ) -> ObjectRef:
        return await self._call("set_class", ref, tier, native_class=native_class)

    async def copy_object(
        self, src: ObjectRef, dst_container: str, dst_key: str, tier: LogicalTier | None = None
    ) -> ObjectRef:
        return await self._call("copy_object", src, dst_container, dst_key, tier)

    async def restore(
        self, ref: ObjectRef, speed: RestoreSpeed, days: int | None = None
    ) -> RestoreHandle:
        return await self._call("restore", ref, speed, days)

    async def restore_status(self, handle: RestoreHandle) -> RestoreStatus:
        return await self._call("restore_status", handle)

    async def delete(self, ref: ObjectRef) -> None:
        await self._call("delete", ref)

    async def set_retention(self, ref: ObjectRef, until: datetime, *, locked: bool = True) -> None:
        await self._call("set_retention", ref, until, locked=locked)

    async def set_legal_hold(self, ref: ObjectRef, on: bool) -> None:
        await self._call("set_legal_hold", ref, on)

    async def set_tags(self, ref: ObjectRef, tags: Mapping[str, str]) -> None:
        await self._call("set_tags", ref, dict(tags))

    async def get_tags(self, ref: ObjectRef) -> Mapping[str, str]:
        return await self._call("get_tags", ref)

    async def set_metadata(self, ref: ObjectRef, metadata: Mapping[str, str]) -> ObjectRef:
        return await self._call("set_metadata", ref, dict(metadata))

    async def committed_blocks(self, ref: ObjectRef) -> Sequence[CommittedBlock]:
        return await self._call("committed_blocks", ref)

    async def read_changes(self, container: str, cursor: str | None) -> ChangePage:
        return await self._call("read_changes", container, cursor)

    # The container-policy seam (`retention.policy_admin`): sim's management plane.
    async def set_container_policy(self, container: str, retention_days: int, *, locked: bool):
        await self._call("set_container_policy", container, retention_days, locked=locked)

    async def create_container(self, container: str, **kw: Any) -> str:
        """Provisioning (the setup guide's step on Azure): e.g. the WORM container."""
        return await self._call("create_container", container, **kw)

    async def delete_container_policy(self, container: str) -> None:
        await self._call("delete_container_policy", container)

    async def put_synthetic(self, container: str, key: str, size: int, tier=None, metadata=None,
                            *, seed: int = 0) -> ObjectRef:  # fmt: skip
        """Test helper: an object of `size` bytes that sim never holds."""
        return await self._call("put_synthetic", container, key, size, tier, metadata, seed=seed)

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
        """The service's own objects (snapshot exports): the bytes cross the
        RPC in one call. Edge uploads go through the Blob data plane."""
        if crc64nvme is not None:
            raise UnsupportedOperation("sim_http put does not check a CRC64")
        data = b"".join([bytes(c) async for c in stream])
        return await self._call("put_bytes", container, key, data, tier, dict(metadata))

    async def edge_credential(self, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedOperation("sim_http credentials are SAS tokens minted by the api")

    async def close(self) -> None:
        await self.plane.aclose()


#: The M1 name; the backend is no longer HEAD-only.
SimHttpHeadBackend = SimHttpBackend


def sim_refusal(kinds: Mapping[int, str], env: Mapping[str, str]) -> str | None:
    """Why this process must not start, or None. A ``sim`` catalog row is a
    dev and test backend: in production it would keep files in memory (lost
    on restart) or sign URLs for whatever ``sim_http`` host the row names.
    Like every dev shortcut, it needs ``OMNIFILE_DEV_MODE=1``."""
    sims = sorted(bid for bid, kind in kinds.items() if kind == "sim")
    if sims and env.get("OMNIFILE_DEV_MODE") != "1":
        return (
            f"catalog backends {sims} are kind 'sim', a dev and test backend:"
            " it needs OMNIFILE_DEV_MODE=1"
        )
    return None


@dataclass(frozen=True)
class BackendRow:
    backend_id: int
    kind: str
    config: Mapping[str, Any] = field(default_factory=dict)
    private_network: bool = True


def backend_for(
    backend_id: int,
    kind: str,
    config: Mapping[str, Any],
    *,
    clock: Clock | None = None,
    credential: Any = None,
    private_network: bool | None = None,
) -> StorageBackend:
    """Build the backend for one catalog row. `private_network` is the row's
    column; for S3 it can only switch private networking on (decision 22)."""
    clock = clock or SystemClock()
    if kind == "azure_blob":
        from ..backends.azure import AzureBlobBackend, AzureBlobConfig

        return AzureBlobBackend(
            backend_id, AzureBlobConfig.from_mapping(config), credential=credential, clock=clock
        )
    if kind == "sim" and config.get("data_plane") == "sim_http":
        return SimHttpBackend.from_config(backend_id, config, clock=clock)
    if kind == "sim":
        from ..backends.sim import SimBackend

        layout = config.get("layout", "azure_two_account")
        if layout not in SIM_LAYOUTS:
            raise ValueError(f"sim layout {layout!r} is not one of {SIM_LAYOUTS}")
        make = getattr(SimBackend, layout)
        return make(clock, backend_id=backend_id)
    if kind == "s3":
        from ..backends.s3 import S3Backend

        # Endpoints, roles, the audience and private networking come from
        # the operator's OMNIFILE_AWS_ACCOUNTS, never from the row
        # (backends/s3/config.py); `credential` is the managed identity the
        # web identity token comes from.
        return S3Backend.from_config(
            backend_id, config, clock=clock, private_network=private_network, credential=credential
        )
    raise ValueError(f"unknown backend kind {kind!r}")


_CAPS_CACHE: dict[tuple[str, str], Capabilities] = {}


def capabilities_for(kind: str, config: Mapping[str, Any]) -> Capabilities:
    """A backend row's `capabilities()` without building the backend (the
    api's recall answer: restore speeds, ETAs, the no-promise size)."""
    if kind == "azure_blob":
        from ..backends.azure.capabilities import capabilities_for as azure_caps
        from ..backends.azure.config import AzureBlobConfig

        return azure_caps(AzureBlobConfig.from_mapping(config))
    if kind == "sim":
        layout = config.get("layout", "azure_two_account")
        if layout not in SIM_LAYOUTS:
            raise ValueError(f"sim layout {layout!r} is not one of {SIM_LAYOUTS}")
        got = _CAPS_CACHE.get((kind, layout))
        if got is None:
            from ..backends.sim import SimBackend, VirtualClock

            got = getattr(SimBackend, layout)(VirtualClock()).capabilities()
            _CAPS_CACHE[(kind, layout)] = got
        return got
    if kind == "s3":
        from ..backends.s3.capabilities import s3_backend_capabilities

        return s3_backend_capabilities(versioning=bool(config.get("versioning", True)))
    raise ValueError(f"unknown backend kind {kind!r}")


class BackendRegistry:
    """Backends by `catalog.backend.backend_id`, built on first use."""

    def __init__(
        self,
        rows: Iterable[BackendRow] = (),
        *,
        clock: Clock | None = None,
        credential: Any = None,
        build: Callable[..., StorageBackend] = backend_for,
    ) -> None:
        self._rows = {r.backend_id: r for r in rows}
        self._built: dict[int, StorageBackend] = {}
        self._clock = clock
        self._credential = credential
        self._build = build

    @classmethod
    def from_mapping(cls, backends: Mapping[int, StorageBackend]) -> BackendRegistry:
        reg = cls()
        reg._built.update(backends)
        return reg

    @classmethod
    async def load(
        cls, conn: Any, *, clock: Clock | None = None, credential: Any = None
    ) -> BackendRegistry:
        """Read every `catalog.backend` row (a psycopg async connection)."""
        cur = await conn.execute(
            "SELECT backend_id, kind, config, private_network FROM catalog.backend"
        )
        rows = [BackendRow(r[0], r[1], r[2] or {}, r[3]) for r in await cur.fetchall()]
        return cls(rows, clock=clock, credential=credential)

    def kinds(self) -> dict[int, str]:
        """backend_id -> catalog kind (``azure_blob``, ``s3``, ``sim``) of every row."""
        return {bid: row.kind for bid, row in self._rows.items()}

    def add(self, backend_id: int, backend: StorageBackend) -> None:
        self._built[backend_id] = backend

    def get(self, backend_id: int) -> StorageBackend:
        backend = self._built.get(backend_id)
        if backend is not None:
            return backend
        row = self._rows.get(backend_id)
        if row is None:
            # Rows are read once, at start (with the dev-clock and sim checks).
            raise KeyError(f"no backend {backend_id} in the catalog; restart the worker")
        backend = self._build(
            row.backend_id,
            row.kind,
            row.config,
            clock=self._clock,
            credential=self._credential,
            private_network=row.private_network,
        )
        self._built[backend_id] = backend
        return backend

    async def close(self) -> None:
        for backend in self._built.values():
            close = getattr(backend, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
