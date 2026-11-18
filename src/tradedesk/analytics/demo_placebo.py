"""Demo trade vs a placebo on the same candle (IDEA-20261030-demo-setups-depth, «S38 / S39 / S40 проти плацебо тієї
самої свічки»): pure helpers, no I/O; times are ms UTC and passed in.

A closed demo trade of a range setup (S38 / S39 short, S40 long) is set against K placebo trades: random top-100 coins
(``s38_modules.draw_coins``, signal coin excluded) entered on the close of the same signal-timeframe candle, on the same
side, with the stop at the same distance in ATR(14, Wilder) as the demo's planned stop, the target at ``target_r`` R,
the same fee model, break-even rule and maximum hold. Δ = demo R − mean placebo R. The demo's own R comes from the
venue (``demo_trade.r_multiple``); the placebo R is simulated (``stop_management.simulate_stop_rule``) on 1h candles.

Causality: the ATR and the placebo entry read only bars that open at or before the signal bar; the outcome reads only
1h bars from the signal-bar close to the exit. Nothing after a trade's exit changes its R.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, simulate_stop_rule

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
ATR_N = 14
Z80 = 1.96 + 0.84  # MDE at 80 % power, two-sided 5 %

__all__ = [
    "ATR_N",
    "PlaceboOutcome",
    "atr_at",
    "stop_atr_mult",
    "placebo_levels",
    "simulate_side",
    "placebo_outcome",
    "trade_delta",
    "summarize",
    "verdict",
]


def atr_at(bars: Bars, signal_ts: int, n: int = ATR_N) -> float | None:
    """Wilder ATR(``n``) at the bar opening at ``signal_ts`` from bars that open at or before it (gaps restart the
    segment, as the live detectors do); None when the bar is absent or the ATR is not yet seeded."""
    j = int(np.searchsorted(bars.t, signal_ts, side="right"))
    if j == 0 or int(bars.t[j - 1]) != signal_ts:
        return None
    b = bars.slice(0, j)
    a = atr(b, n, segment_pos(b.t))
    v = float(a[-1]) if a.size else math.nan
    return v if math.isfinite(v) and v > 0 else None


def stop_atr_mult(entry: float, stop: float, atr_value: float) -> float | None:
    """|entry − stop| in ATR units; None for a non-positive ATR or distance."""
    d = abs(entry - stop)
    if not (atr_value > 0) or not (d > 0) or not math.isfinite(d / atr_value):
        return None
    return d / atr_value


def placebo_levels(side: str, entry: float, k_atr: float, atr_value: float, target_r: float = 2.0) -> tuple[float, float, float]:
    """(entry, stop, target) of a placebo: stop ``k_atr`` ATR away against the side, target ``target_r`` R in favour."""
    d = k_atr * atr_value
    if side == "long":
        return entry, entry - d, entry + target_r * d
    if side == "short":
        return entry, entry + d, entry - target_r * d
    raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def simulate_side(
    side: str,
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    now_ms: int,
    fees: Fees = Fees(),
    max_gap_ms: int = HOUR_MS,
) -> Outcome | None:
    """``simulate_stop_rule`` in real prices (a short runs on mirrored bars) over 1h bars closed by ``now_ms``."""
    keep = path.t + path.bar_ms <= now_ms
    p = Bars(path.t[keep], path.o[keep], path.h[keep], path.low[keep], path.c[keep], path.bar_ms)
    if side == "long":
        return simulate_stop_rule(p, entry_ms, entry, stop, target, hold_ms, rule, fees=fees, max_gap_ms=max_gap_ms)
    return simulate_stop_rule(p.mirrored(), entry_ms, -entry, -stop, -target, hold_ms, rule, fees=fees, max_gap_ms=max_gap_ms)


@dataclass(frozen=True)
class PlaceboOutcome:
    r: float | None  # net R (None: missing)
    status: str  # ok | open (horizon not passed by now: marked at the last closed 1h bar) | no_bar | no_atr | no_path | gap
    exit_kind: str | None = None


def placebo_outcome(
    side: str,
    bars_tf: Bars,
    path_1h: Bars,
    signal_ts: int,
    k_atr: float,
    hold_ms: int,
    rule: StopRule,
    now_ms: int,
    *,
    target_r: float = 2.0,
    fees: Fees = Fees(),
) -> PlaceboOutcome:
    """R of one placebo coin: entry on the close of its bar opening at ``signal_ts`` (``bars_tf``), stop ``k_atr`` of
    its own ATR, target ``target_r`` R, path ``path_1h`` from the bar close. A path that ends before the horizon
    while the horizon has passed (a hole or a delisting) is ``gap`` (missing); one whose horizon is still ahead of
    ``now_ms`` is ``open`` and keeps its mark-to-market R."""
    j = int(np.searchsorted(bars_tf.t, signal_ts, side="right"))
    if j == 0 or int(bars_tf.t[j - 1]) != signal_ts or int(bars_tf.t[j - 1]) + bars_tf.bar_ms > now_ms:
        return PlaceboOutcome(None, "no_bar")
    a = atr_at(bars_tf, signal_ts)
    if a is None:
        return PlaceboOutcome(None, "no_atr")
    entry_ms = signal_ts + bars_tf.bar_ms
    e, st, tg = placebo_levels(side, float(bars_tf.c[j - 1]), k_atr, a, target_r)
    out = simulate_side(side, path_1h, entry_ms, e, st, tg, hold_ms, rule, now_ms, fees)
    if out is None:
        return PlaceboOutcome(None, "no_path")
    if out.exit_kind == "data_end":
        if entry_ms + hold_ms <= now_ms:
            return PlaceboOutcome(None, "gap", out.exit_kind)
        return PlaceboOutcome(out.r_net, "open", out.exit_kind)
    return PlaceboOutcome(out.r_net, "ok", out.exit_kind)


def trade_delta(r_demo: float | None, placebo_r: Sequence[float | None]) -> tuple[float | None, int]:
    """(demo R − mean of the available placebo R, number of placebos used); None without the demo R or any placebo."""
    got = [float(x) for x in placebo_r if x is not None and math.isfinite(float(x))]
    if r_demo is None or not math.isfinite(float(r_demo)) or not got:
        return None, len(got)
    return float(r_demo) - sum(got) / len(got), len(got)


def summarize(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261030) -> dict[str, Any]:
    """n, clusters, mean, SD, cluster-bootstrap (by ``clusters``, e.g. the signal day) 95 % CI and SE, MDE80 = 2.8 SE,
    one-sided bootstrap p for mean > 0 and for mean < 0. Statistics are None with fewer than 2 clusters."""
    x = np.asarray(values, dtype=float)
    n, k = int(x.size), len(set(clusters))
    out: dict[str, Any] = {"n": n, "clusters": k, "mean": float(x.mean()) if n else None, "sd": float(x.std(ddof=1)) if n > 1 else None}
    out.update(ci=None, se=None, mde80=None, p_gt0=None, p_lt0=None)
    if n and k >= 2:
        b = cluster_bootstrap_means(list(x), list(clusters), n_boot=n_boot, seed=seed)
        se = float(np.std(b, ddof=1))
        out.update(
            ci=[float(np.quantile(b, 0.025)), float(np.quantile(b, 0.975))],
            se=se,
            mde80=Z80 * se,
            p_gt0=float((np.sum(b <= 0) + 1) / (b.size + 1)),
            p_lt0=float((np.sum(b >= 0) + 1) / (b.size + 1)),
        )
    return out


def verdict(s: dict[str, Any], *, min_n: int = 30, mde_max: float = 0.30, band: float = 0.30, p_holm: float | None = None) -> str:
    """Registered rule. n < ``min_n`` -> 'descriptive'; MDE80 > ``mde_max`` (or no CI) -> '◯'; CI lower bound > 0 and
    (when given) the Holm-adjusted one-sided p < 0.05 -> '✓'; CI upper bound < 0 -> '✗'; CI inside (−band, +band) ->
    '≈'; otherwise '◯'."""
    if s.get("n", 0) < min_n:
        return "descriptive"
    ci, mde = s.get("ci"), s.get("mde80")
    if ci is None or mde is None or mde > mde_max:
        return "◯"
    lo, hi = ci
    if lo > 0 and (p_holm is None or p_holm < 0.05):
        return "✓"
    if hi < 0:
        return "✗"
    if -band < lo and hi < band:
        return "≈"
    return "◯"
