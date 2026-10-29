"""Brock, Lakonishok and LeBaron moving average band rule (J. Finance 47(5), 1992), daily bars.

This is the fixed-length-hold (FMA-style) variant with a 1 % band, NOT the variable-length (VMA) rule: the signal is
followed by a fixed 10-day holding period. Rule coded: short average = the close (length 1), long average = the 50-day
simple average, band 1 %. Long on the first close above 1.01 x SMA50 (the previous close was not), short on the first
close below 0.99 x SMA50; entry at that close; hold 10 bars.

Source caveat: the cited tradicted.com page mentions only VMA(1,150,0) and the FMA rule with a 1 % band and 10 days. The
1-50 pair is NOT confirmed by that page; it is taken from the paper's rule set from memory of secondary summaries. The
1 % band and the 10-day hold are confirmed by the page; the lookback 1-50 was fixed before any run, not chosen by results.

Defaults added by the harvester (fixed before any run, no variants): the paper has no stop, so the stop is 2.0 x ATR14
from the entry. The paper's variable-length exit (the average crossing back inside the band) is a later event the sieve
cannot express, so only the 10-day fixed-length version is coded. Applied to crypto it is a long and short rule."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "brock_vma_1_50_band"
SOURCE = "Brock, Lakonishok, LeBaron, Simple Technical Trading Rules and the Stochastic Properties of Stock Returns, J. Finance 1992 (https://www.tradicted.com/research/brock-technical-1992/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"long_n": 50, "band": 0.01, "stop_atr": 2.0, "hold_bars": 10}


def signals(bars: Bars, long_n: int = 50, band: float = 0.01, stop_atr: float = 2.0, hold_bars: int = 10) -> list[Signal]:
    n = int(long_n)
    pos = segment_pos(bars.t)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    prev_up = prev_dn = False
    for i in range(len(bars)):
        if pos[i] < n - 1:
            prev_up = prev_dn = False
            continue
        m = float(bars.c[i - n + 1 : i + 1].mean())
        c = float(bars.c[i])
        up, dn = c > (1.0 + band) * m, c < (1.0 - band) * m
        ai = float(a[i])
        if np.isfinite(ai) and ai > 0:
            if up and not prev_up:
                out.append(Signal(bars.close_time(i), "long", c, c - stop_atr * ai, None, hold_ms))
            elif dn and not prev_dn:
                out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold_ms))
        prev_up, prev_dn = up, dn
    return out
