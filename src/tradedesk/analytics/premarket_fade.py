"""Pre-market perp conversion fade (IDEA-20261028-premarket-perp-conversion-fade, step 1).

Pure functions, no I/O. Times are epoch milliseconds, bars are 1h klines keyed by open time, returns are decimal
log returns (0.10 = 10 %), funding rates are decimal per settlement (positive = longs pay shorts).

Sign convention (the card's): R is the token move against BTC; a short earns -R. ``net`` adds the short's costs and
subtracts the funding the short receives, so a short's net P&L on the token notional is about ``-net``.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

Bars = Mapping[int, tuple[float, float, float, float]]  # open_ms -> (open, high, low, close)


def bars_from_rows(rows: Iterable[Sequence[float]]) -> dict[int, tuple[float, float, float, float]]:
    """Kline rows ``[open_ms, o, h, l, c, ...]`` -> {open_ms: (o, h, l, c)}; rows with a non-positive close are dropped."""
    out: dict[int, tuple[float, float, float, float]] = {}
    for r in rows:
        if float(r[4]) > 0:
            out[int(r[0])] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
    return out


def registered_t_conv(announced: Sequence[int], index_1m_first: int | None, spot_first: int | None) -> tuple[int | None, str]:
    """Registered conversion time: announced time (nearest to the first 1m index print when several), else the first
    Binance spot 1h candle open, else the first 1m index print."""
    if announced:
        if index_1m_first is None:
            return min(announced), "announced"
        return min(announced, key=lambda t: (abs(t - index_1m_first), t)), "announced"
    if spot_first is not None:
        return spot_first, "spot"
    if index_1m_first is not None:
        return index_1m_first, "index"
    return None, "none"


@dataclass(frozen=True)
class Entry:
    pre_open: int  # open of the last bar that ended at or before t_conv
    p_pre: float
    entry_open: int  # open of the first bar that opened at or after t_conv
    t_e: int  # entry time = end of that bar
    p0: float

    @property
    def gap(self) -> float:
        """g = ln(P_0 / P_pre)."""
        return math.log(self.p0 / self.p_pre)


def entry_point(bars: Bars, t_conv: int, *, max_pre_gap_ms: int = 6 * HOUR_MS) -> Entry | None:
    """P_pre = close of the last bar fully before t_conv (no older than ``max_pre_gap_ms``); P_0 = close of the first
    bar opening at or after t_conv (within 6 h)."""
    pre = [t for t in bars if t + HOUR_MS <= t_conv]
    post = [t for t in bars if t >= t_conv]
    if not pre or not post:
        return None
    tp, te = max(pre), min(post)
    if t_conv - (tp + HOUR_MS) > max_pre_gap_ms or te - t_conv > 6 * HOUR_MS:
        return None
    return Entry(tp, bars[tp][3], te, te + HOUR_MS, bars[te][3])


def close_at(bars: Bars, t_end: int) -> float | None:
    """Close of the bar that ends exactly at ``t_end``."""
    b = bars.get(t_end - HOUR_MS)
    return None if b is None else b[3]


def funding_sum(funding: Sequence[tuple[int, float]], t_from: int, t_to: int) -> float:
    """Sum of rates with settlement time in (t_from, t_to]."""
    return sum(r for t, r in funding if t_from < t <= t_to)


def round_trip_cost(beta: float, fee: float = 0.0005, slip: float = 0.0) -> float:
    """2 x (fee + slip) on the token leg plus |beta| x 2 x fee on the BTC hedge leg (decimal of token notional)."""
    return 2 * (fee + slip) + abs(beta) * 2 * fee


@dataclass(frozen=True)
class Outcome:
    r_tok: float
    r_btc: float
    gross: float  # r_tok - beta * r_btc
    f_tok: float
    f_btc: float
    cost: float
    net: float  # gross - f_tok + beta * f_btc + cost


def event_outcome(
    bars: Bars,
    btc: Bars,
    fund_tok: Sequence[tuple[int, float]],
    fund_btc: Sequence[tuple[int, float]],
    t_e: int,
    p0: float,
    horizon_ms: int,
    beta: float,
    *,
    fee: float = 0.0005,
    slip: float = 0.0,
) -> Outcome | None:
    """Hedged log return from t_e to t_e + horizon with funding and costs; None when a needed bar is missing."""
    pk, b0, bk = close_at(bars, t_e + horizon_ms), close_at(btc, t_e), close_at(btc, t_e + horizon_ms)
    if pk is None or b0 is None or bk is None or pk <= 0 or p0 <= 0:
        return None
    r_tok, r_btc = math.log(pk / p0), math.log(bk / b0)
    gross = r_tok - beta * r_btc
    f_tok = funding_sum(fund_tok, t_e, t_e + horizon_ms)
    f_btc = funding_sum(fund_btc, t_e, t_e + horizon_ms)
    cost = round_trip_cost(beta, fee, slip)
    return Outcome(r_tok, r_btc, gross, f_tok, f_btc, cost, gross - f_tok + beta * f_btc + cost)


@dataclass(frozen=True)
class StopTrade:
    pnl: float  # short token + long beta BTC, simple return on token notional, after funding and costs (+ = short wins)
    stopped: bool
    t_exit: int


def stop_trade(
    bars: Bars,
    btc: Bars,
    fund_tok: Sequence[tuple[int, float]],
    fund_btc: Sequence[tuple[int, float]],
    t_e: int,
    p0: float,
    horizon_ms: int,
    beta: float,
    *,
    stop: float = 0.30,
    fee: float = 0.0005,
    slip: float = 0.0,
) -> StopTrade | None:
    """Short at P_0 with a stop at P_0 * (1 + stop) checked on 1h highs (fill at the stop level), else exit at t_e +
    horizon. The BTC hedge is closed at the end of the exit bar."""
    b0 = close_at(btc, t_e)
    if b0 is None:
        return None
    level = p0 * (1 + stop)
    t_exit, p_exit, stopped = t_e + horizon_ms, None, False
    t = t_e
    while t < t_e + horizon_ms:
        b = bars.get(t)
        if b is not None and b[1] >= level:
            t_exit, p_exit, stopped = t + HOUR_MS, level, True
            break
        t += HOUR_MS
    if p_exit is None:
        p_exit = close_at(bars, t_exit)
    b_exit = close_at(btc, t_exit)
    if p_exit is None or b_exit is None:
        return None
    pnl = (1 - p_exit / p0) + beta * (b_exit / b0 - 1)
    pnl += funding_sum(fund_tok, t_e, t_exit) - beta * funding_sum(fund_btc, t_e, t_exit)
    pnl -= round_trip_cost(beta, fee, slip)
    return StopTrade(pnl, stopped, t_exit)


def beta_ols(
    bars: Bars, btc: Bars, t_end: int, *, min_obs: int = 48, lo: float = 0.0, hi: float = 3.0, default: float = 1.0
) -> tuple[float, int]:
    """OLS slope of hourly token log returns on BTC over consecutive bars that end at or before ``t_end``.
    Fewer than ``min_obs`` pairs -> ``default``; the slope is clipped to [lo, hi]. Returns (beta, n_obs)."""
    xs: list[float] = []
    ys: list[float] = []
    for t in sorted(bars):
        if t + HOUR_MS > t_end:
            break
        prev = t - HOUR_MS
        if prev in bars and prev in btc and t in btc:
            ys.append(math.log(bars[t][3] / bars[prev][3]))
            xs.append(math.log(btc[t][3] / btc[prev][3]))
    if len(xs) < min_obs:
        return default, len(xs)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return default, len(xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return min(hi, max(lo, b)), len(xs)


# ---------------------------------------------------------------- statistics


def t_quantile(p: float, df: int) -> float:
    """Student-t quantile by the Cornish-Fisher expansion around the normal quantile (error < 1e-3 for df >= 5)."""
    z = statistics.NormalDist().inv_cdf(p)
    d = float(df)
    return (
        z + (z**3 + z) / (4 * d) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * d**2) + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * d**3)
    )


def _pct(sorted_xs: Sequence[float], q: float) -> float:
    k = (len(sorted_xs) - 1) * q
    f = math.floor(k)
    c = min(f + 1, len(sorted_xs) - 1)
    return sorted_xs[f] + (sorted_xs[c] - sorted_xs[f]) * (k - f)


def bootstrap_mean_ci(xs: Sequence[float], *, n_boot: int = 10_000, seed: int = 20261030, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean, resampling events with replacement."""
    if len(xs) < 2:
        return math.nan, math.nan
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(statistics.fmean(rng.choices(xs, k=n)) for _ in range(n_boot))
    return _pct(means, alpha / 2), _pct(means, 1 - alpha / 2)


def bootstrap_diff_ci(
    a: Sequence[float], b: Sequence[float], *, n_boot: int = 10_000, seed: int = 20261030, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile bootstrap CI of mean(a) - mean(b), resampling within each group."""
    if len(a) < 2 or len(b) < 2:
        return math.nan, math.nan
    rng = random.Random(seed)
    ds = sorted(statistics.fmean(rng.choices(a, k=len(a))) - statistics.fmean(rng.choices(b, k=len(b))) for _ in range(n_boot))
    return _pct(ds, alpha / 2), _pct(ds, 1 - alpha / 2)


@dataclass(frozen=True)
class Summary:
    n: int
    mean: float
    median: float
    sd: float
    share_neg: float
    boot_lo: float
    boot_hi: float
    t_lo: float
    t_hi: float


def summarize(xs: Sequence[float], *, n_boot: int = 10_000, seed: int = 20261030) -> Summary:
    n = len(xs)
    if n == 0:
        return Summary(0, *([math.nan] * 8))
    m = statistics.fmean(xs)
    sd = statistics.stdev(xs) if n > 1 else math.nan
    lo, hi = bootstrap_mean_ci(xs, n_boot=n_boot, seed=seed)
    half = t_quantile(0.975, n - 1) * sd / math.sqrt(n) if n > 1 else math.nan
    return Summary(n, m, statistics.median(xs), sd, sum(x < 0 for x in xs) / n, lo, hi, m - half, m + half)


def tercile_groups(keys: Sequence[float]) -> tuple[list[int], list[int]]:
    """Indices of the bottom and top floor(n/3) by key (ties broken by index)."""
    k = len(keys) // 3
    order = sorted(range(len(keys)), key=lambda i: (keys[i], i))
    return order[:k], (order[-k:] if k else [])


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation with average ranks for ties."""
    if len(x) < 3:
        return math.nan
    rx, ry = _ranks(x), _ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else math.nan


def mark_h1(mean_gross: float, s: Summary) -> str:
    """Registered H1 mark on R_7d net (s) and the gross mean."""
    if s.mean >= 0:
        return "✗"
    if mean_gross <= -0.06 and s.mean <= -0.05 and s.boot_hi < 0 and s.median < 0:
        return "✓"
    return "≈"


def mark_h2(s: Summary) -> str:
    if s.mean >= 0:
        return "✗"
    if s.mean <= -0.03 and s.boot_hi < 0 and s.share_neg >= 0.60:
        return "✓"
    return "≈"


def mark_h3(delta: float, hi: float) -> str:
    if delta >= 0:
        return "✗"
    if delta <= -0.05 and hi < 0:
        return "✓"
    return "≈"
