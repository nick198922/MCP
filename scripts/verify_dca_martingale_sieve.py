#!/usr/bin/env python3
"""IDEA-20261024-dca-martingale-bots-sieve, descriptive companion of the three sieve runs of ``dca_martingale``.

Recomputes the sieve trades (``scripts/sieve.py`` ``run_symbol``: same spec, costs, funding, universe, hidden period
cut) without touching the registry, and prints what the sieve does not (description, not a pass criterion):

- win rate, exit kinds, quantiles of R, mean win and mean loss;
- the loss tail by date cluster: sum of R of the trades entered on one UTC day (worst days, quantiles, the share of
  the period's total R lost on the worst 5 % of days) and the maximum drawdown of the cumulative R by entry day (R
  units, each trade risks 1R; not capital);
- per-asset check (owner's rule 5a): IS table per symbol, Cochran's Q / I^2, persistence of the top 10 and the top
  quarter of symbols by IS mean in OOS (``verify_grid_short_vol_regime.per_asset``).

Usage: uv run python scripts/verify_dca_martingale_sieve.py --out ~/.cache/tradedesk-agents/dca_martingale/report.md
"""

from __future__ import annotations

import argparse
import math
import resource
import sys
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sieve as sv  # noqa: E402  (run_symbol)
import verify_course_s14_ema as s1  # noqa: E402
import verify_grid_short_vol_regime as gv  # noqa: E402  (per_asset, summ_row)

from tradedesk.analytics import sieve_specs  # noqa: E402
from tradedesk.analytics.course_s14_ema import DAY_MS  # noqa: E402

SPEC = "dca_martingale"
RUNS = {
    "S-DCA-1: лонг, ціль 1.5 %, стоп 12 %": {},
    "S-DCA-2: лонг, ціль 1.5 %, стоп 6 %": {"stop": 0.06},
    "S-DCA-3: шорт, ціль 1.5 %, стоп 12 %": {"direction": -1},
}


def worker(args: tuple[str, dict[str, Any], str, str]) -> list[dict[str, Any]]:
    s, params, s1_cache, data_dir = args
    res = sv.run_symbol((s, SPEC, params, "strategy", s1_cache, data_dir, False))
    return [
        {"sym": s, "t_ms": t["t_ms"], "day": t["day"], "period": t["period"], "r": t["r_net"], "exit": t["exit_kind"]}
        for t in res["trades"]
    ]


def day_str(day: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(day * DAY_MS / 1000))


def max_drawdown(x: np.ndarray) -> tuple[float, int, int]:
    """Largest peak-to-trough fall of the cumulative sum of ``x`` (R) and the indexes of its peak and trough."""
    cum = np.concatenate([[0.0], np.cumsum(x)])
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    j = int(np.argmax(dd))
    i = int(np.argmax(cum[: j + 1])) if j > 0 else 0
    return float(dd[j]), i, j


def describe(trades: list[dict[str, Any]], emit: Any) -> None:
    emit(
        "| період | n | частка R > 0 | ціль / стоп / час / кінець даних | середній виграш | середній збиток | R q1 % | R q5 % | R медіана |"
    )
    emit("|---|---|---|---|---|---|---|---|---|")
    for per in ("is", "oos"):
        pt = [t for t in trades if t["period"] == per]
        if not pt:
            continue
        r = np.array([t["r"] for t in pt])
        ex = Counter(t["exit"] for t in pt)
        ex_s = " / ".join(f"{100 * ex.get(k, 0) / len(pt):.1f}%" for k in ("target", "stop", "time", "data_end"))
        win, loss = r[r > 0], r[r <= 0]
        q1, q5, q50 = np.quantile(r, [0.01, 0.05, 0.5])
        emit(
            f"| {per} | {len(pt)} | {100 * win.size / r.size:.1f}% | {ex_s} | {win.mean() if win.size else math.nan:+.3f} "
            f"| {loss.mean() if loss.size else math.nan:+.3f} | {q1:+.3f} | {q5:+.3f} | {q50:+.3f} |"
        )
    emit("\nХвіст за кластерами дат (сума R угод, відкритих в одну дату UTC; кожна угода ризикує 1R, не капітал):")
    emit(
        "| період | дат | сума R | q1 % дати | q5 % дати | медіана дати | 5 % найгірших дат: сума R | max просадка кумулятивної R | пік → дно |"
    )
    emit("|---|---|---|---|---|---|---|---|---|")
    worst_all: list[tuple[float, int, int, str]] = []
    for per in ("is", "oos"):
        by: dict[int, list[float]] = {}
        for t in trades:
            if t["period"] == per:
                by.setdefault(t["day"], []).append(t["r"])
        if not by:
            continue
        days = sorted(by)
        sums = np.array([sum(by[d]) for d in days])
        q1, q5, q50 = np.quantile(sums, [0.01, 0.05, 0.5])
        k = max(1, int(math.ceil(0.05 * sums.size)))
        worst = np.sort(sums)[:k].sum()
        dd, i, j = max_drawdown(sums)
        # cum[k] is the total after day k - 1: the peak is reached after days[i - 1], the trough after days[j - 1]
        span = f"{day_str(days[i - 1]) if i > 0 else 'початок'} → {day_str(days[j - 1])}" if j > 0 else "—"
        emit(f"| {per} | {sums.size} | {sums.sum():+.1f} | {q1:+.2f} | {q5:+.2f} | {q50:+.2f} | {worst:+.1f} | {dd:.1f} | {span} |")
        worst_all += [(float(sums[x]), days[x], len(by[days[x]]), per) for x in np.argsort(sums)[:5]]
    emit("- 5 найгірших дат кожного періоду (дата: сума R, угод, з них стопів):")
    for v, d, n, per in worst_all:
        n_stop = sum(1 for t in trades if t["day"] == d and t["exit"].startswith("stop"))
        emit(f"  - {per} {day_str(d)}: {v:+.1f}R, угод {n}, стопів {n_stop}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    a = ap.parse_args()
    lines: list[str] = []
    full: list[str] = []

    def emit(s: str) -> None:
        print(s, flush=True)
        lines.append(s)

    syms = sorted({x for v in s1.members(a.s1_cache).values() for x in v})
    spec = sieve_specs.load(SPEC)
    t0 = time.time()
    for label, over in RUNS.items():
        params = {**spec.PARAMS, **over}
        jobs = [(s, params, str(a.s1_cache), str(a.data_dir)) for s in syms]
        trades: list[dict[str, Any]] = []
        with Pool(a.workers) as pool:
            for res in pool.imap_unordered(worker, jobs, chunksize=4):
                trades += res
        if any(t["period"] == "hidden" for t in trades):
            raise SystemExit("internal error: hidden-period trade")
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        emit(f"\n## {label}: угод {len(trades)} ({time.time() - t0:.0f}s, RSS {rss:.0f} MB)")
        emit("| вибірка | n | дат | E[R] [95 % CI] |")
        emit("|---|---|---|---|")
        emit(gv.summ_row("IS (звірка з ситом)", [t for t in trades if t["period"] == "is"]))
        emit(gv.summ_row("OOS (звірка з ситом)", [t for t in trades if t["period"] == "oos"]))
        emit("\n### Форма виплати")
        describe(trades, emit)
        emit("\n### Перевірка за активами (IS → OOS)")
        full.append(f"\n## {label}: усі символи")
        gv.per_asset(trades, emit, full)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(lines + ["\n# Повні таблиці за символами"] + full) + "\n")
    emit(f"\nзаписано: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
