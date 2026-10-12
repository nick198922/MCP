"""Walk-forward verification of DoubleTop range/SFP lecture rules on real history.

Usage:
    uv run python scripts/verify_lectures.py --cache-dir /tmp/verify --out report.md

Fetches OHLCV through the runtime's ccxt provider (rate limits apply), caches it as
JSON, then prints a markdown report with one block per lecture claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from tradedesk.analytics.sfp_backtest import (
    STOP_VARIANTS,
    range_time_share,
    summarise_r,
    walk_forward_sfp,
)
from tradedesk.server import build_server

TOP20 = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
         "TON", "DOT", "LTC", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "WLD"]  # fmt: skip
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 4 * 3_600_000, "12h": 12 * 3_600_000, "1d": 86_400_000}


def aggregate(bars: list[dict[str, Any]], period_ms: int) -> list[dict[str, Any]]:
    """Resample to a coarser timeframe aligned to UTC multiples of `period_ms`; drops incomplete buckets."""
    out: list[dict[str, Any]] = []
    buckets: dict[int, list[dict[str, Any]]] = {}
    for b in bars:
        buckets.setdefault(b["ts"] // period_ms * period_ms, []).append(b)
    step = bars[1]["ts"] - bars[0]["ts"] if len(bars) > 1 else period_ms
    need = period_ms // step
    for ts in sorted(buckets):
        g = buckets[ts]
        if len(g) != need:
            continue
        out.append(
            {"ts": ts, "open": g[0]["open"], "high": max(x["high"] for x in g), "low": min(x["low"] for x in g), "close": g[-1]["close"]}
        )
    return out


def _run_symbol(job: tuple[str, list[dict[str, Any]], dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    sym, bars, params = job
    horizon = params.pop("mfe_horizon")
    sigs = walk_forward_sfp(bars, **params)
    for s in sigs:
        s["symbol"] = sym
        s["mfe_pct"] = excursion(bars, s, horizon)
    return sym, sigs


def run_all(pool: ProcessPoolExecutor, data: dict[str, list[dict[str, Any]]], params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    jobs = [(sym, bars, dict(params)) for sym, bars in data.items() if len(bars) >= params["window"] + 50]
    return dict(pool.map(_run_symbol, jobs))


def cache_path(cache: Path, venue: str, symbol: str, tf: str, days: int) -> Path:
    return cache / f"{venue}_{symbol.replace('/', '-').replace(':', '_')}_{tf}_{days}d.json"


async def fetch_history(rt: Any, venue: str, symbol: str, tf: str, days: int, page: int = 1500) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    since = now_ms - days * 86_400_000
    rows: dict[int, list[float]] = {}
    while since < now_ms:
        fetched = await rt.exchanges.ohlcv(venue, symbol, tf, page, since_ms=since)
        batch = [b for b in (fetched.data or []) if isinstance(b, (list, tuple)) and len(b) >= 5]
        if not batch:
            break
        for b in batch:
            rows[int(b[0])] = b
        nxt = int(batch[-1][0]) + TF_MS[tf]
        if nxt <= since:
            break
        since = nxt
    last_closed = now_ms - TF_MS[tf]
    return [
        {"ts": ts, "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4])}
        for ts, b in sorted(rows.items())
        if ts <= last_closed
    ]


async def fetch_missing(venue: str, wanted: list[tuple[str, str]], days: int) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not wanted:
        return out
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


def fmt_block(title: str, s: dict[str, Any]) -> str:
    if not s.get("n"):
        return f"| {title} | 0 | — | — | — | — | — |"
    ci = s.get("ci95")
    ci_txt = f"[{ci[0]:+.2f}; {ci[1]:+.2f}]" if ci else "—"
    pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") else "—"
    flag = " ⚠n<20" if s["small_sample"] else ""
    return f"| {title}{flag} | {s['n']} | {s['win_rate']:.0%} | {s['avg_r']:+.3f} | {ci_txt} | {pf} | {s['timeout_rate']:.0%} |"


HDR = "| Вибірка | n | TP1 hit | Сер. R (net) | 95% CI | PF | Таймаут |\n|---|---|---|---|---|---|---|"


def subset(
    signals: list[dict[str, Any]], pred: Callable[[dict[str, Any]], bool], variant: str = "m2_2x", net: bool = True
) -> dict[str, Any]:
    rows = [s["variants"][variant] for s in signals if variant in s["variants"] and pred(s)]
    return summarise_r([v["r_net" if net else "r"] for v in rows], [v["outcome"] for v in rows])


def excursion(bars: list[dict[str, Any]], sig: dict[str, Any], horizon: int) -> float | None:
    t = sig["idx"]
    fwd = bars[t + 1 : t + 1 + horizon]
    if len(fwd) < horizon:
        return None
    entry = bars[t]["close"]
    if sig["direction"] == "long":
        return (max(b["high"] for b in fwd) - entry) / entry * 100.0
    return (entry - min(b["low"] for b in fwd)) / entry * 100.0


def quartiles(xs: list[float]) -> str:
    if len(xs) < 4:
        return "n<4"
    q = statistics.quantiles(xs, n=4)
    return f"p25 {q[0]:.1f}% · медіана {q[1]:.1f}% · p75 {q[2]:.1f}% (n={len(xs)})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="binanceusdm")
    ap.add_argument("--symbols", default=",".join(TOP20))
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--window", type=int, default=300)
    ap.add_argument("--max-width-pct", type=float, default=15.0)
    ap.add_argument("--max-hold-bars", type=int, default=60)
    ap.add_argument("--fee-pct", type=float, default=0.1, help="Round-trip fee, percent of entry.")
    ap.add_argument("--cache-dir", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--signals-out", default="")
    ap.add_argument("--sensitivity", action="store_true", help="Also run the parameter grid (4h and 12h).")
    a = ap.parse_args()
    cache = Path(a.cache_dir) if a.cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    symbols = [f"{s}/USDT:USDT" for s in a.symbols.split(",")]

    loaded: dict[tuple[str, str], list[dict[str, Any]]] = {}
    wanted: list[tuple[str, str]] = []
    for sym in symbols:
        for tf in ("4h", "1d"):
            p = cache_path(cache, a.venue, sym, tf, a.days) if cache else None
            if p and p.exists():
                loaded[(sym, tf)] = json.loads(p.read_text())
            else:
                wanted.append((sym, tf))
    fresh = asyncio.run(fetch_missing(a.venue, wanted, a.days))
    for (sym, tf), bars in fresh.items():
        loaded[(sym, tf)] = bars
        if cache:
            cache_path(cache, a.venue, sym, tf, a.days).write_text(json.dumps(bars))
    data4h = {s: b for (s, tf), b in loaded.items() if tf == "4h"}
    data1d = {s: b for (s, tf), b in loaded.items() if tf == "1d"}

    data12h = {s: aggregate(b, 12 * 3_600_000) for s, b in data4h.items()}
    base = {
        "window": a.window,
        "max_width_pct": a.max_width_pct,
        "max_hold_bars": a.max_hold_bars,
        "fee_pct": a.fee_pct,
        "sweep_wick_ratio": 0.5,
        "mfe_horizon": 30,
    }
    grid_rows: list[str] = []
    with ProcessPoolExecutor() as pool:
        per_symbol = run_all(pool, data4h, base)
        if a.sensitivity:
            for tf, data, window, hold, horizon in (("4h", data4h, a.window, a.max_hold_bars, 30), ("12h", data12h, 150, 20, 10)):
                for width in (10.0, 15.0, 25.0):
                    for wick in (0.5, 0.65):
                        p = dict(base, window=window, max_hold_bars=hold, max_width_pct=width, sweep_wick_ratio=wick, mfe_horizon=horizon)
                        sigs = [s for v in run_all(pool, data, p).values() for s in v]
                        st = subset(sigs, lambda s: True)
                        mfe = [s["mfe_pct"] for s in sigs if s["mfe_pct"] is not None]
                        ci = st.get("ci95")
                        grid_rows.append(
                            f"| {tf} | {width:g}% | {wick} | {st['n']} | {st.get('win_rate', 0):.0%} | "
                            f"{st.get('avg_r', 0):+.3f} | {f'[{ci[0]:+.2f}; {ci[1]:+.2f}]' if ci else '—'} | "
                            f"{statistics.median(mfe) if mfe else 0:.1f}% |"
                        )
    all_sigs = [s for v in per_symbol.values() for s in v]

    L: list[str] = []
    first = min((b[0]["ts"] for b in data4h.values() if b), default=0)
    last = max((b[-1]["ts"] for b in data4h.values() if b), default=0)
    period = f"{time.strftime('%Y-%m-%d', time.gmtime(first / 1000))} … {time.strftime('%Y-%m-%d', time.gmtime(last / 1000))}"
    L.append(f"# Перевірка правил DoubleTop на історії ({a.venue}, {len(data4h)} тикерів)\n")
    L.append(f"Період: {period}. 4h: {sum(len(b) for b in data4h.values())} барів; 1d: {sum(len(b) for b in data1d.values())} барів.")
    L.append(
        f"Параметри: window={a.window}, max_width_pct={a.max_width_pct}, min_bars=20, swing_lookback=3, wick_ratio=0.5, "
        f"вихід — TP1 (середина рейнджу) / стоп / таймаут {a.max_hold_bars} барів, комісія {a.fee_pct}% round-trip, "
        f"одна позиція на тикер. Бар, що зачепив і стоп, і TP — рахується стопом.\n"
    )

    L.append("## L1-a «70% часу ринок у рейнджі»\n")
    L.append("| TF | max_width_pct | медіана частки барів у рейнджі | min … max по тикерах |\n|---|---|---|---|")
    for tf, data in (("1d", data1d), ("4h", data4h)):
        for w in (5.0, 7.0, 10.0, 15.0, 20.0, 30.0):
            shares = [range_time_share(b, min_bars=20, max_width_pct=w) for b in data.values() if len(b) > 100]
            if shares:
                L.append(f"| {tf} | {w:g}% | {statistics.median(shares):.0%} | {min(shares):.0%} … {max(shares):.0%} |")

    L.append("\n## L3 SFP: базова механіка (стоп 2× пересвипу, TP1 = середина)\n")
    L.append(HDR)
    L.append(fmt_block("усі сигнали, net", subset(all_sigs, lambda s: True)))
    L.append(fmt_block("усі сигнали, gross", subset(all_sigs, lambda s: True, net=False)))
    L.append(fmt_block("long після low-sweep", subset(all_sigs, lambda s: s["direction"] == "long")))
    L.append(fmt_block("short після high-sweep", subset(all_sigs, lambda s: s["direction"] == "short")))
    rr = [s["variants"]["m2_2x"]["rr1"] for s in all_sigs]
    if rr:
        be = statistics.mean(1 / (1 + x) for x in rr)
        L.append(f"\nСередній rr1 = {statistics.mean(rr):.2f}; беззбитковий TP1-hit при такому RR (випадкове блукання, gross) ≈ {be:.0%}.")
    both = sum(1 for s in all_sigs if s["variants"]["m2_2x"]["both_in_bar"])
    L.append(f"Барів, що зачепили і стоп, і TP (пораховано як стоп): {both}.")

    L.append("\n## L3-M2 розмір стопу\n")
    L.append(HDR)
    for v in STOP_VARIANTS:
        L.append(fmt_block(v, subset(all_sigs, lambda s: True, variant=v)))

    L.append("\n## L3-M4 фільтр RR ≥ 2\n")
    L.append(HDR)
    L.append(fmt_block("rr1 ≥ 2", subset(all_sigs, lambda s: s["variants"]["m2_2x"]["rr1"] >= 2)))
    L.append(fmt_block("rr1 < 2", subset(all_sigs, lambda s: s["variants"]["m2_2x"]["rr1"] < 2)))

    L.append("\n## L3-M7 повторні свипи з того ж боку\n")
    L.append(HDR)
    L.append(fmt_block("same-side ≥ 3 з 5 (2×)", subset(all_sigs, lambda s: s["same_side_in_last_5"] >= 3)))
    L.append(fmt_block("same-side < 3 (2×)", subset(all_sigs, lambda s: s["same_side_in_last_5"] < 3)))
    L.append(fmt_block("same-side ≥ 3, tight-стоп", subset(all_sigs, lambda s: s["same_side_in_last_5"] >= 3, variant="tight")))
    L.append(fmt_block("same-side < 3, tight-стоп", subset(all_sigs, lambda s: s["same_side_in_last_5"] < 3, variant="tight")))

    L.append("\n## L2 класифікація accumulation / distribution і контекст SMA\n")
    L.append(HDR)
    for d in ("long", "short"):
        for c in ("accumulation", "distribution", "ambiguous"):
            L.append(fmt_block(f"{d} у {c}", subset(all_sigs, lambda s, d=d, c=c: s["direction"] == d and s["classification"] == c)))
    for d in ("long", "short"):
        for c in ("accumulation", "distribution"):
            L.append(
                fmt_block(
                    f"{d}, SMA100{'>' if c == 'accumulation' else '<'}SMA200",
                    subset(all_sigs, lambda s, d=d, c=c: s["direction"] == d and s["context"] == c),
                )
            )

    L.append("\n## L1-b «реакція від краю рейнджу 10–20%»\n")
    L.append("Максимальний рух у бік угоди за 30 барів 4h (5 днів) від close свічки-свипу, без урахування стопу:\n")
    L.append(
        f"- long після low-sweep: {quartiles([s['mfe_pct'] for s in all_sigs if s['direction'] == 'long' and s['mfe_pct'] is not None])}"
    )
    L.append(
        f"- short після high-sweep: {quartiles([s['mfe_pct'] for s in all_sigs if s['direction'] == 'short' and s['mfe_pct'] is not None])}"
    )

    L.append("\n## Стабільність: по роках і по тикерах (2×, net)\n")
    L.append(HDR)
    if all_sigs:
        mid_ts = (first + last) / 2
        L.append(fmt_block("перша половина періоду", subset(all_sigs, lambda s: s["ts"] < mid_ts)))
        L.append(fmt_block("друга половина періоду", subset(all_sigs, lambda s: s["ts"] >= mid_ts)))
    for sym, sigs in sorted(per_symbol.items()):
        L.append(fmt_block(sym.split("/")[0], subset(sigs, lambda s: True)))

    if grid_rows:
        L.append("\n## Чутливість до параметрів детектора (стоп 2×, net)\n")
        L.append("12h зібрано з 4h-барів; window 150, таймаут 20 барів (10 днів), MFE за 10 барів (5 днів).\n")
        L.append("| TF | max_width | wick_ratio | n | TP1 hit | Сер. R | 95% CI | медіана MFE 5д |\n|---|---|---|---|---|---|---|---|")
        L.extend(grid_rows)

    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")
    if a.signals_out:
        Path(a.signals_out).write_text(json.dumps(all_sigs))


if __name__ == "__main__":
    main()
