"""No provider call while the caller holds a transaction id, anywhere in the suite.

The edge feed's horizon is the oldest open transaction id, for every edge, so an id held
across a storage call keeps every edge's ACL and share events unread until the call ends.
`tests/policy/test_no_id_across_calls.py` guards the paths it names. This guards the rest,
including code not written yet: every test runs with the backend clients wrapped, and a
provider call made while the calling code's own connection holds an id fails that test,
unless its site is in `KNOWN`.

**`KNOWN` is meant to shrink.** Each entry is a real stall, named with why it is still there
and where it is tracked. Don't add a line to get a test through: move the call out of the
transaction (`worker.queue.outside`, or read, call, then lock and look again). A whole-suite
run also fails on an entry nothing hit, so a fixed site has to come off the list.

**How it decides.** It finds every connection in the calling frames and asks each one
`pg_current_xact_id_if_assigned()`. So another session's id can't fail a test or pass it, and
a read lock doesn't count. A call under `queue.outside` that runs in place is skipped: that is
a test or a CLI driving a handler in its own transaction, and a consumer makes that call with
no transaction open. `test_no_call_outside_passes_none_in_product_code` keeps that door shut
in the product code.

**What it can't see.** A call on another task or thread (`create_task`, `gather`, `to_thread`)
has none of its caller's frames, so it finds no connection. Nothing in a handler does that with
a wrapped client today; docs/milestones/results/wp23.md names them.
"""

from __future__ import annotations

import functools
import inspect
import sys
from contextlib import aclosing
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

import psycopg
from psycopg.rows import tuple_row

#: Graph and Bot Framework calls in the notify handler, with writes between recipients. Not a
#: storage call, but the same stall. Recording a card with its send stops a double send.
_NOTIFY = "Teams and Graph inside the notify handler (wp23.md)"
KNOWN: dict[str, str] = {
    "notify/delivery.py:_recipients": _NOTIFY,
    "notify/delivery.py:_conversation": _NOTIFY,
    "notify/delivery.py:_send_card": _NOTIFY,
    "notify/delivery.py:_email": _NOTIFY,
    "notify/delivery.py:resolve_identity": _NOTIFY,
    "notify/delivery.py:_resolve_user": _NOTIFY,
}

#: The provider clients. Imported late: this module loads with the conftest.
_CLIENTS = (
    ("omnifile_service.backends.sim.backend", "SimBackend"),
    ("omnifile_service.backends.s3.backend", "S3Backend"),
    ("omnifile_service.backends.azure.backend", "AzureBlobBackend"),
    ("omnifile_service.worker.backends", "SimHttpBackend"),
    ("omnifile_service.migrate.mover", "SimMover"),
    ("omnifile_service.migrate.storage_mover", "AzureStorageMover"),
    ("omnifile_service.api.dataplane", "BackendDataPlane"),
    ("omnifile_service.api.dataplane", "SimHttpDataPlane"),
    ("omnifile_service.notify.graph", "GraphClient"),
    ("omnifile_service.notify.botconnector", "BotConnector"),
    ("omnifile_service.notify.fakes.graph", "FakeGraph"),
    ("omnifile_service.notify.fakes.botconnector", "FakeBotConnector"),
    ("omnifile_service.settings.arm", "HttpArm"),
    ("omnifile_service.settings.fakes", "FakeArm"),
)
#: Not provider calls.
_LOCAL = frozenset({"capabilities", "clock", "kind", "backend_id"})
_SERVICE = "/omnifile_service/"
#: Frames that are the client itself, not who called it.
_INSIDE = (
    "backends/",
    "notify/fakes/",
    "notify/graph.py",
    "notify/botconnector.py",
    "migrate/mover.py",
    "migrate/storage_mover.py",
    "settings/arm.py",
    "settings/fakes.py",
)


@dataclass
class Guard:
    #: Unlisted sites this test hit: site -> the call.
    violations: dict[str, str] = field(default_factory=dict)
    #: Every listed site the session hit.
    hit: set[str] = field(default_factory=set)
    #: Every unlisted site the session hit.
    unlisted: set[str] = field(default_factory=set)


GUARD = Guard()


def _connection_in(frame: FrameType) -> psycopg.AsyncConnection | None:
    for value in frame.f_locals.values():
        if isinstance(value, psycopg.AsyncConnection):
            return value
        if isinstance(value, psycopg.AsyncCursor):
            return value.connection
        inner = getattr(type(value), "__dict__", {}).get("_conn") or vars_get(value, "_conn")
        if isinstance(inner, psycopg.AsyncConnection):
            return inner
    return None


def vars_get(value: Any, name: str) -> Any:
    try:
        return vars(value).get(name)
    except TypeError:
        return None


def _who_called() -> tuple[str, list[psycopg.AsyncConnection]] | None:
    """(the nearest service site, every connection in the frames above it: a nearer idle one
    doesn't excuse a further one with an id). None if a test or a CLI is driving this in its
    own transaction, through `outside` in place, or no service code called it."""
    site = None
    conns: list[psycopg.AsyncConnection] = []
    frame: FrameType | None = sys._getframe(2)
    while frame is not None:
        path = frame.f_code.co_filename
        if _SERVICE in path:
            rel = path.split(_SERVICE, 1)[1]
            if rel == "worker/queue.py" and frame.f_code.co_name == "outside":
                return None
            if not rel.startswith(_INSIDE):
                if site is None and rel != "worker/queue.py":
                    site = f"{rel}:{frame.f_code.co_name}"
                conn = _connection_in(frame)
                if conn is not None and not any(conn is c for c in conns):
                    conns.append(conn)
        frame = frame.f_back
    return (site, conns) if site and conns else None


async def _holds_an_id(conn: psycopg.AsyncConnection) -> bool:
    if conn.closed or conn.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
        return False
    async with conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute("SELECT pg_current_xact_id_if_assigned() IS NOT NULL")
        row = await cur.fetchone()
    return bool(row and row[0])


async def _check(call: str) -> None:
    found = _who_called()
    if found is None:
        return
    site, conns = found
    if not any([await _holds_an_id(c) for c in conns]):
        return
    if site in KNOWN:
        GUARD.hit.add(site)
    else:
        GUARD.violations[site] = call


def _wrap(cls: type) -> None:
    for name, fn in list(vars(cls).items()):
        if name.startswith("_") or name in _LOCAL or not callable(fn):
            continue
        if isinstance(fn, (staticmethod, classmethod, property)):
            continue
        q = f"{cls.__name__}.{name}"
        # Azure's is a plain function that returns its generator.
        if inspect.isasyncgenfunction(fn) or name == "get_range":

            def as_generator(fn: Any = fn, q: str = q) -> Any:
                @functools.wraps(fn)
                async def wrapped(*a: Any, **k: Any) -> Any:
                    await _check(q)
                    async with aclosing(fn(*a, **k)) as chunks:
                        async for chunk in chunks:
                            yield chunk

                return wrapped

            setattr(cls, name, as_generator())
        elif inspect.iscoroutinefunction(fn):

            def as_coroutine(fn: Any = fn, q: str = q) -> Any:
                @functools.wraps(fn)
                async def wrapped(*a: Any, **k: Any) -> Any:
                    await _check(q)
                    return await fn(*a, **k)

                return wrapped

            setattr(cls, name, as_coroutine())


def install() -> None:
    import importlib

    for module, name in _CLIENTS:
        _wrap(getattr(importlib.import_module(module), name))


def take_violations() -> dict[str, str]:
    """What this test hit, and forget it."""
    found, GUARD.violations = GUARD.violations, {}
    return found


def stale() -> set[str]:
    """Listed sites nothing hit: fixed, so they come off the list."""
    return set(KNOWN) - GUARD.hit
