"""In-memory TTL cache with single-flight (concurrent identical requests share one fetch)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class TTLCache:
    def __init__(self, max_items: int = 4096) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._inflight: dict[Any, asyncio.Future] = {}
        self._max = max_items

    def _evict(self) -> None:
        if len(self._data) < self._max:
            return
        now = time.monotonic()
        for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
            del self._data[k]
        while len(self._data) >= self._max:
            del self._data[next(iter(self._data))]

    def peek(self, key: Any) -> tuple[bool, Any]:
        item = self._data.get(key)
        if item and item[0] > time.monotonic():
            return True, item[1]
        return False, None

    def put(self, key: Any, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        self._evict()
        self._data[key] = (time.monotonic() + ttl, value)

    async def get_or_fetch(self, key: Any, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        """Return (value, from_cache). Concurrent callers with the same key share one fetch.

        Cancellation: a waiter that is cancelled itself raises CancelledError and leaves the shared fetch running for
        the others (``asyncio.shield``). When the caller running the fetch (the leader) is cancelled, its waiters do
        not inherit that cancellation: they wake with an internal marker and start over, so one of them runs the fetch
        again and the rest share it. A failure other than cancellation reaches every waiter as before."""
        while True:
            hit, value = self.peek(key)
            if hit:
                return value, True
            fut = self._inflight.get(key)
            if fut is None:
                break
            try:
                return await asyncio.shield(fut), True
            except _LeaderCancelled:
                continue  # the leader was cancelled, not us: fetch again (the first to wake leads)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await fetch()
        except asyncio.CancelledError:
            # Not fut.cancel(): the waiters would raise CancelledError although nobody cancelled them.
            fut.set_exception(_LeaderCancelled())
            fut.exception()  # mark as retrieved so asyncio does not log it when nobody waits
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark as retrieved so asyncio does not log it when nobody waits
            raise
        else:
            fut.set_result(value)
            self.put(key, value, ttl)
            return value, False
        finally:
            if self._inflight.get(key) is fut:
                del self._inflight[key]


class _LeaderCancelled(Exception):
    """Set on the shared future when the caller running the fetch was cancelled; its waiters fetch again."""
