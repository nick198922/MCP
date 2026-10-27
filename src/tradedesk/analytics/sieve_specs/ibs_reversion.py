"""Internal Bar Strength (IBS) mean reversion, long only (Quantified Strategies, "The Internal Bar Strength (IBS)
indicator"): IBS = (close - low) / (high - low). Buy at the close when IBS < 0.2; the source sells at the close of any
later day when IBS > 0.8. The source tests index ETFs and gives the rule long only, so there is no short side here.

Defaults added by the harvester (the source gives neither): the sieve cannot exit on "IBS > 0.8" (that needs future
bars), so the exit is a time exit after ``hold_bars`` = 3 daily bars, chosen before any run with no variants: it
approximates the IBS > 0.8 exit, which usually comes within 1-3 days (a 20-day hold would turn the reversal into a
beta-carrying long). The stop is ``stop_atr`` = 2.0 x ATR14 below the entry. The 0.2 / 0.8 thresholds could not be
verified against the source, which is paywalled. Daily bars (UTC day). A bar with zero range is skipped."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ibs_reversion"
SOURCE = "Quantified Strategies, The Internal Bar Strength (IBS) indicator: buy IBS < 0.2, sell IBS > 0.8 (https://quantifiedstrategies.substack.com/p/the-internal-bar-strength-ibs-indicator)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"ibs_lo": 0.2, "stop_atr": 2.0, "hold_bars": 3}


def signals(bars: Bars, ibs_lo: float = 0.2, stop_atr: float = 2.0, hold_bars: int = 3) -> list[Signal]:
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars)):
        rng = float(bars.h[i] - bars.low[i])
        ai = float(a[i])
        if not (rng > 0 and math.isfinite(ai) and ai > 0):
            continue
        e = float(bars.c[i])
        ibs = (e - float(bars.low[i])) / rng
        if ibs < ibs_lo and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
    return out
