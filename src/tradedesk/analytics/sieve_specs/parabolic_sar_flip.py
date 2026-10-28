"""Parabolic SAR stop-and-reverse flip (J. W. Wilder, "New Concepts in Technical Trading Systems", 1978; parameters and
update rules from the LuxAlgo library page). AF starts at 0.02, rises by 0.02 on each new extreme point (EP), cap 0.20.
SAR(t+1) = SAR(t) + AF * (EP - SAR(t)), clamped to the lows (uptrend) / highs (downtrend) of the last two bars. When the
bar touches or crosses the SAR the trend flips: the new SAR is the old EP and AF resets to 0.02.

Signal: the close of the bar on which the SAR flips. Flip up is long, flip down is short; the stop is the new SAR
(the old extreme point, Wilder's stop-and-reverse level). Daily bars.

Defaults added by the harvester, fixed before any run: the source's always-in exit (next flip) and trailing SAR need
future bars, so the exit is by time after ``hold_bars`` = 20 bars; the stop is the fixed SAR at the flip. The source
does not say how to seed the first SAR: the first trend of a segment is up if the second close is not below the first,
SAR = the first bar's low (down: high), EP = the second bar's extreme; signals in the first ``warmup`` = 50 bars of a
segment are dropped because the seed has not washed out. A flip whose close is beyond the new SAR (stop on the wrong
side of the entry) is skipped. The optional ADX regime filter named by the source is not applied."""

from __future__ import annotations

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "parabolic_sar_flip"
SOURCE = "J. W. Wilder (1978), Parabolic SAR; LuxAlgo (https://www.luxalgo.com/library/concept/parabolic-sar.md)"
TIMEFRAMES = (24,)
MANAGEMENT = "strategy"
PARAMS = {"af0": 0.02, "af_step": 0.02, "af_max": 0.20, "hold_bars": 20, "warmup": 50}


def signals(
    bars: Bars, af0: float = 0.02, af_step: float = 0.02, af_max: float = 0.20, hold_bars: int = 20, warmup: int = 50
) -> list[Signal]:
    pos = segment_pos(bars.t)
    h, lo, c = bars.h, bars.low, bars.c
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    up = True
    sar = ep = af = 0.0
    for i in range(len(bars)):
        p = int(pos[i])
        if p == 0:
            continue
        if p == 1:
            up = bool(c[i] >= c[i - 1])
            sar = float(lo[i - 1]) if up else float(h[i - 1])
            ep = float(h[i]) if up else float(lo[i])
            af = af0
            continue
        s = sar + af * (ep - sar)
        if up:
            s = min(s, float(lo[i - 1]), float(lo[i - 2]))
            flip = bool(lo[i] <= s)
        else:
            s = max(s, float(h[i - 1]), float(h[i - 2]))
            flip = bool(h[i] >= s)
        if flip:
            sar, up, af = ep, not up, af0
            ep = float(h[i]) if up else float(lo[i])
            e = float(c[i])
            if p >= warmup and ((up and sar < e) or (not up and sar > e)):
                out.append(Signal(bars.close_time(i), "long" if up else "short", e, sar, None, hold_ms))
        else:
            sar = s
            if up and h[i] > ep:
                ep, af = float(h[i]), min(af + af_step, af_max)
            elif not up and lo[i] < ep:
                ep, af = float(lo[i]), min(af + af_step, af_max)
    return out
