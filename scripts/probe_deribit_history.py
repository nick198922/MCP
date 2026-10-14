"""Step 0 probe for IDEA-20261013-deribit-put-skew-premium: what does history.deribit.com serve?

Checks (all through the project's provider stack, `rt.deribit_history`):
  1. earliest BTC option trades carrying `iv` (bisects on the start timestamp);
  2. trades of long-expired instruments by currency+time and by instrument+time;
  3. `public/get_delivery_prices` on both hosts;
  4. request latency / throughput, to estimate a full download.
Optionally writes a small trimmed sample to tests/fixtures/.

Usage:
    uv run python scripts/probe_deribit_history.py [--fixture tests/fixtures/deribit_history_trades_2026-10-14.json]
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


async def window(rt: Any, start: int, end: int, count: int = 1000) -> dict[str, Any]:
    f = await rt.deribit_history.last_trades_by_currency_and_time("BTC", "option", start, end, count=count)
    return f.data


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="")
    a = ap.parse_args()
    _, rt = build_server()
    try:
        # 1. a 2022 window with iv
        t0 = time.perf_counter()
        d = await window(rt, ms(2022, 6, 15, 8), ms(2022, 6, 15, 10))
        dt = time.perf_counter() - t0
        tr = d["trades"]
        print(f"[1] 2022-06-15 08-10 UTC: {len(tr)} trades, has_more={d['has_more']}, {dt * 1000:.0f} ms")
        if tr:
            x = tr[0]
            print("    keys:", sorted(x))
            print(
                "    first:",
                {k: x.get(k) for k in ("instrument_name", "timestamp", "iv", "price", "mark_price", "index_price", "direction", "amount")},
            )
            print(f"    share with iv: {sum(1 for t in tr if t.get('iv') is not None) / len(tr):.0%}")
        # earliest: probe yearly then bisect by month
        for y in (2016, 2017, 2018, 2019, 2020, 2021):
            d = await window(rt, ms(y, 1, 1), ms(y + 1, 1, 1), count=5)
            tr = d["trades"]
            first = tr[0] if tr else None
            print(
                f"[1] year {y}: {'first ' + iso(first['timestamp']) + ' ' + first['instrument_name'] + ' iv=' + str(first.get('iv')) if first else 'no trades'}"
            )
        # 2. expired instrument by name (a 2022-06-24 expiry)
        for inst in ("BTC-24JUN22-20000-P", "BTC-24JUN22-25000-P", "BTC-30DEC22-15000-P"):
            try:
                f = await rt.deribit_history.last_trades_by_instrument_and_time(inst, ms(2022, 5, 1), ms(2022, 12, 31), count=10)
                tr = f.data["trades"]
                print(
                    f"[2] {inst}: {len(tr)} trades (has_more={f.data['has_more']}); first {iso(tr[0]['timestamp']) if tr else '-'} iv={tr[0].get('iv') if tr else '-'}"
                )
            except ProviderError as e:
                print(f"[2] {inst}: error {e.reason} {e.detail}")
        try:
            f = await rt.deribit.last_trades_by_instrument_and_time("BTC-24JUN22-20000-P", ms(2022, 5, 1), ms(2022, 12, 31), count=10)
            print(f"[2] www host, expired instrument: {len(f.data['trades'])} trades")
        except ProviderError as e:
            print(f"[2] www host, expired instrument: error {e.reason} {e.detail}")
        try:
            d = (await rt.deribit.last_trades_by_currency_and_time("BTC", "option", ms(2022, 6, 15, 8), ms(2022, 6, 15, 10), count=10)).data
            print(f"[2] www host, 2022 currency window: {len(d['trades'])} trades")
        except ProviderError as e:
            print(f"[2] www host, 2022 currency window: error {e.reason} {e.detail}")
        # 3. delivery prices
        for host, prov in (("www", rt.deribit), ("history", rt.deribit_history)):
            try:
                f = await prov.delivery_prices("btc_usd", offset=0, count=3)
                dd = f.data
                print(f"[3] delivery_prices@{host}: total={dd.get('records_total')} newest={dd.get('data', [])[:3]}")
                tot = int(dd.get("records_total") or 0)
                if tot:
                    g = await prov.delivery_prices("btc_usd", offset=max(tot - 2, 0), count=2)
                    print(f"    oldest: {g.data.get('data')}")
            except ProviderError as e:
                print(f"[3] delivery_prices@{host}: error {e.reason} {e.detail}")
        # 4. throughput: 10 sequential pages of one busy day
        start, end = ms(2024, 8, 5), ms(2024, 8, 6)
        n_pages, n_tr, t0 = 0, 0, time.perf_counter()
        cur = start
        seen: set[str] = set()
        while n_pages < 10:
            d = await window(rt, cur, end)
            tr = [t for t in d["trades"] if t["trade_id"] not in seen]
            seen.update(t["trade_id"] for t in d["trades"])
            n_pages += 1
            n_tr += len(tr)
            if not d["has_more"] or not d["trades"]:
                break
            cur = d["trades"][-1]["timestamp"]
        dt = time.perf_counter() - t0
        print(f"[4] 2024-08-05: {n_pages} pages, {n_tr} trades, {dt:.1f}s ({dt / n_pages:.2f}s/page), reached {iso(cur)}")
        if a.fixture:
            d = await window(rt, ms(2022, 6, 15, 8), ms(2022, 6, 15, 10), count=5)
            keep = (
                "trade_id",
                "instrument_name",
                "timestamp",
                "iv",
                "price",
                "mark_price",
                "index_price",
                "direction",
                "amount",
                "trade_seq",
                "tick_direction",
            )
            sample = {
                "_source": "history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time currency=BTC kind=option "
                "start=2022-06-15T08:00Z end=2022-06-15T10:00Z count=5 sorting=asc",
                "_fetched": datetime.now(UTC).strftime("%Y-%m-%d"),
                "jsonrpc": "2.0",
                "result": {"trades": [{k: t.get(k) for k in keep} for t in d["trades"]], "has_more": d["has_more"]},
            }
            await asyncio.to_thread(Path(a.fixture).write_text, json.dumps(sample, indent=1) + "\n")
            print(f"fixture -> {a.fixture}")
    finally:
        await rt.aclose()


if __name__ == "__main__":
    asyncio.run(main())
