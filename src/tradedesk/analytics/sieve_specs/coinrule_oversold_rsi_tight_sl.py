"""Coinrule bot template "Oversold RSI with Tight Stop-Loss" (TradingView open-source script by Coinrule, Chainlink
example): long when RSI is below 30; take profit 7 % above the entry; stop loss 1 % below. The page says the daily
timeframe worked best in its backtests, so 1d bars. Long only (a long template).

Defaults added by the harvester (fixed before any run, no variants): the page does not give the RSI length, so Wilder's
RSI(14), seeded with the simple mean of the first 14 changes of a segment (TradingView ``ta.rsi`` seeds the same
way); the page has no time exit, so ``hold_bars`` = 20 daily bars. A signal on every closed bar with RSI < 30, as a
bot with a plain condition does ("no position open" is the sieve's own one-position rule). The source is one coin
(Chainlink); the sieve runs the whole universe. Target 7 % at a 1 % stop is 7R before costs, so the expected hit rate
is low and fees on a 1 % risk are heavy."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "coinrule_oversold_rsi_tight_sl"
SOURCE = "Coinrule, Oversold RSI with Tight Stop-Loss Strategy (https://id.tradingview.com/script/Fuzmd3jp-Oversold-RSI-with-tight-SL-Strategy-by-Coinrule)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 14, "rsi_below": 30.0, "target": 0.07, "stop": 0.01, "hold_bars": 20}


def rsi_wilder(c: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """Wilder's RSI: average gain and loss seeded with the simple mean of a segment's first ``n`` changes, then
    smoothed (avg = (avg * (n - 1) + x) / n). No loss -> 100 (no change at all -> 50). NaN before the seed."""
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


def signals(
    bars: Bars, rsi_n: int = 14, rsi_below: float = 30.0, target: float = 0.07, stop: float = 0.01, hold_bars: int = 20
) -> list[Signal]:
    if len(bars) == 0 or not (0 < target and 0 < stop < 1):
        return []
    pos = segment_pos(bars.t)
    r = rsi_wilder(bars.c, int(rsi_n), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(len(bars)):
        ri = float(r[i])
        if math.isfinite(ri) and ri < rsi_below:
            e = float(bars.c[i])
            out.append(Signal(bars.close_time(i), "long", e, e * (1.0 - stop), e * (1.0 + target), hold_ms))
    return out
