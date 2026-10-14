"""Cross-venue perp funding spread (e.g. Hyperliquid vs Binance): ticker matching, a lookahead-free entry
signal, per-trade P&L of a delta-neutral two-leg position, and the statistics used to judge it.

Units follow the project convention: funding rates, spreads and returns are decimal fractions (0.10 = 10 %);
annualised spreads are simple (sum of payments x 365 days / window). Times are ms since epoch, UTC.
A funding record ``(ts, rate)`` is a payment at ``ts`` as a fraction of notional; longs pay when positive.

Everything is pure: the signal at ``t`` reads only funding records with ``ts <= t`` and daily bars that closed
by ``t``; a trade opened at ``t`` reads only prices and payments after ``t``.
"""

from __future__ import annotations

import bisect
import math
import random
import re
import statistics
from collections.abc import Callable, Sequence  # noqa: TC003
from typing import Any

from .carry import pearson
from .dated_basis import FundingSeries

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
YEAR_MS = 365 * DAY_MS

_PREFIX = re.compile(r"^(1000000|10000|1000|1M)([A-Z][A-Z0-9]*)$")
_SUFFIX = re.compile(r"^([A-Z][A-Z0-9]*?)(1000)$")
_HL_K = re.compile(r"^k([A-Z][A-Z0-9]*)$")


# ----------------------------------------------------------------------------- tickers


def normalize_asset(name: str, venue: str) -> tuple[str, int]:
    """Base ticker -> (underlying asset, coins per contract unit).

    Hyperliquid writes thousands with a lower-case ``k`` (``kPEPE`` = 1000 PEPE); CEXes use a numeric
    prefix (``1000PEPE``, ``1000000MOG``, ``1MBABYDOGE``) and Bybit sometimes a suffix (``SHIB1000``).
    """
    if venue == "hyperliquid":
        if (m := _HL_K.match(name)) is not None:
            return m.group(1), 1000
        return name.upper(), 1
    n = name.upper()
    if (m := _PREFIX.match(n)) is not None:
        mult = 1_000_000 if m.group(1) == "1M" else int(m.group(1))
        return m.group(2), mult
    if (m := _SUFFIX.match(n)) is not None:
        return m.group(1), 1000
    return n, 1


def match_tickers(a: dict[str, tuple[str, int]], b: dict[str, tuple[str, int]]) -> tuple[dict[str, str], list[str]]:
    """Pair names of venue A with names of venue B by underlying asset.

    ``a`` / ``b`` map a venue name to ``normalize_asset(...)``. Returns ``({name_a: name_b}, ambiguous_assets)``;
    an asset with more than one name on either venue is ambiguous and left out.
    """
    by_a: dict[str, list[str]] = {}
    by_b: dict[str, list[str]] = {}
    for n, (asset, _) in a.items():
        by_a.setdefault(asset, []).append(n)
    for n, (asset, _) in b.items():
        by_b.setdefault(asset, []).append(n)
    pairs, ambiguous = {}, []
    for asset in sorted(set(by_a) & set(by_b)):
        if len(by_a[asset]) == 1 and len(by_b[asset]) == 1:
            pairs[by_a[asset][0]] = by_b[asset][0]
        else:
            ambiguous.append(asset)
    return pairs, ambiguous


def price_ratio(closes_a: dict[int, float], closes_b: dict[int, float], mult_a: int, mult_b: int, *, min_points: int = 20) -> float | None:
    """Median of ``(close_b / mult_b) / (close_a / mult_a)`` over common timestamps (1.0 = same coin)."""
    rs = [(closes_b[t] / mult_b) / (closes_a[t] / mult_a) for t in closes_a.keys() & closes_b.keys() if closes_a[t] > 0 and closes_b[t] > 0]
    return statistics.median(rs) if len(rs) >= min_points else None


# ----------------------------------------------------------------------------- funding windows


def window_covered(fs: FundingSeries, start_ms: int, end_ms: int, max_gap_ms: int) -> bool:
    """True when records cover ``(start, end]`` without a hole longer than ``max_gap_ms`` (edges included)."""
    lo, hi = bisect.bisect_right(fs.ts, start_ms), bisect.bisect_right(fs.ts, end_ms)
    if hi <= lo:
        return False
    pts = [start_ms, *fs.ts[lo:hi], end_ms]
    return all(b - a <= max_gap_ms for a, b in zip(pts, pts[1:], strict=False))


def annualized(fs: FundingSeries, start_ms: int, end_ms: int) -> float:
    """Sum of payments in (start, end] scaled to a year."""
    return fs.total(start_ms, end_ms) * YEAR_MS / (end_ms - start_ms)


def spread_signal(
    a: FundingSeries, b: FundingSeries, t_ms: int, *, lookback_ms: int = WEEK_MS, gap_a_ms: int, gap_b_ms: int
) -> dict[str, float] | None:
    """Trailing funding spread ``A - B`` (annualised) at ``t`` plus its two halves; None without full coverage.

    Reads only records with ``ts <= t``.
    """
    s0, mid = t_ms - lookback_ms, t_ms - lookback_ms // 2
    if not (window_covered(a, s0, t_ms, gap_a_ms) and window_covered(b, s0, t_ms, gap_b_ms)):
        return None
    return {
        "s": annualized(a, s0, t_ms) - annualized(b, s0, t_ms),
        "s1": annualized(a, s0, mid) - annualized(b, s0, mid),
        "s2": annualized(a, mid, t_ms) - annualized(b, mid, t_ms),
    }


def entry_direction(sig: dict[str, float] | None, threshold: float) -> int:
    """-1 = short A / long B (A pays more), +1 = long A / short B, 0 = no trade.

    Requires ``|s| >= threshold`` and both halves of the window with the same (non-zero) sign as ``s``.
    """
    if sig is None or abs(sig["s"]) < threshold:
        return 0
    sign = 1 if sig["s"] > 0 else -1
    if sig["s1"] * sign <= 0 or sig["s2"] * sign <= 0:
        return 0
    return -sign


def trailing_turnover(daily: Sequence[dict[str, Any]], t_ms: int, days: int = 30) -> float:
    """USD turnover of the last ``days`` UTC days that closed by ``t`` (bars ``{ts, close, volume}``, volume in units)."""
    ts = [int(x["ts"]) for x in daily]
    hi = bisect.bisect_right(ts, t_ms - DAY_MS)  # bars with ts + 1d <= t
    lo = max(0, hi - days)
    lo = max(lo, bisect.bisect_left(ts, t_ms - (days + 1) * DAY_MS))  # no older days across a data hole
    return sum(float(daily[i]["volume"]) * float(daily[i]["close"]) for i in range(lo, hi))


# ----------------------------------------------------------------------------- one trade


class Leg:
    """One perp on one venue: funding records and OHLC bars (``ts`` = bar open, close at ``ts + bar_ms``)."""

    def __init__(self, funding: FundingSeries, bars: Sequence[dict[str, Any]], bar_ms: int) -> None:
        self.funding = funding
        bs = sorted(bars, key=lambda x: int(x["ts"]))
        self.bar_ms = bar_ms
        self.t = [int(x["ts"]) + bar_ms for x in bs]  # close times
        self.close = [float(x["close"]) for x in bs]
        self.high = [float(x["high"]) for x in bs]
        self.low = [float(x["low"]) for x in bs]

    def price(self, t_ms: int) -> float | None:
        """Close of the bar that closed exactly at ``t``."""
        i = bisect.bisect_left(self.t, t_ms)
        return self.close[i] if i < len(self.t) and self.t[i] == t_ms else None

    def last_price(self, t_ms: int) -> float | None:
        """Close of the latest bar that closed at or before ``t``."""
        i = bisect.bisect_right(self.t, t_ms) - 1
        return self.close[i] if i >= 0 else None

    def bar_closes(self, after_ms: int, upto_ms: int) -> list[int]:
        lo, hi = bisect.bisect_right(self.t, after_ms), bisect.bisect_right(self.t, upto_ms)
        return self.t[lo:hi]

    def bar_at(self, close_ms: int) -> tuple[float, float, float] | None:
        i = bisect.bisect_left(self.t, close_ms)
        return (self.high[i], self.low[i], self.close[i]) if i < len(self.t) and self.t[i] == close_ms else None

    def payments(self, after_ms: int, upto_ms: int) -> list[tuple[int, float]]:
        lo, hi = bisect.bisect_right(self.funding.ts, after_ms), bisect.bisect_right(self.funding.ts, upto_ms)
        return list(zip(self.funding.ts[lo:hi], self.funding.rate[lo:hi], strict=True))


def _leg_funding(leg: Leg, side: int, after: int, upto: int, p0: float, price_leg: Leg) -> float:
    """Funding received in (after, upto] by a position of sign ``side`` (+1 long) with entry notional 1; the notional
    of each payment is marked at the latest bar close at or before it (entry is a bar close, so never earlier)."""
    tot = 0.0
    for ts, rate in leg.payments(after, upto):
        p = price_leg.last_price(ts) or p0
        tot -= side * rate * p / p0
    return tot


def trade_pnl(
    a: Leg,
    b: Leg,
    t0_ms: int,
    direction: int,
    *,
    hold_ms: int = WEEK_MS,
    cost: float,
    stop_move: float | None = 0.5,
    stop_penalty: float = 0.01,
    a_prices: Leg | None = None,
) -> dict[str, Any] | None:
    """Net P&L of an equal-notional two-leg position opened at bar close ``t0`` and held ``hold_ms``.

    ``direction`` +1 = long A / short B, -1 = short A / long B. Returns are fractions of one leg's notional
    (both legs have notional 1 at entry; capital at leverage L on each leg ~= 2 / L of that notional).
    ``cost`` is the round-trip cost of both legs. With ``stop_move`` the position is closed at the first bar
    close (bars of leg A) where either leg's high/low went that far against it, paying ``stop_penalty``.
    ``a_prices`` replaces the price series of leg A (sensitivity: price the A leg with B's prices).
    None when an entry price is missing.
    """
    pa_leg = a_prices or a
    pa0, pb0 = pa_leg.price(t0_ms), b.price(t0_ms)
    if pa0 is None or pb0 is None:
        return None
    sa, sb = direction, -direction
    t_end = t0_ms + hold_ms
    exit_t, stopped, max_adv = None, False, 0.0
    for tc in pa_leg.bar_closes(t0_ms, t_end):
        ba, bb = pa_leg.bar_at(tc), b.bar_at(tc)
        if ba is None or bb is None:
            continue
        adv_a = (1.0 - ba[1] / pa0) if sa > 0 else (ba[0] / pa0 - 1.0)
        adv_b = (1.0 - bb[1] / pb0) if sb > 0 else (bb[0] / pb0 - 1.0)
        max_adv = max(max_adv, adv_a, adv_b)
        exit_t = tc
        if stop_move is not None and max(adv_a, adv_b) >= stop_move:
            stopped = True
            break
    if exit_t is None:
        return None
    pa1, pb1 = pa_leg.last_price(exit_t), b.last_price(exit_t)
    if pa1 is None or pb1 is None:
        return None
    f_a = _leg_funding(a, sa, t0_ms, exit_t, pa0, pa_leg)
    f_b = _leg_funding(b, sb, t0_ms, exit_t, pb0, b)
    price = sa * (pa1 / pa0 - 1.0) + sb * (pb1 / pb0 - 1.0)
    costs = cost + (stop_penalty if stopped else 0.0)
    return {
        "open_ts": t0_ms,
        "close_ts": exit_t,
        "dir": direction,
        "funding_a": f_a,
        "funding_b": f_b,
        "funding": f_a + f_b,
        "price": price,
        "cost": costs,
        "net": f_a + f_b + price - costs,
        "stopped": stopped,
        "truncated": exit_t < t_end and not stopped,
        "max_adverse": max_adv,
    }


def walk_pair(
    a: Leg,
    b: Leg,
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int = 4 * HOUR_MS,
    threshold: float = 0.25,
    lookback_ms: int = WEEK_MS,
    hold_ms: int = WEEK_MS,
    cooldown_ms: int = DAY_MS,
    gap_a_ms: int,
    gap_b_ms: int,
    eligible: Callable[[int], bool] | None = None,
    entry_delay_ms: int = 0,
    **trade_kw: Any,
) -> list[dict[str, Any]]:
    """Non-overlapping trades on one pair: at each step ``t`` (multiple of ``step_ms``) with no open trade, enter when
    the signal fires and ``eligible(t)``; the next entry waits until ``close + cooldown``. Entries need ``t + hold <= end``.
    ``entry_delay_ms`` opens at ``t + delay`` (sensitivity: transfer latency)."""
    trades: list[dict[str, Any]] = []
    t = (start_ms + step_ms - 1) // step_ms * step_ms
    while t + entry_delay_ms + hold_ms <= end_ms:
        d = entry_direction(
            spread_signal(a.funding, b.funding, t, lookback_ms=lookback_ms, gap_a_ms=gap_a_ms, gap_b_ms=gap_b_ms), threshold
        )
        if d and (eligible is None or eligible(t)):
            tr = trade_pnl(a, b, t + entry_delay_ms, d, hold_ms=hold_ms, **trade_kw)
            if tr is not None:
                tr["signal_ts"] = t
                trades.append(tr)
                t = (tr["close_ts"] + cooldown_ms + step_ms - 1) // step_ms * step_ms
                continue
        t += step_ms
    return trades


# ----------------------------------------------------------------------------- statistics


def week_of(t_ms: int) -> int:
    """Calendar week index, weeks starting Monday 00:00 UTC (1970-01-01 was a Thursday)."""
    return (t_ms + 3 * DAY_MS) // WEEK_MS


def card_mde(sd: float, n_clusters: int) -> float:
    """Power gate of the idea card: ``2.8 * SD / sqrt(n_clusters)`` (80 % power, two-sided 5 %)."""
    return 2.8 * sd / math.sqrt(n_clusters) if n_clusters > 0 else float("inf")


def cluster_bootstrap_ci(
    xs: Sequence[float], clusters: Sequence[int], *, reps: int = 5000, seed: int = 7, level: float = 0.95
) -> tuple[float, float] | None:
    """Percentile CI of the per-trade mean, resampling whole clusters (calendar weeks) with replacement."""
    groups: dict[int, list[float]] = {}
    for x, c in zip(xs, clusters, strict=True):
        groups.setdefault(c, []).append(x)
    if len(groups) < 2:
        return None
    keys = list(groups)
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        tot, n = 0.0, 0
        for _ in keys:
            g = groups[rng.choice(keys)]
            tot += sum(g)
            n += len(g)
        means.append(tot / n)
    means.sort()
    return means[int((1 - level) / 2 * reps)], means[min(reps - 1, int((1 + level) / 2 * reps))]


def cluster_bootstrap_diff(
    xs: Sequence[float], cx: Sequence[int], ys: Sequence[float], cy: Sequence[int], *, reps: int = 5000, seed: int = 7
) -> dict[str, Any]:
    """Mean(xs) - mean(ys) with a paired cluster bootstrap: the same resampled weeks for both samples."""
    gx: dict[int, list[float]] = {}
    gy: dict[int, list[float]] = {}
    for x, c in zip(xs, cx, strict=True):
        gx.setdefault(c, []).append(x)
    for y, c in zip(ys, cy, strict=True):
        gy.setdefault(c, []).append(y)
    if not xs or not ys:
        return {"diff": None, "ci": None}
    d0 = statistics.fmean(xs) - statistics.fmean(ys)
    keys = sorted(set(gx) | set(gy))
    rng = random.Random(seed)
    out = []
    for _ in range(reps):
        sx = sy = 0.0
        nx = ny = 0
        for _ in keys:
            k = rng.choice(keys)
            sx += sum(gx.get(k, ()))
            nx += len(gx.get(k, ()))
            sy += sum(gy.get(k, ()))
            ny += len(gy.get(k, ()))
        if nx and ny:
            out.append(sx / nx - sy / ny)
    out.sort()
    ci = (out[int(0.025 * len(out))], out[min(len(out) - 1, int(0.975 * len(out)))]) if out else None
    return {"diff": d0, "ci": ci}


def weekly_spreads(a: FundingSeries, b: FundingSeries, weeks: Sequence[int], *, gap_a_ms: int, gap_b_ms: int) -> dict[int, float]:
    """Annualised spread ``A - B`` per calendar week (index from ``week_of``) where both series cover the whole week."""
    out = {}
    for w in weeks:
        s0 = w * WEEK_MS - 3 * DAY_MS
        s1 = s0 + WEEK_MS
        if window_covered(a, s0, s1, gap_a_ms) and window_covered(b, s0, s1, gap_b_ms):
            out[w] = annualized(a, s0, s1) - annualized(b, s0, s1)
    return out


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0
        i = j + 1
    return r


def pooled_ar1(series: Sequence[dict[int, float]], *, rank: bool = False, demean: bool = False) -> tuple[float | None, int]:
    """Pooled lag-1 correlation of consecutive weeks within each series (Pearson, or Spearman with ``rank``).

    Without ``demean`` a constant per-series offset inflates the pooled correlation (between-series level
    differences look like persistence). ``demean`` subtracts each series' own full-sample mean first; it uses
    the whole series, so it is a descriptive statistic, not a point-in-time one.
    """
    if demean:
        series = [{w: v - statistics.fmean(s.values()) for w, v in s.items()} if s else s for s in series]
    pairs = [(s[w], s[w + 1]) for s in series for w in s if w + 1 in s]
    if rank and pairs:
        rx, ry = _ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs])
        pairs = list(zip(rx, ry, strict=True))
    return pearson(pairs), len(pairs)


def quantile(xs: Sequence[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(p * len(s))))]
