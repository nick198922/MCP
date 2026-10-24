"""One rung of a grid bot (IDEA-20261024-grid-short-vol-regime, step 1): a grid buys low and sells high inside a range,
which is mechanically short gamma. The sieve trades one position per symbol, so this is a test of a single rung, not of
a grid with inventory.

Rules on closed 1h bars (everything at bar i uses bars <= i only; windows never cross a data gap > 48 h, see
``course_s1_spot.segments``), fixed before the first run (the critic's six clarifications):

- r_j = ln(c_j / c_{j-1}), hourly log returns.
- sigma24h = sample SD of the last 24 returns x sqrt(24) (daily realised vol, decimal); the step is
  ``step_sigma`` x sigma24h as a fraction of the entry price.
- sigma7 / sigma30 = SD of the last 168 returns / SD of the last 720 returns (a ratio, no scaling).
- ER20 (Kaufman efficiency ratio over 20 days) = |c_i - c_{i-480}| / sum_{j=i-479..i} |c_j - c_{j-1}|.
- location in the 20-day range = (c_i - L) / (H - L), H / L = highest high / lowest low of the last 480 bars (bar i
  included).
- long condition: location <= ``third``; short condition: location >= 1 - ``third``. With ``regime`` = 1 both also
  need ER20 < ``er_max`` and sigma7 / sigma30 <= ``vol_ratio_max`` (the range regime filter); ``regime`` = 0 is the
  unfiltered null (a grid set on the 20-day range: buy in its lower third, sell in its upper third).
- a signal only on the transition of the side's condition from false (at bar i-1) to true (at bar i); the first warm
  bar of a segment counts as "false before" (as ``above_prev`` in ``donchian_breakout``).
- entry = close of bar i, target = 1 step in favour (``target_steps``), stop = 3 steps against (``stop_steps``) measured
  from the entry only (not the range boundary), time exit after ``hold_h`` hours.
- warm-up: 720 returns inside the segment (the longest window), the same for the filtered and the unfiltered runs so
  that both see the same eligible bars.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s1_spot import rolling_extreme, seg_sma, seg_start, segments
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import Signal

NAME = "grid_ladder"
SOURCE = "docs/ideas/IDEA-20261024-grid-short-vol-regime.md (Bybit grid bot as short vol; one rung)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {
    "step_sigma": 0.5,
    "regime": 1,
    "er_max": 0.25,
    "vol_ratio_max": 1.0,
    "third": 1.0 / 3.0,
    "stop_steps": 3.0,
    "target_steps": 1.0,
    "hold_h": 72,
}
W_DAY = 24
W_7D = 168
W_30D = 720
W_20D = 480
MAX_STOP_FRAC = 0.9  # a stop this far below a long entry would be at or under zero: no signal


def _log_returns(c: np.ndarray, start: np.ndarray) -> np.ndarray:
    """ln(c_j / c_{j-1}); 0 at the first bar of a segment (never inside a used window, see ``_rolling_sd``)."""
    r = np.zeros(c.size)
    if c.size > 1:
        r[1:] = np.log(c[1:] / c[:-1])
    r[start == np.arange(c.size)] = 0.0
    return r


def _rolling_sd(r: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    """Sample SD of the last ``n`` returns, all inside the segment (the bar's position in it >= n); NaN before."""
    m1 = seg_sma(r, seg, n)
    m2 = seg_sma(r * r, seg, n)
    var = np.maximum(m2 - m1 * m1, 0.0) * n / (n - 1)
    out = np.sqrt(var)
    pos = np.arange(r.size) - seg_start(seg)
    out[pos < n] = np.nan
    return out


def features(bars: Bars) -> dict[str, np.ndarray]:
    """Per-bar inputs of the rung (NaN where a window is not full): sigma24h, vol_ratio (sigma7 / sigma30), er20,
    loc (location in the 20-day range) and warm (all windows full)."""
    c = np.asarray(bars.c, dtype=float)
    n = c.size
    seg = segments(bars.t)
    start = seg_start(seg)
    pos = np.arange(n) - start
    r = _log_returns(c, start)
    sig24 = _rolling_sd(r, seg, W_DAY) * math.sqrt(W_DAY)
    ratio = _rolling_sd(r, seg, W_7D) / _rolling_sd(r, seg, W_30D)
    dc = np.zeros(n)
    if n > 1:
        dc[1:] = np.abs(np.diff(c))
    dc[start == np.arange(n)] = 0.0
    path = seg_sma(dc, seg, W_20D) * W_20D
    er = np.full(n, np.nan)
    ok = pos >= W_20D
    idx = np.flatnonzero(ok)
    er[idx] = np.abs(c[idx] - c[idx - W_20D]) / np.where(path[idx] > 0, path[idx], np.nan)
    hi = rolling_extreme(bars.h, seg, W_20D, how="max")
    lo = rolling_extreme(bars.low, seg, W_20D, how="min")
    width = hi - lo
    loc = (c - lo) / np.where(width > 0, width, np.nan)
    warm = (pos >= W_30D) & np.isfinite(sig24) & np.isfinite(ratio) & np.isfinite(er) & np.isfinite(loc) & (sig24 > 0)
    return {"sigma24h": sig24, "vol_ratio": ratio, "er20": er, "loc": loc, "warm": warm, "start": start}


def signals(
    bars: Bars,
    step_sigma: float = 0.5,
    regime: int = 1,
    er_max: float = 0.25,
    vol_ratio_max: float = 1.0,
    third: float = 1.0 / 3.0,
    stop_steps: float = 3.0,
    target_steps: float = 1.0,
    hold_h: int = 72,
) -> list[Signal]:
    n = len(bars)
    if n == 0:
        return []
    f = features(bars)
    warm = f["warm"]
    with np.errstate(invalid="ignore"):
        gate = warm.copy()
        if int(regime):
            gate &= (f["er20"] < er_max) & (f["vol_ratio"] <= vol_ratio_max)
        cond = {"long": gate & (f["loc"] <= third), "short": gate & (f["loc"] >= 1.0 - third)}
    first = f["start"] == np.arange(n)
    hold_ms = int(hold_h) * HOUR_MS
    out: list[Signal] = []
    for side, sgn in (("long", 1.0), ("short", -1.0)):
        cnd = cond[side]
        prev = np.concatenate([[False], cnd[:-1]]) & ~first
        for i in np.flatnonzero(cnd & ~prev):
            step = float(step_sigma) * float(f["sigma24h"][i])
            if not (step > 0 and stop_steps * step < MAX_STOP_FRAC):
                continue
            e = float(bars.c[i])
            stop, target = e * (1 - sgn * stop_steps * step), e * (1 + sgn * target_steps * step)
            out.append(Signal(bars.close_time(int(i)), side, e, stop, target, hold_ms))
    return out
