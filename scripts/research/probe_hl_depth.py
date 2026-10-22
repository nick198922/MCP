"""Measure how deep Hyperliquid history goes: candleSnapshot (1h / 4h / 1d) and fundingHistory per coin.

Step 1 of IDEA-20261014-hyperliquid-binance-funding-spread (critic condition 1), run before any P&L.
Network only through the runtime provider (``rt.hyperliquid``), so rate limits and retries apply.

Usage:
    uv run python scripts/research/probe_hl_depth.py [--coins BTC,ETH,kPEPE] [--out probe.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.server import build_server

HOUR_MS = 3_600_000
TF_MS = {"1h": HOUR_MS, "4h": 4 * HOUR_MS, "1d": 24 * HOUR_MS}


def ymd(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


async def info(rt: Any, payload: dict[str, Any]) -> Any:
    # The provider's helpers upper-case coin names, which breaks k-prefixed coins (kPEPE); go through _info.
    return (await rt.hyperliquid._info(payload, ttl=0)).data


async def candle_depth(rt: Any, coin: str, tf: str, now_ms: int) -> dict[str, Any]:
    """Ask from epoch 2022-01-01 to now; the API answers with at most N newest candles."""
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    rows = await info(rt, {"type": "candleSnapshot", "req": {"coin": coin, "interval": tf, "startTime": start, "endTime": now_ms}})
    rows = rows or []
    first = min((int(r["t"]) for r in rows), default=None)
    # second probe: a window far in the past, to tell "cap on count" from "cap on age"
    old = await info(
        rt, {"type": "candleSnapshot", "req": {"coin": coin, "interval": tf, "startTime": start, "endTime": start + 600 * TF_MS[tf]}}
    )
    return {
        "n": len(rows),
        "first": first,
        "days": None if first is None else (now_ms - first) / (24 * HOUR_MS),
        "old_window_n": len(old or []),
    }


async def funding_first(rt: Any, coin: str) -> dict[str, Any]:
    rows = await info(rt, {"type": "fundingHistory", "coin": coin, "startTime": 0})
    rows = rows or []
    return {"first": min((int(r["time"]) for r in rows), default=None), "n_first_page": len(rows), "sample": rows[:2]}


async def run(coins: list[str], out: str) -> None:
    _, rt = build_server()
    now_ms = int(time.time() * 1000)
    try:
        t0 = time.perf_counter()
        meta = await info(rt, {"type": "metaAndAssetCtxs"})
        uni = meta[0]["universe"]
        delisted = [u["name"] for u in uni if u.get("isDelisted")]
        print(
            f"HL universe: {len(uni)} perps, delisted flagged: {len(delisted)} (metaAndAssetCtxs {time.perf_counter() - t0:.1f}s)",
            flush=True,
        )
        res: dict[str, Any] = {"now": now_ms, "n_universe": len(uni), "delisted": delisted, "coins": {}}
        for coin in coins:
            t1 = time.perf_counter()
            row: dict[str, Any] = {"funding": await funding_first(rt, coin)}
            for tf in ("1h", "4h", "1d"):
                row[tf] = await candle_depth(rt, coin, tf, now_ms)
            res["coins"][coin] = row
            print(
                f"{coin:>8}: funding from {ymd(row['funding']['first'])}; "
                + "; ".join(
                    f"{tf} n={row[tf]['n']} from {ymd(row[tf]['first'])} ({row[tf]['days'] or 0:.0f} d, old-window n={row[tf]['old_window_n']})"
                    for tf in TF_MS
                )
                + f"  [{time.perf_counter() - t1:.1f}s]",
                flush=True,
            )
        if out:
            await asyncio.to_thread(Path(out).write_text, json.dumps(res, indent=1))
    finally:
        await rt.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,ETH,SOL,DOGE,kPEPE,HYPE")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    asyncio.run(run(a.coins.split(","), a.out))


if __name__ == "__main__":
    main()
