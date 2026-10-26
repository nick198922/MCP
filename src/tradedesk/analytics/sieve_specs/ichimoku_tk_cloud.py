"""Ichimoku Tenkan/Kijun cross with a cloud filter (G. Hosoda; rules per the StockSharp "Ichimoku Tenkan/Kijun Cross
Strategy" description): Tenkan-sen (9) = midpoint of the highest high and lowest low of 9 bars, Kijun-sen (26) likewise
over 26 bars, Senkou Span A = (Tenkan + Kijun) / 2 and Senkou Span B = midpoint over 52 bars, both plotted 26 bars
ahead, so the cloud at bar i is built from bars up to i - 26 (causal). Long when Tenkan crosses above Kijun on a
finished bar and the close is above the top of the cloud; short when it crosses below and the close is below the bottom.
The stop is the Kijun-sen at entry (fixed, not trailing). Daily bars.

Defaults added by the harvester (fixed before any run, no variants): the source exits on the opposite Tenkan/Kijun
cross (needs future bars; not testable), so the exit is by time after ``hold_bars`` = 20 bars. The source's candles
are 30 minutes; the sieve uses daily bars. Parameters 9 / 26 / 52 and displacement 26 are the classic ones (the
displacement is not stated on the source page). A cross is Tenkan <= Kijun on the previous bar and Tenkan > Kijun now."""

from __future__ import annotations

import math

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "ichimoku_tk_cloud"
SOURCE = "G. Hosoda, Ichimoku Kinko Hyo; rules per StockSharp (https://doc.stocksharp.com/en/api-examples/0085_Ichimoku_Tenkan)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"tenkan": 9, "kijun": 26, "senkou_b": 52, "shift": 26, "hold_bars": 20}


def _mid(b: Bars, j: int, n: int) -> float:
    return (float(b.h[j - n + 1 : j + 1].max()) + float(b.low[j - n + 1 : j + 1].min())) / 2.0


def signals(bars: Bars, tenkan: int = 9, kijun: int = 26, senkou_b: int = 52, shift: int = 26, hold_bars: int = 20) -> list[Signal]:
    tenkan, kijun, senkou_b, shift = int(tenkan), int(kijun), int(senkou_b), int(shift)
    need = shift + senkou_b  # position of the first bar with a full cloud and a previous bar
    pos = segment_pos(bars.t)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        for i in range(len(b)):
            if pos[i] < need:
                continue
            tk, kj = _mid(b, i, tenkan), _mid(b, i, kijun)
            tk0, kj0 = _mid(b, i - 1, tenkan), _mid(b, i - 1, kijun)
            if not (tk0 <= kj0 and tk > kj):
                continue
            j = i - shift
            span_a = (_mid(b, j, tenkan) + _mid(b, j, kijun)) / 2.0
            span_b = _mid(b, j, senkou_b)
            e = float(b.c[i])
            if e > max(span_a, span_b) and math.isfinite(kj) and kj < e:
                out.append(Signal(b.close_time(i), side, sgn * e, sgn * kj, None, hold_ms))
    return out
