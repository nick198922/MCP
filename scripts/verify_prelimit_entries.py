#!/usr/bin/env python3
"""Owner 26.10: enter S12 / S38 with a limit resting at the level in advance instead of at the close of the bar that
came back to it (the demo's rule), so a move that never comes back to the close is not missed. Backtest of both.

Variants (pre-registered in docs/verification/2026-10-26-prelimit-entries.md before the run):

- S12 (FVG first retest with the daily trend, 4h, both sides; ``setup_signals.s12_all``):
  ``close`` the demo: entry at the close of the first bar whose low reaches the gap's middle (no entry when that bar is
  already at the stop or closes at the target); ``lim_mid`` a limit at the middle from the gap's close for 20 bars;
  ``lim_near`` a limit at the gap's near edge, same stop (middle - 1 ATR), target 2R from the edge.
- S38 (range breakdown + retest, 4h, shorts; ``short_setups.breakout_retest``): ``close`` the demo: entry at the close
  of the retest bar back below the level, stop 0.5 ATR over its high; ``lim_touch`` a sell limit at level - 0.25 ATR
  from the breakdown close for 6 bars, stop level + 0.5 ATR; ``lim_level`` a sell limit at the broken level, same stop.

Everything else as ``verify_short_setups.py`` (the S38 step-1 backtest): point-in-time top-30, 1h paths, exit B
(break-even at half the way, the demo's), 7 days, the guard (stop <= 10 %, R:R after fees >= 1.5), one trade at a
time per symbol, setup, side and variant. A limit fill is conservative: the 1h bar that fills it and reaches the stop
is a loss; otherwise the trade runs from the next 1h bar (a target or a break-even trigger in the fill bar is ignored).

    uv run python scripts/verify_prelimit_entries.py --stage run      # per-symbol trade files, worker processes
    uv run python scripts/verify_prelimit_entries.py --stage report   # tables (also <cache>/report.txt)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (loaders, universe, period)
import verify_demo_stop_management as dsm  # noqa: E402  (day-cluster bootstrap)
import verify_short_setups as vss  # noqa: E402  (guard, months, exit rule)

from tradedesk.analytics.course_fvg_ob import fvg_zones  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.setup_signals import (  # noqa: E402
    RANGE_W,
    RANGE_WIDTH_ATR,
    S12_STOP_ATR,
    S12_TARGET_R,
    S12_TREND_EMA,
    S12_WAIT,
    S12_WARMUP,
    S38_TOUCH_ATR,
    SHORT_STOP_ATR,
    SHORT_TARGET_R,
    SHORT_WAIT,
    _first_test,
    align_daily,
    daily_bars,
    ema,
)
from tradedesk.analytics.short_setups import breakout_retest  # noqa: E402
from tradedesk.analytics.stop_management import simulate_stop_rule  # noqa: E402

DAY_MS = 24 * HOUR_MS
TF_MS = 4 * HOUR_MS
HOLD_MS = 7 * DAY_MS
START_MS, END_MS, OOS_MS = s1.START_MS, s1.END_MS, s1.OOS_MS
VARIANTS = {"S12": ("close", "lim_mid", "lim_near"), "S38": ("close", "lim_touch", "lim_level")}
OUT: dict[str, Any] = {}
THROUGH = [0.0]  # robustness (not registered): a limit fills only when the low trades this fraction through it


def emit(s: str = "") -> None:
    print(s, flush=True)
    if OUT.get("fh"):
        OUT["fh"].write(s + "\n")


# ---------------------------------------------------------------- candidates (long form; shorts on mirrored bars)
# a candidate: (variant, kind 'close' | 'limit', t0 ms, t1 ms, price, stop, target, bar index for the universe)
# 'close': entry at price at t0; 'limit': resting from t0 until t1


def s12_candidates(bb: Bars) -> list[tuple[str, str, int, int, float, float, float, int]]:
    pos = segment_pos(bb.t)
    a_arr = atr(bb, 14, pos)
    d = daily_bars(bb)
    e_d = ema(d.c, S12_TREND_EMA, segment_pos(d.t))
    with np.errstate(invalid="ignore"):
        up_d = (d.c > e_d).astype(float)
    htf_up = align_daily(bb, d, up_d) > 0.5
    out = []
    best: dict[int, tuple[int, float, float, float]] = {}
    for z in fvg_zones(bb, pos, a_arr, htf_up, lo=2, hi=len(bb) - 1):
        i = z.form
        if "trend" not in z.arms or pos[i] < S12_WARMUP:
            continue
        mid, near = z.levels["mid"], z.levels["near"]
        stop = mid - S12_STOP_ATR * z.atr
        t_place = bb.close_time(i)
        t_end = t_place + S12_WAIT * TF_MS
        out.append(("lim_mid", "limit", t_place, t_end, mid, stop, mid + S12_TARGET_R * (mid - stop), i))
        out.append(("lim_near", "limit", t_place, t_end, near, stop, near + S12_TARGET_R * (near - stop), i))
        j = _first_test(bb, pos, i, mid)  # the demo's signal (setup_signals.s12_all)
        target = mid + S12_TARGET_R * (mid - stop)
        if j >= 0 and bb.low[j] > stop and bb.c[j] < target and (j not in best or i > best[j][0]):
            best[j] = (i, float(bb.c[j]), stop, target)
    for j, (_, e, stop, target) in best.items():
        t = bb.close_time(j)
        out.append(("close", "close", t, t, e, stop, target, j))
    return out


def s38_candidates(bb: Bars) -> list[tuple[str, str, int, int, float, float, float, int]]:
    """Long form of the shorts (``bb`` mirrored): a close above the range high, then the retest."""
    pos = segment_pos(bb.t)
    a = atr(bb, 14, pos)
    out = []
    kw = dict(w=RANGE_W, width_atr=RANGE_WIDTH_ATR, max_wait=SHORT_WAIT, touch_atr=S38_TOUCH_ATR, stop_atr=SHORT_STOP_ATR)
    for sg in breakout_retest(bb, pos, a, target_r=SHORT_TARGET_R, **kw):
        t = bb.close_time(sg.i)
        out.append(("close", "close", t, t, sg.entry, sg.stop, sg.target, sg.i))
    resolved = -1  # the breakouts themselves (breakout_retest's first loop), one per wait window
    for s in range(RANGE_W, len(bb)):
        if s <= resolved or pos[s] < RANGE_W:
            continue
        a0 = float(a[s - 1])
        if not (np.isfinite(a0) and a0 > 0):
            continue
        lo, hi = float(bb.low[s - RANGE_W : s].min()), float(bb.h[s - RANGE_W : s].max())
        if hi - lo > RANGE_WIDTH_ATR * a0 or not bb.c[s] > hi:
            continue
        resolved = s + SHORT_WAIT
        a_s = float(a[s])
        if not (np.isfinite(a_s) and a_s > 0):
            continue
        t0 = bb.close_time(s)
        t1 = t0 + SHORT_WAIT * TF_MS
        stop = hi - SHORT_STOP_ATR * a_s
        for var, px in (("lim_touch", hi + S38_TOUCH_ATR * a_s), ("lim_level", hi)):
            if px > stop:
                out.append((var, "limit", t0, t1, px, stop, px + SHORT_TARGET_R * (px - stop), s))
    return out


def fill_bar(path: Bars, t0: int, t1: int, px: float) -> int:
    """First 1h bar opening in [t0, t1) whose low reaches the buy limit ``px``; -1 if none."""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t1))
    hit = np.flatnonzero(path.low[i0:i1] <= px - THROUGH[0] * abs(px))
    return i0 + int(hit[0]) if hit.size else -1


def simulate(path: Bars, kind: str, t0: int, t1: int, px: float, stop: float, target: float) -> tuple[Any, int]:
    """(Outcome or None, entry time ms)."""
    if kind == "close":
        return simulate_stop_rule(path, t0, px, stop, target, HOLD_MS, vss.EXIT_B), t0
    k = fill_bar(path, t0, t1, px)
    if k < 0:
        return None, -1
    tk = int(path.t[k])
    if path.low[k] <= stop or k + 1 >= len(path):  # filled and stopped in the same hour (the stop is below the limit)
        return simulate_stop_rule(path, tk, px, stop, target, HOLD_MS, vss.EXIT_B), tk
    return simulate_stop_rule(path, int(path.t[k + 1]), px, stop, target, HOLD_MS - HOUR_MS, vss.EXIT_B), tk


def run_symbol(args: tuple[str, str, str, str]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, through = args
    THROUGH[0] = through
    s1c, dd = Path(s1_cache), Path(data_dir)
    member_months = {vss.mo_int(mo) for mo, v in s1.members(s1c).items() if s in v}
    h1 = s1.load_1h(s1c, dd, s)
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    tb = resample(h1, 4)
    if len(tb) < 120:
        return {"sym": s, "n": 0, "why": {}}
    mem = np.isin(vss.month_idx(tb.t + tb.bar_ms), list(member_months))
    for setup, sides in (("S12", ("long", "short")), ("S38", ("short",))):
        for side in sides:
            bb = tb if side == "long" else tb.mirrored()
            path = h1 if side == "long" else h1.mirrored()
            cands = s12_candidates(bb) if setup == "S12" else s38_candidates(bb)
            for var in VARIANTS[setup]:
                busy = -1
                for _, kind, t0, t1, px, stop, target, bi in sorted((c for c in cands if c[0] == var), key=lambda c: c[2]):
                    if not (START_MS <= t0 < END_MS) or not mem[bi]:
                        continue
                    key = f"{setup}:{side}:{var}"
                    ok, stop_pct, rr = vss.guard_ok(side, px, stop, target)
                    if not ok:
                        why[key + ":guard"] += 1
                        continue
                    if t0 < busy:  # a trade of this variant is open: the limit is not placed / the signal skipped
                        why[key + ":busy"] += 1
                        continue
                    why[key + ":placed"] += 1
                    o, t_in = simulate(path, kind, t0, t1, px, stop, target)
                    if t_in < 0:
                        why[key + ":unfilled"] += 1
                        continue
                    if o is None:
                        why[key + ":no_path"] += 1
                        continue
                    busy = o.exit_ms
                    why[key + ":trade"] += 1
                    rows.append(
                        {
                            "sym": s,
                            "setup": setup,
                            "side": side,
                            "var": var,
                            "entry_ms": t_in,
                            "day": t_in // DAY_MS,
                            "oos": t_in >= OOS_MS,
                            "stop_pct": stop_pct,
                            "r": o.r_net,
                            "k": o.exit_kind,
                        }
                    )
    (Path(out_dir) / "trades").mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "trades" / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why)}))
    return {"sym": s, "n": len(rows), "why": dict(why)}


def stage_run(a: argparse.Namespace) -> None:
    syms = vss.all_syms(a.s1_cache)[: a.limit] if a.limit else vss.all_syms(a.s1_cache)
    jobs = [(s, str(a.s1_cache), str(a.data_dir), str(a.cache_dir), a.through) for s in syms]
    with Pool(a.workers) as p:
        for r in p.imap_unordered(run_symbol, jobs):
            emit(f"{r['sym']}: {r['n']} trades")


def stage_report(cache: Path) -> None:
    rows, why = [], Counter()
    for p in sorted((cache / "trades").glob("*.json")):
        d = json.loads(p.read_text())
        rows += d["rows"]
        why.update(d["why"])
    if not rows:
        emit("no trades")
        return
    T = {k: np.asarray([r[k] for r in rows]) for k in rows[0]}
    years = (END_MS - START_MS) / (365.25 * DAY_MS)
    emit(f"trades {len(rows)}; symbols {np.unique(T['sym']).size}; {years:.1f} years")
    hdr = "| варіант | бік | період | n | дат | E[R] net [95 % CI] | частка R>0 | R за рік (сума) | заповнено лімітів |"
    for setup, sides in (("S12", ("long", "short", "all")), ("S38", ("short",))):
        emit(f"\n## {setup}\n\n{hdr}\n|---|---|---|---|---|---|---|---|---|")
        for side in sides:
            for var in VARIANTS[setup]:
                m0 = (T["setup"] == setup) & (T["var"] == var) & ((T["side"] == side) if side != "all" else True)
                sides_k = ("long", "short") if side == "all" else (side,)
                placed = sum(why.get(f"{setup}:{sd}:{var}:placed", 0) for sd in sides_k)
                filled = sum(why.get(f"{setup}:{sd}:{var}:trade", 0) + why.get(f"{setup}:{sd}:{var}:no_path", 0) for sd in sides_k)
                fill = f"{filled / placed:.0%} з {placed}" if var != "close" and placed else "—"
                for lab, mp in (("усі", np.ones_like(m0)), ("IS", ~T["oos"].astype(bool)), ("OOS", T["oos"].astype(bool))):
                    m = m0 & mp
                    v, day = T["r"][m].astype(float), T["day"][m]
                    x = dsm.summ(v, day)
                    yrs = years if lab == "усі" else ((OOS_MS - START_MS) if lab == "IS" else (END_MS - OOS_MS)) / (365.25 * DAY_MS)
                    ci = dsm.fs(x) if x["n"] >= 2 else "n/a"
                    wr = f"{np.mean(v > 0):.0%}" if v.size else "—"
                    emit(
                        f"| {var} | {side} | {lab} | {x['n']} | {x['d']} | {ci} | {wr} | {v.sum() / yrs:+.1f} | {fill if lab == 'усі' else ''} |"
                    )
    emit(
        "\nexit kinds: "
        + ", ".join(f"{s}:{v}:{dict(Counter(T['k'][(T['setup'] == s) & (T['var'] == v)]))}" for s in VARIANTS for v in VARIANTS[s])
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/prelimit_entries")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--through", type=float, default=0.0, help="robustness: fill only when the low trades this fraction through")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    with (a.cache_dir / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        stage_run(a) if a.stage == "run" else stage_report(a.cache_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
