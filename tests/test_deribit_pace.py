"""Pace of the shared Deribit client against the public limit measured on 2026-11-25 (docs/changes/2026-11-25-deribit-collector-gaps.md).

Since the Deribit maintenance of 2026-11-20 (20:00-21:17 UTC) a burst got HTTP 429 `too_many_requests`: from 20.11 22:19
about half the Deribit legs of `option_xvenue` and, from 21.11, 21-23 of the 40 daily `option_legs` tickers were lost.
The probe (scripts/probe_deribit_pace.py, 14 tickers in one gather per setting) fits a venue bucket of about 5 requests
refilled at 0.9-1 per second: 8/s burst 10 lost 9 of 14 (this model gives exactly 9), 1/s and 0.8/s with burst 4 lost
none. The model here takes the slow end (0.9/s), so a long queue (the hourly snapshot, a service restart) stays under it.
The real `TokenBucket` runs on a virtual clock, so no test sleeps.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tradedesk.config import Settings
from tradedesk.core import ratelimit
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime

VENUE_CAPACITY = 5.0  # requests Deribit let through at once (probe 2026-11-25)
VENUE_REFILL = 0.9  # per second, the slow end of the probe's 0.9-1
NOW = datetime(2026, 11, 25, 15, 11, tzinfo=UTC)  # the probe's time; nothing here reads the clock


async def _send_times(bucket: TokenBucket, n: int, monkeypatch) -> list[float]:
    """Seconds after the first acquire at which ``n`` requests queued at once leave ``bucket`` (virtual clock)."""
    clock = [bucket._stamp]  # the bucket's own construction time: no tokens gained or lost by switching clocks

    async def sleep(s: float) -> None:
        clock[0] += s

    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(ratelimit, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleep))
    t0, out = clock[0], []
    for _ in range(n):  # reservation-based: the sequential schedule equals the one of n concurrent callers
        await bucket.acquire()
        out.append(clock[0] - t0)
    return out


def _venue_rejections(times: list[float], capacity: float = VENUE_CAPACITY, refill: float = VENUE_REFILL) -> int:
    """HTTP 429s from a venue bucket of ``capacity`` requests refilled at ``refill`` per second, full at the start."""
    tokens, last, bad = capacity, 0.0, 0
    for t in times:
        tokens = min(capacity, tokens + (t - last) * refill)
        last = t
        if tokens >= 1.0:
            tokens -= 1.0
        else:
            bad += 1
    return bad


async def test_the_venue_model_reproduces_the_probe_at_the_old_pace(monkeypatch):
    times = await _send_times(TokenBucket(8.0, 10), 14, monkeypatch)
    assert _venue_rejections(times) == 9  # probe 2026-11-25 15:11 UTC: 5 ok, 9 x HTTP 429


@pytest.mark.live_deribit_pace
async def test_runtime_paces_deribit_under_the_public_limit(monkeypatch):
    rt = Runtime(Settings())
    rt.now = lambda: NOW
    try:
        bucket = rt.deribit.http._limiter
        assert bucket.rate <= VENUE_REFILL and bucket.burst < VENUE_CAPACITY  # was 8/s, burst 10
        # 60 requests at once: about a service restart's worth (all jobs start together) plus the hourly snapshot.
        assert _venue_rejections(await _send_times(bucket, 60, monkeypatch)) == 0
    finally:
        await rt.aclose()


async def test_tests_run_deribit_at_the_fast_pace():
    rt = Runtime(Settings())  # tests/conftest.py fast_deribit_pace: fake venues have no limit
    rt.now = lambda: NOW
    try:
        assert (rt.deribit.http._limiter.rate, rt.deribit.http._limiter.burst) == (8.0, 10.0)
    finally:
        await rt.aclose()
