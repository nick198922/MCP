"""S3 of the demo trader: a one-bar sweep of a tight range's low (high for a short) closing back inside, below the
middle. Entry at the close, stop under the sweep low minus 0.5 ATR14, target the range middle; 4h (hold 7 days) and 1d
(hold 30 days), long and short. Same detector and levels as ``scripts/verify_demo_stop_management.py`` (setup "S3"),
which found E[R] IS under rule B of about -0.05R; the sieve test reproduces it."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "s3_range_sweep"
SOURCE = "docs/verification/2026-10-22-demo-stop-management.md (setup S3)"
TIMEFRAMES = (4, 24)
MANAGEMENT = "B"
PARAMS = {"w": 30, "width_atr": 6.0, "stop_atr": 0.5}
HOLD_MS = {4: 7 * DAY_MS, 24: 30 * DAY_MS}


def signals(bars: Bars, w: int = 30, width_atr: float = 6.0, stop_atr: float = 0.5) -> list[Signal]:
    hold = HOLD_MS.get(bars.bar_ms // 3_600_000, 7 * DAY_MS)
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        b = bars if side == "long" else bars.mirrored()
        pos = segment_pos(b.t)
        a = atr(b, 14, pos)
        for ev in range_events(b, pos, a, w=int(w), width_atr=float(width_atr)):
            if ev.kind != "sweep":
                continue
            ar = float(a[ev.r])
            if not (np.isfinite(ar) and ar > 0):
                continue
            e, st, tg = float(b.c[ev.r]), ev.ld - stop_atr * ar, ev.mid
            out.append(Signal(b.close_time(ev.r), side, sgn * e, sgn * st, sgn * tg, hold))
    return out
