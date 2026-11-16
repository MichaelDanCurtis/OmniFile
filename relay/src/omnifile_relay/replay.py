"""Single-use intents.

The relay reserves an intent id before forwarding it and keeps it until the intent expires
(after that the token is rejected as expired anyway). If forwarding fails, the reservation is
released so the user can try again.

This store is per replica. The service is the authority: it must treat ``intent_id`` as an
idempotency key (a unique constraint in the catalog), so a click that reaches two replicas is
still acted on once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol


class ReplayStoreFull(Exception):
    """Every slot holds an unexpired intent: refuse (fail closed) rather than
    forget one, and say so, rather than answer as if the click were a repeat."""


class ReplayStore(Protocol):
    def reserve(self, intent_id: str, expires_at: float) -> bool:
        """Reserve ``intent_id``; False if it was already used or reserved.
        Raises ReplayStoreFull when it can hold no more."""
        ...

    def release(self, intent_id: str) -> None:
        """Undo a reservation whose forwarding failed."""
        ...


class MemoryReplayStore:
    def __init__(
        self,
        *,
        max_entries: int = 200_000,
        leeway_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._seen: dict[str, float] = {}
        self._max = max_entries
        self._leeway = leeway_seconds
        self._clock = clock
        # While full, a purge (a scan of every entry) runs at most once a second.
        self._last_purge = float("-inf")

    def _purge(self, now: float) -> None:
        dead = [k for k, until in self._seen.items() if until <= now]
        for k in dead:
            del self._seen[k]

    def reserve(self, intent_id: str, expires_at: float) -> bool:
        now = self._clock()
        until = self._seen.get(intent_id)
        if until is not None and until > now:
            return False
        if len(self._seen) >= self._max:
            if now - self._last_purge >= 1.0:
                self._last_purge = now
                self._purge(now)
            if len(self._seen) >= self._max:
                # Fail closed: refuse rather than forget used intents.
                raise ReplayStoreFull
        self._seen[intent_id] = expires_at + self._leeway
        return True

    def release(self, intent_id: str) -> None:
        self._seen.pop(intent_id, None)

    def __len__(self) -> int:
        return len(self._seen)
