"""Funding carry on all Binance crypto perps with a spot market (alts included), as a slot portfolio.

Pre-registered before running:
  primary = 5 slots, trailing 7d APR >= 20 % to enter, <= 0 % to exit, taker fees
  (spot 0.10 % + perp 0.05 %) plus 0.05 % slippage per leg per side.
  H1: the all-symbol portfolio beats the same rule restricted to the top-20 majors.
  Practical bar: >= 10 % APR on capital after costs.
The rule picks symbols walk-forward from trailing funding only, so the universe is not chosen
by hindsight; it is still limited to symbols listed today (delisted coins are not in ccxt).

Usage:
    uv run python scripts/verify_carry_alts.py --cache-dir <dir> --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any

from verify_funding_carry import fetch_funding, funding_path
from verify_lectures import TOP20, cache_path, fetch_history

from tradedesk.analytics.carry import YEAR_MS, simulate_carry_portfolio
from tradedesk.server import build_server

PERP, SPOT = "binanceusdm", "binance"
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "USD1", "RLUSD", "EUR", "XUSD", "BFUSD"}
MULT = re.compile(r"^(1000000|10000|1000)([A-Z].*)$")


async def universe(rt: Any) -> dict[str, tuple[str, int]]:
    """perp symbol -> (spot symbol, quantity multiplier) for crypto perps that have a Binance USDT spot market."""
    perps = (await rt.exchanges.markets(PERP)).data or {}
    spots = (await rt.exchanges.markets(SPOT)).data or {}
    spot_ok = {s for s, m in spots.items() if (m or {}).get("type") == "spot" and (m or {}).get("active") is not False}
    out: dict[str, tuple[str, int]] = {}
    for sym, m in perps.items():
        info = (m or {}).get("info") or {}
        if not sym.endswith("/USDT:USDT") or info.get("contractType") != "PERPETUAL" or info.get("underlyingType") != "COIN":
            continue
        if (m or {}).get("active") is False:
            continue
        base = sym.split("/")[0]
        mult, spot_base = 1, base
        if (g := MULT.match(base)) is not None:
            mult, spot_base = int(g.group(1)), g.group(2)
        if spot_base in STABLES or f"{spot_base}/USDT" not in spot_ok:
            continue
        out[sym] = (f"{spot_base}/USDT", mult)
    return out


async def paced(go: Any, pause: float, tries: int = 6) -> Any:
    for attempt in range(tries):
        try:
            data = await go()
            await asyncio.sleep(pause)
            return data
        except Exception as e:  # noqa: BLE001
            wait = 30 * (attempt + 1)
            print(f"  retry in {wait}s after {type(e).__name__}: {str(e)[:120]}", flush=True)
            await asyncio.sleep(wait)
    return None


def max_trailing_apr(funding: list[dict[str, Any]], lookback_days: float = 7.0) -> float:
    ts = [int(e["ts_ms"]) for e in funding]
    prefix = [0.0]
    for e in funding:
        prefix.append(prefix[-1] + float(e["rate"]))
    lb = int(lookback_days * 86_400_000)
    best = float("-inf")
    for k, t in enumerate(ts):
        if t - ts[0] < lb:
            continue
        lo = bisect.bisect_right(ts, t - lb)
        best = max(best, (prefix[k + 1] - prefix[lo]) * 365.0 / lookback_days)
    return best


async def fetch(cache: Path, days: int, min_apr: float) -> dict[str, tuple[str, int]]:
    _, rt = build_server()
    try:
        uni = await universe(rt)
        print(f"universe: {len(uni)} crypto perps with spot", flush=True)
        for i, sym in enumerate(sorted(uni)):
            path = funding_path(cache, sym, days)
            if path.exists():
                continue
            data = await paced(lambda s=sym: fetch_funding(rt, s, days), pause=1.9)
            if data is not None:
                await asyncio.to_thread(path.write_text, json.dumps(data))
            if i % 25 == 0:
                print(f"  funding {i}/{len(uni)}", flush=True)
        hot = []
        for sym in sorted(uni):
            path = funding_path(cache, sym, days)
            if path.exists() and max_trailing_apr(json.loads(await asyncio.to_thread(path.read_text))) >= min_apr:
                hot.append(sym)
        print(f"symbols that ever reached trailing 7d >= {min_apr:.0%}: {len(hot)}", flush=True)
        for i, sym in enumerate(hot):
            spot_sym, _ = uni[sym]
            for path, go in (
                (cache_path(cache, PERP, sym, "4h", days), lambda s=sym: fetch_history(rt, PERP, s, "4h", days)),
                (cache_path(cache, SPOT, spot_sym, "4h", days), lambda s=spot_sym: fetch_history(rt, SPOT, s, "4h", days, page=1000)),
            ):
                if path.exists():
                    continue
                data = await paced(go, pause=0.2)
                if data is not None:
                    await asyncio.to_thread(path.write_text, json.dumps(data))
            if i % 25 == 0:
                print(f"  prices {i}/{len(hot)}", flush=True)
        return uni
    finally:
        await rt.aclose()


def load_markets(cache: Path, days: int, uni: dict[str, tuple[str, int]]) -> dict[str, dict[str, Any]]:
    out = {}
    for sym, (spot_sym, mult) in uni.items():
        paths = funding_path(cache, sym, days), cache_path(cache, PERP, sym, "4h", days), cache_path(cache, SPOT, spot_sym, "4h", days)
        if not all(p.exists() for p in paths):
            continue
        funding, perp, spot = (json.loads(p.read_text()) for p in paths)
        if not funding or not perp or not spot:
            continue
        spot = [dict(b, close=b["close"] * mult) for b in spot]
        out[sym] = {"funding": funding, "perp": perp, "spot": spot}
    return out


def q(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def row(label: str, res: dict[str, Any]) -> str:
    st, tr = res["stats"], res["trades"]
    if not st:
        return f"| {label} | — |"
    hold = statistics.mean((t["close_ts"] - t["open_ts"]) / 86_400_000 for t in tr) if tr else 0.0
    neg = sum(1 for t in tr if t["pnl"] < 0) / len(tr) if tr else 0.0
    return (
        f"| {label} | {st['apr_on_capital'] * 100:+.1f}% | {st['max_drawdown'] * 100:.1f}% | {st['n_trades']} | {hold:.1f} | "
        f"{st['slot_utilisation']:.0%} | {st['funding'] / st['years'] * 100:+.1f}% | {st['basis'] / st['years'] * 100:+.1f}% | "
        f"{-st['costs'] / st['years'] * 100:+.1f}% | {neg:.0%} |"
    )


HDR = (
    "| Варіант | APR на номінал | макс. DD | угод | сер. днів | зайнятість слотів | funding / рік | basis / рік | витрати / рік | угод у мінусі |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    uni = asyncio.run(fetch(cache, a.days, min_apr=0.20))
    mk = load_markets(cache, a.days, uni)
    majors = {s: d for s, d in mk.items() if s.split("/")[0] in TOP20}
    cost = {"spot_fee": 0.001, "perp_fee": 0.0005}
    base = dict(slots=5, lookback_days=7, entry_apr=0.20, exit_apr=0.0, slippage=0.0005, **cost)

    primary = simulate_carry_portfolio(mk, **base)
    L = ["# Funding carry на альтах: портфель зі слотами (Binance, крипто-перпи зі спотом)\n"]
    first = min(d["funding"][0]["ts_ms"] for d in mk.values())
    last = max(d["funding"][-1]["ts_ms"] for d in mk.values())
    fmt = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    L.append(
        f"Період {fmt(first)} … {fmt(last)}. Всесвіт: {len(uni)} крипто-перпів зі спотом; ціни завантажено для {len(mk)}, "
        f"у яких trailing 7d APR хоч раз сягав 20% (інші правило з порогом ≥20% не торгує). Мейджори (топ-20): {len(majors)}.\n"
    )
    L.append(
        "APR тут рахується на **номінал** портфеля (номінал слота = 1/K). Реальний капітал більший: спот + маржа шорт-перпа ≈ "
        "номінал × (1 + 1/плече). Перерахунок для основного варіанта — у розділі «Ризик».\n"
    )
    L.append("## Основний варіант (зафіксований до запуску) і H1\n")
    L.append(HDR)
    L.append(row("**усі, 5 слотів, ≥20% / ≤0%, слипедж 0.05%**", primary))
    L.append(row("лише мейджори, те саме правило", simulate_carry_portfolio(majors, **base)))
    L.append(row("лише мейджори, ≥10% (як у попередньому звіті)", simulate_carry_portfolio(majors, **dict(base, entry_apr=0.10))))

    L.append("\n## Чутливість (усі символи)\n")
    L.append(HDR)
    for entry in (0.20, 0.30, 0.50, 1.00):
        for slots in (3, 5, 10):
            L.append(row(f"вхід ≥{entry:.0%}, {slots} слотів", simulate_carry_portfolio(mk, **dict(base, entry_apr=entry, slots=slots))))
    for slip in (0.0, 0.002, 0.005):
        L.append(row(f"основний, слипедж {slip * 100:.2f}%/нога/сторона", simulate_carry_portfolio(mk, **dict(base, slippage=slip))))
    L.append(row("основний, вихід ≤10%", simulate_carry_portfolio(mk, **dict(base, exit_apr=0.10))))

    tr = primary["trades"]
    w = 1 / base["slots"]
    L.append("\n## Ризик основного варіанта\n")
    if tr:
        ru = [t["max_runup"] for t in tr]
        pr = [t["max_premium_rise"] for t in tr]
        L.append(
            f"- Максимальний ріст ціни перпа від входу за угоду (ризик ліквідації шорт-ноги): медіана {q(ru, 0.5):.0%}, "
            f"p90 {q(ru, 0.9):.0%}, p99 {q(ru, 0.99):.0%}, максимум {max(ru):.0%}."
        )
        L.append(
            f"- Частка угод, де ріст ≥33% (≈ ліквідація ізольованого шорта 3x без поповнення): {sum(1 for x in ru if x >= 0.33) / len(ru):.0%}; "
            f"≥50% (2x): {sum(1 for x in ru if x >= 0.5) / len(ru):.0%}; ≥100% (1x): {sum(1 for x in ru if x >= 1.0) / len(ru):.0%}."
        )
        apr = primary["stats"]["apr_on_capital"]
        L.append(
            f"- APR основного варіанта на реальний капітал (спот + маржа перпа): плече 1x → {apr / 2:+.1%}, "
            f"2x → {apr / 1.5:+.1%}, 3x → {apr / (1 + 1 / 3):+.1%}. Плече вище 1x вимагає поповнювати маржу (продажем спота "
            f"або переказом) у кожній угоді, де ціна росте сильніше за 1/плече."
        )
        L.append(
            f"- Найгірше розширення премії перпа до спота за угоду: p90 {q(pr, 0.9):+.2%}, p99 {q(pr, 0.99):+.2%}, максимум {max(pr):+.2%}."
        )
        L.append(f"- Найгірша угода: {min(t['pnl'] for t in tr):+.2%} на слот; найкраща: {max(t['pnl'] for t in tr):+.2%}.")
        by: dict[str, float] = {}
        for t in tr:
            by[t["symbol"]] = by.get(t["symbol"], 0.0) + w * t["pnl"]
        top = sorted(by.items(), key=lambda x: -x[1])
        named = [(s.split("/")[0], v) for s, v in top]
        L.append("- Найбільший внесок (на номінал): " + ", ".join(f"{n} {v:+.2%}" for n, v in named[:8]) + ".")
        L.append("- Найбільший мінус: " + ", ".join(f"{n} {v:+.2%}" for n, v in named[-5:]) + ".")
        L.append(
            f"- Різних символів за період: {len(by)}; частка P&L від топ-5: {sum(v for _, v in top[:5]) / max(sum(by.values()), 1e-12):.0%}."
        )

    L.append("\n## Стабільність основного варіанта у часі\n")
    L.append("| Період | APR на номінал | угод |\n|---|---|---|")
    start = primary["equity"][0][0] if primary["equity"] else first
    mid = (start + last) // 2
    for label, lo, hi in (("перша половина", start, mid), ("друга половина", mid, last)):
        part = [t for t in tr if lo <= t["open_ts"] < hi]
        yrs = (hi - lo) / YEAR_MS
        L.append(f"| {label} ({fmt(lo)} … {fmt(hi)}) | {sum(w * t['pnl'] for t in part) / yrs * 100:+.1f}% | {len(part)} |")
    for y0 in range(int(fmt(start)[:4]), int(fmt(last)[:4]) + 1):
        lo = int(time.mktime((y0, 1, 1, 0, 0, 0, 0, 0, 0)) * 1000)
        hi = int(time.mktime((y0 + 1, 1, 1, 0, 0, 0, 0, 0, 0)) * 1000)
        part = [t for t in tr if lo <= t["open_ts"] < hi]
        span = (min(hi, last) - max(lo, start)) / YEAR_MS
        if part and span > 0:
            L.append(f"| {y0} | {sum(w * t['pnl'] for t in part) / span * 100:+.1f}% | {len(part)} |")

    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
