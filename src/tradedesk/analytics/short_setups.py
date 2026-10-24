"""Trend-continuation setups for the demo trader (IDEA-20261024-short-setups-demo): A, a breakout of the S3 range and a
retest of the broken edge; B, a failed S3 sweep (the opposite S3 trade is stopped out) turned into a trade the other way.

Pure functions, no I/O, time enters only through the bars. Every detector is written for the **long** side on the bars
it gets; a short is the same detector on mirrored (negated) prices (``Bars.mirrored``), as in ``course_basics`` and
``stop_management``. In the long form:

* A: bar s closes above the high H of the 30-bar range before it (width <= ``width_atr`` * ATR[s - 1]); the first bar
  j in s+1 .. s+``max_wait`` (no hole) whose low is at or below H + ``touch_atr`` * ATR[j] decides: close[j] > H is a
  long at close[j], anything else is a failed breakout. Stop low[j] - ``stop_atr`` * ATR[j], target ``target_r`` R.
  While a breakout is pending, new breakouts of the same series are ignored.
* B: an S3 short (``range_events`` on the mirrored series: a one-bar sweep of the range high, close back inside) whose
  stop (sweep high + 0.5 ATR[r]) is closed beyond by the first bar j in r+1 .. r+``max_wait`` (no hole), while no bar in
  r+1 .. j-1 reached the S3 target (the range middle). Long at close[j], stop min(low[r .. j]) - ``stop_atr`` * ATR[j].

Each signal decided at bar j depends on bars <= j only. Prices stay in the bars' units; ``target_r`` is a multiple of
the price risk entry - stop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_s14_ema import Bars

S3_STOP_ATR = 0.5  # the live S3 stop buffer (setup_signals.STOP_ATR)


@dataclass(frozen=True)
class Signal:
    kind: str  # "A" | "B"
    start: int  # breakout bar (A) or the S3 sweep bar (B)
    i: int  # decision bar: the entry is its close
    level: float  # broken range edge (A) or the S3 stop that was closed beyond (B)
    entry: float
    stop: float
    target: float
    alt_target: float  # A: the liquidity target (extreme of the bars before the range window), nan if none / too close
    atr: float  # ATR at the decision bar


def _range(b: Bars, s: int, w: int) -> tuple[float, float]:
    return float(b.low[s - w : s].min()), float(b.h[s - w : s].max())


def breakout_retest(
    b: Bars,
    pos: np.ndarray,
    atr: np.ndarray,
    *,
    w: int = 30,
    width_atr: float = 6.0,
    max_wait: int = 6,
    touch_atr: float = 0.25,
    stop_atr: float = 0.5,
    target_r: float = 2.0,
    liq_w: int = 30,
    liq_min_r: float = 1.0,
) -> list[Signal]:
    """Detector A in the long form (see the module docstring)."""
    n = len(b)
    out: list[Signal] = []
    resolved = -1  # bar where the last breakout was decided (entry, failure or the end of the wait)
    for s in range(w, n):
        if s <= resolved or pos[s] < w:
            continue
        a0 = float(atr[s - 1])
        if not (np.isfinite(a0) and a0 > 0):
            continue
        lo, hi = _range(b, s, w)
        if hi - lo > width_atr * a0 or not b.c[s] > hi:
            continue
        resolved = s
        for j in range(s + 1, min(n, s + max_wait + 1)):
            if pos[j] != pos[s] + (j - s):
                break
            resolved = j
            aj = float(atr[j])
            if not (np.isfinite(aj) and aj > 0):
                continue
            if b.low[j] > hi + touch_atr * aj:
                continue
            if b.c[j] > hi:
                entry = float(b.c[j])
                stop = float(b.low[j]) - stop_atr * aj
                risk = entry - stop
                if risk > 0:
                    alt = math.nan
                    k0 = s - w - liq_w
                    if k0 >= 0 and pos[s] >= w + liq_w:
                        liq = float(b.h[k0 : s - w].max())
                        if liq >= entry + liq_min_r * risk:
                            alt = liq
                    out.append(Signal("A", s, j, hi, entry, stop, entry + target_r * risk, alt, aj))
            break
    return out


def failed_sweep(
    b: Bars,
    pos: np.ndarray,
    atr: np.ndarray,
    *,
    w: int = 30,
    width_atr: float = 6.0,
    max_wait: int = 6,
    stop_atr: float = 0.5,
    target_r: float = 2.0,
) -> list[Signal]:
    """Detector B in the long form: the S3 shorts of ``b`` (S3 longs of the mirrored series) that fail upwards."""
    m = b.mirrored()
    n = len(b)
    out: list[Signal] = []
    used: set[int] = set()
    for ev in range_events(m, pos, atr, w=w, width_atr=width_atr):
        if ev.kind != "sweep":
            continue
        r = ev.r
        ar = float(atr[r])
        if not (np.isfinite(ar) and ar > 0):
            continue
        s3_stop = -(ev.ld - S3_STOP_ATR * ar)  # sweep high + 0.5 ATR, in the prices of b
        s3_target = -ev.mid
        for j in range(r + 1, min(n, r + max_wait + 1)):
            if pos[j] != pos[r] + (j - r):
                break
            if b.c[j] > s3_stop:
                aj = float(atr[j])
                if j not in used and np.isfinite(aj) and aj > 0:
                    entry = float(b.c[j])
                    stop = float(b.low[r : j + 1].min()) - stop_atr * aj
                    risk = entry - stop
                    if risk > 0:
                        used.add(j)
                        out.append(Signal("B", r, j, s3_stop, entry, stop, entry + target_r * risk, math.nan, aj))
                break
            if b.low[j] <= s3_target:
                break  # the S3 short reached its target first: it did not fail
    out.sort(key=lambda x: (x.i, x.start))
    return out


def regimes_at(close_ms: np.ndarray, trend: np.ndarray, t_ms: np.ndarray) -> np.ndarray:
    """Vectorised: value of a daily series (``trend[i]`` known at ``close_ms[i]``, ascending) on the last bar closed at
    or before each time in ``t_ms``; 0 before the first close. This is the function the verification script uses."""
    i = np.searchsorted(close_ms, t_ms, side="right") - 1
    return np.where(i >= 0, trend[np.maximum(i, 0)], 0).astype(np.int64)


def regime_at(close_ms: np.ndarray, trend: np.ndarray, t_ms: int) -> int:
    """Scalar form of ``regimes_at``."""
    return int(regimes_at(close_ms, trend, np.asarray([t_ms]))[0])


def placebo_levels(entry: float, atr_now: float, stop_atr_mult: float, target_r: float = 2.0) -> tuple[float, float]:
    """Long stop and target of a placebo entry with the same stop distance in ATR units as the matched trade."""
    stop = entry - stop_atr_mult * atr_now
    return stop, entry + target_r * (entry - stop)
