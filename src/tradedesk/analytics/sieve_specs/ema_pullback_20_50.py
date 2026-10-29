"""EMA pullback as tested on The Right Trader page "EMA Pullback Strategy" (daily chart). Daily bars.

Long. Trend: EMA20 above EMA50 and EMA20 above its value five bars earlier. Tag: a bar in the trend whose low is at or
below EMA20 while its close is still above EMA50 ("a daily low touches the 20-day EMA without a close beyond the 50-day
EMA"). Trigger: the next close after the tag that finishes back above EMA20 ("the next close finishes back above the
20-day EMA"), with the trend still true on that bar and no close at or below EMA50 on the way (such a close cancels the
setup). Entry at the trigger close. Stop: the lowest low from the tag bar through the trigger bar (exactly on it; the
page says "just beyond"). The trade is not valid if that swing low is at or below EMA50 of the trigger bar. Short:
mirror (EMA20 below EMA50 and below its value five bars earlier, a high at or above EMA20 with the close below EMA50,
the next close back below EMA20, stop at the highest high of the rally, invalid if it is at or above EMA50).

EMA seeded with the mean of the first n closes. A bar that triggers does not start a new tag on the same bar (the page
does not say). While a setup is open, later lows below EMA20 only extend the stop extreme.

Defaults added by the harvester (fixed before any run, no variants): the page has no profit target ("no fixed profit
target in the base rule") and exits on a close beyond EMA50 or an EMA20 / EMA50 cross against the trade, which are
conditions on later bars, so the exit is by time after ``hold_bars`` = 20 bars with the swing stop in place. The page's
optional filters (0.25 ATR tag tolerance, RSI > 50, long only) are not applied. It states no published success rate.
"""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ema_pullback_20_50"
SOURCE = "The Right Trader, EMA Pullback Strategy (https://therighttrader.com/learn/trading-strategies/ema-pullback-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 20, "slow": 50, "slope_lag": 5, "hold_bars": 20}


def signals(bars: Bars, fast: int = 20, slow: int = 50, slope_lag: int = 5, hold_bars: int = 20) -> list[Signal]:
    lag = int(slope_lag)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        ef = ema(b.c, int(fast), pos)
        es = ema(b.c, int(slow), pos)
        active = False
        ext = math.inf  # lowest low since the tag (mirrored for shorts)
        for i in range(lag, len(b)):
            f, s, fp = float(ef[i]), float(es[i]), float(ef[i - lag])
            if not (math.isfinite(f) and math.isfinite(s) and math.isfinite(fp)):
                active = False
                continue
            c, low = float(b.c[i]), float(b.low[i])
            trend = f > s and f > fp
            triggered = False
            if active:
                ext = min(ext, low)
                if c <= s:
                    active = False
                elif c > f:
                    active = False
                    triggered = True
                    if trend and ext > s and ext < c and sgn * ext > 0:
                        out.append(Signal(b.close_time(i), side, sgn * c, sgn * ext, None, hold_ms))
            if not active and not triggered and trend and low <= f and c > s:
                active = True
                ext = low
    return out
