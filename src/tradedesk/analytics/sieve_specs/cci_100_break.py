"""CCI +-100 break (D. Lambert, 1980; rules per StockCharts ChartSchool CCI): CCI = (TP - SMA20(TP)) / (0.015 x mean
deviation of TP over 20 bars), TP = (high + low + close) / 3. The source suggests requiring "a move above +100 for a
bullish signal and a move below -100 for a bearish signal" to cut zero-line whipsaws. Entry: CCI crosses above +100
(previous bar <= 100) goes long, crosses below -100 goes short, at the close. Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source gives no stop and no exit rule, so the
stop is ``stop_atr`` = 2.0 x ATR14 from the entry and the exit is by time after ``hold_bars`` = 20 bars. The source does
not say whether the +100 break is a trend entry or an overbought fade; it is read as the trend entry (a break in the
direction of the move), as the quoted sentence says "bullish"."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "cci_100_break"
SOURCE = "D. Lambert (1980), CCI; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/commodity-channel-index-cci)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "level": 100.0, "stop_atr": 2.0, "hold_bars": 20}


def cci(b: Bars, pos: np.ndarray, n: int) -> np.ndarray:
    """CCI per bar, NaN until ``n`` bars of the segment exist."""
    tp = (b.h + b.low + b.c) / 3.0
    out = np.full(len(b), np.nan)
    for i in range(len(b)):
        if pos[i] < n - 1:
            continue
        w = tp[i - n + 1 : i + 1]
        m = float(w.mean())
        md = float(np.abs(w - m).mean())
        if md > 0:
            out[i] = (float(tp[i]) - m) / (0.015 * md)
    return out


def signals(bars: Bars, n: int = 20, level: float = 100.0, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()  # mirrored prices negate CCI
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        x = cci(b, pos, int(n))
        for i in range(1, len(b)):
            ai = float(a[i])
            if not (np.isfinite(x[i]) and np.isfinite(x[i - 1]) and np.isfinite(ai) and ai > 0):
                continue
            if x[i] > level and x[i - 1] <= level:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
