"""Out-of-sample check of the SFP hypothesis picked after the first verification run.

Pre-registered before running (docs/verification/2026-10-12-doubletop-ranges-sfp.md):
  12h, max_width_pct=15, wick_ratio=0.5, window=150, hold 20 bars, stop 2x overshoot,
  TP1 = range mid, fee 0.1 % round-trip  ->  mean net R > 0.
Verdict rule: confirmed if the out-of-sample mean > 0 AND the 95 % CI lower bound > 0;
directional if mean > 0 but the CI includes 0; rejected if mean <= 0.

In-sample = the original 20 symbols after --split-date. Out-of-sample = the same symbols
before it, plus --new-count fresh symbols (top by current quote volume) over the whole span.

Usage:
    uv run python scripts/verify_sfp_oos.py --cache-dir <dir> --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from verify_lectures import HDR, TOP20, cache_path, fetch_history, fmt_block, run_all, subset

from tradedesk.analytics.sfp_backtest import STOP_VARIANTS
from tradedesk.server import build_server

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "USD1", "RLUSD", "EUR", "XUSD"}


async def top_by_volume(venue: str, exclude: set[str], count: int, listed_before_ms: int) -> list[str]:
    """Top crypto perps by current quote volume, listed before `listed_before_ms`.

    Binance also lists TradFi perps (gold, oil, equities, leveraged ETFs); the lecture is about
    crypto, so only `underlyingType=COIN`, `contractType=PERPETUAL` markets qualify.
    """
    _, rt = build_server()
    try:
        markets = (await rt.exchanges.markets(venue)).data or {}
        tickers = (await rt.exchanges.tickers(venue)).data or {}
    finally:
        await rt.aclose()
    rows = []
    for sym, t in tickers.items():
        m = markets.get(sym) or {}
        info = m.get("info") or {}
        if not sym.endswith("/USDT:USDT") or info.get("contractType") != "PERPETUAL" or info.get("underlyingType") != "COIN":
            continue
        if int(info.get("onboardDate") or 0) > listed_before_ms:
            continue
        base = sym.split("/")[0]
        if base in exclude or base in STABLE_BASES:
            continue
        rows.append((float(t.get("quoteVolume") or 0.0), base))
    rows.sort(reverse=True)
    return [b for _, b in rows[:count]]


async def fetch_all(venue: str, wanted: list[tuple[str, str]], days: int) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    _, rt = build_server()
    try:
        for sym, tf in wanted:
            try:
                out[(sym, tf)] = await fetch_history(rt, venue, sym, tf, days)
            except Exception as e:  # noqa: BLE001
                print(f"skip {sym} {tf}: {type(e).__name__}: {e}")
    finally:
        await rt.aclose()
    return out


def load(venue: str, symbols: list[str], tfs: list[str], days: int, cache: Path | None) -> dict[str, dict[str, list[dict[str, Any]]]]:
    loaded: dict[tuple[str, str], list[dict[str, Any]]] = {}
    wanted = []
    for sym in symbols:
        for tf in tfs:
            p = cache_path(cache, venue, sym, tf, days) if cache else None
            if p and p.exists():
                loaded[(sym, tf)] = json.loads(p.read_text())
            else:
                wanted.append((sym, tf))
    for key, bars in asyncio.run(fetch_all(venue, wanted, days)).items() if wanted else []:
        loaded[key] = bars
        if cache:
            cache_path(cache, venue, key[0], key[1], days).write_text(json.dumps(bars))
    return {tf: {s: b for (s, t), b in loaded.items() if t == tf and b} for tf in tfs}


def cells(sigs: list[dict[str, Any]], in_syms: set[str], split_ms: int, variant: str = "m2_2x") -> list[str]:
    def is_in(s: dict[str, Any]) -> bool:
        return s["symbol"] in in_syms and s["ts"] >= split_ms

    return [
        fmt_block("in-sample: старі 20, після split", subset(sigs, is_in, variant)),
        fmt_block("OOS час: старі 20, до split", subset(sigs, lambda s: s["symbol"] in in_syms and s["ts"] < split_ms, variant)),
        fmt_block("OOS тикери: нові, після split", subset(sigs, lambda s: s["symbol"] not in in_syms and s["ts"] >= split_ms, variant)),
        fmt_block("OOS обидва: нові, до split", subset(sigs, lambda s: s["symbol"] not in in_syms and s["ts"] < split_ms, variant)),
        fmt_block("**OOS разом**", subset(sigs, lambda s: not is_in(s), variant)),
    ]


def verdict(st: dict[str, Any]) -> str:
    if not st.get("n"):
        return "немає даних"
    lo = st["ci95"][0] if st.get("ci95") else float("-inf")
    if st["avg_r"] > 0 and lo > 0:
        return "✓ ПІДТВЕРДЖЕНО"
    if st["avg_r"] > 0:
        return "≈ напрям, незначуще"
    return "✗ НЕ ПІДТВЕРДЖЕНО"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="binanceusdm")
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--split-date", default="2024-10-12")
    ap.add_argument("--new-count", type=int, default=30)
    ap.add_argument("--fee-pct", type=float, default=0.1)
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir) if a.cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    split_ms = calendar.timegm(time.strptime(a.split_date, "%Y-%m-%d")) * 1000

    new_bases = asyncio.run(top_by_volume(a.venue, set(TOP20), a.new_count, split_ms))
    in_syms = {f"{b}/USDT:USDT" for b in TOP20}
    symbols = sorted(in_syms) + [f"{b}/USDT:USDT" for b in new_bases]
    data = load(a.venue, symbols, ["12h", "4h"], a.days, cache)

    primary = {"window": 150, "max_width_pct": 15.0, "max_hold_bars": 20, "fee_pct": a.fee_pct, "sweep_wick_ratio": 0.5, "mfe_horizon": 10}
    with ProcessPoolExecutor() as pool:
        p12 = [s for v in run_all(pool, data["12h"], primary).values() for s in v]
        side = {w: [s for v in run_all(pool, data["12h"], dict(primary, max_width_pct=w)).values() for s in v] for w in (10.0, 25.0)}
        p4 = [s for v in run_all(pool, data["4h"], dict(primary, window=300, max_hold_bars=60, mfe_horizon=30)).values() for s in v]

    first = min(b[0]["ts"] for b in data["12h"].values())
    last = max(b[-1]["ts"] for b in data["12h"].values())
    fmt = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    oos_primary = subset(p12, lambda s: not (s["symbol"] in in_syms and s["ts"] >= split_ms))

    L = [f"# SFP 12h: out-of-sample перевірка ({a.venue})\n"]
    L.append(
        f"Період {fmt(first)} … {fmt(last)}, split {a.split_date}. Тикерів з даними: {len(data['12h'])} (12h), {len(data['4h'])} (4h)."
    )
    L.append(f"Нові тикери (лише крипто-перпи з лістингом до split, топ за поточним обсягом, без старих 20): {', '.join(new_bases)}.\n")
    L.append("## Основна гіпотеза: 12h, ширина 15%, фітиль 0.5, стоп 2×\n")
    L.append(HDR)
    L.extend(cells(p12, in_syms, split_ms))
    L.append(f"\n**Вердикт за заздалегідь заданим правилом: {verdict(oos_primary)}.**\n")
    L.append("## Вторинні: інша ширина на 12h і 4h (OOS разом)\n")
    L.append(HDR)
    for w, sigs in side.items():
        L.append(fmt_block(f"12h ширина {w:g}%", subset(sigs, lambda s: not (s["symbol"] in in_syms and s["ts"] >= split_ms))))
    L.append(fmt_block("4h ширина 15%", subset(p4, lambda s: not (s["symbol"] in in_syms and s["ts"] >= split_ms))))
    L.append("\n## Правила лекцій на всій вибірці 12h (50 тикерів × 4 роки, основні параметри)\n")
    L.append(HDR)
    for v in STOP_VARIANTS:
        L.append(fmt_block(f"стоп {v}", subset(p12, lambda s: True, v)))
    L.append(fmt_block("same-side ≥ 3 з 5", subset(p12, lambda s: s["same_side_in_last_5"] >= 3)))
    L.append(fmt_block("same-side < 3", subset(p12, lambda s: s["same_side_in_last_5"] < 3)))
    L.append(fmt_block("rr1 ≥ 2", subset(p12, lambda s: s["variants"]["m2_2x"]["rr1"] >= 2)))
    L.append(fmt_block("rr1 < 2", subset(p12, lambda s: s["variants"]["m2_2x"]["rr1"] < 2)))
    for d in ("long", "short"):
        for c in ("accumulation", "distribution", "ambiguous"):
            L.append(fmt_block(f"{d} у {c}", subset(p12, lambda s, d=d, c=c: s["direction"] == d and s["classification"] == c)))
    L.append("\n## По роках (12h, основні параметри)\n")
    L.append(HDR)
    for y in range(int(fmt(first)[:4]), int(fmt(last)[:4]) + 1):
        lo = calendar.timegm((y, 1, 1, 0, 0, 0)) * 1000
        hi = calendar.timegm((y + 1, 1, 1, 0, 0, 0)) * 1000
        L.append(fmt_block(str(y), subset(p12, lambda s, lo=lo, hi=hi: lo <= s["ts"] < hi)))

    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
