"""SFP with a lower-timeframe entry (DoubleTop «Торговля SFP»: read the sweep on 4h/12h, enter on 15m).

Same higher-timeframe signals as scripts/verify_lectures.py (walk-forward, no lookahead).
Every signal is replayed on 15m bars under three entry modes — htf_close, retest, confirm —
with the same 2x stop and TP1 = range mid, so the modes are compared on identical signals.
Exits are simulated on 15m bars (10-day timeout).

Usage:
    uv run python scripts/verify_sfp_ltf.py --cache-dir <dir> --out report.md
"""

from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from verify_lectures import TF_MS, TOP20, aggregate, cache_path, fetch_missing, run_all

from tradedesk.analytics.sfp_backtest import ltf_entry, simulate_bracket, summarise_r

MODES = ("htf_close", "retest", "confirm")
HOLD_15M = 960


def replay(job: tuple[str, str, list[dict[str, Any]], float, int]) -> list[dict[str, Any]]:
    path, sym, signals, fee_pct, wait = job
    bars = json.loads(Path(path).read_text())
    ts = [b["ts"] for b in bars]
    rows = []
    for sig in signals:
        v = sig["variants"]["m2_2x"]
        start = bisect.bisect_left(ts, sig["close_ts"])
        if start >= len(bars) or bars[start]["ts"] - sig["close_ts"] > TF_MS["15m"]:
            continue
        for mode in MODES:
            row = {"symbol": sym, "tf": sig["tf"], "ts": sig["ts"], "direction": sig["direction"], "mode": mode}
            e = ltf_entry(bars, start, sig["direction"], sig["swept_level"], v["stop"], v["tp1"], mode, wait_bars=wait)
            if "skip" in e:
                rows.append({**row, "skip": e["skip"]})
                continue
            entry = e["entry_price"]
            risk = abs(entry - v["stop"])
            sign = 1.0 if sig["direction"] == "long" else -1.0
            if e["stopped_in_fill_bar"]:
                sim = {"outcome": "stop", "r": -1.0}
            else:
                sim = simulate_bracket(bars, e["exit_from"], sig["direction"], entry, v["stop"], v["tp1"], HOLD_15M)
                if sim is None:
                    rows.append({**row, "skip": "no_bars_after_entry"})
                    continue
            risk_pct = risk / entry * 100.0
            rows.append(
                {
                    **row,
                    "outcome": sim["outcome"],
                    "r": sim["r"],
                    "r_net": sim["r"] - fee_pct / risk_pct,
                    "rr1": abs(v["tp1"] - entry) / risk,
                    "risk_pct": risk_pct,
                    "sign": sign,
                }
            )
    return rows


def line(title: str, s: dict[str, Any], extra: str = "") -> str:
    if not s.get("n"):
        return f"| {title} | 0 | — | — | — | {extra} |"
    ci = s["ci95"]
    return f"| {title} | {s['n']} | {s['win_rate']:.0%} | {s['avg_r']:+.3f} | [{ci[0]:+.2f}; {ci[1]:+.2f}] | {extra} |"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="binanceusdm")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--fee-pct", type=float, default=0.1)
    ap.add_argument("--wait-bars", type=int, default=16, help="15m bars to wait for a retest fill or a confirmation.")
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    symbols = [f"{b}/USDT:USDT" for b in TOP20]

    wanted = [(s, tf) for s in symbols for tf in ("4h", "15m") if not cache_path(cache, a.venue, s, tf, a.days).exists()]
    if wanted:
        import asyncio

        for (sym, tf), bars in asyncio.run(fetch_missing(a.venue, wanted, a.days)).items():
            cache_path(cache, a.venue, sym, tf, a.days).write_text(json.dumps(bars))
    data4h = {s: json.loads(cache_path(cache, a.venue, s, "4h", a.days).read_text()) for s in symbols}
    data12h = {s: aggregate(b, TF_MS["12h"]) for s, b in data4h.items()}

    base = {"max_width_pct": 15.0, "fee_pct": a.fee_pct, "sweep_wick_ratio": 0.5}
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor() as pool:
        sig4 = run_all(pool, data4h, dict(base, window=300, max_hold_bars=60, mfe_horizon=30))
        sig12 = run_all(pool, data12h, dict(base, window=150, max_hold_bars=20, mfe_horizon=10))
        jobs = []
        for sym in symbols:
            sigs = [dict(s, tf="4h", close_ts=s["ts"] + TF_MS["4h"]) for s in sig4.get(sym, [])]
            sigs += [dict(s, tf="12h", close_ts=s["ts"] + TF_MS["12h"]) for s in sig12.get(sym, [])]
            jobs.append((str(cache_path(cache, a.venue, sym, "15m", a.days)), sym, sigs, a.fee_pct, a.wait_bars))
        for part in pool.map(replay, jobs):
            rows.extend(part)

    L = ["# SFP: вхід на 15m після свипу на 4h / 12h\n"]
    L.append(
        f"20 тикерів, {a.days} днів; ті самі HTF-сигнали, що в першому прогоні (ширина 15%, фітиль 0.5). "
        f"Стоп 2× пересвипу, TP1 = середина рейнджу, таймаут 10 днів, виходи на 15m-барах, комісія {a.fee_pct}% round-trip. "
        f"Очікування ретесту/підтвердження — {a.wait_bars} барів 15m.\n"
    )
    L.append("«R на сигнал» рахує пропущені сигнали як 0 R, тож режими з різною кількістю входів порівнюються чесно.\n")
    for tf in ("4h", "12h"):
        n_sig = len({(r["symbol"], r["ts"]) for r in rows if r["tf"] == tf})
        L.append(f"## {tf}: {n_sig} сигналів\n")
        L.append(
            "| Режим входу | угод | TP1 hit | Сер. R на угоду (net) | 95% CI | R gross · медіана ризику · R на сигнал (net) [95% CI] · сер. RR · пропуски |"
        )
        L.append("|---|---|---|---|---|---|")
        for mode in MODES:
            mr = [r for r in rows if r["tf"] == tf and r["mode"] == mode]
            trades = [r for r in mr if "skip" not in r]
            st = summarise_r([r["r_net"] for r in trades], [r["outcome"] for r in trades])
            per_sig = summarise_r([r.get("r_net", 0.0) for r in mr], [r.get("outcome", "skip") for r in mr])
            skips = Counter(r["skip"] for r in mr if "skip" in r)
            rr = sum(r["rr1"] for r in trades) / len(trades) if trades else 0.0
            ps = f"{per_sig['avg_r']:+.3f} [{per_sig['ci95'][0]:+.2f}; {per_sig['ci95'][1]:+.2f}]" if per_sig.get("n", 0) > 1 else "—"
            skip_txt = ", ".join(f"{k} {v}" for k, v in skips.most_common()) or "—"
            gross = sum(r["r"] for r in trades) / len(trades) if trades else 0.0
            med_risk = sorted(r["risk_pct"] for r in trades)[len(trades) // 2] if trades else 0.0
            L.append(line(mode, st, f"gross {gross:+.3f} · ризик {med_risk:.2f}% · {ps} · RR {rr:.2f} · {skip_txt}"))
        L.append("")
        L.append("| Режим, напрям | угод | TP1 hit | Сер. R (net) | 95% CI | |\n|---|---|---|---|---|---|")
        for mode in MODES:
            for d in ("long", "short"):
                t = [r for r in rows if r["tf"] == tf and r["mode"] == mode and r["direction"] == d and "skip" not in r]
                L.append(line(f"{mode}, {d}", summarise_r([r["r_net"] for r in t], [r["outcome"] for r in t])))
        L.append("")

    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).write_text(report + "\n")


if __name__ == "__main__":
    main()
