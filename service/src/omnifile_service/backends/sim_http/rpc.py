"""The sim backend's control calls over HTTP, for processes that don't host it.

In the end-to-end setup sim_http runs as its own process (the edge writes
blobs into it), so the worker reaches the same `SimBackend` through
``POST /_sim/backend`` (admin only): tier moves, restores, the change feed,
retention and holds, tags, versions, deletes. Reads still go through the
Azure-shaped data plane with a SAS, like real Azure.

The wire form is JSON with tagged values: the contract's dataclasses
(``{"__t": "ObjectRef", ...}``), enums (``{"__e": "LogicalTier", "v": "hot"}``),
datetimes, timedeltas and bytes. Only the types and operations listed here
cross the wire; an error comes back as ``{"error": {"type", "message"}}`` and
is raised again on the caller's side as the same `BackendError` subclass.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .. import base

TYPES: dict[str, type] = {
    t.__name__: t
    for t in (
        base.ObjectRef,
        base.ObjectHead,
        base.ObjectPage,
        base.ObjectVersion,
        base.ListedVersion,
        base.VersionPage,
        base.RestoreHandle,
        base.RestoreStatus,
        base.ChangePage,
        base.ChangeRecord,
        base.CommittedBlock,
    )
}
ENUMS: dict[str, type[Enum]] = {
    e.__name__: e for e in (base.LogicalTier, base.RestoreSpeed, base.RestoreState, base.ChangeKind)
}
ERRORS: dict[str, type[base.BackendError]] = {
    e.__name__: e
    for e in (
        base.BackendError,
        base.ObjectNotFound,
        base.Immutable,
        base.UnsupportedOperation,
        base.NotRestored,
        base.Throttled,
    )
}

#: Operations callable over the wire (`SimBackend` methods), async unless noted.
OPS = frozenset(
    {
        "head",
        "list",
        "list_versions",
        "list_all_versions",
        "set_class",
        "copy_object",
        "restore",
        "restore_status",
        "delete",
        "set_retention",
        "set_legal_hold",
        "set_tags",
        "get_tags",
        "set_metadata",
        "committed_blocks",
        "read_changes",
        # sync helpers (sim's management plane and test fixtures)
        "create_container",
        "set_container_policy",
        "delete_container_policy",
        "put_synthetic",
        # bytes in hand: a stream does not cross the wire
        "put_bytes",
    }
)


def encode(v: Any) -> Any:
    if isinstance(v, Enum):  # before str: the contract's enums are StrEnums
        return {"__e": type(v).__name__, "v": v.value}
    if v is None or isinstance(v, bool | int | float | str):
        return v
    if isinstance(v, datetime):
        return {"__dt": v.isoformat()}
    if isinstance(v, timedelta):
        return {"__td": v.total_seconds()}
    if isinstance(v, bytes | bytearray | memoryview):
        return {"__b": bytes(v).hex()}
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        name = type(v).__name__
        if name not in TYPES:
            raise TypeError(f"{name} doesn't cross the sim RPC")
        out = {f.name: encode(getattr(v, f.name)) for f in dataclasses.fields(v)}
        out["__t"] = name
        return out
    if isinstance(v, Mapping):
        return {"__m": [[encode(k), encode(x)] for k, x in v.items()]}
    if isinstance(v, list | tuple | set | frozenset):
        return [encode(x) for x in v]
    raise TypeError(f"{type(v).__name__} doesn't cross the sim RPC")


def decode(v: Any) -> Any:
    if isinstance(v, list):
        return [decode(x) for x in v]
    if not isinstance(v, dict):
        return v
    if "__e" in v:
        return ENUMS[v["__e"]](v["v"])
    if "__dt" in v:
        return datetime.fromisoformat(v["__dt"])
    if "__td" in v:
        return timedelta(seconds=v["__td"])
    if "__b" in v:
        return bytes.fromhex(v["__b"])
    if "__m" in v:
        return {decode(k): decode(x) for k, x in v["__m"]}
    if "__t" in v:
        cls = TYPES[v["__t"]]
        kw = {k: decode(x) for k, x in v.items() if k != "__t"}
        if cls is base.ObjectPage or cls is base.VersionPage:
            kw["items"] = tuple(kw["items"])
        if cls is base.ChangePage:
            kw["records"] = tuple(kw["records"])
        return cls(**kw)
    raise ValueError(f"untagged object in sim RPC: {sorted(v)}")


def error_body(e: BaseException) -> dict[str, Any]:
    name = type(e).__name__
    for cls in type(e).__mro__:
        if cls.__name__ in ERRORS:
            name = cls.__name__
            break
    else:
        name = "BackendError"
    out: dict[str, Any] = {"type": name, "message": str(e)}
    retry = getattr(e, "retry_after", None)
    if isinstance(retry, timedelta):
        out["retry_after_s"] = retry.total_seconds()
    return out


def raise_error(err: Mapping[str, Any]) -> None:
    cls = ERRORS.get(str(err.get("type")), base.BackendError)
    message = str(err.get("message", ""))
    if cls is base.Throttled:
        retry = err.get("retry_after_s")
        raise base.Throttled(message, None if retry is None else timedelta(seconds=retry))
    raise cls(message)
