"""Save OUR provider responses for step 1b of the DoubleTop parity check
(docs/reviews/2026-10-18-doubletop-parity-1b.md): ETH, SOL (alt) and Bybit branches, plus Binance funding.

Network only through the runtime providers. Run on a machine that can reach the venues:

    uv run python scripts/doubletop_parity_1b.py --out tests/fixtures/doubletop
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.server import build_server

BASES = ("BTC", "ETH", "SOL")


def save(out: Path, name: str, source: str, data: Any) -> None:
    day = datetime.now(UTC).date().isoformat()
    doc = {"_source": f"OURS {source}, taken {day}; compact live response", "data": data}
    path = out / f"ours__{name}_{day}.json"
    path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    print("wrote", path)


async def main(out: Path) -> None:
    _, rt = build_server()
    try:
        now_ms = int(rt.now().timestamp() * 1000)
        since = now_ms - 6 * 86_400_000
        for base in BASES:
            sym = f"{base}USDT"
            if base != "BTC":
                f = await rt.exchanges.ohlcv("binance", f"{base}/USDT", "1d", 5)
                save(out, f"market_ohlcv_binance_{sym}_1d", f"exchanges.ohlcv binance {base}/USDT 1d limit=5", f.data)
                f = await rt.binance_futures.open_interest_hist(sym, "1d", 5)
                save(out, f"binance_openInterestHist_{sym}_1d", f"binance_futures.open_interest_hist {sym} 1d limit=5", f.data)
                f = await rt.binance_futures.long_short_account_ratio(sym, "1d", 5)
                save(out, f"binance_globalLongShortAccountRatio_{sym}_1d", f"binance_futures.long_short_account_ratio {sym} 1d", f.data)
            f = await rt.exchanges.funding_rate_history("binanceusdm", f"{base}/USDT:USDT", since, 30)
            save(out, f"binanceusdm_funding_history_{sym}", f"exchanges.funding_rate_history binanceusdm since={since}", f.data)
            f = await rt.bybit.open_interest(sym, "1d", 5)
            save(out, f"bybit_open_interest_{sym}_1d", f"bybit.open_interest {sym} 1d limit=5", f.data)
            f = await rt.bybit.long_short_account_ratio(sym, "1d", 5)
            save(out, f"bybit_account_ratio_{sym}_1d", f"bybit.long_short_account_ratio {sym} 1d limit=5", f.data)
    finally:
        await rt.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args.out))
