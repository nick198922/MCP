"""Larry Williams' Smash Day reversal. A buy-side smash day closes below the previous bar's low and its low breaks the
lowest low of the previous ``lookback`` = 8 bars; the next bar trades above the smash-day high -> long (mirrored for the
sell side: close above the previous high, high above the highest high of 8 bars, next bar trades below the smash-day
low -> short). Daily bars. Rules per R. Quant, "I Backtested Larry Williams' Trading Strategy Across 15 Markets"
(lookback 3-8, 8 used); stop below the smash-day low per RoboForex's description.

Adaptations and defaults added by the harvester (fixed before any run, no variants):
- the source triggers with a buy-stop above the smash-day high on the next bar; the sieve enters at a close, so the
  signal is the first bar after the smash day that CLOSES above its high (only the bar right after the smash day can
  trigger, "next day"). Entry = that close.
- "breaks the lowest low of the past 3-8 days" is read as: the smash bar's low is below the lowest low of the previous 8
  bars (the article does not say whether this condition was required; it is "often" present).
- stop = the smash-day low (high for a short), taken from a secondary description (the tested article used no stop).
- the source holds until an opposite signal (needs future bars) -> time exit after ``hold_bars`` = 20 bars (default).
Cannot be tested as the source states: the "bailout" exit on a profitable open."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "smash_day_reversal"
SOURCE = (
    "L. Williams, Smash Day reversal; rules per R. Quant (https://roguequant.substack.com/p/i-backtested-larry-williams-trading), "
    "stop per RoboForex (https://roboforex.com/blog/education/catch-your-smash-day-with-larry-williams/)"
)
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"lookback": 8, "hold_bars": 20}


def signals(bars: Bars, lookback: int = 8, hold_bars: int = 20) -> list[Signal]:
    n = int(lookback)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        for i in range(n + 1, len(b)):
            s = i - 1  # smash-day candidate
            if pos[i] < n + 1:
                continue
            smash = b.c[s] < b.low[s - 1] and b.low[s] < b.low[s - n : s].min()
            if not (smash and b.c[i] > b.h[s]):
                continue
            e, st = float(b.c[i]), float(b.low[s])
            if sgn * st > 0 and e > st and math.isfinite(e):
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * st, None, hold_ms))
    return out
