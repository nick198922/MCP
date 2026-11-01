"""Entry filters for strategy A (cash-and-carry) against perp-leg delisting (item 8g of IDEA-20261031).

Two pre-registered filters, both causal (they read only data closed at or before the decision time ``t``):

* F1, perp age: ``t - first_perp_bar >= min_age_days`` where ``first_perp_bar`` is the open of the first daily perp
  bar with base volume > 0 in the archive (a bar opening after ``t`` means "not listed yet" -> False);
* F2, spot liquidity: median daily spot quote volume (USDT) over the daily bars that opened in
  ``[day(t) - window_days, day(t))`` (so they closed at or before ``t``) ``>= min_volume``, with at least
  ``min_days`` such bars (fewer -> False; a zero-volume bar counts as 0).

A symbol without data for a filter is not allowed (nothing is substituted). Pure functions, no I/O; volumes in the
quote currency (USDT), times in ms UTC.
"""

from __future__ import annotations

import bisect
import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence

DAY_MS = 86_400_000


def first_traded_open(daily: Sequence[Sequence[float]]) -> int | None:
    """Open (ms) of the first daily bar ``[open_ms, ..., base_volume, ...]`` with base volume > 0 (column 5)."""
    for r in sorted(daily, key=lambda r: r[0]):
        if r[5] > 0:
            return int(r[0])
    return None


def age_ok(first_open_ms: int | None, t: int, min_age_days: float) -> bool:
    """F1: the perp has traded for at least ``min_age_days`` at ``t`` (unknown listing -> False)."""
    return first_open_ms is not None and t - first_open_ms >= min_age_days * DAY_MS


def median_volume(opens: Sequence[int], vols: Sequence[float], t: int, *, window_days: int = 30, min_days: int = 20) -> float | None:
    """Median quote volume of the daily bars opened in ``[day(t) - window_days, day(t))``; ``opens`` sorted.

    Bars opened at or after the start of ``t``'s day have not closed at ``t`` and are never read. ``None`` when
    fewer than ``min_days`` bars are in the window."""
    day = t // DAY_MS * DAY_MS
    lo = bisect.bisect_left(opens, day - window_days * DAY_MS)
    hi = bisect.bisect_left(opens, day)
    if hi - lo < min_days:
        return None
    return statistics.median(vols[lo:hi])


def volume_ok(opens: Sequence[int], vols: Sequence[float], t: int, min_volume: float, *, window_days: int = 30, min_days: int = 20) -> bool:
    """F2: median daily quote volume before ``t`` >= ``min_volume`` (too little history -> False)."""
    m = median_volume(opens, vols, t, window_days=window_days, min_days=min_days)
    return m is not None and m >= min_volume


def make_entry_filter(
    *,
    first_open: Mapping[str, int | None] | None = None,
    min_age_days: float | None = None,
    spot_daily: Mapping[str, Sequence[Sequence[float]]] | None = None,
    min_volume: float | None = None,
    window_days: int = 30,
    min_days: int = 20,
) -> Callable[[str, int], bool]:
    """``entry_allowed(sym, t)``: F1 if ``min_age_days`` is set, F2 if ``min_volume`` is set, both -> F1 and F2.

    ``first_open``: symbol -> first traded perp daily open (``first_traded_open``); ``spot_daily``: symbol -> daily
    spot rows ``[open_ms, open, high, low, close, base_volume, quote_volume]``. Results are memoised per (symbol,
    UTC day) for F2 (the window only moves at day boundaries)."""
    fo = dict(first_open or {})
    series: dict[str, tuple[list[int], list[float]]] = {}
    for sym, rows in (spot_daily or {}).items():
        rs = sorted(rows, key=lambda r: r[0])
        series[sym] = ([int(r[0]) for r in rs], [float(r[6]) for r in rs])
    memo: dict[tuple[str, int], bool] = {}

    def allowed(sym: str, t: int) -> bool:
        if min_age_days is not None and not age_ok(fo.get(sym), t, min_age_days):
            return False
        if min_volume is not None:
            key = (sym, t // DAY_MS)
            if key not in memo:
                s = series.get(sym)
                memo[key] = s is not None and volume_ok(s[0], s[1], t, min_volume, window_days=window_days, min_days=min_days)
            if not memo[key]:
                return False
        return True

    return allowed


def block_symbols(blocked: Iterable[str]) -> Callable[[str, int], bool]:
    """Placebo filter: never open the symbols in ``blocked``."""
    b = frozenset(blocked)
    return lambda sym, t: sym not in b


def random_blocked(symbols: Iterable[str], k: int, seed: int) -> list[str]:
    """``k`` symbols drawn without replacement (sorted input, so the draw depends only on the set and the seed)."""
    pool = sorted(symbols)
    return sorted(random.Random(seed).sample(pool, min(k, len(pool))))
