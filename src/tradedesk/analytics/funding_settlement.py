"""Price and funding around a USDT-M funding settlement (IDEA-20261020-funding-settlement-window).

Pure functions, no I/O; time enters as parameters (epoch ms). Rates are decimals per funding interval (0.003 = 0.30 %).

- ``predicted_funding``: the funding Binance will settle, estimated at the entry bar from 1m ``premiumIndexKlines``
  closes (weighted average premium with weights 1..n by minute, plus clamp(I - P, +-0.05 %), optional symbol cap). It
  reads only premium bars that opened in [interval start, entry bar open], so nothing at or after the entry.
- ``cap_from_history``: a symbol's funding cap inferred from earlier settlements (the same extreme |rate| at least twice).
- ``scale_mode`` / ``final_prediction``: non-8h symbols settle at the raw formula or at raw x hours/8 (Binance changed
  this around 2025-09); the mode is chosen from settlements already published, so the signal stays causal.
- ``settlement_trade``: a receive-the-funding trade on the perp: enter at the close of the bar opening at T + entry_off
  minutes, exit at the close of the bar opening at T + exit_off minutes; price return, funding, MAE.
- ``window_return``: side-signed close-to-close return between two bars (placebo and H2 windows).
- ``round_trip_cost`` / ``trailing_median``: taker fee plus a turnover-ladder slippage; 7-day median turnover.

Returns are on notional (not on capital).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

FEE_TAKER = 0.0005  # per side, VIP0 without the BNB discount
INTEREST_8H = 0.0001  # Binance interest component, 0.01 % per 8 h
CLAMP = 0.0005  # +-0.05 % clamp of (I - P)

Bar = Sequence[float]  # (open, high, low, close, quote volume)


def interval_start(t_ms: int, hours: float, prev_settlement_ms: int | None) -> int:
    """Start of the funding interval that settles at ``t_ms``: the previous settlement if it lies within 1.5 intervals
    before ``t_ms``, else ``t_ms - hours``."""
    span = int(round(hours * HOUR_MS))
    if prev_settlement_ms is not None and 0 < t_ms - prev_settlement_ms <= 1.5 * span:
        return int(prev_settlement_ms)
    return t_ms - span


def cap_from_history(history: Sequence[tuple[int, float]], t_ms: int, *, window_days: int = 30, min_hits: int = 2) -> float | None:
    """Funding cap of a symbol inferred from settlements in ``[t_ms - window_days, t_ms)``: the largest |rate| if that
    exact value occurs at least ``min_hits`` times (a clamped rate repeats exactly), else None."""
    lo = t_ms - window_days * DAY_MS
    vals = [abs(r) for t, r in history if lo <= t < t_ms]
    if not vals:
        return None
    m = max(vals)
    if m <= 0:
        return None
    hits = sum(1 for v in vals if abs(v - m) <= 1e-12)
    return m if hits >= min_hits else None


def predicted_funding(
    premium: Mapping[int, float],
    start_ms: int,
    entry_bar_ms: int,
    hours: float,
    *,
    cap: float | None = None,
    min_coverage: float = 0.5,
    weighted: bool = True,
) -> float | None:
    """Estimate of the settling funding rate from premium bars opening in ``[start_ms, entry_bar_ms]``.

    ``premium``: 1m premium index close by bar open time (ms). Weight of a bar = its minute index in the interval + 1
    (Binance weights later samples more); ``weighted=False`` gives the plain mean. None if fewer than ``min_coverage``
    of those minutes have a bar."""
    if entry_bar_ms < start_ms:
        return None
    n_min = (entry_bar_ms - start_ms) // MIN_MS + 1
    num = den = 0.0
    k = 0
    for i in range(n_min):
        t = start_ms + i * MIN_MS
        p = premium.get(t)
        if p is None:
            continue
        w = float(i + 1) if weighted else 1.0
        num += w * p
        den += w
        k += 1
    if k == 0 or k < min_coverage * n_min:
        return None
    avg = num / den
    rate_i = INTEREST_8H * hours / 8.0
    f = avg + min(max(rate_i - avg, -CLAMP), CLAMP)
    if cap is not None:
        f = min(max(f, -cap), cap)
    return f


def scale_factor(mode: str, hours: float) -> float:
    """Factor on the raw 8h-style formula: ``unscaled`` 1, ``scaled`` hours/8 (Binance, non-8h symbols, from ~2025-09)."""
    return hours / 8.0 if mode == "scaled" else 1.0


def scale_mode(history: Sequence[tuple[float, float, float]], *, min_n: int = 3) -> str | None:
    """Which formula fits earlier settlements better: ``scaled`` (raw x hours/8) or ``unscaled`` (raw).

    ``history``: (interval hours, raw prediction, settled rate) of settlements already published, non-8h intervals,
    not at the cap. Picks the mode with the smaller median |log(settled / (factor * raw))| over pairs with the same
    sign; None with fewer than ``min_n`` usable pairs. Found on 2026-10-20: Binance settled non-8h symbols at the raw
    formula until mid-September 2025 and at raw x hours/8 after it."""
    errs: dict[str, list[float]] = {"unscaled": [], "scaled": []}
    for h, raw, f in history:
        if h == 8 or raw == 0 or f == 0 or (raw > 0) != (f > 0):
            continue
        for m in errs:
            errs[m].append(abs(math.log(f / (scale_factor(m, h) * raw))))
    if len(errs["scaled"]) < min_n:
        return None
    med = {m: statistics.median(v) for m, v in errs.items()}
    return "scaled" if med["scaled"] < med["unscaled"] else "unscaled"


def final_prediction(raw: float, hours: float, mode: str, cap: float | None) -> float:
    """Raw prediction x the mode's factor, clipped to the symbol cap if known."""
    f = raw * scale_factor(mode, hours)
    if cap is not None:
        f = min(max(f, -cap), cap)
    return f


def receiver_side(rate: float) -> int:
    """Side that receives funding: +1 (long) when the rate is negative, -1 (short) when positive, 0 at zero."""
    return 1 if rate < 0 else (-1 if rate > 0 else 0)


@dataclass(frozen=True)
class SettlementTrade:
    side: int
    p_in: float
    p_out: float
    price_ret: float  # side * (p_out / p_in - 1)
    funding: float  # funding received, decimal of notional: -side * rate
    mae: float  # worst side-signed excursion from entry over the bars after the entry bar up to the exit bar (<= 0)

    @property
    def gross(self) -> float:
        return self.price_ret + self.funding


def _close(bars: Mapping[int, Bar], t: int) -> float | None:
    b = bars.get(t)
    return float(b[3]) if b is not None and b[3] > 0 else None


def settlement_trade(
    bars: Mapping[int, Bar],
    t_ms: int,
    side: int,
    rate: float,
    *,
    entry_off: int = -3,
    exit_off: int = 1,
    worse: bool = False,
) -> SettlementTrade | None:
    """Trade through the settlement at ``t_ms`` on 1m perp bars (keyed by open ms; rows open, high, low, close, ...).

    Entry: close of the bar opening at ``t_ms + entry_off`` min; exit: close of the bar opening at ``t_ms + exit_off``
    min. ``worse``: entry and exit each take the worse (for ``side``) of that bar and the next one. ``rate``: the
    settled funding rate (received: ``-side * rate``). None if a needed bar is missing."""
    if side not in (1, -1):
        return None
    e0 = t_ms + entry_off * MIN_MS
    x0 = t_ms + exit_off * MIN_MS
    p_in = _close(bars, e0)
    p_out = _close(bars, x0)
    if p_in is None or p_out is None:
        return None
    if worse:
        e1, x1 = _close(bars, e0 + MIN_MS), _close(bars, x0 + MIN_MS)
        if e1 is None or x1 is None:
            return None
        p_in = max(p_in, e1) if side > 0 else min(p_in, e1)
        p_out = min(p_out, x1) if side > 0 else max(p_out, x1)
    last = x0 + (MIN_MS if worse else 0)
    worst = 0.0
    t = e0 + MIN_MS
    while t <= last:
        b = bars.get(t)
        if b is not None:
            ex = (float(b[2]) / p_in - 1) if side > 0 else (1 - float(b[1]) / p_in)
            worst = min(worst, ex)
        t += MIN_MS
    return SettlementTrade(side, p_in, p_out, side * (p_out / p_in - 1), -side * rate, worst)


def window_return(bars: Mapping[int, Bar], side: int, a_ms: int, b_ms: int) -> float | None:
    """``side * (close(b) / close(a) - 1)`` for bars opening at ``a_ms`` and ``b_ms``; None if either is missing."""
    pa, pb = _close(bars, a_ms), _close(bars, b_ms)
    if pa is None or pb is None:
        return None
    return side * (pb / pa - 1)


def slippage(turnover: float | None) -> float:
    """Per-side slippage by median daily quote turnover (USD): >= 50 M 0.05 %, >= 5 M 0.10 %, else 0.20 %."""
    if turnover is not None and turnover >= 50e6:
        return 0.0005
    if turnover is not None and turnover >= 5e6:
        return 0.0010
    return 0.0020


def round_trip_cost(turnover: float | None, *, fee: float = FEE_TAKER) -> float:
    """Round-trip cost on notional: 2 x (taker fee + slippage)."""
    return 2 * (fee + slippage(turnover))


def trailing_median(by_day: Mapping[int, float], day: int, *, window: int = 7) -> tuple[float | None, int]:
    """(median over days ``[day - window, day - 1]`` that have a value, number of such days); reads no day >= ``day``."""
    xs = [by_day[d] for d in range(day - window, day) if d in by_day]
    return (statistics.median(xs) if xs else None), len(xs)


def band(rate: float, edges: Sequence[float] = (0.001, 0.002, 0.003, 0.01)) -> str | None:
    """Dose-response band of |rate| with its sign, e.g. ``-[0.30,1.00)``; None below the first edge."""
    a = abs(rate)
    if a < edges[0]:
        return None
    sign = "-" if rate < 0 else "+"
    for lo, hi in zip(edges, edges[1:], strict=False):
        if lo <= a < hi:
            return f"{sign}[{lo * 100:.2f},{hi * 100:.2f})"
    return f"{sign}>={edges[-1] * 100:.2f}"
