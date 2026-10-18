"""Save OUR provider responses for the DoubleTop parity check (docs/reviews/2026-10-18-doubletop-parity.md).

Same inputs as the DoubleTop samples in tests/fixtures/doubletop/ (BTC, Binance, 1d / HL hourly window of the sample).
Network only through the runtime providers. Run on a machine that can reach the venues:

    uv run python scripts/doubletop_parity.py --out tests/fixtures/doubletop
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.server import build_server

HL_START, HL_END = 1760659200000, 1760745600000  # window of the DoubleTop hyperliquid_info sample


def save(out: Path, name: str, source: str, data: Any) -> None:
    day = datetime.now(UTC).date().isoformat()
    doc = {"_source": f"OURS {source}, taken {day}; compact live response", "data": data}
    path = out / f"ours__{name}_{day}.json"
    path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print("wrote", path)


async def main(out: Path) -> None:
    _, rt = build_server()
    try:
        f = await rt.exchanges.ohlcv("binance", "BTC/USDT", "1d", 5)
        save(out, "market_ohlcv_binance_BTCUSDT_1d", "exchanges.ohlcv binance BTC/USDT 1d limit=5", f.data)
        f = await rt.binance_futures.open_interest_hist("BTCUSDT", "1d", 5)
        save(out, "binance_openInterestHist_BTCUSDT_1d", "binance_futures.open_interest_hist BTCUSDT 1d limit=5", f.data)
        f = await rt.binance_futures.long_short_account_ratio("BTCUSDT", "1d", 5)
        save(out, "binance_globalLongShortAccountRatio_BTCUSDT_1d", "binance_futures.long_short_account_ratio 1d", f.data)
        f = await rt.hyperliquid.funding_history("BTC", HL_START, HL_END)
        save(out, "hyperliquid_funding_history_BTC", f"hyperliquid.funding_history BTC {HL_START}..{HL_END}", f.data)
    finally:
        await rt.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args.out))
