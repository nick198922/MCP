"""Check whether Bybit lists daily BTC/ETH options reachable through ccxt (unknown (a) of
docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-forward.md).

Read-only: one public GET per base coin (`/v5/market/tickers?category=option`) through the runtime's ccxt layer
(``Exchanges.public_get`` -> ``Exchanges._call``), so rate limits, health and evidence apply. No keys, no orders.

Usage:
    uv run python scripts/probe_bybit_options.py [--bases BTC,ETH] [--out probe.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.option_xvenue import parse_bybit_symbol
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server


async def run(bases: list[str], out: str) -> None:
    _, rt = build_server()
    now = datetime.now(UTC)
    res: dict[str, Any] = {"now": now.isoformat(), "bases": {}}
    try:
        for base in bases:
            try:
                f = await rt.exchanges.public_get("bybit", "publicGetV5MarketTickers", {"category": "option", "baseCoin": base})
            except ProviderError as e:
                print(f"{base}: FAILED {e.reason}: {e.detail}", flush=True)
                res["bases"][base] = {"error": f"{e.reason}: {e.detail}"}
                continue
            rows = ((f.data or {}).get("result") or {}).get("list") or []
            expiries: Counter = Counter()
            suffixes: Counter = Counter()
            for r in rows:
                p = parse_bybit_symbol(str(r.get("symbol", "")))
                if p is None:
                    suffixes["unparsed"] += 1
                    continue
                expiries[p["expiry"].isoformat()] += 1
                suffixes[p["settle"] or "-"] += 1
            nearest = sorted(expiries)[:4]
            hours = [(datetime.fromisoformat(e) - now).total_seconds() / 3600 for e in nearest]
            print(
                f"{base}: {len(rows)} option tickers; settle suffix {dict(suffixes)}; nearest expiries "
                + ", ".join(f"{e} ({h:.1f} h, {expiries[e]} instr.)" for e, h in zip(nearest, hours, strict=True)),
                flush=True,
            )
            res["bases"][base] = {
                "n": len(rows),
                "settle": dict(suffixes),
                "expiries": dict(sorted(expiries.items())),
                "sample": rows[:2],
            }
        if out:
            await asyncio.to_thread(Path(out).write_text, json.dumps(res, indent=1, default=str))
    finally:
        await rt.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="BTC,ETH")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    asyncio.run(run(a.bases.split(","), a.out))


if __name__ == "__main__":
    main()
