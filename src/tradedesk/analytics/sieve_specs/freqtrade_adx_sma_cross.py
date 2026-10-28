"""ADX-filtered SMA cross (Freqtrade ``futures/FAdxSmaStrategy.py``, a sample strategy of the repo): on 1h bars, long when
ADX14 is above 30 and SMA12 crosses above SMA48, short when ADX14 is above 30 and SMA12 crosses below SMA48. Stop 5 %
(``stoploss = -0.05``), ``minimal_roi = {"60": 0.075, "30": 0.1, "0": 0.05}`` (minutes), exit when ADX falls below 30.

Source parameters, used unchanged: ADX 14, SMA 12 and 48, ADX guard 30, stop 5 %. "Crosses" is: previous SMA12 at or
below SMA48, now above (mirrored for the short). ADX is Wilder's (TA-Lib): smoothed +DM, -DM and TR over 14 bars, ADX is
the mean of the first 14 DX values, then Wilder-smoothed.

Sieve adaptation: the source's ROI table depends on elapsed minutes; on 1h bars every check after the entry bar is at
60 minutes or later, so the 60-minute value is used as a fixed target, 7.5 % from the entry (the table's 5 % and 10 %
rows apply only inside the entry hour). The exit "ADX below 30" needs future bars, so a time exit is used.

Default added by the harvester (the source has none): time exit after ``hold_bars`` = 20 bars (20 hours). No variants."""

from __future__ import annotations

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars, segment_pos
from tradedesk.analytics.strategy_sieve import Signal

NAME = "freqtrade_adx_sma_cross"
SOURCE = "Freqtrade strategies repo, futures/FAdxSmaStrategy.py (https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/futures/FAdxSmaStrategy.py)"
TIMEFRAMES = (1,)
MANAGEMENT = "strategy"
PARAMS = {"adx_n": 14, "fast": 12, "slow": 48, "adx_min": 30.0, "stop_pct": 0.05, "tp_pct": 0.075, "hold_bars": 20}


def adx_wilder(b: Bars, n: int, pos: np.ndarray) -> np.ndarray:
    """Wilder's ADX; NaN until ``2 n - 1`` bars into a segment."""
    out = np.full(len(b), np.nan)
    tr_s = pdm_s = mdm_s = 0.0
    dx_acc = 0.0
    adx_prev = np.nan
    for i in range(len(b)):
        p = int(pos[i])
        if p == 0:
            tr_s = pdm_s = mdm_s = 0.0
            dx_acc = 0.0
            adx_prev = np.nan
            continue
        up = float(b.h[i] - b.h[i - 1])
        dn = float(b.low[i - 1] - b.low[i])
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        pc = float(b.c[i - 1])
        tr = max(float(b.h[i] - b.low[i]), abs(float(b.h[i]) - pc), abs(float(b.low[i]) - pc))
        if p <= n:
            tr_s += tr
            pdm_s += pdm
            mdm_s += mdm
            if p < n:
                continue
        else:
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            mdm_s = mdm_s - mdm_s / n + mdm
        if tr_s <= 0:
            dx = 0.0
        else:
            pdi, mdi = 100.0 * pdm_s / tr_s, 100.0 * mdm_s / tr_s
            dx = 0.0 if pdi + mdi == 0 else 100.0 * abs(pdi - mdi) / (pdi + mdi)
        if p < 2 * n - 1:
            dx_acc += dx
        elif p == 2 * n - 1:
            dx_acc += dx
            adx_prev = dx_acc / n
            out[i] = adx_prev
        else:
            adx_prev = (adx_prev * (n - 1) + dx) / n
            out[i] = adx_prev
    return out


def sma(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    out = np.full(x.size, np.nan)
    for i in range(x.size):
        if pos[i] >= n - 1:
            out[i] = float(np.mean(x[i - n + 1 : i + 1]))
    return out


def signals(
    bars: Bars,
    adx_n: int = 14,
    fast: int = 12,
    slow: int = 48,
    adx_min: float = 30.0,
    stop_pct: float = 0.05,
    tp_pct: float = 0.075,
    hold_bars: int = 20,
) -> list[Signal]:
    pos = segment_pos(bars.t)
    ax = adx_wilder(bars, int(adx_n), pos)
    sf = sma(bars.c, int(fast), pos)
    ss = sma(bars.c, int(slow), pos)
    hold_ms = int(hold_bars) * bars.bar_ms
    out: list[Signal] = []
    for i in range(1, len(bars)):
        if not (np.isfinite(ax[i]) and np.isfinite(sf[i]) and np.isfinite(ss[i]) and np.isfinite(sf[i - 1]) and np.isfinite(ss[i - 1])):
            continue
        if not ax[i] > adx_min:
            continue
        e = float(bars.c[i])
        if sf[i - 1] <= ss[i - 1] and sf[i] > ss[i]:
            out.append(Signal(bars.close_time(i), "long", e, e * (1.0 - stop_pct), e * (1.0 + tp_pct), hold_ms))
        elif sf[i - 1] >= ss[i - 1] and sf[i] < ss[i]:
            out.append(Signal(bars.close_time(i), "short", e, e * (1.0 + stop_pct), e * (1.0 - tp_pct), hold_ms))
    return out
