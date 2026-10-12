"""Does funding carry pay after fees? Cash-and-carry backtest on Binance (long spot + short perp).

Pre-registered primary rule (fixed before running): trailing 7d funding APR >= 10 % to enter,
<= 0 % to exit, taker fees (spot 0.10 % + perp 0.05 % per side). Everything else is reported
as sensitivity. Funding income = sum of actual payments; basis P&L from 4h closes at funding
timestamps.

Usage:
    uv run python scripts/verify_funding_carry.py --cache-dir <dir> --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import statistics
import time
from pathlib import Path
from typing import Any

from verify_lectures import TF_MS, TOP20, cache_path, fetch_history

from tradedesk.analytics.carry import funding_persistence, pearson, simulate_cash_and_carry
from tradedesk.server import build_server

PERP_VENUE, SPOT_VENUE = "binanceusdm", "binance"
BUCKETS = [(-1e9, 0.0), (0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50), (0.50, 1e9)]


def funding_path(cache: Path, sym: str, days: int) -> Path:
    return cache / f"{PERP_VENUE}_{sym.replace('/', '-').replace(':', '_')}_funding_{days}d.json"


async def fetch_funding(rt: Any, sym: str, days: int) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    since = now_ms - days * 86_400_000
    rows: dict[int, float] = {}
    while since < now_ms:
        fetched = await rt.exchanges.funding_rate_history(PERP_VENUE, sym, since, 1000)
        batch = [x for x in (fetched.data or []) if x.get("timestamp") is not None and x.get("fundingRate") is not None]
        if not batch:
            break
        for x in batch:
            rows[int(x["timestamp"])] = float(x["fundingRate"])
        nxt = int(batch[-1]["timestamp"]) + 1
        if nxt <= since or len(batch) < 1000:
            break
        since = nxt
    return [{"ts_ms": t, "rate": r} for t, r in sorted(rows.items())]


async def fetch_all(syms: list[str], days: int, cache: Path) -> None:
    _, rt = build_server()
    try:
        for sym in syms:
            base = sym.split("/")[0]
            jobs = [
                (funding_path(cache, sym, days), lambda s=sym: fetch_funding(rt, s, days)),
                (cache_path(cache, PERP_VENUE, sym, "4h", days), lambda s=sym: fetch_history(rt, PERP_VENUE, s, "4h", days)),
                (
                    cache_path(cache, SPOT_VENUE, f"{base}/USDT", "4h", days),
                    lambda b=base: fetch_history(rt, SPOT_VENUE, f"{b}/USDT", "4h", days, page=1000),
                ),
            ]
            for path, go in jobs:
                if path.exists():
                    continue
                try:
                    data = await go()
                except Exception as e:  # noqa: BLE001
                    print(f"skip {path.name}: {type(e).__name__}: {e}")
                    continue
                await asyncio.to_thread(path.write_text, json.dumps(data))
    finally:
        await rt.aclose()


def align(funding: list[dict[str, Any]], spot: list[dict[str, Any]], perp: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the last closed 4h close (spot and perp) at or before each funding timestamp."""
    step = TF_MS["4h"]

    def index(bars: list[dict[str, Any]]) -> tuple[list[int], list[float]]:
        return [b["ts"] + step for b in bars], [b["close"] for b in bars]

    s_t, s_c = index(spot)
    p_t, p_c = index(perp)
    out = []
    for f in funding:
        t = f["ts_ms"] + 60_000
        i, j = bisect.bisect_right(s_t, t) - 1, bisect.bisect_right(p_t, t) - 1
        if i < 0 or j < 0 or f["ts_ms"] - s_t[i] > step or f["ts_ms"] - p_t[j] > step:
            continue
        out.append({"ts_ms": f["ts_ms"], "rate": f["rate"], "spot": s_c[i], "perp": p_c[j]})
    return out


def pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{digits}f}%"


def strategy_rows(data: dict[str, list[dict[str, Any]]], label: str, **kw: Any) -> tuple[str, list[dict[str, Any]]]:
    res = {s: simulate_cash_and_carry(ev, **kw)["stats"] for s, ev in data.items()}
    st = [r for r in res.values() if r]
    aprs = [r["apr"] for r in st]
    row = (
        f"| {label} | {len(st)} | {pct(statistics.mean(aprs))} | {pct(statistics.median(aprs))} | "
        f"{sum(1 for a in aprs if a > 0)}/{len(aprs)} | {pct(statistics.mean(r['funding'] / r['years'] for r in st))} | "
        f"{pct(-statistics.mean(r['fees'] / r['years'] for r in st))} | {pct(statistics.mean(r['basis'] / r['years'] for r in st))} | "
        f"{statistics.mean(r['time_in_market'] for r in st):.0%} | {statistics.mean(r['n_trades'] / r['years'] for r in st):.1f} | "
        f"{pct(min(aprs))} | {statistics.median(r['max_drawdown'] for r in st) * 100:.1f}% / {max(r['max_drawdown'] for r in st) * 100:.1f}% |"
    )
    return row, [dict(r, symbol=s) for s, r in res.items() if r]


STRAT_HDR = (
    "| Правило | тикерів | APR сер. | APR медіана | > 0 | funding / рік | комісії / рік | basis / рік | у ринку | угод / рік | гірший тикер | DD медіана / макс |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--symbols", default=",".join(TOP20))
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    syms = [f"{b}/USDT:USDT" for b in a.symbols.split(",")]
    asyncio.run(fetch_all(syms, a.days, cache))

    data: dict[str, list[dict[str, Any]]] = {}
    for sym in syms:
        base = sym.split("/")[0]
        paths = (
            funding_path(cache, sym, a.days),
            cache_path(cache, SPOT_VENUE, f"{base}/USDT", "4h", a.days),
            cache_path(cache, PERP_VENUE, sym, "4h", a.days),
        )
        if not all(p.exists() for p in paths):
            continue
        f, s, p = (json.loads(x.read_text()) for x in paths)
        ev = align(f, s, p)
        if len(ev) > 200:
            data[base] = ev

    L = ["# Funding carry: cash-and-carry на Binance (лонг спот + шорт перп)\n"]
    first = min(ev[0]["ts_ms"] for ev in data.values())
    last = max(ev[-1]["ts_ms"] for ev in data.values())
    fmt = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    L.append(
        f"Період {fmt(first)} … {fmt(last)}; тикерів: {len(data)} ({', '.join(data)}); подій funding: {sum(len(v) for v in data.values())}."
    )
    L.append("APR — на номінал позиції (капітал ≈ номінал × (1 + 1/плече перпа)). Комісії — за кожен вхід і вихід на обох ногах.\n")

    L.append("## 1. Чи передбачуваний funding: trailing 7d APR → наступні 7 днів\n")
    pairs = [p for ev in data.values() for p in funding_persistence(ev, lookback_days=7, horizon_days=7, step_days=1)]
    per_sym = [
        c for ev in data.values() if (c := pearson(funding_persistence(ev, lookback_days=7, horizon_days=7, step_days=1))) is not None
    ]
    L.append(
        f"Кореляція Пірсона (усі тикери, щоденні точки, n={len(pairs)}): {pearson(pairs):.2f}; медіана по тикерах: {statistics.median(per_sym):.2f}.\n"
    )
    L.append("| Trailing 7d APR | n | наступні 7д: сер. APR | медіана | частка > 0 |\n|---|---|---|---|---|")
    for lo, hi in BUCKETS:
        nxt = [y for x, y in pairs if lo <= x < hi]
        if nxt:
            name = f"{'<' if lo < -1 else ''}{'' if lo < -1 else f'{lo * 100:.0f}%'}{' … ' if lo > -1 and hi < 1e8 else ''}{'' if hi > 1e8 else f'{hi * 100:.0f}%'}{'+' if hi > 1e8 else ''}"
            L.append(
                f"| {name} | {len(nxt)} | {pct(statistics.mean(nxt))} | {pct(statistics.median(nxt))} | {sum(1 for y in nxt if y > 0) / len(nxt):.0%} |"
            )

    L.append("\n## 2. Основне правило (зафіксоване до запуску) і базові варіанти\n")
    L.append(STRAT_HDR)
    taker = {"spot_fee": 0.001, "perp_fee": 0.0005}
    maker = {"spot_fee": 0.00075, "perp_fee": 0.0002}
    primary_row, primary = strategy_rows(
        data, "**поріг 7д ≥10% / ≤0%, taker**", rule="threshold", lookback_days=7, entry_apr=0.10, exit_apr=0.0, **taker
    )
    L.append(primary_row)
    L.append(strategy_rows(data, "завжди в позиції, taker", rule="always", lookback_days=7, **taker)[0])
    L.append(
        strategy_rows(
            data, "поріг 7д ≥10% / ≤0%, знижені комісії", rule="threshold", lookback_days=7, entry_apr=0.10, exit_apr=0.0, **maker
        )[0]
    )

    L.append("\n## 3. Чутливість (taker)\n")
    L.append(STRAT_HDR)
    for lb in (3, 7, 14):
        for entry in (0.05, 0.10, 0.20, 0.30):
            for ex in (0.0, 0.05):
                if ex >= entry:
                    continue
                L.append(
                    strategy_rows(
                        data, f"{lb}д ≥{entry:.0%} / ≤{ex:.0%}", rule="threshold", lookback_days=lb, entry_apr=entry, exit_apr=ex, **taker
                    )[0]
                )

    L.append("\n## 3b. Портфель: капітал ділиться на K слотів, вільний слот бере перший сигнал основного правила\n")
    L.append("Дохідність — на номінал портфеля (номінал слота = 1/K, без маржі перпа), незайняті слоти лежать без доходу.\n")
    L.append(
        "| K слотів | угод взято | APR на номінал | частка часу хоч один слот зайнятий | середня зайнятість слотів |\n|---|---|---|---|---|"
    )
    trades = []
    for s_name, ev in data.items():
        res = simulate_cash_and_carry(ev, rule="threshold", lookback_days=7, entry_apr=0.10, exit_apr=0.0, **taker)
        trades += [dict(t, symbol=s_name) for t in res["trades"]]
    trades.sort(key=lambda t: t["open_ts"])
    span_years = (last - first - 7 * 86_400_000) / (365 * 86_400_000)
    for k_slots in (1, 3, 5, 10):
        taken, open_until = [], []
        for t in trades:
            open_until = [x for x in open_until if x > t["open_ts"]]
            if len(open_until) < k_slots:
                taken.append(t)
                open_until.append(t["close_ts"])
        occupied = sum(t["close_ts"] - t["open_ts"] for t in taken) / ((last - first) * k_slots)
        grid = range(first, last, 8 * 3_600_000)
        busy = sum(1 for g in grid if any(t["open_ts"] <= g < t["close_ts"] for t in taken)) / max(len(grid), 1)
        apr_cap = sum(t["pnl"] for t in taken) / k_slots / span_years
        L.append(f"| {k_slots} | {len(taken)} | {pct(apr_cap)} | {busy:.0%} | {occupied:.0%} |")

    L.append("\n## 4. Стабільність основного правила: по половинах періоду\n")
    L.append(STRAT_HDR)
    mid = (first + last) // 2
    for label, cut in (("перша половина", lambda e: e["ts_ms"] < mid), ("друга половина", lambda e: e["ts_ms"] >= mid)):
        part = {s: [e for e in ev if cut(e)] for s, ev in data.items()}
        part = {s: ev for s, ev in part.items() if len(ev) > 100}
        L.append(strategy_rows(part, label, rule="threshold", lookback_days=7, entry_apr=0.10, exit_apr=0.0, **taker)[0])

    L.append("\n## 5. Основне правило по тикерах\n")
    L.append("| Тикер | APR | funding / рік | комісії / рік | basis / рік | у ринку | угод | макс. DD |\n|---|---|---|---|---|---|---|---|")
    for r in sorted(primary, key=lambda r: -r["apr"]):
        L.append(
            f"| {r['symbol']} | {pct(r['apr'])} | {pct(r['funding'] / r['years'])} | {pct(-r['fees'] / r['years'])} | "
            f"{pct(r['basis'] / r['years'])} | {r['time_in_market']:.0%} | {r['n_trades']} | {r['max_drawdown'] * 100:.1f}% |"
        )

    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
