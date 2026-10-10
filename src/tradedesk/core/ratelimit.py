from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Async token bucket: `rate` tokens/second, up to `burst` stored."""

    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else max(1, int(rate)))
        self._tokens = self.burst
        self._stamp = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            self._tokens -= n  # reserve; may go negative, which means "wait"
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait > 0:
            await asyncio.sleep(wait)
