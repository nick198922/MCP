"""Perp-spot premium spikes: a delta-neutral short perp + long spot (or the mirror) opened on a 1m premium-index spike
and closed when the premium converges (or after a time limit).

Pure functions (no I/O). Times are epoch milliseconds (UTC); 1m bars are keyed by their open time. Premiums, returns,
costs and funding rates are decimal fractions (0.01 = 1 %). Returns are on the notional of one leg (both legs have
equal notional at entry); capital = spot notional + perp margin = notional * (1 + 1/leverage).

Lookahead rules: a signal at bar ``t`` reads premium closes of bars ``<= t`` only; the trade opens at the open of a
later bar; an exit decision at bar ``s`` (premium close) executes at the open of ``s + 1 min``. Turnover and price-level
filters for an event on UTC day ``D`` read daily bars of days ``< D`` only.

Idea card: docs/ideas/IDEA-20261015-perp-spot-premium-spike-convergence.md.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from .funding_spread import normalize_asset

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

FEE_SPOT = 0.001  # per side, taker, without the BNB discount
FEE_PERP = 0.0005  # per side, taker
MAINT_MARGIN = 0.025  # maintenance margin used for the liquidation price (as in listing_drift.liq_threshold)


# ---------------------------------------------------------------- tickers, filters


def spot_candidates(perp_symbol: str, spot_symbols: set[str] | frozenset[str]) -> list[tuple[str, int]]:
    """Binance spot pairs that may hold the perp's underlying: ``(spot symbol, coins per perp unit)``.

    The same symbol first (Binance spot lists some ``1000...`` tickers too, e.g. ``1000SATSUSDT``), then the
    normalised asset with its multiplier (``1000PEPEUSDT`` -> ``PEPEUSDT`` x 1000). The caller must still confirm
    the pairing by price level (``price_ratio``): a shared ticker can be a different token."""
    out: list[tuple[str, int]] = []
    if perp_symbol in spot_symbols:
        out.append((perp_symbol, 1))
    asset, mult = normalize_asset(perp_symbol[:-4], "binanceusdm")
    alt = f"{asset}USDT"
    if mult != 1 and alt in spot_symbols and alt != perp_symbol:
        out.append((alt, mult))
    return out


def price_ratio(perp_close: Mapping[int, float], spot_close: Mapping[int, float], mult: int, day: int, *, window: int = 30) -> float | None:
    """Median of ``perp close / (spot close * mult)`` over UTC days ``[day - window; day - 1]`` (None without overlap)."""
    qs = [
        perp_close[d] / (spot_close[d] * mult)
        for d in range(day - window, day)
        if d in perp_close and d in spot_close and spot_close[d] > 0
    ]
    return statistics.median(qs) if qs else None


def trailing_turnover(qv: Mapping[int, float], day: int, *, window: int = 30) -> tuple[float | None, int]:
    """(median daily quote volume over days ``[day - window; day - 1]`` that have a bar, number of such days)."""
    xs = [qv[d] for d in range(day - window, day) if d in qv]
    return (statistics.median(xs) if xs else None), len(xs)


def slippage(turnover: float | None) -> float:
    """Per-side slippage of one leg by its trailing median daily quote turnover (USD), as in the token-unlock report."""
    if turnover is not None and turnover >= 50e6:
        return 0.0005
    if turnover is not None and turnover >= 5e6:
        return 0.0010
    return 0.0020


# ---------------------------------------------------------------- signals


def run_signals(
    premium: Mapping[int, float], start_ms: int, end_ms: int, *, side: int = 1, lo: float = 0.01, hi: float = math.inf, run: int = 2
) -> list[int]:
    """Open times ``t`` in ``[start_ms; end_ms)`` of bars that complete a fresh run of ``run`` contiguous 1m bars with
    ``lo <= side * premium_close <= hi``; the bar before the run is missing or outside the band. Reads bars ``<= t``."""

    def ok(t: int) -> bool:
        p = premium.get(t)
        return p is not None and lo <= side * p <= hi

    out = []
    t = start_ms - start_ms % MIN_MS
    while t < end_ms:
        if all(ok(t - k * MIN_MS) for k in range(run)) and not ok(t - run * MIN_MS):
            out.append(t)
        t += MIN_MS
    return out


# ---------------------------------------------------------------- trades


@dataclass
class Bars:
    """1m klines keyed by open time: ``(open, high, low, close, base volume)``."""

    rows: dict[int, tuple[float, float, float, float, float]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Sequence[Sequence[float]]) -> Bars:
        return cls({int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows})

    def open_at(self, t: int, *, max_wait_ms: int = 0) -> tuple[int, float] | None:
        """(bar time, open) of the first bar in ``[t; t + max_wait_ms]``."""
        k = t
        while k <= t + max_wait_ms:
            if k in self.rows:
                return k, self.rows[k][0]
            k += MIN_MS
        return None


@dataclass
class Trade:
    side: int  # +1: short perp, long spot (premium spike); -1: long perp, short spot (discount spike)
    signal_ms: int
    entry_ms: int
    exit_ms: int
    converged: bool  # False: closed at the time limit
    p_in: float
    p_out: float
    s_in: float
    s_out: float
    prem_signal: float
    mae: float  # max side * (premium close - premium at the signal) over the holding bars (decimal)
    liq_ms: dict[float, int | None]  # leverage -> first bar whose perp extreme breaches the liquidation price
    funding: list[tuple[int, float, float]]  # (settlement ms, rate, perp price / p_in)
    funding_ok: bool  # settlements cover the holding window without holes

    @property
    def perp_leg(self) -> float:
        return -self.side * (self.p_out / self.p_in - 1.0)

    @property
    def spot_leg(self) -> float:
        return self.side * (self.s_out / self.s_in - 1.0)

    @property
    def basis_pnl(self) -> float:
        return self.perp_leg + self.spot_leg

    @property
    def hold_ms(self) -> int:
        return self.exit_ms - self.entry_ms

    def funding_pnl(self, leverage: float | None = None) -> float:
        """Funding the perp leg receives (side +1 receives positive rates), up to a liquidation at ``leverage``."""
        stop = self.liq_ms.get(leverage) if leverage is not None else None
        return sum(self.side * r * px for t, r, px in self.funding if stop is None or t < stop)

    def net(self, leverage: float, cost: float) -> float:
        """Net on one leg's notional: basis legs (perp margin lost on liquidation) + funding - ``cost``.

        After a liquidation the perp leg is -1/leverage and the spot leg stays open to the regular exit."""
        liq = self.liq_ms.get(leverage)
        perp = -1.0 / leverage if liq is not None else self.perp_leg
        return perp + self.spot_leg + self.funding_pnl(leverage) - cost


def round_trip_cost(slip_spot: float, slip_perp: float, *, mult: float = 1.0, slip_mult: float = 1.0) -> float:
    """Fees and slippage for opening and closing both legs, on one leg's notional."""
    return mult * (2 * FEE_SPOT + 2 * FEE_PERP + slip_mult * 2 * (slip_spot + slip_perp))


def liq_move(leverage: float, mm: float = MAINT_MARGIN) -> float:
    """Adverse perp move (fraction of entry) that liquidates an isolated position at ``leverage``."""
    return 1.0 / leverage - mm


def funding_covered(records: Sequence[Sequence[float]], start_ms: int, end_ms: int, *, slack_ms: int = HOUR_MS) -> bool:
    """True when the settlement series has no hole around ``[start_ms; end_ms)``.

    ``records`` = ``[time_ms, interval_hours, rate]``. Needs a settlement at or before ``start_ms`` (within one interval
    + slack) and spacing <= interval + slack between consecutive settlements up to the first one at or after
    ``end_ms`` (or, if the series ends earlier, up to ``end_ms``)."""
    ts = sorted((int(r[0]), float(r[1])) for r in records)
    if not ts:
        return False

    def iv(h: float) -> int:
        return int((h if h > 0 else 8.0) * HOUR_MS)

    before = [x for x in ts if x[0] <= start_ms]
    if not before or start_ms - before[-1][0] > iv(before[-1][1]) + slack_ms:
        return False
    prev = before[-1]
    for x in ts[len(before) :]:
        if x[0] - prev[0] > iv(prev[1]) + slack_ms:
            return False
        prev = x
        if x[0] >= end_ms:
            return True
    return end_ms - prev[0] <= iv(prev[1]) + slack_ms


def simulate(
    premium: Mapping[int, float],
    perp: Bars,
    spot: Bars,
    funding: Sequence[Sequence[float]],
    signal_ms: int,
    *,
    side: int = 1,
    delay_bars: int = 1,
    same_bar: bool = False,
    exit_level: float = 0.002,
    max_hold_ms: int = DAY_MS,
    leverages: Sequence[float] = (1.0, 2.0, 3.0),
    exit_wait_ms: int = 60 * MIN_MS,
) -> tuple[Trade | None, str]:
    """One trade from a signal bar. Returns ``(trade, "")`` or ``(None, reason)``.

    Entry: open of bar ``signal + delay_bars`` on both legs (``same_bar``: close of the signal bar), both bars traded
    (volume > 0). Exit: the first bar ``s`` from the entry bar on with ``side * premium_close <= exit_level`` closes at
    the open of ``s + 1 min``; otherwise at the open of the bar ``max_hold_ms`` after entry. A missing exit bar is
    replaced by the next bar within ``exit_wait_ms``. Reads no bar after the exit."""
    if same_bar:
        pb, sb = perp.rows.get(signal_ms), spot.rows.get(signal_ms)
        if pb is None or sb is None or pb[4] <= 0 or sb[4] <= 0:
            return None, "no traded entry bar"
        entry_ms, first_bar, p_in, s_in = signal_ms + MIN_MS, signal_ms + MIN_MS, pb[3], sb[3]
    else:
        entry_ms = signal_ms + delay_bars * MIN_MS
        pb, sb = perp.rows.get(entry_ms), spot.rows.get(entry_ms)
        if pb is None or sb is None or pb[4] <= 0 or sb[4] <= 0:
            return None, "no traded entry bar"
        first_bar, p_in, s_in = entry_ms, pb[0], sb[0]
    deadline = entry_ms + max_hold_ms
    exit_ms, converged = deadline, False
    p0 = premium[signal_ms]
    mae = 0.0
    t = first_bar
    while t < deadline:
        p = premium.get(t)
        if p is not None:
            mae = max(mae, side * (p - p0))
            if side * p <= exit_level:
                exit_ms, converged = t + MIN_MS, True
                break
        t += MIN_MS
    po, so = perp.open_at(exit_ms, max_wait_ms=exit_wait_ms), spot.open_at(exit_ms, max_wait_ms=exit_wait_ms)
    if po is None or so is None:
        return None, "no exit bar"
    liq: dict[float, int | None] = {}
    for lev in leverages:
        bound = p_in * (1.0 + side * liq_move(lev))
        hit = None
        t = first_bar
        while t < exit_ms:
            b = perp.rows.get(t)
            if b is not None and ((side > 0 and b[1] >= bound) or (side < 0 and b[2] <= bound)):
                hit = t
                break
            t += MIN_MS
        liq[lev] = hit
    pays = []
    for r in funding:
        ts = int(r[0])
        if entry_ms <= ts < exit_ms:
            b = perp.open_at(ts - ts % MIN_MS, max_wait_ms=5 * MIN_MS)
            pays.append((ts, float(r[2]), (b[1] / p_in) if b else 1.0))
    trade = Trade(
        side=side,
        signal_ms=signal_ms,
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        converged=converged,
        p_in=p_in,
        p_out=po[1],
        s_in=s_in,
        s_out=so[1],
        prem_signal=p0,
        mae=mae,
        liq_ms=liq,
        funding=pays,
        funding_ok=funding_covered(funding, entry_ms, exit_ms),
    )
    return trade, ""


def select_events(
    signals: Sequence[int], sim: Callable[[int], tuple[Trade | None, str]], *, cooldown_ms: int = 6 * HOUR_MS
) -> list[tuple[int, Trade | None, str]]:
    """Walk signals in time: a signal is an event unless it is within ``cooldown_ms`` of the previous event's signal
    or before the previous trade's exit. Untradable events (``sim`` returns None) still start a cooldown."""
    out: list[tuple[int, Trade | None, str]] = []
    last_sig: int | None = None
    last_exit = -1
    for s in sorted(signals):
        if (last_sig is not None and s < last_sig + cooldown_ms) or s < last_exit:
            continue
        tr, why = sim(s)
        out.append((s, tr, why))
        last_sig = s
        if tr is not None:
            last_exit = tr.exit_ms
    return out


# ---------------------------------------------------------------- statistics helpers


def top_cluster_drop(values: Sequence[float], clusters: Sequence[object], k: int) -> list[float]:
    """Values without the ``k`` clusters with the largest summed value."""
    tot: dict[object, float] = {}
    for v, c in zip(values, clusters, strict=True):
        tot[c] = tot.get(c, 0.0) + v
    drop = set(sorted(tot, key=lambda c: -tot[c])[:k])
    return [v for v, c in zip(values, clusters, strict=True) if c not in drop]
