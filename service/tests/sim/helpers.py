"""Small sync wrappers so sim tests read top to bottom without an async plugin."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Any

from omnifile_service.backends.base import LogicalTier, ObjectRef
from omnifile_service.backends.sim import SimBackend


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def put(
    sim: SimBackend,
    container: str,
    key: str,
    *chunks: bytes,
    tier: LogicalTier | None = None,
    metadata: dict[str, str] | None = None,
) -> ObjectRef:
    return run(sim.put(container, key, stream(*chunks), tier, metadata or {}))


async def _read(sim: SimBackend, ref: ObjectRef, offset: int, length: int) -> bytes:
    return b"".join([c async for c in sim.get_range(ref, offset, length)])


def read(sim: SimBackend, ref: ObjectRef, offset: int = 0, length: int = 1 << 30) -> bytes:
    return run(_read(sim, ref, offset, length))


def current(ref: ObjectRef) -> ObjectRef:
    return ObjectRef(ref.backend_id, ref.container, ref.key)
