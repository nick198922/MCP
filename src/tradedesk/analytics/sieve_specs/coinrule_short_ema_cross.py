"""Short on an EMA 20 / EMA 50 cross-under with a percent stop and target (Coinrule, "Short Selling EMA Cross", TradingView
open-source strategy, AVAXUSDT on Binance).

Rule as published: enter short when EMA 20 crosses under EMA 50; exit the short at -8 % price decrease from the entry
price (take profit) or at +16 % price increase (stop loss). The page tests it on the 30-minute chart and recommends it in
downtrends and on weak coins. Short only.

Readings and defaults (fixed before any run, no variants):
- The sieve has no 30-minute bars; the same lengths are used on 1h closes (``TIMEFRAMES`` = 1). EMAs are of the close
  (the page does not say), seeded with the mean of the first n closes.
- "Crosses under" = EMA20 at or above EMA50 on the previous bar and below it on the signal bar; entry at that close.
- The page has no time exit (it waits for the target or the stop). A time exit of 20 days (480 hourly bars) is added as a
  cap, the 20-day convention of the catalog. A cap of 20 hourly bars was rejected before any run: an 8 % move rarely
  happens in 20 hours, so the test would no longer measure the rule of the page.
- The page's 30 % of coins per order is sizing and is not modelled. The source tests a single weak coin since
  December 2021; the sieve runs the rule on the whole universe, in every regime.
- The page publishes a 93 % profit rate for AVAXUSDT in a bear market, a sample chosen by the author."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, ema, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "coinrule_short_ema_cross"
SOURCE = "Coinrule, Short Selling EMA Cross (https://il.tradingview.com/script/qDkU6QLs-Short-Selling-EMA-Cross-By-Coinrule/)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"fast": 20, "slow": 50, "take_pct": 0.08, "stop_pct": 0.16, "hold_days": 20}


def signals(
    bars: Bars, fast: int = 20, slow: int = 50, take_pct: float = 0.08, stop_pct: float = 0.16, hold_days: int = 20
) -> list[Signal]:
    if bars.bar_ms != HOUR_MS or len(bars) < 2:
        return []
    pos = segment_pos(bars.t)
    ef, es = ema(bars.c, int(fast), pos), ema(bars.c, int(slow), pos)
    hold_ms = int(hold_days) * DAY_MS
    out: list[Signal] = []
    for i in range(1, len(bars)):
        f0, s0, f1, s1 = float(ef[i - 1]), float(es[i - 1]), float(ef[i]), float(es[i])
        if not all(math.isfinite(v) for v in (f0, s0, f1, s1)):
            continue
        if f0 >= s0 and f1 < s1:
            e = float(bars.c[i])
            out.append(Signal(bars.close_time(i), "short", e, e * (1.0 + stop_pct), e * (1.0 - take_pct), hold_ms))
    return out
