"""Volatility risk premium on Deribit BTC/ETH: DVOL vs realized vol, and a delta-hedged short straddle.

Pre-registered before running:
  H1: mean forward VRP (DVOL - realized vol over the next 30 days) > 0, 95 % CI > 0 on
      non-overlapping 30-day samples.
  H2: the spread DVOL - trailing 30d realized vol (known at t) predicts the forward VRP.
  H3: selling a 30d ATM straddle at DVOL with daily delta hedging, after costs (1 vol point on
      entry, Deribit fee 0.03 % of spot per option, 0.05 % on hedge turnover), has mean P&L > 0
      with 95 % CI > 0 on non-overlapping trades.
  H4: selling only when the spread > 0 improves H3.
DVOL is a 30-day constant-maturity index, used here as the ATM straddle's IV (an approximation:
real ATM options trade at their own IV and spread). Prices are Binance spot daily closes.

Usage:
    uv run python scripts/verify_vrp.py --cache-dir <dir> --out report.md
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

from verify_lectures import fetch_history

from tradedesk.analytics.carry import pearson
from tradedesk.analytics.vrp import DAY_MS, align_daily, short_straddle_hedged, stats, vrp_records
from tradedesk.server import build_server

DVOL_START_MS = 1_614_556_800_000  # 2021-03-01


async def fetch_dvol(rt: Any, currency: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    rows: dict[int, float] = {}
    end = end_ms
    for _ in range(20):
        f = await rt.deribit.volatility_index_data(currency, start_ms, end, 86400)
        data = f.data or {}
        for x in data.get("data") or []:
            rows[int(x[0])] = float(x[4]) / 100.0
        cont = data.get("continuation")
        if not cont or int(cont) <= start_ms or int(cont) >= end:
            break
        end = int(cont)
    return sorted(rows.items())


async def fetch_all(cache: Path, currencies: list[str]) -> None:
    _, rt = build_server()
    now = int(time.time() * 1000)
    days = (now - DVOL_START_MS) // DAY_MS + 40
    try:
        for cur in currencies:
            p = cache / f"dvol_{cur}.json"
            if not p.exists():
                data = await fetch_dvol(rt, cur, DVOL_START_MS, now)
                await asyncio.to_thread(p.write_text, json.dumps(data))
            q = cache / f"binance_{cur}_1d.json"
            if not q.exists():
                bars = await fetch_history(rt, "binance", f"{cur}/USDT", "1d", int(days), page=1000)
                await asyncio.to_thread(q.write_text, json.dumps([[b["ts"], b["close"]] for b in bars]))
    finally:
        await rt.aclose()


def fmt_stats(label: str, s: dict[str, Any], pct: bool = True, risk: bool = True) -> str:
    if not s.get("n"):
        return f"| {label} | 0 | — | — | — | — | — | — |"
    k = 100 if pct else 1
    ci = s["ci95"]
    sharpe = f"{s['sharpe']:.2f}" if risk and s.get("sharpe") is not None else "—"
    dd = f"{s['max_drawdown'] * k:.2f}" if risk else "—"
    return (
        f"| {label} | {s['n']} | {s['mean'] * k:+.2f} | [{ci[0] * k:+.2f}; {ci[1] * k:+.2f}] | {s['share_positive']:.0%} | "
        f"{s['min'] * k:+.2f} | {dd} | {sharpe} |"
    )


def _half(xs: list[float]) -> str:
    return f"{statistics.mean(xs) * 100:+.2f} (n={len(xs)})" if xs else "—"


HDR = "| Вибірка | n | середнє | 95% CI | частка > 0 | найгірше | макс. DD (сума) | Sharpe (річний) |\n|---|---|---|---|---|---|---|---|"
PPY = 365 / 30


def non_overlapping(rows: list[dict[str, float]], offset: int, keep: Any = None, **kw: Any) -> list[dict[str, float]]:
    out, i = [], offset
    while i + 30 < len(rows):
        if keep is None or keep(i):
            t = short_straddle_hedged(rows, i, **kw)
            if t is not None:
                out.append(t)
                i += 30
                continue
        i += 1 if keep is not None else 30
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    currencies = ["BTC", "ETH"]
    asyncio.run(fetch_all(cache, currencies))

    fmt = lambda d: time.strftime("%Y-%m-%d", time.gmtime(d * 86400))  # noqa: E731
    L = ["# Премія за волатильність (VRP) на Deribit: DVOL проти реалізованої волатильності\n"]
    L.append(
        "Вола — десяткові частки, у таблицях — у вол-пунктах (п.п.) або % від спота. Реалізована вола — "
        "`sqrt(365 · mean(r²))` за денними лог-доходностями Binance spot. Угоди стреддла — % від спота на старті.\n"
    )
    for cur in currencies:
        iv = [tuple(x) for x in json.loads((cache / f"dvol_{cur}.json").read_text())]
        px = [tuple(x) for x in json.loads((cache / f"binance_{cur}_1d.json").read_text())]
        rows = align_daily(iv, px)
        recs = vrp_records(rows)
        spread_by_day = {int(r["day"]): r["spread"] for r in recs}
        L.append(f"## {cur}: {fmt(rows[0]['day'])} … {fmt(rows[-1]['day'])}, {len(rows)} днів\n")

        L.append("### H1. DVOL − реалізована вола наступних 30 днів (вол-пункти)\n")
        L.append(HDR)
        L.append(fmt_stats("усі дні (перекриваються)", stats([r["vrp_vol"] for r in recs]), risk=False))
        for off in (0, 10, 20):
            L.append(fmt_stats(f"неперекривні, старт +{off}д", stats([r["vrp_vol"] for r in recs[off::30]]), risk=False))
        L.append(
            f"\nСередні DVOL {statistics.mean(r['iv'] for r in recs) * 100:.1f}%, реалізована наперед "
            f"{statistics.mean(r['rv_forward'] for r in recs) * 100:.1f}%; DVOL > реалізованої у "
            f"{sum(1 for r in recs if r['vrp_vol'] > 0) / len(recs):.0%} днів.\n"
        )

        L.append("### H2. Чи передбачає спред (DVOL − реалізована за минулі 30д) майбутню премію\n")
        corr = pearson([(r["spread"], r["vrp_vol"]) for r in recs])
        L.append(f"Кореляція спреду з майбутнім VRP (усі дні): {corr:.2f}.\n")
        L.append("| Квінтиль спреду | межі спреду, п.п. | n | майбутній VRP, п.п. | частка VRP > 0 |\n|---|---|---|---|---|")
        srt = sorted(recs, key=lambda r: r["spread"])
        for qn in range(5):
            part = srt[qn * len(srt) // 5 : (qn + 1) * len(srt) // 5]
            L.append(
                f"| Q{qn + 1} | {part[0]['spread'] * 100:+.1f} … {part[-1]['spread'] * 100:+.1f} | {len(part)} | "
                f"{statistics.mean(r['vrp_vol'] for r in part) * 100:+.2f} | {sum(1 for r in part if r['vrp_vol'] > 0) / len(part):.0%} |"
            )

        L.append("\n### H3. Продаж 30-денного ATM-стреддла з щоденним дельта-хеджем (% від спота за угоду)\n")
        L.append(HDR)
        base = {"entry_cost_vol": 0.01, "option_fee": 0.0003, "hedge_fee": 0.0005}
        phase_means = []
        for off in range(30):
            tr = non_overlapping(rows, off, **base)
            phase_means.append(stats([t["pnl"] for t in tr])["mean"])
            if off in (0, 10, 20):
                L.append(fmt_stats(f"хедж, неперекривні, старт +{off}д", stats([t["pnl"] for t in tr], periods_per_year=PPY)))
        all_hedged = [t for i in range(len(rows)) if (t := short_straddle_hedged(rows, i, **base))]
        L.append(fmt_stats("хедж, усі дні старту (перекриваються)", stats([t["pnl"] for t in all_hedged])))
        L.append(
            fmt_stats(
                "без хеджу, неперекривні +0д",
                stats([t["pnl"] for t in non_overlapping(rows, 0, hedge=False, **base)], periods_per_year=PPY),
            )
        )
        L.append(
            fmt_stats(
                "хедж, без витрат, неперекривні +0д",
                stats([t["pnl"] for t in non_overlapping(rows, 0, entry_cost_vol=0, option_fee=0, hedge_fee=0)], periods_per_year=PPY),
            )
        )
        L.append(
            fmt_stats(
                "хедж, вхід −2 п.п., неперекривні +0д",
                stats([t["pnl"] for t in non_overlapping(rows, 0, **dict(base, entry_cost_vol=0.02))], periods_per_year=PPY),
            )
        )
        L.append(
            fmt_stats(
                "хедж, вхід −4 п.п. (DVOL→ATM + спред), неперекривні +0д",
                stats([t["pnl"] for t in non_overlapping(rows, 0, **dict(base, entry_cost_vol=0.04))], periods_per_year=PPY),
            )
        )
        L.append(
            f"\nСередній P&L за угоду по всіх 30 фазах старту: від {min(phase_means) * 100:+.2f}% до {max(phase_means) * 100:+.2f}% "
            f"(медіана {statistics.median(phase_means) * 100:+.2f}%). Середня премія стреддла: "
            f"{statistics.mean(t['premium'] for t in all_hedged) * 100:.1f}% від спота.\n"
        )

        L.append("### H4. Продавати лише при спреді > 0\n")
        L.append(HDR)
        for label, cond in (("спред > 0", lambda s: s > 0), ("спред ≤ 0", lambda s: s <= 0), ("спред > +5 п.п.", lambda s: s > 0.05)):
            sel = [t for t in all_hedged if (s := spread_by_day.get(int(t["day"]))) is not None and cond(s)]
            L.append(fmt_stats(f"{label}, усі дні старту", stats([t["pnl"] for t in sel])))
            keep = lambda i, cond=cond, sb=spread_by_day, rw=rows: (s := sb.get(int(rw[i]["day"]))) is not None and cond(s)  # noqa: E731
            L.append(
                fmt_stats(
                    f"{label}, неперекривні", stats([t["pnl"] for t in non_overlapping(rows, 0, keep=keep, **base)], periods_per_year=PPY)
                )
            )

        L.append("\n### H5 (розвідувальна, не зафіксована до запуску). Рівень DVOL: перцентиль за попередні 365 днів\n")
        L.append(
            "| Перцентиль DVOL | n | майбутній VRP, п.п. | частка VRP > 0 | стреддл (хедж), % від спота | частка > 0 | "
            "стреддл 2021–23 | стреддл 2024–26 |\n|---|---|---|---|---|---|---|---|"
        )
        split_day = 19723  # 2024-01-01
        by_day_trade = {int(t["day"]): t for t in all_hedged}
        ivs = [(int(r["day"]), r["iv"]) for r in rows]
        pct_by_day: dict[int, float] = {}
        for i in range(365, len(ivs)):
            past = sorted(v for _, v in ivs[i - 365 : i])
            pct_by_day[ivs[i][0]] = bisect.bisect_left(past, ivs[i][1]) / len(past)
        for lo, hi in ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
            sel = [r for r in recs if lo <= pct_by_day.get(int(r["day"]), -1) < hi]
            trd = [by_day_trade[int(r["day"])]["pnl"] for r in sel if int(r["day"]) in by_day_trade]
            early = [by_day_trade[int(r["day"])]["pnl"] for r in sel if int(r["day"]) in by_day_trade and int(r["day"]) < split_day]
            late = [by_day_trade[int(r["day"])]["pnl"] for r in sel if int(r["day"]) in by_day_trade and int(r["day"]) >= split_day]
            if sel and trd:
                L.append(
                    f"| {lo:.0%}–{min(hi, 1):.0%} | {len(sel)} | {statistics.mean(r['vrp_vol'] for r in sel) * 100:+.2f} | "
                    f"{sum(1 for r in sel if r['vrp_vol'] > 0) / len(sel):.0%} | {statistics.mean(trd) * 100:+.2f} | "
                    f"{sum(1 for x in trd if x > 0) / len(trd):.0%} | {_half(early)} | {_half(late)} |"
                )

        L.append("\n### По роках (хедж, усі дні старту, % від спота за угоду)\n")
        L.append(HDR)
        years = sorted({fmt(int(t["day"]))[:4] for t in all_hedged})
        for y in years:
            L.append(fmt_stats(y, stats([t["pnl"] for t in all_hedged if fmt(int(t["day"]))[:4] == y])))
        worst = sorted(all_hedged, key=lambda t: t["pnl"])[:5]
        L.append(
            "\nНайгірші старти (хедж): "
            + "; ".join(
                f"{fmt(int(t['day']))}: {t['pnl'] * 100:+.1f}% (рух {t['move'] * 100:+.0f}%, IV {t['iv'] * 100:.0f}%)" for t in worst
            )
            + ".\n"
        )

    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
