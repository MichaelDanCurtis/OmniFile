"""Virtual time for sim: a 15-hour rehydrate takes one `advance()` call.

Two clocks:

- `VirtualClock`: in-process, starts at a fixed instant and only moves when
  told to (unit tests, the contract suite).
- `OffsetClock`: the wall clock plus an offset kept in a small file, so
  several processes share one controllable "now" (the dev and end-to-end
  setup: sim_http, the api and the worker, and Postgres under libfaketime).
  `POST /_sim/advance` on sim_http moves it for everyone. The file holds
  libfaketime's relative-offset syntax, ``+<seconds>`` (for example
  ``+86400``), so the same file can be Postgres's ``FAKETIME_TIMESTAMP_FILE``
  and the database's ``now()`` moves too.

`dev_clock()` returns the `OffsetClock` named by ``OMNIFILE_DEV_CLOCK_FILE``,
only in dev mode (``OMNIFILE_DEV_MODE=1``); otherwise the real clock.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_START = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CLOCK_FILE_ENV = "OMNIFILE_DEV_CLOCK_FILE"


class VirtualClock:
    """A `Clock` that only moves when told to.

    sim never reads the wall clock; it evaluates due events (restore
    completions, expiries, change-feed lag) against `now()` on every call, so
    any object with a `now()` method works, including the contract suite's
    `ManualClock`.
    """

    def __init__(self, start: datetime | None = None) -> None:
        start = start or DEFAULT_START
        if start.tzinfo is None:
            raise ValueError("VirtualClock needs a timezone-aware start")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("time only moves forward")
        self._now += delta
        return self._now

    def set(self, when: datetime) -> datetime:
        when = when.astimezone(UTC)
        if when < self._now:
            raise ValueError("time only moves forward")
        self._now = when
        return self._now


_OFFSET = re.compile(r"^\s*\+?(\d+(?:\.\d+)?)\s*$")


def parse_offset(text: str) -> timedelta:
    """``+86400`` (libfaketime's relative form, seconds) -> 1 day. Empty is 0."""
    if not text.strip():
        return timedelta(0)
    m = _OFFSET.match(text)
    if m is None:
        raise ValueError(f"clock offset {text!r} is not +<seconds>")
    return timedelta(seconds=float(m[1]))


def format_offset(offset: timedelta) -> str:
    return f"+{int(offset.total_seconds())}\n"


class OffsetClock:
    """The wall clock plus an offset shared through `path`.

    `now()` re-reads the file when its modification time changes (one stat
    per call), so an `advance` in one process is seen by every other. The
    offset only grows; whole seconds, as libfaketime reads them.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._offset = timedelta(0)

    def offset(self) -> timedelta:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return timedelta(0)
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if stamp != self._stamp:
                self._offset = parse_offset(self.path.read_text())
                self._stamp = stamp
            return self._offset

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset()

    def advance(self, delta: timedelta) -> datetime:
        """Move every process's clock forward by `delta` (rounded up to whole seconds)."""
        if delta < timedelta(0):
            raise ValueError("time only moves forward")
        with self._lock:
            current = parse_offset(self.path.read_text()) if self.path.exists() else timedelta(0)
            seconds = int(-(-(current + delta).total_seconds() // 1))
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(format_offset(timedelta(seconds=seconds)))
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            self._stamp = None
        return self.now()


class DevClockRefused(RuntimeError):
    """A virtual clock was asked for where it must never run."""


def dev_clock(environ: dict[str, str] | None = None):
    """The shared `OffsetClock` when ``OMNIFILE_DEV_CLOCK_FILE`` is set, else
    the system clock.

    The file is refused outside dev mode (``OMNIFILE_DEV_MODE=1``): a
    production process must fail, not quietly run on real time with a dev
    setting in its environment. The caller must also check every storage
    binding is simulated (`require_sim_only`) before using it.
    """
    from ..base import SystemClock

    env = os.environ if environ is None else environ
    path = env.get(CLOCK_FILE_ENV)
    if not path:
        return SystemClock()
    if env.get("OMNIFILE_DEV_MODE") != "1":
        raise DevClockRefused(f"{CLOCK_FILE_ENV} is a dev setting: it needs OMNIFILE_DEV_MODE=1")
    return OffsetClock(path)


#: Backend kinds a virtual clock may drive: the simulated backend only. Real
#: Azure under a moved clock would get skewed SAS expiries and, worse,
#: locked WORM retention stamped from a future "now", which can't be undone.
SIM_KINDS = frozenset({"sim"})


def require_sim_only(clock: object, kinds: dict[int, str]) -> None:
    """Refuse a virtual clock unless every catalog backend (id -> kind) is sim."""
    if not isinstance(clock, OffsetClock):
        return
    # A `nas` row (an annex's filer) has no data plane in the service: the
    # edge reads the NAS, on its own clock.
    real = {bid: kind for bid, kind in kinds.items() if kind not in SIM_KINDS | {"nas"}}
    if real:
        raise DevClockRefused(
            f"{CLOCK_FILE_ENV} is set but backends {sorted(real)} are not sim"
            f" ({', '.join(sorted(set(real.values())))}); the virtual clock only runs over sim"
        )
