"""S38 modules from the weekly audit of 2026-11-03 (IDEA-20261030-demo-setups-depth, «Покращення»): pure helpers for
T (touches of the range low before the breakout), D (a "dry" retest: quote volume of the retest bar over that of the
breakout bar) and E (a sell-stop entry at the retest bar's low instead of the entry at its close), plus the E placebo
(a market entry after a random delay of the same frequency). Registration: the card's section «Попередня реєстрація
хвилі S38 з аудиту 03.11».

Features are computed on real (short-side) prices and read only bars closed at or before the entry time ``t_in``
(T reads bars before the breakout bar s only). Trade simulations work in the long price space on mirrored 1h bars, as
``simulate_stop_rule`` does. No I/O; times are ms UTC.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.s3_modules import unit_draw
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit, simulate_stop_rule

HOUR_MS = 3_600_000
TICK_FRAC = 1e-4  # "one tick" below the retest low: 1 bp (no historical tick sizes)


# ---------------------------------------------------------------- features (real prices, short side)


def range_touches(b: Bars, s: int, level: float, atr_prev: float, w: int = 30, touch_atr: float = 0.25) -> int | None:
    """T: number of bars in the range window [s - w, s - 1] whose low is within ``touch_atr`` * ATR of the range low
    ``level`` (the bar that sets the low counts; adjacent bars count separately). ``atr_prev`` is ATR14 of bar s - 1.
    Reads bars before s only. None without an ATR or with fewer than ``w`` bars before s."""
    if not (np.isfinite(atr_prev) and atr_prev > 0) or s < w:
        return None
    lows = b.low[s - w : s]
    return int(np.sum(lows <= level + touch_atr * atr_prev))


def bar_quote_volume(t_1h: np.ndarray, qv_1h: np.ndarray, t0: int, bar_ms: int) -> float | None:
    """Quote volume of the bar opening at ``t0`` of length ``bar_ms``: the sum of the 1h volumes inside it. None unless
    every hour of the bar is present."""
    i0 = int(np.searchsorted(t_1h, t0))
    i1 = int(np.searchsorted(t_1h, t0 + bar_ms))
    if i1 - i0 != bar_ms // HOUR_MS:
        return None
    return float(np.sum(qv_1h[i0:i1]))


def dry_ratio(qv_retest: float | None, qv_breakout: float | None) -> float | None:
    """D: volume of the retest bar over the volume of the breakout bar; None if either is missing or the breakout
    volume is not positive."""
    if qv_retest is None or qv_breakout is None or not qv_breakout > 0:
        return None
    return float(qv_retest / qv_breakout)


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation with average ranks for ties (finite pairs only); NaN with < 3 pairs."""
    a, c = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    ok = np.isfinite(a) & np.isfinite(c)
    a, c = a[ok], c[ok]
    if a.size < 3:
        return math.nan
    ra, rc = _ranks(a), _ranks(c)
    sa, sc = ra.std(), rc.std()
    if sa == 0 or sc == 0:
        return math.nan
    return float(np.mean((ra - ra.mean()) * (rc - rc.mean())) / (sa * sc))


def _ranks(v: np.ndarray) -> np.ndarray:
    order = np.argsort(v, kind="stable")
    r = np.empty(v.size)
    r[order] = np.arange(1, v.size + 1, dtype=float)
    _, inv, cnt = np.unique(v, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=r)
    return sums[inv] / cnt[inv]


# ---------------------------------------------------------------- E: stop entry (long space, mirrored 1h path)


@dataclass(frozen=True)
class StopEntry:
    status: str  # filled | cancel_stop | no_fill | no_path
    fill_ms: int  # open time of the fill hour, -1 if not filled
    entry: float  # fill price (long space), NaN if not filled
    outcome: Outcome | None

    @property
    def r_net(self) -> float:
        """R per signal: 0 for a cancelled / unfilled order, NaN without a path after the fill."""
        if self.status in ("cancel_stop", "no_fill"):
            return 0.0
        return math.nan if self.outcome is None else self.outcome.r_net


def _taker_entry(o: Outcome | None, entry: float, stop: float, fees: Fees) -> Outcome | None:
    """The outcome with a taker (+ slippage) entry fee instead of the maker one."""
    if o is None:
        return None
    extra = (fees.taker_exit - fees.maker) * abs(entry) / risk_unit(entry, stop, fees)
    return Outcome(o.exit, o.exit_kind, o.moved, o.move_ms, o.r_gross, o.cost_r + extra, o.mfe_r, o.entry_ms, o.exit_ms)


def stop_entry_trade(
    path: Bars,
    t_in: int,
    trigger: float,
    stop: float,
    wait_ms: int,
    hold_ms: int,
    rule: StopRule,
    fees: Fees,
    target_r: float = 2.0,
) -> StopEntry:
    """A buy-stop at ``trigger`` (long space; the short's sell-stop below the retest low) resting over the 1h bars
    opening in [t_in, t_in + wait_ms). Before the fill, a bar reaching ``stop`` cancels the order. Fill at ``trigger``
    or at the bar's open if it opened beyond it (gap); taker entry fee. Target ``target_r`` R from the fill. The fill
    hour is pessimistic (as ``setup_passport.limit_trade``): if it also reaches the stop the trade is a loss at the stop;
    otherwise the trade runs from the next hour (a target / break-even trigger in the fill hour is ignored), held
    ``hold_ms`` from the fill hour. Uses bars from t_in up to the exit only."""
    if not trigger > stop:
        return StopEntry("no_path", -1, math.nan, None)
    i0 = int(np.searchsorted(path.t, t_in))
    i1 = int(np.searchsorted(path.t, t_in + wait_ms))
    for k in range(i0, i1):
        hit = path.h[k] >= trigger
        if not hit:
            if path.low[k] <= stop:
                return StopEntry("cancel_stop", -1, math.nan, None)
            continue
        if path.o[k] <= stop:  # opened through the stop before any fill
            return StopEntry("cancel_stop", -1, math.nan, None)
        fill = float(max(trigger, path.o[k]))
        tk = int(path.t[k])
        target = fill + target_r * (fill - stop)
        if path.low[k] <= stop or k + 1 >= len(path):
            o = simulate_stop_rule(path, tk, fill, stop, target, hold_ms, rule, fees=fees)
        else:
            t_next = int(path.t[k + 1])
            o = simulate_stop_rule(path, t_next, fill, stop, target, hold_ms - (t_next - tk), rule, fees=fees)
        return StopEntry("filled", tk, fill, _taker_entry(o, fill, stop, fees))
    if i1 == i0:
        return StopEntry("no_path", -1, math.nan, None)
    return StopEntry("no_fill", -1, math.nan, None)


def delayed_market_trade(
    path: Bars, t_in: int, delay_h: int, stop: float, hold_ms: int, rule: StopRule, fees: Fees, target_r: float = 2.0
) -> float:
    """E placebo: a market (taker) entry at the open of the 1h bar opening at t_in + delay_h hours, stop ``stop``,
    target ``target_r`` R from the entry. 0 if a bar before it reached the stop or the open is already through the stop
    (cancelled); NaN if that bar is missing."""
    t = t_in + delay_h * HOUR_MS
    i0 = int(np.searchsorted(path.t, t_in))
    k = int(np.searchsorted(path.t, t))
    if k >= len(path) or int(path.t[k]) != t:
        return math.nan
    if k > i0 and float(np.min(path.low[i0:k])) <= stop:
        return 0.0
    e = float(path.o[k])
    if e <= stop:
        return 0.0
    o = simulate_stop_rule(path, t, e, stop, e + target_r * (e - stop), hold_ms, rule, fees=fees)
    o = _taker_entry(o, e, stop, fees)
    return math.nan if o is None else float(o.r_net)


def placebo_delay(r_by_delay: Sequence[float | None], fill_share: float, delay_probs: Sequence[float], key: str, n_draw: int = 20) -> float:
    """Mean R over ``n_draw`` deterministic draws (sha256 of ``key``): filled with probability ``fill_share`` at a
    delay drawn from ``delay_probs`` (index = hours after t_in), else 0. Draws landing on a missing delay are skipped;
    NaN if none is usable."""
    p = np.asarray(delay_probs, dtype=float)
    cdf = np.cumsum(p / p.sum())
    vals: list[float] = []
    for d in range(n_draw):
        if unit_draw(key, f"e_fill|{d}") >= fill_share:
            vals.append(0.0)
            continue
        h = min(int(np.searchsorted(cdf, unit_draw(key, f"e_delay|{d}"), side="right")), len(cdf) - 1)
        v = r_by_delay[h] if h < len(r_by_delay) else None
        if v is not None and np.isfinite(v):
            vals.append(float(v))
    return float(np.mean(vals)) if vals else math.nan
