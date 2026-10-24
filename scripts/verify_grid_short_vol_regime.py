#!/usr/bin/env python3
"""IDEA-20261024-grid-short-vol-regime, descriptive companion of the three sieve runs of ``grid_ladder``.

Recomputes the sieve trades (``scripts/sieve.py`` ``run_symbol``: same spec, costs, funding, universe, hidden period
cut) without touching the registry, and prints what the sieve does not:

- per-asset check (owner's rule 5a): IS table per symbol, Cochran's Q / I^2 of the per-symbol means, and persistence
  (top 10 and top quarter of symbols by IS mean -> their OOS mean against the rest);
- H3 (descriptive, no effect on the status): R by tercile of sigma7 / sigma30 at the entry (tercile edges from IS
  trades), and by Deribit BTC DVOL of the previous UTC day below / above its trailing 365-day median (all trades and
  BTC / ETH only).

Usage: uv run python scripts/verify_grid_short_vol_regime.py --out ~/.cache/tradedesk-agents/grid_short_vol_regime/report.md
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sieve as sv  # noqa: E402  (run_symbol, cut_hidden)
import verify_course_s14_ema as s1  # noqa: E402

from tradedesk.analytics import sieve_specs  # noqa: E402
from tradedesk.analytics.course_s14_ema import DAY_MS  # noqa: E402
from tradedesk.analytics.strategy_sieve import summarize  # noqa: E402

SPEC = "grid_ladder"
RUNS = {
    "1: без фільтра 0.5σ": {"regime": 0},
    "2: з фільтром 0.5σ": {},
    "3: з фільтром 1.0σ": {"step_sigma": 1.0},
}
N_BOOT = 2000
MIN_N = 30  # trades per symbol for the per-asset statistics


def worker(args: tuple[str, dict[str, Any], str, str]) -> list[dict[str, Any]]:
    s, params, s1_cache, data_dir = args
    res = sv.run_symbol((s, SPEC, params, "strategy", s1_cache, data_dir, False))
    trades = res["trades"]
    if not trades:
        return []
    spec = sieve_specs.load(SPEC)
    h1 = sv.cut_hidden(s1.load_1h(Path(s1_cache), Path(data_dir), s))
    f = spec.features(h1)
    close_t = h1.t + h1.bar_ms
    out = []
    for t in trades:
        k = int(np.searchsorted(close_t, t["t_ms"]))
        ratio = float(f["vol_ratio"][k]) if k < close_t.size and int(close_t[k]) == t["t_ms"] else math.nan
        out.append(
            {"sym": s, "t_ms": t["t_ms"], "day": t["day"], "period": t["period"], "side": t["side"], "r": t["r_net"], "ratio": ratio}
        )
    return out


def fmt(x: float) -> str:
    return f"{x:+.3f}" if math.isfinite(x) else "—"


def summ_row(label: str, rows: list[dict[str, Any]]) -> str:
    if len(rows) < 2:
        return f"| {label} | {len(rows)} | — | — |"
    sm = summarize([r["r"] for r in rows], [r["day"] for r in rows], n_boot=N_BOOT)
    return f"| {label} | {sm.n} | {sm.dates} | {fmt(sm.mean)} [{fmt(sm.lo)}; {fmt(sm.hi)}] |"


def per_asset(trades: list[dict[str, Any]], emit: Any, full: list[str]) -> None:
    by: dict[str, dict[str, list[float]]] = {}
    for t in trades:
        by.setdefault(t["sym"], {"is": [], "oos": []}).setdefault(t["period"], []).append(t["r"])
    stats = []
    for s, d in by.items():
        r = np.asarray(d["is"])
        if r.size >= MIN_N:
            se = float(r.std(ddof=1) / math.sqrt(r.size))
            stats.append((s, r.size, float(r.mean()), se, len(d["oos"]), float(np.mean(d["oos"])) if d["oos"] else math.nan))
    stats.sort(key=lambda x: -x[2])
    m = np.array([x[2] for x in stats])
    w = np.array([1 / x[3] ** 2 if x[3] > 0 else 0.0 for x in stats])
    pooled = float((w * m).sum() / w.sum())
    q = float((w * (m - pooled) ** 2).sum())
    df = len(stats) - 1
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    # Wilson-Hilferty approximation of the chi-square upper tail
    z = ((q / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df)) if df > 0 else math.nan
    p = 1 - NormalDist().cdf(z) if math.isfinite(z) else math.nan
    emit(
        f"- символів з n IS ≥ {MIN_N}: {len(stats)}; Cochran Q = {q:.1f} при df {df}, p ≈ {p:.3f}, I² = {i2:.2f}; зважене середнє {pooled:+.3f}R"
    )
    full.append("| символ | n IS | E[R] IS | SE | n OOS | E[R] OOS |")
    full.append("|---|---|---|---|---|---|")
    full += [f"| {s} | {n} | {mi:+.3f} | {se:.3f} | {no} | {fmt(mo)} |" for s, n, mi, se, no, mo in stats]
    emit(
        "- 5 найкращих / 5 найгірших за IS (символ n E[R] IS → E[R] OOS): "
        + "; ".join(f"{s} {n} {mi:+.3f}→{fmt(mo)}" for s, n, mi, _, _, mo in stats[:5])
    )
    emit("  … " + "; ".join(f"{s} {n} {mi:+.3f}→{fmt(mo)}" for s, n, mi, _, _, mo in stats[-5:]))
    ranked = [x[0] for x in stats]
    oos = [t for t in trades if t["period"] == "oos"]
    for label, top in (("топ-10", set(ranked[:10])), ("верхня чверть", set(ranked[: max(1, len(ranked) // 4)]))):
        emit("| персистентність OOS | n | дат | E[R] [95 % CI] |")
        emit("|---|---|---|---|")
        emit(summ_row(f"{label} за IS", [t for t in oos if t["sym"] in top]))
        emit(summ_row("решта", [t for t in oos if t["sym"] not in top]))


def dvol_flags(path: Path) -> dict[int, bool]:
    """UTC day -> DVOL of the previous day below its trailing 365-day median (at least 180 days of history)."""
    rows = sorted(json.loads(path.read_text()))
    days = [int(t) // DAY_MS for t, _ in rows]
    vals = [float(v) for _, v in rows]
    out: dict[int, bool] = {}
    for i in range(len(rows)):
        hist = vals[max(0, i - 365) : i + 1]
        if len(hist) >= 180:
            out[days[i] + 1] = vals[i] < float(np.median(hist))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--dvol", type=Path, default=Path.home() / ".cache/tradedesk-agents/put_skew/dvol_BTC.json")
    a = ap.parse_args()
    lines: list[str] = []
    full: list[str] = []

    def emit(s: str) -> None:
        print(s, flush=True)
        lines.append(s)

    syms = sorted({x for v in s1.members(a.s1_cache).values() for x in v})
    dv = dvol_flags(a.dvol)
    spec = sieve_specs.load(SPEC)
    t0 = time.time()
    for label, over in RUNS.items():
        params = {**spec.PARAMS, **over}
        jobs = [(s, params, str(a.s1_cache), str(a.data_dir)) for s in syms]
        trades: list[dict[str, Any]] = []
        with Pool(a.workers) as pool:
            for res in pool.imap_unordered(worker, jobs, chunksize=4):
                trades += res
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        emit(f"\n## Прогін {label}: угод {len(trades)} ({time.time() - t0:.0f}s, RSS {rss:.0f} MB)")
        tis = [t for t in trades if t["period"] == "is"]
        emit("| вибірка | n | дат | E[R] [95 % CI] |")
        emit("|---|---|---|---|")
        emit(summ_row("IS (звірка з ситом)", tis))
        emit(summ_row("OOS (звірка з ситом)", [t for t in trades if t["period"] == "oos"]))
        emit("\n### Перевірка за активами (IS)")
        full.append(f"\n## Прогін {label}: усі символи")
        per_asset(trades, emit, full)
        emit("\n### H3 (описово): тертилі σ7 / σ30 у момент входу (межі з IS)")
        rat = np.array([t["ratio"] for t in tis if math.isfinite(t["ratio"])])
        q1, q2 = (float(np.quantile(rat, 1 / 3)), float(np.quantile(rat, 2 / 3))) if rat.size else (math.nan, math.nan)
        emit(f"- межі тертилів: {q1:.3f}, {q2:.3f}")
        emit("| тертиль | період | n | дат | E[R] [95 % CI] |")
        emit("|---|---|---|---|---|")
        for per in ("is", "oos"):
            pt = [t for t in trades if t["period"] == per and math.isfinite(t["ratio"])]
            terc = [0 if t["ratio"] <= q1 else (1 if t["ratio"] <= q2 else 2) for t in pt]
            for j, name in enumerate(("низький", "середній", "високий")):
                emit(summ_row(f"{name} | {per}", [t for t, g in zip(pt, terc, strict=True) if g == j]))
        emit("\n### H3 (описово): BTC DVOL попереднього дня нижче / вище власної медіани за 365 днів")
        emit("| вибірка | n | дат | E[R] [95 % CI] |")
        emit("|---|---|---|---|")
        for sub, keep in (("усі символи", lambda s: True), ("BTC / ETH", lambda s: s in ("BTCUSDT", "ETHUSDT"))):
            for per in ("is", "oos"):
                pt = [t for t in trades if t["period"] == per and keep(t["sym"]) and t["day"] in dv]
                emit(summ_row(f"{sub}, {per}, DVOL нижче медіани", [t for t in pt if dv[t["day"]]]))
                emit(summ_row(f"{sub}, {per}, DVOL вище медіани", [t for t in pt if not dv[t["day"]]]))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(lines + ["\n# Повні таблиці за символами"] + full) + "\n")
    emit(f"\nзаписано: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
