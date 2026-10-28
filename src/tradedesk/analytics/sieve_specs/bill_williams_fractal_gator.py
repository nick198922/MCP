"""Bill Williams Alligator + fractal breakout (StockSharp API example 2077 "Bill Williams", 1h candles by default): median
price = (high + low) / 2; Alligator Jaw = SMMA13, Teeth = SMMA8, Lips = SMMA5 of the median price, unshifted as in the
source. The Alligator is "open" when |Jaw - Teeth| >= 0.30 % of the close and |Lips - Teeth| >= 0.15 % of the close. Fractal =
the middle bar of five whose high (low) is strictly above (below) the other four; the last confirmed up / down fractal is
kept. filter = 0.05 % of the close.

Long when, on a closed bar, the Alligator is open, a last up fractal exists, the bar's high >= up fractal + filter, the bar
is bullish (close > open) and the up fractal >= Teeth. Short is the mirror (down fractal, low <= fractal - filter, bearish,
down fractal <= Teeth). The source exits a long when close <= last down fractal - filter (short: close >= last up fractal +
filter) and reverses on the opposite signal.

Source parameters, used unchanged: 13 / 8 / 5, 0.30 % / 0.15 % / 0.05 %, 5-bar fractals, 1h. The fractal at bar i-2 is
confirmed on bar i (uses bars i-4 .. i only), so the signal is causal; the fractal list is updated before the entry check on
the same bar (the source order is not on the page). SMMA is seeded with the mean of the first n values of a segment, then
(previous * (n - 1) + x) / n.

Sieve adaptation (fixed before any run, no variants): the source's exit is a close beyond the last opposite fractal; a bar
close trigger cannot be expressed, so the stop is a fixed price at the opposite fractal -/+ filter at the signal (touched by
the bar's low / high rather than its close, and not trailed). A signal where that stop is not beyond the entry, or where no
opposite fractal exists yet, is skipped. Time exit after ``hold_bars`` = 20 bars (20 hours) (harvester default; the source
has none and reverses only on the opposite signal). A signal is emitted on every qualifying bar (the source enters whenever
flat); the engine keeps one position per symbol."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "bill_williams_fractal_gator"
SOURCE = "StockSharp API example 2077 Bill Williams (https://doc.stocksharp.com/en/api-examples/2077_Bill_Williams); B. Williams, Alligator and fractals"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"jaw": 13, "teeth": 8, "lips": 5, "slow_pct": 0.30, "fast_pct": 0.15, "filter_pct": 0.05, "hold_bars": 20}


def smma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(x.size, np.nan)
    prev = np.nan
    acc = 0.0
    for i in range(x.size):
        p = int(pos[i])
        if p == 0:
            acc = 0.0
            prev = np.nan
        if p < n:
            acc += float(x[i])
            if p == n - 1:
                prev = acc / n
                out[i] = prev
            continue
        prev = (prev * (n - 1) + float(x[i])) / n
        out[i] = prev
    return out


def signals(
    bars: Bars,
    jaw: int = 13,
    teeth: int = 8,
    lips: int = 5,
    slow_pct: float = 0.30,
    fast_pct: float = 0.15,
    filter_pct: float = 0.05,
    hold_bars: int = 20,
) -> list[Signal]:
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        m = len(b)
        if m == 0:
            continue
        pos = segment_pos(b.t)
        mp = (b.h + b.low) / 2.0
        jw, tt, lp = smma(mp, int(jaw), pos), smma(mp, int(teeth), pos), smma(mp, int(lips), pos)
        up_f = np.nan  # last confirmed up fractal (of the mirrored bars)
        dn_f = np.nan
        for i in range(m):
            p = int(pos[i])
            if p == 0:
                up_f = dn_f = np.nan
            if p >= 4:
                mid = i - 2
                w_h = np.delete(b.h[i - 4 : i + 1], 2)
                w_l = np.delete(b.low[i - 4 : i + 1], 2)
                if b.h[mid] > w_h.max():
                    up_f = float(b.h[mid])
                if b.low[mid] < w_l.min():
                    dn_f = float(b.low[mid])
            if not (np.isfinite(jw[i]) and np.isfinite(tt[i]) and np.isfinite(lp[i]) and np.isfinite(up_f) and np.isfinite(dn_f)):
                continue
            ac = abs(float(b.c[i]))
            flt = filter_pct / 100.0 * ac
            if not (abs(jw[i] - tt[i]) >= slow_pct / 100.0 * ac and abs(lp[i] - tt[i]) >= fast_pct / 100.0 * ac):
                continue
            if b.h[i] >= up_f + flt and b.c[i] > b.o[i] and up_f >= tt[i]:
                e = float(b.c[i])
                st = dn_f - flt
                if st < e:
                    out.append(Signal(b.close_time(i), side, sgn * e, sgn * st, None, hold_ms))
    return out
