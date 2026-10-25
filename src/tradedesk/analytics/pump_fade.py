"""Extreme pump fade: short a mature USDT-M perp after a >= +30 % week, BTC-beta hedged, hold 7 days.

Pure functions (no I/O). Time is passed in as UTC day indices (``ms // DAY_MS``). Returns are decimals on the
notional of the short leg (0.01 = 1 %); funding rates are decimals per settlement (positive: longs pay shorts).
Idea card: docs/ideas/IDEA-20261025-extreme-pump-fade-short.md (section "Попередня реєстрація").

Conventions (registered):
- signal day ``t``: ``r7 = close(t) / close(t - 7) - 1``; entry at the open of ``t + 1``, exit at the close of
  ``t + hold`` (``hold`` days of 24 h);
- universe at ``t`` uses bars up to ``t`` only: current run age >= 90 days, median quote volume over ``[t - 29; t]``;
- one event per symbol: the first trigger, then the next one no earlier than ``t + cooldown``;
- the hedge beta uses daily returns over ``[t - 96; t - 7]`` (before the pump window), clipped to ``[0; 3]``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .funding_crowding import Bars, LegWeek, median_turnover, trailing_beta


def ret_n(bars: Bars, day: int, lookback: int = 7) -> float | None:
    """close(day) / close(day - lookback) - 1; None when either bar is missing."""
    c1, c0 = bars.close.get(day), bars.close.get(day - lookback)
    if c1 is None or c0 is None:
        return None
    return c1 / c0 - 1.0


def universe_member(
    bars: Bars, day: int, starts: Mapping[int, int], *, min_age: int = 90, min_median_qv: float = 5e6
) -> tuple[float | None, float | None, str]:
    """(r7, median turnover, "ok") when the symbol is in the universe on signal day ``day`` (bars <= ``day`` only),
    else (None, None, reason). ``starts`` is ``run_start_map(bars)``."""
    rs = starts.get(day) if day in bars.close else None
    if rs is None:
        return None, None, "no_bar"
    if day - rs < min_age:
        return None, None, "young"
    mq = median_turnover(bars, day + 1)
    if mq is None or mq < min_median_qv:
        return None, None, "illiquid"
    r = ret_n(bars, day)
    if r is None:
        return None, None, "no_r7"
    return r, mq, "ok"


def first_events(triggers: Iterable[tuple[int, float]], threshold: float, *, cooldown: int = 14) -> list[int]:
    """Days with r >= ``threshold``, keeping the first and then only triggers >= ``cooldown`` days after the last kept
    event. Each kept day depends only on triggers up to that day."""
    out: list[int] = []
    for d, r in sorted(triggers):
        if r >= threshold and (not out or d - out[-1] >= cooldown):
            out.append(d)
    return out


def pre_pump_beta(
    bars: Bars, btc: Bars, day: int, *, window: int = 90, gap: int = 7, min_days: int = 60, lo: float = 0.0, hi: float = 3.0
) -> float | None:
    """BTC beta from daily returns over ``[day - gap - window + 1; day - gap]``, clipped to ``[lo; hi]``."""
    b = trailing_beta(bars, btc, day - gap + 1, window=window, min_days=min_days)
    return None if b is None else min(hi, max(lo, b))


@dataclass(frozen=True)
class ShortTrade:
    """Result of one short (on the short leg's notional). ``price`` = -r_coin + beta * r_btc."""

    price_coin: float  # -r_coin
    hedge: float  # beta * r_btc
    funding_coin: float  # funding the short received
    funding_hedge: float  # beta * funding the BTC long received
    cost: float  # positive number, subtracted

    @property
    def price(self) -> float:
        return self.price_coin + self.hedge

    @property
    def funding(self) -> float:
        return self.funding_coin + self.funding_hedge

    @property
    def net(self) -> float:
        return self.price + self.funding - self.cost


def short_trade(
    coin: LegWeek,
    btc: LegWeek | None,
    beta: float,
    *,
    cost_coin: float = 0.0025,
    cost_btc: float = 0.0010,
    coin_r: float | None = None,
    coin_funding: float | None = None,
) -> ShortTrade:
    """Short ``coin`` and (when ``btc`` is given) long ``beta`` x notional of BTC over the same window.

    ``coin_r`` / ``coin_funding`` override the coin leg's price return / long-side funding (used for stops and
    liquidations); costs are round trips per unit of each leg's notional."""
    r = coin.r if coin_r is None else coin_r
    fl = coin.funding_long if coin_funding is None else coin_funding
    if btc is None:
        return ShortTrade(-r, 0.0, -fl, 0.0, cost_coin)
    return ShortTrade(-r, beta * btc.r, -fl, beta * btc.funding_long, cost_coin + beta * cost_btc)


def first_breach(bars: Bars, entry: int, exit_day: int, level: float) -> tuple[int, float] | None:
    """First day in ``[entry; exit_day]`` whose high reaches ``open(entry) * (1 + level)`` and the adverse fill
    return: ``max(level, open(d) / open(entry) - 1)`` (a gap through the level fills at that day's open)."""
    p0 = bars.open.get(entry)
    if p0 is None:
        return None
    stop = p0 * (1.0 + level)
    for d in range(entry, exit_day + 1):
        h = bars.high.get(d)
        if h is None:
            continue
        o = bars.open.get(d, p0)
        if d > entry and o >= stop:
            return d, o / p0 - 1.0
        if h >= stop:
            return d, level
    return None


def placebo_members(
    r7: Mapping[str, float], *, lo: float = 0.40, hi: float = 0.60, exclude: Iterable[str] = (), min_n: int = 20
) -> list[str]:
    """Symbols whose r7 percentile rank (rank / (N - 1), ties by symbol) is in ``[lo; hi]``; [] with N < ``min_n``.
    The percentile is computed over all members; ``exclude`` (the day's events) is dropped after ranking."""
    n = len(r7)
    if n < min_n:
        return []
    order = sorted(r7, key=lambda s: (r7[s], s))
    ex = set(exclude)
    return [s for i, s in enumerate(order) if lo <= i / (n - 1) <= hi and s not in ex]


def btc_regime(btc: Bars, day: int, *, window: int = 30, band: float = 0.10) -> int | None:
    """+1 when BTC rose > ``band`` over ``window`` days to close(day), -1 when it fell > ``band``, else 0."""
    r = ret_n(btc, day, window)
    if r is None:
        return None
    return 1 if r > band else -1 if r < -band else 0


def capped_weights(entries: Sequence[int], hold: int, *, per_event: float = 0.03, cap: float = 0.15) -> list[float]:
    """Capital weight per event (same order as ``entries``): ``per_event * min(1, cap / (per_event * n_open))``,
    where ``n_open`` counts events entered in ``(d - hold; d]`` (same-day entries included)."""
    full = cap / per_event
    out = []
    for d in entries:
        n_open = sum(1 for e in entries if d - hold < e <= d)
        out.append(per_event * min(1.0, full / n_open))
    return out


def max_concurrent(entries: Sequence[int], hold: int) -> int:
    """Largest number of positions open on one day (each lasts ``hold`` days from its entry day)."""
    return max((sum(1 for e in entries if e <= d < e + hold) for d in set(entries)), default=0)
