"""Support / resistance break and retest, as written on The Right Trader's page "Support and Resistance Trading
Strategy" (daily bars). A swing high is a high with two lower highs on each side (swing low: mirror). Resistance: at
least two swing highs within 40 bars lying inside a band 1 % of price wide. A daily close above the band top is the
break (a wick is not). Long: after the break, the first later bar that trades back into the band (low <= band top)
and closes back above the band top is the entry (at its close). Stop below the retest bar's low and below the band,
target 2R, time exit 8 bars after the fill. The idea is cancelled if no bar tags the band within 8 bars of the break.
Short: the mirror - a close below a support band, then the first bar that trades back into it and closes back below.

Readings and adaptations (the page leaves them open, chosen before any run):
- a swing is known two bars after it forms (it needs two bars on the right), so the break can use it only later;
- the 40-bar window ends at the break bar; the cluster is built from the latest swing and earlier ones, newest first,
  while the whole set stays inside a band of 1 % of the band's far edge; the band is [min, max] of the cluster;
- the break must be the first close beyond the band since the cluster's first swing (the level was intact);
- "one retest only": the first bar that trades into the band decides; if it does not close back outside, the setup is
  dropped; the 8-bar limit counts from the break bar;
- the 8-bar time exit counts from the fill (the page leaves this open);
- stop = the lower of the retest bar's low and the band low (the page: below both), target = entry + 2 x (entry - stop)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "sr_break_retest"
SOURCE = "The Right Trader, Support and Resistance Trading Strategy, https://therighttrader.com/learn/trading-strategies/support-and-resistance-strategy"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"window": 40, "band": 0.01, "min_swings": 2, "retest_bars": 8, "rr": 2.0, "hold_bars": 8}


def _swing_highs(h: np.ndarray, pos: np.ndarray) -> np.ndarray:
    n = h.size
    sw = np.zeros(n, dtype=bool)
    for p in range(2, n - 2):
        if pos[p] < 2 or pos[p + 2] < pos[p] + 2:  # the five bars must sit in one segment
            continue
        v = h[p]
        sw[p] = v > h[p - 1] and v > h[p - 2] and v > h[p + 1] and v > h[p + 2]
    return sw


def _side(b: Bars, side: str, window: int, band: float, min_swings: int, retest_bars: int, rr: float, hold_bars: int) -> list[Signal]:
    """Long orientation on ``b`` (a mirrored series for shorts): resistance from swing highs, break up, retest."""
    sgn = 1.0 if side == "long" else -1.0
    pos = segment_pos(b.t)
    sw = _swing_highs(b.h, pos)
    swing_idx = np.flatnonzero(sw)
    out: list[Signal] = []
    armed: tuple[float, float, int] | None = None  # band low, band top, break bar
    for i in range(len(b)):
        if armed is not None:
            lo_band, top, i0 = armed
            if pos[i] < pos[i0] or i - i0 > retest_bars:
                armed = None
            elif b.low[i] <= top:  # first bar that trades back into the band decides
                armed = None
                if b.c[i] > top:
                    stop = float(min(b.low[i], lo_band))
                    e = float(b.c[i])
                    if stop < e:
                        tgt = e + rr * (e - stop)
                        out.append(Signal(b.close_time(i), side, sgn * e, sgn * stop, sgn * tgt, int(hold_bars) * b.bar_ms))
                continue
            else:
                continue
        # look for a break at bar i; swings confirmed by bar i - 1 at the latest (p + 2 <= i - 1)
        cand = swing_idx[(swing_idx + 2 <= i - 1) & (swing_idx >= i - window)]
        if cand.size < min_swings:
            continue
        cluster = [int(cand[-1])]
        hi_v = lo_v = float(b.h[cand[-1]])
        for p in cand[-2::-1]:
            v = float(b.h[p])
            nh, nl = max(hi_v, v), min(lo_v, v)
            if nh - nl <= band * abs(nh):
                cluster.append(int(p))
                hi_v, lo_v = nh, nl
        if len(cluster) < min_swings:
            continue
        top = hi_v
        first = min(cluster)
        if b.c[i] > top and pos[i] > pos[first] and float(b.c[first:i].max()) <= top:
            armed = (lo_v, top, i)
    return out


def signals(
    bars: Bars,
    window: int = 40,
    band: float = 0.01,
    min_swings: int = 2,
    retest_bars: int = 8,
    rr: float = 2.0,
    hold_bars: int = 8,
) -> list[Signal]:
    args = (int(window), float(band), int(min_swings), int(retest_bars), float(rr), int(hold_bars))
    return _side(bars, "long", *args) + _side(bars.mirrored(), "short", *args)
