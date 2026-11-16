"""A clock the tests move by hand (virtual time)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class ManualClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("time only moves forward")
        self._now += delta
