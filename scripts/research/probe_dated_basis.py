"""Step 0 probe for IDEA-20261013-dated-basis-vs-funding-forward: is there history of expired dated futures?

Checks (all through the project's provider stack):
  (a) Deribit ``get_tradingview_chart_data`` for an expired quarterly (BTC-30DEC22, BTC-25DEC20) on www and history,
      plus ``get_last_trades_by_instrument_and_time`` on history (known to work for expired options);
  (b) Deribit ``get_funding_rate_history`` for BTC-PERPETUAL: earliest month with records;
  (c) Binance ``continuousKlines`` CURRENT_QUARTER / NEXT_QUARTER (USDT-M pair BTCUSDT, COIN-M pair BTCUSD) and
      ``indexPriceKlines``: earliest bar;
  (d) klines of expired COIN-M ``BTCUSD_YYMMDD`` and USDT-M ``BTCUSDT_YYMMDD`` contracts; funding history depth
      of BTCUSD_PERP / BTCUSDT.
With ``--fixtures DIR`` writes small trimmed dated samples there.

Usage:
    uv run python scripts/research/probe_dated_basis.py [--fixtures tests/fixtures]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server


def ms(y: int, m: int, d: int, h: int = 0) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp() * 1000)


def iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d %H:%M")


async def tv(prov: Any, inst: str, a: int, b: int, res: str = "1D") -> dict[str, Any] | str:
    try:
        return (await prov.tradingview_chart_data(inst, a, b, res)).data
    except ProviderError as e:
        return f"error {e.reason} {e.detail}"


def tv_line(d: dict[str, Any] | str) -> str:
    if isinstance(d, str):
        return d
    ticks = d.get("ticks") or []
    vol = d.get("volume") or []
    if not ticks:
        return f"status={d.get('status')} 0 bars"
    nz = sum(1 for v in vol if v)
    return (
        f"status={d.get('status')} {len(ticks)} bars {iso(ticks[0])} .. {iso(ticks[-1])}, bars with volume {nz}, "
        f"close first/last {d['close'][0]} / {d['close'][-1]}"
    )


async def bn(rt: Any, endpoint: str, params: dict[str, Any]) -> list[Any] | str:
    try:
        return (await rt.exchanges.public_get("binance", endpoint, params)).data
    except ProviderError as e:
        return f"error {e.reason} {str(e.detail)[:160]}"


def k_line(d: list[Any] | str) -> str:
    if isinstance(d, str):
        return d
    if not d:
        return "0 bars"
    return f"{len(d)} bars {iso(int(d[0][0]))} .. {iso(int(d[-1][0]))}, close first/last {d[0][4]} / {d[-1][4]}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="")
    a = ap.parse_args()
    _, rt = build_server()
    fx: dict[str, Any] = {}
    t_start = time.perf_counter()
    try:
        # (a) Deribit expired quarterlies
        for inst, s, e in (
            ("BTC-30DEC22", ms(2022, 9, 1), ms(2022, 12, 31)),
            ("BTC-25DEC20", ms(2020, 9, 1), ms(2020, 12, 26)),
            ("BTC-28JUN19", ms(2019, 3, 1), ms(2019, 6, 29)),
            ("ETH-30DEC22", ms(2022, 9, 1), ms(2022, 12, 31)),
        ):
            for host, prov in (("www", rt.deribit), ("history", rt.deribit_history)):
                d = await tv(prov, inst, s, e)
                print(f"[a] tradingview {inst}@{host}: {tv_line(d)}", flush=True)
                if host == "www" and inst == "BTC-30DEC22" and isinstance(d, dict) and d.get("ticks"):
                    fx["deribit_tv"] = {k: (v[-5:] if isinstance(v, list) else v) for k, v in d.items()}
            try:
                f = await rt.deribit_history.last_trades_by_instrument_and_time(inst, e - 86_400_000, e, count=5, sorting="desc")
                tr = f.data["trades"]
                print(
                    f"[a] history trades {inst} last day: {len(tr)} "
                    + (f"last {iso(tr[0]['timestamp'])} price={tr[0]['price']} index={tr[0].get('index_price')}" if tr else ""),
                    flush=True,
                )
            except ProviderError as ex:
                print(f"[a] history trades {inst}: error {ex.reason} {ex.detail}", flush=True)
        # (b) Deribit perp funding depth
        for y, m in ((2018, 9), (2019, 1), (2019, 5), (2019, 9), (2020, 1), (2020, 6)):
            try:
                f = await rt.deribit.funding_rate_history("BTC-PERPETUAL", ms(y, m, 1), ms(y, m, 3))
                rows = f.data or []
                print(f"[b] deribit BTC-PERPETUAL funding {y}-{m:02d}-01..03: {len(rows)} rows" + (f", first {rows[0]}" if rows else ""))
            except ProviderError as ex:
                print(f"[b] deribit funding {y}-{m:02d}: error {ex.reason} {ex.detail}")
        # (c) Binance continuous and index klines: earliest bar
        for ep, pair in (("fapiPublicGetContinuousKlines", "BTCUSDT"), ("dapiPublicGetContinuousKlines", "BTCUSD")):
            for ct in ("CURRENT_QUARTER", "NEXT_QUARTER", "PERPETUAL"):
                d = await bn(rt, ep, {"pair": pair, "contractType": ct, "interval": "1d", "startTime": ms(2017, 1, 1), "limit": 1500})
                print(f"[c] {ep} {pair} {ct}: {k_line(d)}", flush=True)
                if ep.startswith("dapi") and ct == "CURRENT_QUARTER" and isinstance(d, list) and d:
                    fx["binance_cont"] = d[:3]
        for ep, pair in (("fapiPublicGetIndexPriceKlines", "BTCUSDT"), ("dapiPublicGetIndexPriceKlines", "BTCUSD")):
            d = await bn(rt, ep, {"pair": pair, "interval": "1d", "startTime": ms(2017, 1, 1), "limit": 1500})
            print(f"[c] {ep} {pair}: {k_line(d)}", flush=True)
        # (d) expired delivery contracts
        for ep, sym, s in (
            ("dapiPublicGetKlines", "BTCUSD_201225", ms(2020, 6, 1)),
            ("dapiPublicGetKlines", "BTCUSD_221230", ms(2022, 6, 1)),
            ("dapiPublicGetKlines", "BTCUSD_240927", ms(2024, 3, 1)),
            ("fapiPublicGetKlines", "BTCUSDT_221230", ms(2022, 6, 1)),
            ("fapiPublicGetKlines", "BTCUSDT_240927", ms(2024, 3, 1)),
        ):
            d = await bn(rt, ep, {"symbol": sym, "interval": "1d", "startTime": s, "limit": 400})
            print(f"[d] {ep} {sym}: {k_line(d)}", flush=True)
            if sym == "BTCUSD_221230" and isinstance(d, list) and d:
                fx["binance_dapi_expired"] = d[-3:]
        for ep, sym in (("dapiPublicGetFundingRate", "BTCUSD_PERP"), ("fapiPublicGetFundingRate", "BTCUSDT")):
            d = await bn(rt, ep, {"symbol": sym, "startTime": ms(2017, 1, 1), "limit": 3})
            print(f"[d] {ep} {sym} earliest: {d if isinstance(d, str) else d[:2]}", flush=True)
        print(f"probe done in {time.perf_counter() - t_start:.1f}s")
        if a.fixtures:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            out = Path(a.fixtures)
            if "deribit_tv" in fx:
                blob = {
                    "_source": "www.deribit.com/api/v2/public/get_tradingview_chart_data instrument_name=BTC-30DEC22 "
                    "start=2022-09-01 end=2022-12-31 resolution=1D (last 5 bars kept)",
                    "_fetched": today,
                    "jsonrpc": "2.0",
                    "result": fx["deribit_tv"],
                }
                (out / f"deribit_tradingview_expired_{today}.json").write_text(json.dumps(blob, indent=1) + "\n")
            bin_blob = {
                "_source": "binance dapi continuousKlines pair=BTCUSD contractType=CURRENT_QUARTER interval=1d (first 3) and "
                "dapi klines symbol=BTCUSD_221230 interval=1d (last 3), via ccxt implicit endpoints",
                "_fetched": today,
                "continuous_current_quarter": fx.get("binance_cont"),
                "expired_delivery_klines": fx.get("binance_dapi_expired"),
            }
            (out / f"binance_delivery_klines_{today}.json").write_text(json.dumps(bin_blob, indent=1) + "\n")
            print(f"fixtures -> {out}")
    finally:
        await rt.aclose()


if __name__ == "__main__":
    asyncio.run(main())
