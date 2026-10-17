"""Dated snapshot of Bybit's public risk-limit ladder for the paper carry criteria (g) and (ye).

Public endpoint, no keys: GET /v5/market/risk-limit?category=linear (through ccxt's implicit
``publicGetV5MarketRiskLimit`` via ``rt.exchanges.public_get``). Per USDT perp it keeps the raw tiers and a summary
for a $2000 leg at 2x (``analytics.carry_bench.ladder_summary``): MMR of the tier that covers the notional, the largest
position that still allows 2x, and whether the leg fits. Binance's ladder (/fapi/v1/leverageBracket) is signed, so it
is not taken (see the card's registration amendment, item 2).

Usage:
    uv run python scripts/paper_ladder_snapshot.py --out docs/paper/ladder/2026-10-17-bybit.json \
        --raw-out ~/.cache/tradedesk-agents/paper_carry_bench/ladder/2026-10-17-bybit-raw.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from tradedesk.analytics.carry_bench import ladder_summary
from tradedesk.server import build_server

NOTIONAL_USD = 2000.0
LEVERAGE = 2.0


async def fetch() -> list[dict[str, Any]]:
    _, rt = build_server()
    rows: list[dict[str, Any]] = []
    try:
        cursor = ""
        for page in range(200):
            params: dict[str, Any] = {"category": "linear", "limit": 1000}  # Bybit caps the page; the cursor walks on
            if cursor:
                params["cursor"] = cursor
            got = (await rt.exchanges.public_get("bybit", "publicGetV5MarketRiskLimit", params)).data or {}
            res = got.get("result") or {}
            batch = res.get("list") or []
            rows += batch
            cursor = res.get("nextPageCursor") or ""
            print(f"  page {page}: {len(batch)} tiers, total {len(rows)}", flush=True)
            if not cursor or not batch:
                break
            await asyncio.sleep(0.2)
    finally:
        await rt.aclose()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="compact summary per symbol (goes to git)")
    ap.add_argument("--raw-out", default="", help="optional: all tiers per symbol (large, keep outside the repo)")
    a = ap.parse_args()
    rows = asyncio.run(fetch())
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sym = str(r.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        by.setdefault(sym, []).append(
            {"limit_usd": float(r["riskLimitValue"]), "mmr": float(r["maintenanceMargin"]), "max_leverage": float(r["maxLeverage"])}
        )
    out = {
        "source": "Bybit GET /v5/market/risk-limit?category=linear (public)",
        "taken_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "leg_notional_usd": NOTIONAL_USD,
        "leverage": LEVERAGE,
        "symbols": {s: ladder_summary(t, notional_usd=NOTIONAL_USD, leverage=LEVERAGE) for s, t in sorted(by.items())},
    }
    if a.raw_out:
        raw = {**out, "symbols": {s: sorted(t, key=lambda x: x["limit_usd"]) for s, t in sorted(by.items())}}
        Path(a.raw_out).expanduser().parent.mkdir(parents=True, exist_ok=True)
        Path(a.raw_out).expanduser().write_text(json.dumps(raw, separators=(",", ":")) + "\n")
    syms = out["symbols"]
    fits = sum(1 for v in syms.values() if v["fits"])
    mmrs = sorted(v["mmr"] for v in syms.values() if v["mmr"] is not None)
    print(
        f"symbols {len(syms)}; $2000 leg fits at 2x: {fits}; tier MMR min {mmrs[0]:.4f} median {mmrs[len(mmrs) // 2]:.4f} max {mmrs[-1]:.4f}"
    )
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=0, separators=(",", ":")) + "\n")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
