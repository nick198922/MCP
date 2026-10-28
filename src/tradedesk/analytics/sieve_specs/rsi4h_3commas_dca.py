"""3Commas RSI DCA bot rule (3Commas, "BTC RSI Strategy [3Commas]" / "SOL RSI Strategy [3Commas]", TradingView
open-source scripts): on 4h bars, long when Wilder's RSI(14) closes below 28; take profit 3 % above the average entry;
no stop loss; five safety orders at -2 %, -5 %, -9.5 %, -16 % and -25 % below the base entry (sizes x1.8 per rung).
Long only.

The sieve trades one position per symbol and never adds to it, so the safety-order ladder cannot be described. Same
treatment as ``dca_martingale``: this measures a bot that has only its first order, a 3 % target and a hard total stop at
the depth of its last safety order (25 % below the entry). Without averaging the average entry is the entry, so the
target is entry x 1.03. A signal on every closed 4h bar with RSI(14) < 28, as the script does ("no position open" is the
sieve's own one-position rule).

Defaults added by the harvester (fixed before any run, no variants): the stop is the 25 % depth of the last safety order
(the source has no stop; this is the bot's capital cap, not a risk rule), and the time exit is ``hold_bars`` = 20 4h bars
(the source waits for the target indefinitely). The RSI is Wilder's, seeded with the simple mean of the first 14
changes of a segment (TradingView ``ta.rsi`` seeds the same way). The source runs on single Bybit perpetuals (BTC, SOL,
the same settings); the sieve applies it to the whole universe."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "rsi4h_3commas_dca"
SOURCE = "3Commas, BTC RSI Strategy [3Commas] (https://tr.tradingview.com/script/eZsaqsZY-BTC-RSI-Strategy-3Commas/), SOL variant with the same settings"
TIMEFRAMES = (4,)
MANAGEMENT = "strategy"
PARAMS = {"rsi_n": 14, "rsi_below": 28.0, "target": 0.03, "stop": 0.25, "hold_bars": 20}


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
    bars: Bars, rsi_n: int = 14, rsi_below: float = 28.0, target: float = 0.03, stop: float = 0.25, hold_bars: int = 20
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
