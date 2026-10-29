"""52-week high breakout, as written on The Right Trader's page "52-Week High Strategy" (daily bars), long only.
Signal: today's close is above High_N (the highest high of the previous N bars, today excluded) and yesterday's close was
not above yesterday's N-bar high (fresh cross only). Entry at today's close. Stop = the lowest low of the previous 20
bars (today excluded), fixed at entry. Target 2R (entry plus twice the distance to the stop). Skip the trade if the
stop distance is more than 8 % of the entry close.

N = 364: the page says 252 sessions for stocks and, in its FAQ, "A crypto chart that prints every day needs about
364 bars for the same year", so the crypto reading of the source is used (not a tuned value).

Defaults added by the harvester (the page has none): the page's only exit is "stop or target, whichever prints first",
so a time exit after 20 bars is added for the sieve. Entry is at the signal bar's close, as on the page. Symbols need
more than 365 contiguous daily bars of history before the first signal."""

from __future__ import annotations

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "fifty_two_week_high_364"
SOURCE = "The Right Trader, 52-Week High Strategy, https://therighttrader.com/learn/trading-strategies/fifty-two-week-high-strategy"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"high_n": 364, "stop_n": 20, "rr": 2.0, "max_stop_pct": 0.08, "hold_bars": 20}


def signals(
    bars: Bars,
    high_n: int = 364,
    stop_n: int = 20,
    rr: float = 2.0,
    max_stop_pct: float = 0.08,
    hold_bars: int = 20,
) -> list[Signal]:
    high_n, stop_n = int(high_n), int(stop_n)
    pos = segment_pos(bars.t)
    hold = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars.c)):
        if pos[i] < high_n + 1 or pos[i] < stop_n:
            continue
        c = float(bars.c[i])
        if not c > float(bars.h[i - high_n : i].max()):
            continue
        if float(bars.c[i - 1]) > float(bars.h[i - 1 - high_n : i - 1].max()):
            continue  # not a fresh cross
        stop = float(bars.low[i - stop_n : i].min())
        risk = c - stop
        if not (risk > 0 and risk <= max_stop_pct * c):
            continue
        out.append(Signal(bars.close_time(i), "long", c, stop, c + rr * risk, hold))
    return out
