"""BB-RSI overbought short (TradingView open-source "BB-RSI Overbought DCA - Short Strategy" and its signal-only twin
"BB-RSI Short Indicator", Bybit-style crypto-perp bot): short when, on the same bar close, Bollinger %B(20, 2.0) crosses
up through 1.00 (close crosses above the upper band) and RSI(7) crosses up through 65. Take profit 2.11 % below the
entry. The source has no stop loss ("bounded position size is the structural risk cap") and no time exit, and it adds
safety orders on a ladder (0.72, 1.84, 3.57 ... % above the base entry) averaging the entry.

Sieve adaptation: the sieve holds one position without adding, so only the base order is tested (as for
``rsi4h_3commas_dca``). The source computes the gates on 30-minute bars, the sieve has 1h at the finest, so the same
parameters run on 1h. Bands use the population standard deviation of closes (TradingView ``ta.bb``); RSI is Wilder's
(RMA). "Crosses up" is: prior value at or below the level, current value above it. Defaults added by the harvester,
since the source has no stop or exit: stop ``stop_atr`` = 2.0 ATR14 above the entry and a time exit after
``hold_bars`` = 20 bars. The page does not name the price source of the indicators; close is used."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "bb_rsi_overbought_short"
SOURCE = "TradingView, BB-RSI Overbought DCA - Short Strategy (https://th.tradingview.com/script/PnnQc7fs-BB-RSI-Overbought-DCA-Short-Strategy/); BB-RSI Short Indicator (https://id.tradingview.com/script/3mWvFV6y-BB-RSI-Short-Indicator/)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"bb_n": 20, "bb_k": 2.0, "bb_level": 1.0, "rsi_n": 7, "rsi_level": 65.0, "tp_pct": 0.0211, "stop_atr": 2.0, "hold_bars": 20}


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
        g, ls = max(d, 0.0), max(-d, 0.0)
        if p <= n:
            ag += g
            al += ls
            if p < n:
                continue
            ag /= n
            al /= n
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + ls) / n
        out[i] = 50.0 if ag == 0.0 and al == 0.0 else (100.0 if al == 0.0 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


def percent_b(c: np.ndarray, n: int, k: float, pos: np.ndarray) -> np.ndarray:
    """Bollinger %B = (close - lower) / (upper - lower), SMA(n) +- k * population std; NaN before ``n`` bars."""
    out = np.full(c.size, np.nan)
    for i in range(c.size):
        if pos[i] < n - 1:
            continue
        w = c[i - n + 1 : i + 1]
        m, s = float(np.mean(w)), float(np.std(w))
        if s > 0:
            out[i] = (float(c[i]) - (m - k * s)) / (2.0 * k * s)
    return out


def signals(
    bars: Bars,
    bb_n: int = 20,
    bb_k: float = 2.0,
    bb_level: float = 1.0,
    rsi_n: int = 7,
    rsi_level: float = 65.0,
    tp_pct: float = 0.0211,
    stop_atr: float = 2.0,
    hold_bars: int = 20,
) -> list[Signal]:
    pos = segment_pos(bars.t)
    pb = percent_b(bars.c, int(bb_n), float(bb_k), pos)
    rs = rsi_wilder(bars.c, int(rsi_n), pos)
    a = atr(bars, 14, pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (np.isfinite(pb[i]) and np.isfinite(pb[i - 1]) and np.isfinite(rs[i]) and np.isfinite(rs[i - 1])):
            continue
        ai = float(a[i])
        if not (np.isfinite(ai) and ai > 0):
            continue
        if pb[i - 1] <= bb_level < pb[i] and rs[i - 1] <= rsi_level < rs[i]:
            e = float(bars.c[i])
            out.append(Signal(bars.close_time(i), "short", e, e + stop_atr * ai, e * (1.0 - tp_pct), hold_ms))
    return out
