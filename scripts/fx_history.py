"""Forex history from Dukascopy's free datafeed: 1-minute bid candles per day, aggregated to the timeframe you ask.

For research (agents' tests of the course's forex setups); not an MCP tool. Dukascopy publishes the data for free under
its own terms of use (personal research): https://www.dukascopy.com — do not redistribute the files.

    uv run python scripts/fx_history.py EURUSD --start 2021-01-01 --end 2026-09-30 --tf 5m \
        --cache ~/.cache/tradedesk-agents/fx --out ~/.cache/tradedesk-agents/fx/EURUSD_5m.csv

Each day is one file https://datafeed.dukascopy.com/datafeed/<SYM>/<YYYY>/<MM-1>/<DD>/BID_candles_min_1.bi5 (LZMA,
24-byte big-endian records: seconds from midnight UTC, open, close, low, high as integers in points, volume float).
Weekend days are empty. Raw days are cached as they arrive, so an interrupted run resumes. Times are UTC.

Dukascopy throttles long downloads (connection resets, timeouts, 503 after a few thousand days). Requests are paced
(``--pace`` seconds apart), a failing day is retried with growing pauses and then left for later instead of aborting the
run, and the other days keep downloading. If days are still missing at the end, they are listed, no CSV is written and
the exit code is 3: run the same command again later (cached days are not fetched again), or pass ``--allow-missing``
to write the CSV without them.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import lzma
import struct
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
RECORD = struct.Struct(">IIIIIf")
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def point(symbol: str) -> float:
    """Price units per point: JPY pairs and metals quote with 3 decimals, other pairs with 5."""
    s = symbol.upper()
    return 1e3 if s.endswith("JPY") or s.startswith(("XAU", "XAG")) else 1e5


def parse_day(raw: bytes, day: date, symbol: str) -> list[tuple[int, float, float, float, float, float]]:
    """Decode one day's file into (ts_ms, open, high, low, close, volume) rows."""
    if not raw:
        return []
    data = lzma.decompress(raw)
    p = point(symbol)
    base = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    rows = []
    for off in range(0, len(data) - RECORD.size + 1, RECORD.size):
        sec, o, c, lo, h, v = RECORD.unpack_from(data, off)
        rows.append((base + sec * 1000, o / p, h / p, lo / p, c / p, float(v)))
    return rows


def aggregate(rows: list[tuple[int, float, float, float, float, float]], minutes: int) -> list[list[float]]:
    """Group 1-minute rows into buckets of ``minutes`` aligned to UTC midnight."""
    out: list[list[float]] = []
    step = minutes * 60_000
    for ts, o, h, lo, c, v in rows:
        b = ts - ts % step
        if out and out[-1][0] == b:
            cur = out[-1]
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], h), min(cur[3], lo), c, cur[5] + v
        else:
            out.append([b, o, h, lo, c, v])
    return out


class Pacer:
    """At most one request start per ``interval`` seconds across all workers."""

    def __init__(self, interval: float) -> None:
        self.interval, self.next_at, self.lock = interval, 0.0, asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = asyncio.get_running_loop().time()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


async def fetch_day(
    client: httpx.AsyncClient, symbol: str, day: date, cache: Path, pacer: Pacer | None = None, attempts: int = 6
) -> bytes | None:
    """The day's raw file (b"" for a day without data), or None if it could not be downloaded now."""
    f = cache / symbol / f"{day.isoformat()}.bi5"
    if f.exists():
        return f.read_bytes()
    url = URL.format(sym=symbol, y=day.year, m=day.month - 1, d=day.day)
    for attempt in range(attempts):
        if pacer:
            await pacer.wait()
        try:
            r = await client.get(url)
            if r.status_code == 404:
                raw = b""
                break
            r.raise_for_status()
            raw = r.content
            break
        except httpx.HTTPError as e:
            print(f"{symbol} {day}: {type(e).__name__} (attempt {attempt + 1}/{attempts})", file=sys.stderr)
            await asyncio.sleep(min(120, 3 * 2**attempt))
    else:
        return None
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(raw)
    return raw


async def run(
    symbol: str,
    start: date,
    end: date,
    tf: str,
    cache: Path,
    out: Path,
    concurrency: int,
    pace: float = 0.5,
    allow_missing: bool = False,
    client: httpx.AsyncClient | None = None,
) -> tuple[int, list[date]]:
    """Download (or read from the cache) every day and write the CSV; returns (bars written, days still missing)."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    sem = asyncio.Semaphore(concurrency)
    pacer = Pacer(pace)
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, headers={"User-Agent": "tradedesk-research"})
    try:

        async def one(d: date) -> tuple[date, bytes | None]:
            async with sem:
                return d, await fetch_day(client, symbol, d, cache, pacer)

        results = await asyncio.gather(*(one(d) for d in days))
        missing = sorted(d for d, raw in results if raw is None)
        if missing:  # one more slow pass over the days that failed
            again = [(d, await fetch_day(client, symbol, d, cache, Pacer(max(pace, 2.0)), attempts=3)) for d in missing]
            results = [(d, raw) for d, raw in results if raw is not None] + again
            missing = sorted(d for d, raw in again if raw is None)
    finally:
        if own:
            await client.aclose()
    if missing and not allow_missing:
        return 0, missing
    rows: list[tuple[int, float, float, float, float, float]] = []
    for d, raw in sorted(results, key=lambda x: x[0]):
        rows.extend(parse_day(raw or b"", d, symbol))
    bars = aggregate(rows, TF_MIN[tf]) if tf != "1m" else [list(r) for r in rows]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "open", "high", "low", "close", "volume"])
        w.writerows(bars)
    return len(bars), missing


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbol", help="Dukascopy symbol, e.g. EURUSD, GBPUSD, USDJPY, XAUUSD")
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="last day, YYYY-MM-DD (UTC)")
    ap.add_argument("--tf", default="5m", choices=sorted(TF_MIN))
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "fx")
    ap.add_argument("--out", type=Path, required=True, help="CSV to write (ts_ms, open, high, low, close, volume)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--pace", type=float, default=0.5, help="seconds between request starts (Dukascopy throttles)")
    ap.add_argument("--allow-missing", action="store_true", help="write the CSV even if some days could not be fetched")
    a = ap.parse_args()
    n, missing = asyncio.run(
        run(
            a.symbol.upper(),
            date.fromisoformat(a.start),
            date.fromisoformat(a.end),
            a.tf,
            a.cache.expanduser(),
            a.out.expanduser(),
            a.concurrency,
            a.pace,
            a.allow_missing,
        )
    )
    if missing:
        days = ", ".join(d.isoformat() for d in missing[:20]) + (" …" if len(missing) > 20 else "")
        print(f"{a.symbol.upper()}: {len(missing)} days could not be downloaded now: {days}", file=sys.stderr)
        if not a.allow_missing:
            print("no CSV written; run the same command again later (cached days are kept)", file=sys.stderr)
            sys.exit(3)
    print(f"{a.symbol.upper()} {a.tf}: {n} bars -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
