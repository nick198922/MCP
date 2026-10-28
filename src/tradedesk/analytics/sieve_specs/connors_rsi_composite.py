"""ConnorsRSI composite (L. Connors; formula and scan thresholds per StockCharts ChartSchool): ConnorsRSI(3, 2, 100) =
(RSI(close, 3) + RSI(streak, 2) + PercentRank(100)) / 3, where ``streak`` counts consecutive higher (+) or lower (-)
closes (an unchanged close resets it to 0) and PercentRank is the share of the previous 100 one-day percent changes of the
close that are lower than today's change. The page's two scans are the rule: close above its SMA200 and ConnorsRSI <= 10
-> long; close below its SMA200 and ConnorsRSI >= 90 -> short. Signal on the first bar where the condition becomes true.
Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the page gives no entry timing, stop or exit, so
entry is the close of the signal bar, the stop is ``stop_atr`` = 2.0 x ATR14 from the entry, and the exit is by time after
``hold_bars`` = 20 bars. "Price change" in PercentRank is read as the 1-day percent change of the close (the page does not
define it). RSI is Wilder's, seeded with the simple mean of a segment's first ``n`` changes. Related to ``connors_rsi2``
(single RSI(2)) and ``cumulative_rsi2`` but a different composite indicator."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "connors_rsi_composite"
SOURCE = "L. Connors, ConnorsRSI; StockCharts ChartSchool (https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/connorsrsi)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 3, "streak_n": 2, "rank_n": 100, "low": 10.0, "high": 90.0, "trend_n": 200, "stop_atr": 2.0, "hold_bars": 20}


def rsi_wilder(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Wilder's RSI seeded with the simple mean of a segment's first ``n`` changes; no loss -> 100, flat -> 50."""
    out = np.full(c.size, np.nan)
    ag = al = 0.0
    for i in range(c.size):
        p = pos[i]
        if p == 0:
            ag = al = 0.0
            continue
        d = float(c[i] - c[i - 1])
        g, lo = max(d, 0.0), max(-d, 0.0)
        if p <= n:
            ag += g
            al += lo
            if p < n:
                continue
            ag /= n
            al /= n
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + lo) / n
        out[i] = 50.0 if ag == 0.0 and al == 0.0 else (100.0 if al == 0.0 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def connors_rsi(c: np.ndarray, pos: np.ndarray, rsi_n: int, streak_n: int, rank_n: int) -> np.ndarray:
    m = c.size
    streak = np.zeros(m)
    for i in range(1, m):
        if pos[i] == 0:
            continue
        if c[i] > c[i - 1]:
            streak[i] = streak[i - 1] + 1.0 if streak[i - 1] > 0 else 1.0
        elif c[i] < c[i - 1]:
            streak[i] = streak[i - 1] - 1.0 if streak[i - 1] < 0 else -1.0
    r1 = rsi_wilder(c, rsi_n, pos)
    r2 = rsi_wilder(streak, streak_n, pos)
    roc = np.full(m, np.nan)
    for i in range(1, m):
        if pos[i] >= 1 and c[i - 1] != 0:
            roc[i] = c[i] / c[i - 1] - 1.0
    rank = np.full(m, np.nan)
    for i in range(m):
        if pos[i] >= rank_n + 1:
            prev = roc[i - rank_n : i]  # the previous rank_n changes, not today's
            rank[i] = 100.0 * float(np.count_nonzero(prev < roc[i])) / rank_n
    return (r1 + r2 + rank) / 3.0


def signals(
    bars: Bars,
    rsi_n: int = 3,
    streak_n: int = 2,
    rank_n: int = 100,
    low: float = 10.0,
    high: float = 90.0,
    trend_n: int = 200,
    stop_atr: float = 2.0,
    hold_bars: int = 20,
) -> list[Signal]:
    m = len(bars)
    if m == 0:
        return []
    pos = segment_pos(bars.t)
    crsi = connors_rsi(bars.c, pos, int(rsi_n), int(streak_n), int(rank_n))
    trend = sma(bars.c, int(trend_n), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, m):
        ci, ti, ai = float(crsi[i]), float(trend[i]), float(a[i])
        if not (math.isfinite(ci) and math.isfinite(ti) and math.isfinite(ai) and ai > 0):
            continue
        cp = float(crsi[i - 1])
        e = float(bars.c[i])
        if e > ti and ci <= low and not (math.isfinite(cp) and cp <= low and bars.c[i - 1] > trend[i - 1]) and e - stop_atr * ai > 0:
            out.append(Signal(bars.close_time(i), "long", e, e - stop_atr * ai, None, hold_ms))
        elif e < ti and ci >= high and not (math.isfinite(cp) and cp >= high and bars.c[i - 1] < trend[i - 1]):
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, None, hold_ms))
    return out
