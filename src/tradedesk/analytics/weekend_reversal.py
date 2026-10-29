"""Weekend idiosyncratic reversal of USDT-M alt perps: does a coin's beta-neutral weekend move revert on Monday?

Pure functions (no I/O). Time is passed in as UTC hour indices (``ms // HOUR_MS``) and UTC day indices
(``ms // DAY_MS``; day 0 = Thursday 1970-01-01). Returns and funding are decimals (0.01 = 1 %).
Idea card: docs/ideas/IDEA-20261029-weekend-idio-reversal-alt-perps.md (sections "Тріаж критика", "Попередня реєстрація").

Registered calendar (critic condition 1: no shared print between the signal and the target):
- signal window: day ``d0`` 00:00 UTC -> day ``d0 + 1`` 22:00 UTC (weekend: ``d0`` = Saturday, i.e. Friday's close
  -> Sunday 22:00);
- target window: day ``d0 + 2`` 01:00 UTC -> 24 hours later (weekend: Monday 01:00 -> Tuesday 01:00);
- a price "at hour h" is the open of the 1h bar that starts at h (the first trade of that hour);
- both windows are made beta-neutral with the same trailing 30-day daily beta to BTC (days before ``d0``).
The weekday placebo uses the same construction with ``d0`` = Tuesday.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .funding_crowding import Bars
from .xs_factors import rank_ic

HOUR_MS = 3_600_000
DAY_H = 24


def weekday(day: int) -> int:
    """ISO weekday of a UTC day index, Monday = 0 ... Sunday = 6 (day 0 is Thursday 1970-01-01)."""
    return (day + 3) % 7


@dataclass(frozen=True)
class HourBar:
    open: float
    high: float
    low: float
    close: float
    qv: float


class HourBars:
    """1h bars keyed by UTC hour index; only bars that traded (base volume > 0, prices > 0) are kept."""

    def __init__(self, bars: Mapping[int, HourBar]) -> None:
        self.bars = dict(bars)

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[float]]) -> HourBars:
        """Rows ``[open_ms, o, h, l, c, base_vol, quote_vol]`` (the archive layout)."""
        out: dict[int, HourBar] = {}
        for r in rows:
            if len(r) < 7 or r[5] <= 0 or min(r[1], r[2], r[3], r[4]) <= 0:
                continue
            out[int(r[0]) // HOUR_MS] = HourBar(float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6]))
        return cls(out)

    def price_at(self, hour: int) -> float | None:
        """Open of the bar that starts at ``hour`` (exact hour, no fallback to a neighbouring bar)."""
        b = self.bars.get(hour)
        return b.open if b is not None else None

    def bar(self, hour: int) -> HourBar | None:
        return self.bars.get(hour)


@dataclass(frozen=True)
class Window:
    """Hour indices of the signal window [sig_start, sig_end] and the target window [tgt_start, tgt_end]."""

    sig_start: int
    sig_end: int
    tgt_start: int
    tgt_end: int


def calendar_window(d0: int, *, sig_end_hour: int = 22, tgt_start_hour: int = 1, hold_hours: int = 24) -> Window:
    """Signal ``d0`` 00:00 -> ``d0 + 1`` at ``sig_end_hour``; target ``d0 + 2`` at ``tgt_start_hour`` for ``hold_hours``."""
    if not 0 <= sig_end_hour <= 23 or not 0 <= tgt_start_hour <= 23 or hold_hours <= 0:
        raise ValueError("bad window hours")
    sig_end = (d0 + 1) * DAY_H + sig_end_hour
    tgt_start = (d0 + 2) * DAY_H + tgt_start_hour
    if tgt_start <= sig_end:
        raise ValueError("the target must start after the signal ends")
    return Window(d0 * DAY_H, sig_end, tgt_start, tgt_start + hold_hours)


def idio_return(p0: float, p1: float, b0: float, b1: float, beta: float) -> float:
    """Beta-neutral return (p1 / p0 - 1) - beta * (b1 / b0 - 1)."""
    return (p1 / p0 - 1.0) - beta * (b1 / b0 - 1.0)


def span_idio(hb: HourBars, btc: HourBars, h0: int, h1: int, beta: float) -> float | None:
    """Beta-neutral return between the prices at hours ``h0`` and ``h1``; None when any of the four prices is missing."""
    p0, p1, b0, b1 = hb.price_at(h0), hb.price_at(h1), btc.price_at(h0), btc.price_at(h1)
    if p0 is None or p1 is None or b0 is None or b1 is None:
        return None
    return idio_return(p0, p1, b0, b1, beta)


def turnover_ratio(bars: Bars, d0: int, *, ndays: int = 2, ref_days: int = 28, min_ref: int = 10) -> float | None:
    """Mean daily quote volume over days ``[d0, d0 + ndays)`` / median quote volume of the weekdays (Mon-Fri) in
    ``[d0 - ref_days, d0)``. None when a window day has no bar or there are fewer than ``min_ref`` reference days."""
    win = [bars.qv.get(d) for d in range(d0, d0 + ndays)]
    if any(v is None for v in win):
        return None
    ref = [bars.qv[d] for d in range(d0 - ref_days, d0) if d in bars.qv and weekday(d) < 5]
    if len(ref) < min_ref:
        return None
    med = statistics.median(ref)
    return statistics.fmean(win) / med if med > 0 else None  # type: ignore[arg-type]


def funding_sum(records: Sequence[Sequence[float]], lo_ms: int, hi_ms: int) -> float:
    """Sum of funding rates settled in ``(lo_ms, hi_ms]`` (records ``[time_ms, interval_h, rate]``; positive: longs pay)."""
    return float(sum(r[2] for r in records if lo_ms < r[0] <= hi_ms))


def maker_short_filled(bar: HourBar) -> bool:
    """A sell limit resting at the bar's open fills only if the price later trades above it (conservative: high > open)."""
    return bar.high > bar.open


def top_k(values: Mapping[str, float], k: int, *, largest: bool = True) -> list[str]:
    """The ``k`` keys with the largest (or smallest) values; ties broken by key for determinism."""
    s = -1.0 if largest else 1.0
    return [key for key, _ in sorted(values.items(), key=lambda kv: (s * kv[1], kv[0]))[:k]]


def quintile_size(n: int, *, frac: float = 0.2) -> int:
    return max(1, int(math.floor(n * frac)))


def capped_weight(k: int, *, cap: float = 0.05) -> float:
    """Equal weight per name in a leg of ``k`` names, capped at ``cap`` (the leg is under-invested when 1/k > cap)."""
    return min(1.0 / k, cap) if k > 0 else 0.0


def tercile_labels(xs: Sequence[float]) -> list[int]:
    """0 / 1 / 2 by rank (lowest third = 0); sizes as equal as possible, ties broken by position."""
    n = len(xs)
    order = sorted(range(n), key=lambda i: (xs[i], i))
    out = [0] * n
    for r, i in enumerate(order):
        out[i] = min(2, 3 * r // n)
    return out


def cross_ic(sig: Mapping[str, float], tgt: Mapping[str, float], *, min_n: int = 50) -> float | None:
    """Rank IC over the symbols present in both maps (pairs a signal with a target of another week for the shift
    placebo); None with fewer than ``min_n`` common symbols."""
    common = sorted(set(sig) & set(tgt))
    if len(common) < min_n:
        return None
    return rank_ic([sig[s] for s in common], [tgt[s] for s in common])


def dominated(ws: Sequence[float], *, k: float = 10.0) -> bool:
    """True when one value sits more than ``k`` MADs from the cross-sectional median (one coin dominates the week)."""
    a = np.asarray(ws, dtype=float)
    if a.size < 3:
        return False
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med)))
    return bool(mad > 0 and np.max(np.abs(a - med)) > k * mad)
