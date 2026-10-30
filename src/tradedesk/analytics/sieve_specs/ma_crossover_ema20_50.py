"""EMA 20 / EMA 50 crossover as stated on The Right Trader page "Moving Average Crossover Strategy" (daily closes).

Long on the first day the 20-day EMA closes above the 50-day EMA (EMA20 was at or below EMA50 the day before), short on
the first close the other way (EMA20 was at or above EMA50 the day before). Entry at that day's close. Stop:
"two times ATR(14) from the entry close" (Wilder ATR14), fixed, not trailed. The page exits on "the reverse cross or the
ATR stop, whichever comes first", no profit target.

EMA seeded with the simple mean of the first n closes (``course_s14_ema``).

Defaults added by the harvester (fixed before any run, no variants): the reverse-cross exit is a condition on later
bars, which the sieve cannot express, so the exit is by time after ``hold_bars`` = 20 bars with the 2 x ATR14 stop in
place (the page gives no holding time). The page does not say whether a reverse cross also opens the opposite
position; here every cross is its own signal in its own direction (the sieve skips signals while a position is open).
Sizing (1 % of equity to the stop) is the sieve's R and not modelled here. Nothing in the rule needs volume. The page
quotes no published win rate."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ma_crossover_ema20_50"
SOURCE = (
    "The Right Trader, Moving Average Crossover Strategy (https://therighttrader.com/learn/trading-strategies/moving-average-crossover)"
)
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 20, "slow": 50, "atr_n": 14, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, fast: int = 20, slow: int = 50, atr_n: int = 14, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    pos = segment_pos(bars.t)
    ef = ema(bars.c, int(fast), pos)
    es = ema(bars.c, int(slow), pos)
    a = atr(bars, int(atr_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if pos[i] == 0:
            continue
        f, s, fp, sp, ai = float(ef[i]), float(es[i]), float(ef[i - 1]), float(es[i - 1]), float(a[i])
        if not all(math.isfinite(v) for v in (f, s, fp, sp)) or not (math.isfinite(ai) and ai > 0):
            continue
        c = float(bars.c[i])
        if f > s and fp <= sp:
            stop = c - stop_atr * ai
            if stop > 0:
                out.append(Signal(bars.close_time(i), "long", c, stop, None, hold_ms))
        elif f < s and fp >= sp:
            out.append(Signal(bars.close_time(i), "short", c, c + stop_atr * ai, None, hold_ms))
    return out
