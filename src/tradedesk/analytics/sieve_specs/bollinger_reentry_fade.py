"""Bollinger band fade with a re-entry trigger (The Right Trader, "Mean Reversion Strategy", daily chart).

Bands: 20-bar SMA plus and minus 2 standard deviations of the close. Long: a bar closes below the lower band (the
stretch bar); the next bar closes back inside the bands; entry at that close; stop = the stretch bar's low - 1 ATR14
("a small buffer such as 1 ATR(14)"); target the 20-bar middle band; exit at the market after a few bars if the target
is not reached. Short: mirror (a close above the upper band, the next close back inside, stop = the stretch bar's high
+ 1 ATR14). Daily bars.

Readings and defaults (fixed before any run, no variants):
- Standard deviation is the population one (ddof = 0), the usual Bollinger definition; the page does not say.
- "Back inside the bands" = the close is at or above the lower band and at or below the upper band of the trigger bar.
  The page enters "on that close or the next open"; the sieve enters at the close.
- The time exit is "5 to 8 bars"; the upper bound, 8, is taken (the page gives a range; the choice is made once and
  not tuned). The target is the middle band of the trigger bar (fixed at entry). A signal whose entry is already at or
  beyond the middle band is skipped.
- The page's invalidation (a later close outside the same band again) is a condition on later bars and is not coded;
  its optional setup filter (flat 200-day average or a 20-bar range) and the 1 ATR buffer alternative ("a round tick
  increment") are not applied. The page publishes no win rate.
- This differs from ``bollinger_reversion`` (enter on the touch, hold 20 d) and ``cm_bb_close_beyond`` (enter on the
  close beyond the band, percent stop): here the entry waits for the close back inside."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "bollinger_reentry_fade"
SOURCE = "The Right Trader, Mean Reversion Strategy (https://therighttrader.com/learn/trading-strategies/mean-reversion-strategy)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"n": 20, "k": 2.0, "atr_n": 14, "buffer_atr": 1.0, "hold_bars": 8}


def bands(b: Bars, n: int, k: float, pos: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Middle, lower and upper Bollinger bands; NaN until the segment holds ``n`` bars."""
    mid = np.full(len(b), np.nan)
    sd = np.full(len(b), np.nan)
    for i in range(n - 1, len(b)):
        if pos[i] >= n - 1:
            w = b.c[i - n + 1 : i + 1]
            mid[i], sd[i] = float(w.mean()), float(w.std())
    return mid, mid - k * sd, mid + k * sd


def signals(bars: Bars, n: int = 20, k: float = 2.0, atr_n: int = 14, buffer_atr: float = 1.0, hold_bars: int = 8) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        mid, lower, upper = bands(b, int(n), float(k), pos)
        a = atr(b, int(atr_n), pos)
        for i in range(1, len(b)):
            m, lo_b, up_b, ar = float(mid[i]), float(lower[i]), float(upper[i]), float(a[i])
            if not all(math.isfinite(v) for v in (m, lo_b, up_b, float(lower[i - 1]), ar)) or ar <= 0:
                continue
            if not (b.c[i - 1] < lower[i - 1] and lo_b <= b.c[i] <= up_b):
                continue
            e = float(b.c[i])
            stop = float(b.low[i - 1]) - buffer_atr * ar
            if stop < e < m:
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * stop, sgn * m, hold_ms))
    return out
