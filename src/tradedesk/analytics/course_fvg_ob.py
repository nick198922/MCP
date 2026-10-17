"""Course setups S12 + S28 (IDEA-20261016-course-setups-crypto, step 4): fair value gaps, order blocks with the
course's validity flags and breakers as reaction levels, traded with a resting limit on the first test.

Pure functions, no I/O. Everything is written for the long side ("long frame": a support zone below the price, a buy
limit at its level); bearish zones are the same logic on negated prices (``Bars.mirrored``). Bars are the step-1
``course_s14_ema.Bars`` (open times in ms UTC, sorted). Detection matches ``markup.fvgs`` / ``markup.order_blocks``
(``tests/test_course_fvg_ob.py`` checks this), but every flag is causal: a flag known at the close of bar k uses bars
<= k only, the ATR is the one of its own bar (``markup`` uses the latest ATR for the whole chart) and a zone's state
never looks ahead. Each flag set of a zone has an ``arm`` bar: the bar at whose close all its flags are known. Trade
outcomes look forward by design (``limit_long``).

Prices are in quote currency, R is the return on the trade's planned risk (level - stop), not on capital. Fees are
decimal fractions of price per side: maker 0.0002 for the limit entry and a limit target, market 0.0007 (taker 0.05 %
+ slippage 0.02 %) for stops, time exits and data ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, MARKET_SIDE, MAX_GAP_MS, Bars

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "MAKER",
    "MARKET_SIDE",
    "LimitTrade",
    "Zone",
    "bos_up",
    "breaker_zones",
    "fvg_zones",
    "limit_long",
    "ob_zones",
    "untouched",
]

MAKER = 0.0002
BOS_WINDOW = 20  # bos_after: a close-BOS on bars e .. i + 20 (markup: i + 1 .. i + bos_window)
BREAK_WINDOW = 60  # breaker: the OB must be broken on bars e + 1 .. e + 60


@dataclass(frozen=True)
class Zone:
    """A support zone in the long frame.

    ``form``: bar at whose close the zone exists (FVG: third candle i; OB: engulfing candle e; breaker: breaking
    candle k). ``arms``: flag-set name -> bar at whose close the set is known (absent: the set never completes).
    ``levels``: level name -> price. ``far``: the far edge for the far-stop variant. ``atr``: ATR of the form bar.
    ``impulse``: FVG only, the middle candle's body in ATR of the first candle (for the impulse-matched placebo).
    """

    kind: str
    form: int
    arms: dict[str, int]
    levels: dict[str, float]
    far: float
    atr: float
    impulse: float = float("nan")
    meta: dict[str, int] = field(default_factory=dict)


def _same_seg(pos: np.ndarray, i: int, back: int) -> bool:
    """Bars i - back .. i lie in one segment."""
    return bool(pos[i] >= back)


def bos_up(b: Bars, pos: np.ndarray) -> np.ndarray:
    """Close-BOS up per bar, as ``markup.structure``: a strict 3-candle fractal high f is active from bar f + 2; a bar
    whose high exceeds active swings removes them all (sweep or BOS), and it is a ``bos_up`` if its close is above at
    least one of them. Swings restart with each segment."""
    n = len(b)
    out = np.zeros(n, dtype=bool)
    active: list[float] = []
    for t in range(n):
        if pos[t] == 0:
            active = []
        if pos[t] >= 3:  # fractal f = t - 2 needs bars f - 1 .. f + 1 in this segment
            f = t - 2
            if b.h[f] > b.h[f - 1] and b.h[f] > b.h[f + 1]:
                active.append(float(b.h[f]))
        if active:
            h, c = b.h[t], b.c[t]
            taken = [p for p in active if h > p]
            if taken:
                out[t] = any(c > p for p in taken)
                active = [p for p in active if not h > p]
    return out


def _bull_fvg(b: Bars, pos: np.ndarray, m: int) -> bool:
    """A bullish FVG with its middle candle at m (bars m-1, m, m+1)."""
    return 1 <= m < len(b) - 1 and _same_seg(pos, m + 1, 2) and bool(b.low[m + 1] > b.h[m - 1])


def fvg_zones(
    b: Bars, pos: np.ndarray, atr: np.ndarray, htf_up: np.ndarray | None = None, lo: int = 2, hi: int | None = None
) -> list[Zone]:
    """Bullish FVGs whose third candle i is in lo .. hi-1. Flag sets: ``all``, ``imp`` (middle body >= ATR[i-2]),
    ``trend`` (``htf_up[i]``: the higher-timeframe trend known at the close of i)."""
    hi = len(b) if hi is None else hi
    out: list[Zone] = []
    for i in range(max(lo, 2), hi):
        if not _same_seg(pos, i, 2) or not b.low[i] > b.h[i - 2]:
            continue
        a = float(atr[i])
        if not np.isfinite(a) or a <= 0:
            continue
        top, bot = float(b.low[i]), float(b.h[i - 2])
        a0 = float(atr[i - 2])
        imp = float((b.c[i - 1] - b.o[i - 1]) / a0) if np.isfinite(a0) and a0 > 0 else float("nan")
        arms = {"all": i}
        if np.isfinite(imp) and imp >= 1.0:
            arms["imp"] = i
        if htf_up is not None and bool(htf_up[i]):
            arms["trend"] = i
        out.append(Zone("fvg", i, arms, {"near": top, "mid": (top + bot) / 2, "full": bot}, bot, a, imp))
    return out


def ob_zones(b: Bars, pos: np.ndarray, atr: np.ndarray, bos: np.ndarray, lo: int = 1, hi: int | None = None) -> list[Zone]:
    """Bullish order blocks: bearish candle i engulfed by the bullish candle e = i + 1 (close[e] > open[i]); e in
    lo .. hi-1. Flag sets ``all`` (known at e), ``imp`` (body of e >= ATR[i]; e), ``fvg`` (bullish FVG with middle e,
    known at e + 1, or middle e + 1, known at e + 2), ``bos`` (first close-BOS up on e .. i + 20), ``valid`` (all
    three: the latest of their bars)."""
    hi = len(b) if hi is None else hi
    n = len(b)
    out: list[Zone] = []
    for e in range(max(lo, 1), hi):
        i = e - 1
        if not _same_seg(pos, e, 1):
            continue
        if not (b.c[i] < b.o[i] and b.c[e] > b.o[e] and b.c[e] > b.o[i]):
            continue
        a = float(atr[e])
        if not np.isfinite(a) or a <= 0:
            continue
        arms = {"all": e}
        ai = float(atr[i])
        known: dict[str, int] = {}
        if np.isfinite(ai) and b.c[e] - b.o[e] >= ai:
            known["imp"] = e
        if _bull_fvg(b, pos, e):
            known["fvg"] = e + 1
        elif _bull_fvg(b, pos, e + 1):
            known["fvg"] = e + 2
        for k in range(e, min(i + BOS_WINDOW, n - 1) + 1):
            if pos[k] < pos[e] + (k - e):  # left the segment
                break
            if bos[k]:
                known["bos"] = k
                break
        arms.update(known)
        if len(known) == 3:
            arms["valid"] = max(known.values())
        body_hi, body_lo = float(b.o[i]), float(b.c[i])
        out.append(
            Zone(
                "ob",
                e,
                arms,
                {"wick": float(b.h[i]), "body": body_hi, "bmid": (body_hi + body_lo) / 2},
                float(min(b.low[i], b.low[e])),
                a,
                meta={"i": i},
            )
        )
    return out


def breaker_zones(b: Bars, pos: np.ndarray, atr: np.ndarray, lo: int = 1, hi: int | None = None) -> list[Zone]:
    """Bullish breakers: a bearish order block (bullish candle i engulfed by the bearish e = i + 1 with close[e] <
    open[i]) broken upwards by the first close above high[i] on bars e + 1 .. e + 60 (bar k, in lo .. hi-1). Flag sets
    ``all`` (k), ``bimp`` (open[k] <= low[i] or |body k| >= ATR[k-1]; k), ``fob`` (a bullish FVG with middle m in
    k-1 .. k+1 overlapping [low[i], high[i]]; known at m + 1), ``valid`` (both). Level: the top of i's body."""
    hi_ = len(b) if hi is None else hi
    n = len(b)
    out: list[Zone] = []
    for e in range(1, n):
        i = e - 1
        if not _same_seg(pos, e, 1):
            continue
        if not (b.c[i] > b.o[i] and b.c[e] < b.o[e] and b.c[e] < b.o[i]):
            continue
        zl, zh = float(b.low[i]), float(b.h[i])
        k = -1
        for j in range(e + 1, min(e + BREAK_WINDOW, n - 1) + 1):
            if pos[j] < pos[e] + (j - e):
                break
            if b.c[j] > zh:
                k = j
                break
        if k < max(lo, 1) or k >= hi_:
            continue
        a = float(atr[k])
        if not np.isfinite(a) or a <= 0:
            continue
        arms = {"all": k}
        known: dict[str, int] = {}
        ak = float(atr[k - 1])
        if b.o[k] <= zl or (np.isfinite(ak) and abs(b.c[k] - b.o[k]) >= ak):
            known["bimp"] = k
        for m in (k - 1, k, k + 1):
            if _bull_fvg(b, pos, m) and b.h[m - 1] < zh and b.low[m + 1] > zl:
                known["fob"] = m + 1
                break
        arms.update(known)
        if len(known) == 2:
            arms["valid"] = max(known.values())
        out.append(Zone("breaker", k, arms, {"body": float(b.c[i])}, zl, a, meta={"i": i}))
    return out


def untouched(b: Bars, form: int, arm: int, level: float) -> bool:
    """The level was not touched (every low above it) on bars form + 1 .. arm."""
    if arm <= form:
        return True
    return bool(b.low[form + 1 : arm + 1].min() > level)


# ---------------------------------------------------------------- the limit trade


@dataclass(frozen=True)
class LimitTrade:
    filled: bool
    touch_ms: int  # open time of the 1h bar that filled the limit (-1 if not filled)
    fill: float
    exit: float
    exit_kind: str  # stop | stop_gap | target | time | data_end | none
    risk: float  # level - stop (planned risk, price units)
    r_gross: float
    cost_r: float
    mfe_r: float  # best high after the fill bar before the exit, in R
    react: int  # 1: +react before -react from the level; 0: the other way (ties count against); -1: neither
    exit_ms: int

    @property
    def r_net(self) -> float:
        return self.r_gross - self.cost_r


def _first(mask: np.ndarray) -> int:
    k = int(np.argmax(mask)) if mask.size else 0
    return k if mask.size and mask[k] else -1


def limit_long(
    path: Bars,
    start_ms: int,
    wait_ms: int,
    hold_ms: int,
    level: float,
    stop: float,
    target: float,
    *,
    react_px: float | None = None,
    pen: float = 0.0,
    entry_fee: float = MAKER,
    target_fee: float = MAKER,
    market_fee: float = MARKET_SIDE,
    max_gap_ms: int = MAX_GAP_MS,
) -> LimitTrade | None:
    """A buy limit at ``level`` resting from ``start_ms`` (a bar close) for ``wait_ms`` on ``path`` (1h bars).

    Fill: the first bar opening in [start, start + wait) with low <= level - pen, at min(open, level). On the fill bar
    only the stop is checked (the order inside the bar is unknown). Then bars opening in [fill bar close, fill bar
    close + hold) are walked: the stop wins a bar that touches both, an open at or below the stop exits at the open,
    the target (a limit) exits at the target, else the last close (``time``). A hole longer than ``max_gap_ms`` or the
    end of the data cuts the wait (not filled) or the hold (``data_end``). ``react_px``: also record whether the price
    went ``react_px`` above the level before going ``react_px`` below it (after the fill, the fill bar's low counts
    against). None when the path has no bar near ``start_ms``."""
    n = len(path)
    i0 = int(np.searchsorted(path.t, start_ms))
    if i0 >= n or int(path.t[i0]) - start_ms > max_gap_ms:
        return None
    risk = level - stop
    if risk <= 0:
        raise ValueError("stop must be below the level")
    iw = int(np.searchsorted(path.t, start_ms + wait_ms))
    if iw > i0:
        holes = np.flatnonzero(np.diff(path.t[i0:iw]) > max_gap_ms)
        if holes.size:
            iw = i0 + int(holes[0]) + 1
    k = _first(path.low[i0:iw] <= level - pen)
    if k < 0:
        return LimitTrade(False, -1, float("nan"), float("nan"), "none", risk, float("nan"), float("nan"), float("nan"), -1, -1)
    f = i0 + k
    fill_open = int(path.t[f])
    fill = min(float(path.o[f]), level)

    def done(px: float, kind: str, j: int, mfe: float, react: int) -> LimitTrade:
        fee_x = target_fee if kind == "target" else market_fee
        gross = (px - fill) / risk
        cost = (entry_fee * abs(fill) + fee_x * abs(px)) / risk
        return LimitTrade(True, fill_open, fill, px, kind, risk, gross, cost, mfe, react, int(path.t[j]) + path.bar_ms)

    h0 = int(np.searchsorted(path.t, fill_open + path.bar_ms))
    h1 = int(np.searchsorted(path.t, fill_open + path.bar_ms + hold_ms))
    cut = False
    if h1 > h0:
        holes = np.flatnonzero(np.diff(path.t[f:h1]) > max_gap_ms)
        if holes.size:
            h1, cut = max(h0, f + int(holes[0]) + 1), True
    lo_, hi_, op = path.low[h0:h1], path.h[h0:h1], path.o[h0:h1]
    react = -1
    if react_px is not None:
        if path.low[f] <= level - react_px:
            react = 0
        else:
            u, d = _first(hi_ >= level + react_px), _first(lo_ <= level - react_px)
            if u >= 0 and (d < 0 or u < d):
                react = 1
            elif d >= 0:
                react = 0
    if path.low[f] <= stop:
        gap = bool(path.o[f] <= stop)
        return done(float(path.o[f]) if gap else stop, "stop_gap" if gap else "stop", f, 0.0, react)
    if h1 <= h0:  # nothing after the fill bar inside the segment
        return done(float(path.c[f]), "data_end", f, 0.0, react)
    s = _first(lo_ <= stop)
    t = _first(hi_ >= target)
    if s >= 0 and (t < 0 or s <= t):
        mfe = max((float(hi_[:s].max()) - fill) / risk, 0.0) if s > 0 else 0.0
        if op[s] <= stop:
            return done(float(op[s]), "stop_gap", h0 + s, mfe, react)
        return done(stop, "stop", h0 + s, mfe, react)
    if t >= 0:
        return done(target, "target", h0 + t, (target - fill) / risk, react)
    j = h1 - 1
    mfe = max((float(hi_.max()) - fill) / risk, 0.0)
    ended = cut or (h1 == n and int(path.t[j]) + path.bar_ms < fill_open + path.bar_ms + hold_ms)
    return done(float(path.c[j]), "data_end" if ended else "time", j, mfe, react)
