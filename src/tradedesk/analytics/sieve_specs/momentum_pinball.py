"""Momentum Pinball (L. B. Raschke and L. Connors, "Street Smarts", 1995, chapter 7); the rule set is the one in the MQL5
article "Momentum Pinball" (https://www.mql5.com/en/articles/2825), which is the most detailed public description found.
Timeframe 1h bars, the day is the UTC day.

LBR/RSI on daily bars: x(d) = close(d) - close(d - 2) (the article: compare "not the prices of bars following each
other, but missing one bar between them"), LBR/RSI = Wilder RSI(3) of the series x (the first value is the plain mean of
the first three gains / losses). A daily value needs consecutive complete UTC days (24 hourly bars); a hole restarts it.
Other sources use a one-bar rate of change; the article's two-bar form is taken because it is stated explicitly.

Long on day D: LBR/RSI of the last closed day (D - 1) is below 30. The article places a buy-stop above the high of the
first hourly bar of the day (the 00:00 UTC hour) after it closes, with the stop at that bar's low. Short: LBR/RSI above
70, sell-stop below the first hourly bar's low, stop at its high (thresholds as in the article's code; its text swaps
them once). Exit: "on the second trading day the position has to be closed" - here at 00:00 UTC of day D + 2.

Adaptation to bar closes: the sieve enters at a bar close, so the entry is the close of the first 1h bar of day D, from
01:00 on, that closes above the first hourly bar's high (below its low for a short); the pending order is cancelled at the
end of day D, and one signal per day. Defaults added by the harvester (fixed before any run, no variants): the article's
profit-dependent holding ("if by the end of the day the position remains profitable, leave it for the next day") is
read as a flat time exit at the end of day D + 1; its re-entry after a loss and the three optional exit modules are not
used; the article gives no trend filter, none is applied. The article tested a single FX pair on H1; the sieve runs the
whole universe.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, resample
from tradedesk.analytics.strategy_sieve import Signal

NAME = "momentum_pinball"
SOURCE = "L. B. Raschke, L. Connors, Street Smarts (1995); MQL5 article Momentum Pinball (https://www.mql5.com/en/articles/2825)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 3, "roc_lag": 2, "oversold": 30.0, "overbought": 70.0}


def lbr_rsi(day: Bars, rsi_n: int, roc_lag: int) -> np.ndarray:
    """LBR/RSI per daily bar (NaN until defined); restarts after a non-consecutive day."""
    m = len(day)
    out = np.full(m, np.nan)
    run = 0  # consecutive days ending at i
    x_prev = math.nan
    avg_g = avg_l = math.nan
    seed_g: list[float] = []
    seed_l: list[float] = []
    for i in range(m):
        run = run + 1 if i > 0 and int(day.t[i] - day.t[i - 1]) == DAY_MS else 1
        if run == 1:
            x_prev = math.nan
            avg_g = avg_l = math.nan
            seed_g, seed_l = [], []
        if run <= roc_lag:
            continue
        x = float(day.c[i] - day.c[i - roc_lag])
        if math.isfinite(x_prev):
            d = x - x_prev
            g, lo = max(d, 0.0), max(-d, 0.0)
            if math.isfinite(avg_g):
                avg_g = (avg_g * (rsi_n - 1) + g) / rsi_n
                avg_l = (avg_l * (rsi_n - 1) + lo) / rsi_n
            else:
                seed_g.append(g)
                seed_l.append(lo)
                if len(seed_g) == rsi_n:
                    avg_g, avg_l = sum(seed_g) / rsi_n, sum(seed_l) / rsi_n
            if math.isfinite(avg_g):
                out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
        x_prev = x
    return out


def signals(bars: Bars, rsi_n: int = 3, roc_lag: int = 2, oversold: float = 30.0, overbought: float = 70.0) -> list[Signal]:
    if bars.bar_ms != HOUR_MS or len(bars) == 0:
        return []
    day = resample(bars, 24)  # complete UTC days only
    if len(day) == 0:
        return []
    rsi = lbr_rsi(day, int(rsi_n), int(roc_lag))
    by_day = {int(day.t[j]) // DAY_MS: float(rsi[j]) for j in range(len(day))}
    dnum = bars.t // HOUR_MS // 24
    starts = np.flatnonzero(np.concatenate([[True], dnum[1:] != dnum[:-1]]))
    ends = np.concatenate([starts[1:], [len(bars)]])
    out: list[Signal] = []
    for a, z in zip(starts.tolist(), ends.tolist(), strict=True):
        d = int(dnum[a])
        if int(bars.t[a]) != d * DAY_MS or z - a < 2:
            continue  # the first hourly bar of the day must be the 00:00 hour
        prev = by_day.get(d - 1, math.nan)
        if not math.isfinite(prev):
            continue
        if prev < oversold:
            side = "long"
        elif prev > overbought:
            side = "short"
        else:
            continue
        hi1, lo1 = float(bars.h[a]), float(bars.low[a])
        for i in range(a + 1, z):
            e = float(bars.c[i])
            if side == "long" and e > hi1 and lo1 > 0:
                st = lo1
            elif side == "short" and e < lo1:
                st = hi1
            else:
                continue
            t_ms = bars.close_time(i)
            hold_ms = (d + 2) * DAY_MS - t_ms
            if hold_ms > 0:
                out.append(Signal(t_ms, side, e, st, None, hold_ms))
            break
    return out
