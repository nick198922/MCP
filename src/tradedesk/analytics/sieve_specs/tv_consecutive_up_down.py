"""TradingView built-in "Consecutive Up/Down Strategy": go long once the close has been above the previous close for
``bars_up`` bars in a row, go short once it has been below the previous close for ``bars_down`` bars in a row (source
defaults 3 and 3; the page calls it a strategy for trending markets, any timeframe). Daily bars, both sides.

Rule as written in the Pine code the page quotes: ``ups = close > close[1] ? ups[1] + 1 : 0``, entry when
``ups >= consecutiveBarsUp`` (mirror for ``dns``). Entry at the close of the signal bar.

Defaults added by the harvester (fixed before any run, no variants): the page has no exit, stop or target (the script
only calls ``strategy.entry``; it reverses on the opposite signal, which needs future bars), so the stop is
``stop_atr`` = 2.0 ATR14 and the time exit ``hold_bars`` = 20 bars. The signal fires on the bar that completes the run
(``ups == bars_up`` exactly), not on every later bar of the same run (the script would re-submit the same entry while
already long; the sieve would re-enter after a stop-out inside the same run). The timeframe is 1d (the page: any)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "tv_consecutive_up_down"
SOURCE = "TradingView built-in Consecutive Up/Down Strategy (https://www.tradingview.com/support/solutions/43000599836-consecutive-up-down-strategy/)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"bars_up": 3, "bars_down": 3, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, bars_up: int = 3, bars_down: int = 3, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    out: list[Signal] = []
    for side, sgn, need in (("long", 1.0, int(bars_up)), ("short", -1.0, int(bars_down))):
        b = bars if side == "long" else bars.mirrored()  # mirrored: a fall becomes a rise
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        run = 0
        for i in range(len(b)):
            if pos[i] == 0:
                run = 0
                continue
            run = run + 1 if b.c[i] > b.c[i - 1] else 0
            ai = float(a[i])
            if run == need and np.isfinite(ai) and ai > 0:
                e = float(b.c[i])
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * (e - stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
