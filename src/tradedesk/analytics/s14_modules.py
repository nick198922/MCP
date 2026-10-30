"""S14 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): pure helpers for the registered modules H10 (half out at
the base target, the rest held until a close of the signal timeframe under its EMA21), H7 (the impulse high breaks a
90-bar channel), H1 (the swept fractal is the lowest low of the whole pullback) and H2 (the sweep zone holds a higher
timeframe level: EMA100 / EMA200, the previous day's / week's low). Registration: the card's section «Попередня
реєстрація модулів S14».

Everything works in the long price space (a short is simulated on mirrored bars, as ``simulate_stop_rule`` does). No
I/O; times are parameters (ms UTC). Every feature depends only on bars closed before the signal bar closes, and the
runner's exit decision at time t only on bars closed by t; the tests check both by changing later data.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import MAX_GAP_MS, Bars, Ind, fractal_lows
from tradedesk.analytics.stop_management import Fees, Outcome, risk_unit

HOUR_MS = 3_600_000


# ---------------------------------------------------------------- signal structure (as the detector sees it)


def swept_fractal(b: Bars, ind: Ind, i: int, k: int) -> int:
    """Index f of the fractal low swept by signal bar ``i`` with impulse high ``k``: the last strict 2 + 2 fractal low
    inside the EMA34 .. EMA13 zone (EMAs of bar f - 1) with f + 2 <= i - 1 and f > k, exactly as
    ``course_s14_ema.find_signals(variant="sweep")``; -1 if there is none. Reads bars <= i - 1 only (a fractal needs
    two later bars, both before i)."""
    if i < 3:
        return -1
    low = b.low[:i]  # bars <= i - 1: the confirmation of a fractal at f <= i - 3 needs no later bar
    fr = fractal_lows(low)
    for f in fr[::-1]:
        f = int(f)
        if f + 2 > i - 1:
            continue
        if f <= k:
            return -1
        if f >= 1 and np.isfinite(ind.e_slow[f - 1]) and ind.e_slow[f - 1] <= b.low[f] <= ind.e_fast[f - 1]:
            return f
    return -1


def channel_breakout(high: np.ndarray, pos: np.ndarray, k: int, w: int) -> bool | None:
    """H7: the impulse high ``high[k]`` is above the highest high of the ``w`` bars before it (k - w .. k - 1); None
    when the segment has fewer than ``w`` bars before k."""
    if k < w or pos[k] < w:
        return None
    return bool(high[k] > float(np.max(high[k - w : k])))


def whole_pullback(low: np.ndarray, k: int, f: int, i: int) -> bool:
    """H1: the swept fractal low ``low[f]`` is at or below every low of bars k .. i - 1 (the lowest low of the whole
    pullback from the impulse high; ties count)."""
    if not (0 <= k <= f < i):
        raise ValueError("need 0 <= k <= f < i")
    return bool(low[f] <= float(np.min(low[k:i])))


def zone_distance(levels: list[float], zone_lo: float, zone_hi: float, atr: float) -> float:
    """Distance (in ATR) from the zone [zone_lo, zone_hi] to the nearest finite level, 0 inside; NaN without a finite
    level or ATR."""
    if not (np.isfinite(atr) and atr > 0):
        return math.nan
    ds = [max(0.0, zone_lo - v, v - zone_hi) for v in levels if np.isfinite(v)]
    return min(ds) / atr if ds else math.nan


def sweep_zone(low_i: float, low_f: float, atr: float, pad_atr: float = 0.5) -> tuple[float, float]:
    """H2 sweep zone: [low of the signal bar - pad ATR, swept fractal low + pad ATR]."""
    return low_i - pad_atr * atr, low_f + pad_atr * atr


# ---------------------------------------------------------------- H10: half at the target, the rest under EMA21


def _bounds(path: Bars, t_in: int, hold_ms: int, max_gap_ms: int) -> int:
    """End index (exclusive) of the 1h bars the trade may use: opening before t_in + hold, cut at the first hole."""
    i0 = int(np.searchsorted(path.t, t_in))
    i1 = int(np.searchsorted(path.t, t_in + hold_ms))
    holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
    return i0 + int(holes[0]) + 1 if holes.size else i1


def _r(entry: float, px: float, ru: float, fees: Fees) -> float:
    return (px - entry) / ru - (fees.maker * abs(entry) + fees.taker_exit * abs(px)) / ru


def runner_after_target(
    path: Bars,
    t_in: int,
    entry: float,
    stop: float,
    hold_ms: int,
    fees: Fees,
    base: Outcome,
    tf_close_ms: np.ndarray,
    tf_close: np.ndarray,
    tf_ema: np.ndarray,
    max_gap_ms: int = MAX_GAP_MS,
) -> tuple[float, int, str]:
    """The H10 runner of a base trade that exited at its target (``base.exit_kind == "target"``): from the next 1h bar
    a break-even stop (entry plus both taker fees, gap at the open), no target; at every close of a signal-timeframe
    bar (``tf_close_ms`` sorted close times, ``tf_close`` / ``tf_ema`` its close and EMA21) at or after the target
    bar's close and within the hold, a close below the EMA exits at that close (taker); otherwise the time exit at the
    last close. Returns (R of the runner in the base risk unit, exit close time, kind: ema | be | be_gap | time)."""
    if base.exit_kind != "target":
        raise ValueError("the runner starts only after the base target")
    ru = risk_unit(entry, stop, fees)
    be = entry + fees.be_offset * abs(entry)
    i1 = _bounds(path, t_in, hold_ms, max_gap_ms)
    end_ms = t_in + hold_ms

    def ema_exit(close_ms: int) -> float | None:
        j = int(np.searchsorted(tf_close_ms, close_ms))
        if j < tf_close_ms.size and int(tf_close_ms[j]) == close_ms and close_ms <= end_ms:
            c, e = float(tf_close[j]), float(tf_ema[j])
            if np.isfinite(e) and c < e:
                return c
        return None

    px = ema_exit(base.exit_ms)
    if px is not None:
        return _r(entry, px, ru, fees), base.exit_ms, "ema"
    k0 = int(np.searchsorted(path.t, base.exit_ms))
    if k0 >= i1:
        j = k0 - 1
        return _r(entry, float(path.c[j]), ru, fees), int(path.t[j]) + path.bar_ms, "time"
    for k in range(k0, i1):
        ce = int(path.t[k]) + path.bar_ms
        if path.o[k] <= be:
            return _r(entry, float(path.o[k]), ru, fees), ce, "be_gap"
        if path.low[k] <= be:
            return _r(entry, be, ru, fees), ce, "be"
        px = ema_exit(ce)
        if px is not None:
            return _r(entry, px, ru, fees), ce, "ema"
    j = i1 - 1
    return _r(entry, float(path.c[j]), ru, fees), int(path.t[j]) + path.bar_ms, "time"


def runner_at_hours(
    path: Bars, t_in: int, entry: float, stop: float, hold_ms: int, fees: Fees, base: Outcome, hours: int, max_gap_ms: int = MAX_GAP_MS
) -> float:
    """H10 placebo: the same runner (break-even stop, no target) but closed at market at the close of the 1h bar
    ``hours`` after the target bar's close (``hours == 0``: at that close), or earlier by the break-even stop or the end
    of the hold."""
    if base.exit_kind != "target":
        raise ValueError("the runner starts only after the base target")
    ru = risk_unit(entry, stop, fees)
    be = entry + fees.be_offset * abs(entry)
    i1 = _bounds(path, t_in, hold_ms, max_gap_ms)
    k0 = int(np.searchsorted(path.t, base.exit_ms))
    if hours <= 0 or k0 >= i1:
        return _r(entry, float(path.c[k0 - 1]), ru, fees)
    last = min(i1, k0 + hours) - 1
    for k in range(k0, last + 1):
        if path.o[k] <= be:
            return _r(entry, float(path.o[k]), ru, fees)
        if path.low[k] <= be:
            return _r(entry, be, ru, fees)
    return _r(entry, float(path.c[last]), ru, fees)


def runner_curve(
    path: Bars, t_in: int, entry: float, stop: float, hold_ms: int, fees: Fees, base: Outcome, max_gap_ms: int = MAX_GAP_MS
) -> np.ndarray:
    """``runner_at_hours`` for hours 0, 1, ... up to the end of the hold (one pass): element h is the placebo runner's R
    when told to close ``h`` hours after the target bar's close; later hours equal the last element."""
    if base.exit_kind != "target":
        raise ValueError("the runner starts only after the base target")
    ru = risk_unit(entry, stop, fees)
    be = entry + fees.be_offset * abs(entry)
    i1 = _bounds(path, t_in, hold_ms, max_gap_ms)
    k0 = int(np.searchsorted(path.t, base.exit_ms))
    out = [_r(entry, float(path.c[k0 - 1]), ru, fees)]
    for k in range(k0, i1):
        if path.o[k] <= be:
            out.append(_r(entry, float(path.o[k]), ru, fees))
            break
        if path.low[k] <= be:
            out.append(_r(entry, be, ru, fees))
            break
        out.append(_r(entry, float(path.c[k]), ru, fees))
    return np.asarray(out)


def half_and_runner(base_r: float, runner_r: float) -> float:
    """R of the H10 position: half the base trade, half the runner, same risk unit."""
    return 0.5 * base_r + 0.5 * runner_r


def quantile_hours(durations: np.ndarray, u: float) -> int:
    """The ``u``-quantile (lower) of the runner durations in whole hours (the placebo's random exit moment)."""
    d = np.sort(np.asarray(durations, dtype=float))
    if d.size == 0:
        raise ValueError("no durations")
    return int(d[min(d.size - 1, int(math.floor(u * d.size)))])
