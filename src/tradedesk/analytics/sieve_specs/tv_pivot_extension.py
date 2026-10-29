"""TradingView built-in "Pivot Extension Strategy" (based on Pivots HL): a confirmed pivot high triggers a **short**, a
confirmed pivot low a **long** (fade of the swing; the opposite of Pivot Reversal). Inputs ``leftBars`` = 4 and
``rightBars`` = 2 (defaults per the OANDA Japan description of the built-in; the TradingView page does not list them). The
page says the strategy lags ``rightBars`` bars because a pivot needs that many bars after it to be confirmed: the signal
is at the close of bar p + 2 for a pivot at bar p. Daily bars, both sides.

A pivot high is a high strictly above the 4 bars before it and not below the 2 bars after it (tie rule is the
harvester's reading; the page gives no formula). The pivot-high signal is taken even when price has already fallen back
from the high by the confirmation bar, as the source reverses on every pivot.

Defaults added by the harvester (fixed before any run, no variants): the page gives no stop or exit (it reverses on the
opposite pivot, which needs future bars), so ``stop_atr`` = 2.0 ATR14 and ``hold_bars`` = 20; timeframe 1d (any)."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "tv_pivot_extension"
SOURCE = "TradingView built-in Pivot Extension Strategy (https://www.tradingview.com/support/solutions/43000599888-pivot-extension-strategy/); defaults via https://www.oanda.jp/lab-education/tradingviewstrategy/strategy/tradingview_pivot_extension_strategy"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"left": 4, "right": 2, "stop_atr": 2.0, "hold_bars": 20}


def signals(bars: Bars, left: int = 4, right: int = 2, stop_atr: float = 2.0, hold_bars: int = 20) -> list[Signal]:
    left, right = int(left), int(right)
    out: list[Signal] = []
    # Both sides are a fade of a pivot HIGH on the series ``b``: a short on the original bars, and a long (pivot low)
    # on the mirrored bars. On ``b`` the trade is a short: stop above the entry. ``f`` maps ``b`` prices to real prices.
    for side, f in (("long", -1.0), ("short", 1.0)):
        b = bars.mirrored() if side == "long" else bars
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        for i in range(len(b)):
            if pos[i] < left + right:
                continue
            p = i - right
            hp = float(b.h[p])
            if hp > float(b.h[p - left : p].max()) and hp >= float(b.h[p + 1 : i + 1].max()):
                ai = float(a[i])
                if np.isfinite(ai) and ai > 0:
                    e = float(b.c[i])
                    out.append(Signal(b.close_time(i), side, f * e, f * (e + stop_atr * ai), None, int(hold_bars) * b.bar_ms))
    return out
