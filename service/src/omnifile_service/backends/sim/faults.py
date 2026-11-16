"""Fault injection for sim.

Small and explicit: every fault is registered by a test, applies to named
operations, and is consumed or bounded in time. Nothing is random.

    sim.faults.fail_nth(Op.GET_RANGE, 3)            # the 3rd get_range from now fails
    sim.faults.throttle(Op.RESTORE, times=2)        # next 2 restores raise Throttled
    sim.faults.latency(Op.PUT, timedelta(seconds=2), virtual=True)
    sim.faults.outage(start, end)                   # every op in [start, end) is Unavailable
    sim.faults.corrupt_next_copy(CopyFault.PARTIAL) # next copy_object silently truncates
    sim.faults.fail_next_restore()                  # next restore ends FAILED
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ..base import BackendError, Throttled


class Op(StrEnum):
    """Backend operations faults can target (the StorageBackend method names)."""

    PUT = "put"
    GET_RANGE = "get_range"
    HEAD = "head"
    LIST = "list"
    LIST_VERSIONS = "list_versions"
    SET_CLASS = "set_class"
    COPY_OBJECT = "copy_object"
    RESTORE = "restore"
    RESTORE_STATUS = "restore_status"
    DELETE = "delete"
    SET_RETENTION = "set_retention"
    SET_LEGAL_HOLD = "set_legal_hold"
    EDGE_CREDENTIAL = "edge_credential"
    READ_CHANGES = "read_changes"
    COMPUTE_CHECKSUM = "compute_checksum"
    SET_TAGS = "set_tags"
    GET_TAGS = "get_tags"
    SET_METADATA = "set_metadata"


class CopyFault(StrEnum):
    CORRUPT = "corrupt"  # same length, one byte flipped
    PARTIAL = "partial"  # truncated to half


class InjectedError(BackendError):
    """An error a test asked for."""


class Unavailable(BackendError):
    """The provider is unreachable (an injected outage window)."""


@dataclass
class _NthFailure:
    op: Op
    at_call: int
    error: BaseException


@dataclass
class _Latency:
    op: Op
    delay: timedelta
    virtual: bool
    remaining: int | None


@dataclass
class _Throttle:
    op: Op
    remaining: int
    retry_after: timedelta


@dataclass
class _Outage:
    start: datetime
    end: datetime
    ops: frozenset[Op] | None


@dataclass(frozen=True)
class Delay:
    """What the backend must wait before running an operation."""

    real_seconds: float = 0.0
    virtual: timedelta = timedelta(0)


class FaultInjector:
    def __init__(self) -> None:
        self.calls: Counter[Op] = Counter()
        self._nth: list[_NthFailure] = []
        self._latency: list[_Latency] = []
        self._throttle: list[_Throttle] = []
        self._outages: list[_Outage] = []
        self._copy_faults: list[CopyFault] = []
        self._restore_faults: list[str] = []

    # --- registration -------------------------------------------------

    def fail_nth(self, op: Op, n: int = 1, error: BaseException | None = None) -> None:
        """The `n`th call of `op`, counted from now, raises `error`."""
        if n < 1:
            raise ValueError("n starts at 1")
        err = error or InjectedError(f"injected failure on {op} call {n}")
        self._nth.append(_NthFailure(Op(op), self.calls[Op(op)] + n, err))

    def latency(
        self, op: Op, delay: timedelta, *, virtual: bool = False, count: int | None = None
    ) -> None:
        """Delay `op`. Real time (asyncio.sleep) by default; `virtual=True`
        advances the backend's clock instead (needs a clock with `advance`).
        `count=None` applies until `clear()`."""
        self._latency.append(_Latency(Op(op), delay, virtual, count))

    def throttle(
        self, op: Op, times: int = 1, retry_after: timedelta = timedelta(seconds=1)
    ) -> None:
        """The next `times` calls of `op` raise `Throttled(retry_after)`."""
        self._throttle.append(_Throttle(Op(op), times, retry_after))

    def outage(self, start: datetime, end: datetime, ops: set[Op] | None = None) -> None:
        """Calls in [start, end) on the backend clock raise `Unavailable`."""
        if end <= start:
            raise ValueError("an outage ends after it starts")
        self._outages.append(_Outage(start, end, frozenset(Op(o) for o in ops) if ops else None))

    def corrupt_next_copy(self, mode: CopyFault = CopyFault.CORRUPT, count: int = 1) -> None:
        """The next `count` copies (copy_object, archive moves, rehydrate
        copies) succeed but land damaged bytes. Recorded checksums are copied
        from the source unchanged, so only a read-back verification finds it
        (decision 24)."""
        self._copy_faults += [CopyFault(mode)] * count

    def fail_next_restore(self, detail: str = "injected restore failure", count: int = 1) -> None:
        """The next `count` restores are accepted, then end FAILED when due."""
        self._restore_faults += [detail] * count

    def clear(self) -> None:
        self._nth.clear()
        self._latency.clear()
        self._throttle.clear()
        self._outages.clear()
        self._copy_faults.clear()
        self._restore_faults.clear()

    # --- used by the backend -------------------------------------------

    def check(self, op: Op, now: datetime) -> Delay:
        """Count a call of `op`; raise the injected error, or return a delay."""
        self.calls[op] += 1
        call = self.calls[op]
        for outage in self._outages:
            if outage.start <= now < outage.end and (outage.ops is None or op in outage.ops):
                raise Unavailable(f"{op}: provider unavailable until {outage.end.isoformat()}")
        for t in self._throttle:
            if t.op == op and t.remaining > 0:
                t.remaining -= 1
                raise Throttled(f"{op}: injected throttling", retry_after=t.retry_after)
        for f in self._nth:
            if f.op == op and f.at_call == call:
                self._nth.remove(f)
                raise f.error
        real = 0.0
        virtual = timedelta(0)
        for lat in self._latency:
            if lat.op != op or lat.remaining == 0:
                continue
            if lat.remaining is not None:
                lat.remaining -= 1
            if lat.virtual:
                virtual += lat.delay
            else:
                real += lat.delay.total_seconds()
        return Delay(real, virtual)

    def take_copy_fault(self) -> CopyFault | None:
        return self._copy_faults.pop(0) if self._copy_faults else None

    def take_restore_fault(self) -> str | None:
        return self._restore_faults.pop(0) if self._restore_faults else None
