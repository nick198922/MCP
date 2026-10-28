"""Awesome Oscillator zero cross with a MACD filter (Freqtrade ``berlinguyinca/AwesomeMacd.py``): on 1h bars go long when
the MACD line (12, 26, 9 TA-Lib defaults) is above zero and the Awesome Oscillator crosses from below zero to above zero
(AO[i-1] < 0 and AO[i] > 0). The source is long only; ``stoploss = -0.25``, ``minimal_roi = {"0": 0.10}`` (take profit at
+10 % from the entry).

Source parameters, used unchanged: MACD 12 / 26 / 9, stop 25 %, target 10 %. The source calls the library's
``awesome_oscillator`` without arguments; its defaults (AO = SMA5 - SMA34 of the median price (high + low) / 2, the
standard Bill Williams definition) are assumed, they are not on the page. The MACD line is EMA12(close) - EMA26(close).
Only the MACD line is used (not the signal line or histogram), as in the code.

Defaults added by the harvester (fixed before any run, no variants): the source's exit (AO crosses down while MACD < 0)
needs future bars, so a time exit after ``hold_bars`` = 20 bars (20 hours) is used. R is large (25 % stop) and a 10 %
target is rarely reached within 20 hours: the test is mostly a time exit with a wide stop. Short side not tested (the
source has none)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "freqtrade_awesome_macd"
SOURCE = "Freqtrade strategies repo, berlinguyinca/AwesomeMacd.py (https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/AwesomeMacd.py)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 12, "slow": 26, "ao_fast": 5, "ao_slow": 34, "stop_pct": 0.25, "tp_pct": 0.10, "hold_bars": 20}


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def signals(
    bars: Bars,
    fast: int = 12,
    slow: int = 26,
    ao_fast: int = 5,
    ao_slow: int = 34,
    stop_pct: float = 0.25,
    tp_pct: float = 0.10,
    hold_bars: int = 20,
) -> list[Signal]:
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    macd = ema(bars.c, int(fast), pos) - ema(bars.c, int(slow), pos)
    mp = (bars.h + bars.low) / 2.0
    ao = sma(mp, int(ao_fast), pos) - sma(mp, int(ao_slow), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        if not (np.isfinite(macd[i]) and np.isfinite(ao[i]) and np.isfinite(ao[i - 1])):
            continue
        if macd[i] > 0 and ao[i] > 0 and ao[i - 1] < 0:
            e = float(bars.c[i])
            out.append(Signal(bars.close_time(i), "long", e, e * (1.0 - stop_pct), e * (1.0 + tp_pct), hold_ms))
    return out
