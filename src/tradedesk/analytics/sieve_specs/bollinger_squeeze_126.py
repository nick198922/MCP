"""Bollinger squeeze breakout as tested on The Right Trader page "Bollinger Squeeze Strategy" (the band and BandWidth
definitions are J. Bollinger's). Daily bars.

Bands: middle = SMA20 of closes, upper / lower = middle +- 2 x sigma with the *population* standard deviation (divide by
20). BandWidth = (upper - lower) / middle. Squeeze on bar s: BandWidth(s) <= the lowest BandWidth of the previous 125
bars (126 bars in total, the page's "six months"). Only the latest squeeze is active; a newer squeeze restarts the clock.

Long: the first close above the upper band (of the same bar) on the squeeze bar or within the next 10 bars; short: the
first close below the lower band in the same window; the earlier close wins, and one entry per squeeze. Entry at that
close. Stop: the middle band (SMA20) of the entry bar, fixed. Target: 2R from the entry close. The setup expires when 10
bars after the squeeze pass without a close outside either band.

Defaults added by the harvester (fixed before any run, no variants): the page exits only at the stop or the target and
sets no time limit, so the exit is by time after ``hold_bars`` = 20 bars (the stop and the 2R target still apply). The
StockCharts BandWidth page gives no squeeze threshold (``bollinger_bandwidth_squeeze`` was declined for that); this spec
takes the threshold from the Right Trader page. The page is the site's "test version" and cites no published results.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "bollinger_squeeze_126"
SOURCE = "J. Bollinger, BandWidth squeeze; The Right Trader, Bollinger Squeeze Strategy (https://therighttrader.com/learn/trading-strategies/bollinger-squeeze-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "k": 2.0, "lookback": 125, "window": 10, "rr": 2.0, "hold_bars": 20}


def signals(
    bars: Bars, n: int = 20, k: float = 2.0, lookback: int = 125, window: int = 10, rr: float = 2.0, hold_bars: int = 20
) -> list[Signal]:
    n, lookback, window = int(n), int(lookback), int(window)
    m = len(bars)
    pos = segment_pos(bars.t)
    mid = np.full(m, np.nan)
    up = np.full(m, np.nan)
    dn = np.full(m, np.nan)
    bw = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= n - 1:
            w = bars.c[i - n + 1 : i + 1]
            mu, sd = float(w.mean()), float(w.std())  # population sigma
            if mu > 0:
                mid[i], up[i], dn[i] = mu, mu + k * sd, mu - k * sd
                bw[i] = (up[i] - dn[i]) / mu
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    s = -1  # latest squeeze bar, -1 = none
    for i in range(m):
        if pos[i] >= n - 1 + lookback and math.isfinite(float(bw[i])) and float(bw[i]) <= float(np.min(bw[i - lookback : i])):
            s = i
        if s >= 0 and (i - s > window or pos[i] < i - s):  # expired, or a data gap restarted the segment
            s = -1
        if s < 0 or not math.isfinite(float(up[i])):
            continue
        e = float(bars.c[i])
        if e > up[i]:
            side, st = "long", float(mid[i])
            tg = e + rr * (e - st)
        elif e < dn[i]:
            side, st = "short", float(mid[i])
            tg = e - rr * (st - e)
        else:
            continue
        s = -1
        if (side == "long" and 0 < st < e) or (side == "short" and st > e and tg > 0):
            out.append(Signal(bars.close_time(i), side, e, st, tg, hold_ms))
    return out
