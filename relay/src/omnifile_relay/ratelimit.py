"""Token-bucket rate limiting with a bounded key table."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.pop(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)  # evict least recently seen
        return allowed

    def refund(self, key: str) -> None:
        """Give back the token `allow` took (capped at the burst)."""
        if key in self._buckets:
            tokens, last = self._buckets[key]
            self._buckets[key] = (min(self._burst, tokens + 1.0), last)
