"""EMA20 +- 1.5 x ATR14 channel breakout with a 2R target, as stated on The Right Trader page "Volatility Breakout
Strategy" (daily bars, after the close).

Upper = EMA(20) + 1.5 x ATR(14), Lower = EMA(20) - 1.5 x ATR(14) (Wilder ATR). Long: "today's close is above Upper,
and yesterday's close was at or below yesterday's Upper"; entry at that close. Short: mirror against Lower. Stop: "the
EMA(20) value on the signal bar", fixed, not trailed. Target: 2R from the entry close (R = distance to the stop).
Filter: "skip the trade if that distance [close to the EMA] is greater than 3 times ATR(14)". One position per market,
later signals are ignored while a trade is open (the sieve does this).

EMA seeded with the simple mean of the first n closes (``course_s14_ema``).

Defaults added by the harvester (fixed before any run, no variants): the page gives no maximum holding time, so a
trade that reaches neither stop nor target is closed after ``hold_bars`` = 20 bars (the EMA window). Not the same rule
as ``keltner_breakout`` (2.0 x ATR10, no target, no distance filter, first-ever cross); the shared idea is a Keltner
breakout, the parameters, target and filter are this page's own. Sizing (1 % of equity) is the sieve's R. The page
quotes no published win rate."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, atr, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "rt_keltner_15_target2r"
SOURCE = "The Right Trader, Volatility Breakout Strategy (https://therighttrader.com/learn/trading-strategies/volatility-breakout-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"ema_n": 20, "atr_n": 14, "k": 1.5, "target_r": 2.0, "max_dist_atr": 3.0, "hold_bars": 20}


def signals(
    bars: Bars,
    ema_n: int = 20,
    atr_n: int = 14,
    k: float = 1.5,
    target_r: float = 2.0,
    max_dist_atr: float = 3.0,
    hold_bars: int = 20,
) -> list[Signal]:
    pos = segment_pos(bars.t)
    c = bars.c
    mid = ema(c, int(ema_n), pos)
    a = atr(bars, int(atr_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, c.size):
        if pos[i] == 0:
            continue
        m, ai, mp, ap = float(mid[i]), float(a[i]), float(mid[i - 1]), float(a[i - 1])
        if not (math.isfinite(m) and math.isfinite(mp) and math.isfinite(ai) and math.isfinite(ap) and ai > 0):
            continue
        e, ep = float(c[i]), float(c[i - 1])
        dist = abs(e - m)
        if dist > max_dist_atr * ai or dist <= 0:
            continue
        if e > m + k * ai and ep <= mp + k * ap:
            out.append(Signal(bars.close_time(i), "long", e, m, e + target_r * (e - m), hold_ms))
        elif e < m - k * ai and ep >= mp - k * ap:
            out.append(Signal(bars.close_time(i), "short", e, m, e - target_r * (m - e), hold_ms))
    return out
