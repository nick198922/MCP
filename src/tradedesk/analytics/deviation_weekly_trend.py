"""Deviation under a range into the "weekly trend" (daily EMA 100/200) and back
(IDEA-20261017-deviation-weekly-trend, the Good_Mood setup).

A 4h sweep or a 1-3 close deviation under the low of a 60-bar range (``course_basics.range_events``, causal rule)
is split by where its extreme sits against the band of the daily EMA 100/200:
  T: the lowest low of bars s .. r lies in [min(EMA100, EMA200) - 0.5 ATR_d, max(EMA100, EMA200) + 0.25 ATR_d] and
     the band's upper edge is below the range low;
  C: that low is above the band's upper edge by more than 0.25 ATR_d (the same event without the touch);
  deep / gap / band_high / na: everything else (reported, not traded as a group).
The daily EMAs and ATR are those of the last daily bar closed at or before the open of bar s (``last_daily_before``),
so a setup decided at the close of bar r uses bars <= r only (``tests/test_deviation_weekly_trend.py`` checks this).

The trade: a buy limit at the range low resting for ``wait`` after the close of r, stop at the extreme minus 0.5 ATR
(4h, bar r), target the nearest of {4h EMA100, range middle} at least 1.5R above the entry, else 2R, time exit after
the hold. The placebo P is a plain test of the range low from above without a breach (``placebo_setups``).

Pure functions, no I/O. Long frame only; shorts are the same logic on ``Bars.mirrored`` prices. Prices are in quote
currency; R is the return on the planned risk (entry - stop), not on capital; fees are decimal fractions of price per
side (0.0007 = taker 0.05 % + slippage 0.02 %).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_fvg_ob import LimitTrade, limit_long
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, MARKET_SIDE, Bars

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "MARKET_SIDE",
    "Params",
    "Setup",
    "classify",
    "deviation_setups",
    "last_daily_before",
    "placebo_setups",
    "target_price",
    "trade_setups",
]


@dataclass(frozen=True)
class Params:
    w: int = 60  # range window, 4h bars before s
    # range width <= width_atr * ATR14(4h)[s - 1]; deviation: kmin .. kmax closes under the range low, then a close
    # back at or above it. The card said 8 ATR and 1-2 closes; the registered primary is 10 ATR and 1-3 closes,
    # calibrated (before any outcome) so that the author's own example, ETH 2026-10-15, is detected
    width_atr: float = 10.0
    kmin: int = 1
    kmax: int = 3
    band_below: float = 0.5  # band = [min(EMA) - 0.5 ATR_d, max(EMA) + 0.25 ATR_d]
    band_above: float = 0.25
    c_gap: float = 0.25  # C: extreme above the band's upper edge by more than 0.25 ATR_d
    stop_atr: float = 0.5  # stop = extreme - 0.5 ATR14(4h)[r]
    min_target_r: float = 1.5
    fallback_r: float = 2.0
    wait_ms: int = 48 * HOUR_MS
    hold_ms: int = 7 * DAY_MS
    max_daily_age_ms: int = 2 * DAY_MS  # the daily context older than this (a hole) is unknown
    placebo_above: int = 10  # P: >= 10 closes above the range middle before the touch
    placebo_touch_atr: float = 0.25  # P: lr <= low[j] <= lr + 0.25 ATR14(4h)[j - 1]


@dataclass(frozen=True)
class Setup:
    kind: str  # dev | sweep | placebo
    s: int  # first bar under the range (dev), the sweep bar, or the placebo touch bar
    r: int  # decision bar: the order rests from its close
    lr: float  # range low = the limit price
    tr: float
    mid: float
    ld: float  # lowest low of bars s .. r (placebo: low of the touch bar)
    group: str  # T | C | deep | gap | band_high | na | P
    stop: float
    target: float
    target_kind: str  # ema100_4h | mid | fixed
    atr4: float  # ATR14(4h) at r
    atr_d: float  # daily ATR14 at the last daily close <= open of s
    e100: float  # daily EMA100, same daily bar
    e200: float

    @property
    def risk(self) -> float:
        return self.lr - self.stop

    @property
    def stop_atr4(self) -> float:
        return self.risk / self.atr4


def last_daily_before(b: Bars, daily: Bars, values: np.ndarray, max_age_ms: int = 2 * DAY_MS) -> np.ndarray:
    """For each bar i of ``b``: ``values`` of the last daily bar closed at or before the OPEN of bar i (the last
    completed day before the bar starts). NaN when there is none or it closed more than ``max_age_ms`` before."""
    out = np.full(len(b), np.nan)
    if len(b) == 0 or len(daily) == 0:
        return out
    close_d = daily.t + daily.bar_ms
    k = np.searchsorted(close_d, b.t, side="right") - 1
    ok = k >= 0
    kk = np.where(ok, k, 0)
    ok &= (b.t - close_d[kk]) <= max_age_ms
    out[ok] = np.asarray(values, dtype=float)[kk[ok]]
    return out


def classify(ld: float, lr: float, e100: float, e200: float, atr_d: float, p: Params = Params()) -> str:
    """Group of an event by its extreme ``ld`` against the daily EMA band (see the module docstring)."""
    if not (np.isfinite(e100) and np.isfinite(e200) and np.isfinite(atr_d) and atr_d > 0):
        return "na"
    lo = min(e100, e200) - p.band_below * atr_d
    hi = max(e100, e200) + p.band_above * atr_d
    if lo <= ld <= hi:
        return "T" if hi < lr else "band_high"
    if ld > hi + p.c_gap * atr_d:
        return "C"
    if ld < lo:
        return "deep"
    return "gap"


def target_price(entry: float, stop: float, levels: Sequence[float], p: Params = Params()) -> tuple[float, int]:
    """The nearest finite level at least ``min_target_r`` R above the entry; else entry + ``fallback_r`` R.
    Returns (price, index of the level or -1 for the fixed target)."""
    risk = entry - stop
    if risk <= 0:
        raise ValueError("stop must be below the entry")
    best, k = np.inf, -1
    for i, lv in enumerate(levels):
        if np.isfinite(lv) and lv >= entry + p.min_target_r * risk and lv < best:
            best, k = float(lv), i
    if k < 0:
        return entry + p.fallback_r * risk, -1
    return best, k


_TKIND = ("ema100_4h", "mid")


def deviation_setups(
    b4: Bars,
    pos4: np.ndarray,
    atr4: np.ndarray,
    ema100_4h: np.ndarray,
    d_e100: np.ndarray,
    d_e200: np.ndarray,
    d_atr: np.ndarray,
    p: Params = Params(),
) -> list[Setup]:
    """Sweeps and ``p.kmin``..``p.kmax`` close deviations (primary 1-3, card variant 1-2) under a ``p.w``-bar range
    (causal ``range_events``), grouped by the daily EMA band. ``d_*`` are aligned to the 4h bars with ``last_daily_before`` (value used: the one at bar s)."""
    out: list[Setup] = []
    for e in range_events(b4, pos4, atr4, p.w, p.width_atr, p.kmin, p.kmax):
        a4 = float(atr4[e.r])
        if not (np.isfinite(a4) and a4 > 0):
            continue
        s = e.s
        g = classify(e.ld, e.lr, float(d_e100[s]), float(d_e200[s]), float(d_atr[s]), p)
        stop = e.ld - p.stop_atr * a4
        tgt, k = target_price(e.lr, stop, (float(ema100_4h[e.r]), e.mid), p)
        out.append(
            Setup(
                e.kind, s, e.r, e.lr, e.tr, e.mid, e.ld, g, stop, tgt, _TKIND[k] if k >= 0 else "fixed",
                a4, float(d_atr[s]), float(d_e100[s]), float(d_e200[s]),
            )
        )  # fmt: skip
    return out


def placebo_setups(
    b4: Bars,
    pos4: np.ndarray,
    atr4: np.ndarray,
    ema100_4h: np.ndarray,
    stop_mult: float,
    p: Params = Params(),
) -> list[Setup]:
    """Placebo P: a plain test of the range low from above, no breach. Bar j with a valid ``p.w``-bar range before it
    (width <= ``p.width_atr`` ATR[j - 1]), the previous ``p.placebo_above`` closes above the range middle and their
    lows above the touch zone, and lr <= low[j] <= lr + ``p.placebo_touch_atr`` ATR[j - 1]. Same limit at lr; stop
    lr - ``stop_mult`` ATR[j] (the median stop of the T group in ATR units); the same target rule."""
    n = len(b4)
    out: list[Setup] = []
    k = p.placebo_above
    for j in range(p.w, n):
        if pos4[j] < p.w:
            continue
        a = float(atr4[j - 1])
        a4 = float(atr4[j])
        if not (np.isfinite(a) and a > 0 and np.isfinite(a4) and a4 > 0):
            continue
        lr = float(b4.low[j - p.w : j].min())
        tr = float(b4.h[j - p.w : j].max())
        if tr - lr > p.width_atr * a:
            continue
        zone = lr + p.placebo_touch_atr * a
        if not (lr <= b4.low[j] <= zone):
            continue
        mid = (tr + lr) / 2
        if not (np.all(b4.c[j - k : j] > mid) and np.all(b4.low[j - k : j] > zone)):
            continue
        stop = lr - stop_mult * a4
        tgt, kk = target_price(lr, stop, (float(ema100_4h[j]), mid), p)
        out.append(
            Setup(
                "placebo", j, j, lr, tr, mid, float(b4.low[j]), "P", stop, tgt, _TKIND[kk] if kk >= 0 else "fixed",
                a4, float("nan"), float("nan"), float("nan"),
            )
        )  # fmt: skip
    return out


def trade_setups(
    path: Bars,
    setups: Sequence[Setup],
    close_ms: np.ndarray,
    p: Params = Params(),
    fee_side: float = MARKET_SIDE,
) -> list[tuple[Setup, LimitTrade | None, bool]]:
    """Simulate setups in decision order, one at a time per call (call it per symbol, side and group): a setup decided
    while the previous order still rests (unfilled, until its expiry) or its trade is still open is skipped.
    Fees: ``fee_side`` on both the entry and the exit (the card's 0.14 % round trip). Returns (setup, trade or None
    when the path has no bar there, skipped)."""
    out: list[tuple[Setup, LimitTrade | None, bool]] = []
    busy = -1
    for st in sorted(setups, key=lambda x: (x.r, x.s)):
        t0 = int(close_ms[st.r])
        if t0 < busy:
            out.append((st, None, True))
            continue
        tr = limit_long(
            path, t0, p.wait_ms, p.hold_ms, st.lr, st.stop, st.target, entry_fee=fee_side, target_fee=fee_side, market_fee=fee_side
        )
        if tr is not None:
            busy = tr.exit_ms if tr.filled else t0 + p.wait_ms
        out.append((st, tr, False))
    return out
