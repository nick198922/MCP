"""Funding crowding: weekly cross-sectional long/short of USDT-M perps ranked by trailing funding (no spot leg).

Pure functions (no I/O). Time is passed in: UTC day indices (``ms // DAY_MS``) and epoch milliseconds.
Returns are decimals on notional (0.01 = 1 %); funding rates are decimals per settlement (positive: longs pay shorts).
Idea card: docs/ideas/IDEA-20261015-funding-crowding-longshort.md (section "Попередня реєстрація").

Conventions (registered):
- rank day ``t`` (Sunday 00:00 UTC); entry at the daily open of ``t``, exit at the close of ``t + hold - 1``;
- a settlement belongs to the signal if its time is in ``[t - W days - 30 min; t - 30 min)`` and to the holding P&L if
  it is in ``[t + 30 min; t + hold days + 30 min)``: the settlement at ``t`` itself is in neither;
- the basket return is per unit of gross notional: each side holds half of it, equal weights inside a side.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from .listing_drift import DAY_MS, HOUR_MS, funding_gaps

HALF_HOUR_MS = HOUR_MS // 2


@dataclass
class Bars:
    """Daily bars keyed by UTC day index; only days that traded (base volume > 0, prices > 0) are kept."""

    open: dict[int, float]
    high: dict[int, float]
    low: dict[int, float]
    close: dict[int, float]
    qv: dict[int, float]

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[float]]) -> Bars:
        """Rows ``[open_ms, o, h, l, c, base_vol, quote_vol]`` (the archive layout)."""
        o: dict[int, float] = {}
        h: dict[int, float] = {}
        lo: dict[int, float] = {}
        c: dict[int, float] = {}
        q: dict[int, float] = {}
        for r in rows:
            if len(r) < 6 or r[5] <= 0 or r[4] <= 0 or r[1] <= 0 or r[3] <= 0:
                continue
            d = int(r[0]) // DAY_MS
            o[d], h[d], lo[d], c[d] = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            q[d] = float(r[6]) if len(r) > 6 else float(r[5]) * float(r[4])
        return cls(o, h, lo, c, q)

    def close_on_or_before(self, day: int, floor: int) -> int | None:
        for d in range(day, floor - 1, -1):
            if d in self.close:
                return d
        return None


# ---------------------------------------------------------------- point-in-time universe


def run_start(bars: Bars, day: int, *, max_gap: int = 7) -> int | None:
    """First day of the continuous run of bars that contains ``day`` (uses bars <= ``day`` only).

    A gap of more than ``max_gap`` days without a bar starts a new run (relisting or a new token on an old ticker).
    None when ``day`` has no bar."""
    if day not in bars.close:
        return None
    start = day
    d = day - 1
    while True:
        prev = next((x for x in range(d, d - max_gap - 1, -1) if x in bars.close), None)
        if prev is None:
            return start
        start, d = prev, prev - 1


def run_start_map(bars: Bars, *, max_gap: int = 7) -> dict[int, int]:
    """``run_start`` for every bar day at once (one pass in time order; each value depends only on earlier bars)."""
    out: dict[int, int] = {}
    prev: int | None = None
    start = 0
    for d in sorted(bars.close):
        if prev is None or d - prev > max_gap + 1:
            start = d
        out[d] = start
        prev = d
    return out


def median_turnover(bars: Bars, day: int, *, window: int = 30, min_days: int = 20) -> float | None:
    """Median daily quote volume (USD) over the ``window`` days strictly before ``day``; None with < ``min_days`` bars."""
    vals = [bars.qv[d] for d in range(day - window, day) if d in bars.qv]
    return statistics.median(vals) if len(vals) >= min_days else None


def trailing_funding(records: Sequence[Sequence[float]], t_ms: int, window_days: float, *, slack_ms: int = HOUR_MS) -> float | None:
    """Annualised funding over the settlements in ``[t - W - 30 min; t - 30 min)``: sum / W * 365.

    Summing actual settlements makes the annualisation follow each symbol's real interval (1h/4h/8h, also when it
    changes). None when the window has no settlement or a hole (see ``funding_gaps``): the symbol-week is excluded.
    Only records strictly before the window end are looked at."""
    lo = t_ms - int(window_days * DAY_MS) - HALF_HOUR_MS
    hi = t_ms - HALF_HOUR_MS
    past = [r for r in records if int(r[0]) < hi]
    inside = [r for r in past if int(r[0]) >= lo]
    if not inside or funding_gaps(past, lo, hi, slack_ms=slack_ms):
        return None
    return sum(float(r[2]) for r in inside) / window_days * 365.0


@dataclass(frozen=True)
class Candidate:
    symbol: str
    signal: float  # annualised trailing funding (decimal per year)
    median_qv: float  # USD per day, 30 days before t
    age: int  # days since the start of the current continuous run of bars


def week_candidate(
    symbol: str,
    bars: Bars,
    records: Sequence[Sequence[float]],
    day: int,
    *,
    window_days: float = 7.0,
    min_age: int = 30,
    min_median_qv: float = 5e6,
    starts: Mapping[int, int] | None = None,
) -> tuple[Candidate | None, str]:
    """Universe membership of one symbol at rank day ``day`` using data before ``day`` only (and the existence of the
    entry bar). ``starts`` is an optional precomputed ``run_start_map``. Returns (candidate, "ok") or (None, reason)."""
    rs = (starts.get(day) if day in bars.close else None) if starts is not None else run_start(bars, day)
    if rs is None:
        return None, "no_entry_bar"
    if day - rs < min_age:
        return None, "young"
    mq = median_turnover(bars, day)
    if mq is None or mq < min_median_qv:
        return None, "illiquid"
    sig = trailing_funding(records, day * DAY_MS, window_days)
    if sig is None:
        return None, "funding_gap"
    return Candidate(symbol, sig, mq, day - rs), "ok"


def side_width(n_universe: int, *, cap: int = 10, quantile: float = 0.10, min_universe: int = 50) -> int:
    """Symbols per side: min(cap, floor(N * quantile)); 0 (week skipped) when N < ``min_universe``."""
    if n_universe < min_universe:
        return 0
    return min(cap, int(math.floor(n_universe * quantile + 1e-9)))


def pick_baskets(signals: Mapping[str, float], n: int) -> tuple[list[str], list[str]]:
    """(longs, shorts): the ``n`` lowest and ``n`` highest signals; ties break by symbol name."""
    order = sorted(signals, key=lambda s: (signals[s], s))
    if n <= 0 or 2 * n > len(order):
        return [], []
    return order[:n], order[-n:]


# ---------------------------------------------------------------- one week of one leg


@dataclass(frozen=True)
class LegWeek:
    symbol: str
    r: float  # price return open(t) -> close(exit)
    funding_long: float  # funding a long receives per unit of entry notional (negative when longs pay)
    up: float  # max(high) / open(t) - 1 over the holding days
    down: float  # 1 - min(low) / open(t)
    exit: int
    truncated: bool  # planned exit day had no bar (delisted / settled): closed at the last earlier close
    n_settle: int
    hold_gaps: int  # funding holes inside the holding window (reported, not excluded)


def leg_week(symbol: str, bars: Bars, records: Sequence[Sequence[float]], day: int, *, hold: int = 7) -> LegWeek | None:
    """Fixed-quantity position from the open of ``day`` to the close of ``day + hold - 1``.

    Funding: settlements in ``[t + 30 min; t + hold days + 30 min)`` up to the end of the exit day, each marked
    at the close of its day (last earlier close) over the entry price. None without an entry open."""
    if day not in bars.open:
        return None
    p0 = bars.open[day]
    planned = day + hold - 1
    ex = bars.close_on_or_before(planned, day)
    if ex is None:
        return None
    lo = day * DAY_MS + HALF_HOUR_MS
    hi = min((day + hold) * DAY_MS, (ex + 1) * DAY_MS) + HALF_HOUR_MS
    f = 0.0
    n = 0
    inside = [r for r in records if lo <= int(r[0]) < hi]
    for r in inside:
        d = int(r[0]) // DAY_MS
        px = next((bars.close[k] for k in range(d, d - 5, -1) if k in bars.close), p0)
        f -= float(r[2]) * px / p0
        n += 1
    gaps = len(funding_gaps([r for r in records if int(r[0]) < hi], lo, hi)) if records else 1
    up = max((bars.high[d] for d in range(day, ex + 1) if d in bars.high), default=p0) / p0 - 1.0
    down = 1.0 - min((bars.low[d] for d in range(day, ex + 1) if d in bars.low), default=p0) / p0
    return LegWeek(symbol, bars.close[ex] / p0 - 1.0, f, up, down, ex, ex < planned, n, gaps)


def liquidated(leg: LegWeek, side: int, leverage: float, *, mm: float = 0.025) -> bool:
    """Isolated leg liquidated within the week: adverse move >= 1/k - maintenance (side +1 long, -1 short)."""
    move = leg.down if side > 0 else leg.up
    return move >= 1.0 / leverage - mm


# ---------------------------------------------------------------- basket and turnover


def target_weights(longs: Sequence[str], shorts: Sequence[str], *, long_scale: float = 1.0, short_scale: float = 1.0) -> dict[str, float]:
    """Signed weights per unit of gross notional: +0.5/n per long, -0.5/n per short (optionally rescaled sides)."""
    w: dict[str, float] = {}
    for s in longs:
        w[s] = w.get(s, 0.0) + 0.5 * long_scale / len(longs)
    for s in shorts:
        w[s] = w.get(s, 0.0) - 0.5 * short_scale / len(shorts)
    return w


def basket_components(weights: Mapping[str, float], legs: Mapping[str, LegWeek]) -> tuple[float, float]:
    """(price, funding) of a signed-weight basket over one week, per unit of gross notional at entry."""
    price = sum(w * legs[s].r for s, w in weights.items())
    fund = sum(w * legs[s].funding_long for s, w in weights.items())
    return price, fund


def drift(weights: Mapping[str, float], legs: Mapping[str, LegWeek]) -> dict[str, float]:
    """Signed notional of each position at the end of the week (fixed quantity): w * (1 + r)."""
    return {s: w * (1.0 + legs[s].r) for s, w in weights.items()}


def rebalance_cost(
    prev: Mapping[str, float], target: Mapping[str, float], unit_cost: Mapping[str, float], default_cost: float
) -> tuple[float, float]:
    """(turnover, cost) of moving from drifted ``prev`` to ``target`` weights: sum |delta| and sum |delta| * cost_i."""
    turn = cost = 0.0
    for s in set(prev) | set(target):
        d = abs(target.get(s, 0.0) - prev.get(s, 0.0))
        turn += d
        cost += d * unit_cost.get(s, default_cost)
    return turn, cost


def unit_cost(median_qv: float | None, *, fee: float = 0.0005, slip_mult: float = 1.0) -> float:
    """Cost per unit of notional traded: taker fee + slippage tier by median daily USD volume (token-unlock scale)."""
    if median_qv is None or median_qv < 5e6:
        slip = 0.0015
    elif median_qv < 5e7:
        slip = 0.0010
    else:
        slip = 0.0005
    return fee + slip * slip_mult


@dataclass(frozen=True)
class Week:
    """One rebalance point: rank day, the point-in-time universe and every member's leg over the holding period."""

    day: int
    universe: Mapping[str, Candidate]
    legs: Mapping[str, LegWeek]


@dataclass(frozen=True)
class Pick:
    longs: list[str]
    shorts: list[str]
    long_scale: float = 1.0
    short_scale: float = 1.0


@dataclass(frozen=True)
class WeekResult:
    day: int
    n_universe: int
    n_side: int
    price: float  # per unit of gross notional
    funding: float
    cost: float
    turnover: float
    long_price: float  # long side alone, per unit of its own notional
    short_price: float  # short side alone (short P&L), per unit of its own notional
    long_funding: float
    short_funding: float

    @property
    def net(self) -> float:
        return self.price + self.funding - self.cost


def simulate(
    weeks: Sequence[Week],
    choose: Callable[[Week], Pick],
    *,
    fee: float = 0.0005,
    slip_mult: float = 1.0,
) -> list[WeekResult]:
    """Run the basket over consecutive rebalance points; costs on actual turnover from drifted weights.

    Entry cost is charged in the first week, the final exit cost in the last week. A pick with an empty side means
    flat for that period (positions closed at the rebalance)."""
    out: list[WeekResult] = []
    prev: dict[str, float] = {}
    last_mq: dict[str, float] = {}
    for i, wk in enumerate(weeks):
        for s, c in wk.universe.items():
            last_mq[s] = c.median_qv
        pk = choose(wk)
        longs = [s for s in pk.longs if s in wk.legs]
        shorts = [s for s in pk.shorts if s in wk.legs]
        tw = target_weights(longs, shorts, long_scale=pk.long_scale, short_scale=pk.short_scale) if longs and shorts else {}
        costs = {s: unit_cost(last_mq.get(s), fee=fee, slip_mult=slip_mult) for s in set(prev) | set(tw)}
        turn, cost = rebalance_cost(prev, tw, costs, unit_cost(None, fee=fee, slip_mult=slip_mult))
        price, fund = basket_components(tw, wk.legs)
        prev = drift(tw, wk.legs)
        if i == len(weeks) - 1:
            t2, c2 = rebalance_cost(prev, {}, {s: unit_cost(last_mq.get(s), fee=fee, slip_mult=slip_mult) for s in prev}, 0.0)
            turn, cost = turn + t2, cost + c2
        lp = statistics.fmean(wk.legs[s].r for s in longs) if longs and tw else 0.0
        sp = -statistics.fmean(wk.legs[s].r for s in shorts) if shorts and tw else 0.0
        lf = statistics.fmean(wk.legs[s].funding_long for s in longs) if longs and tw else 0.0
        sf = -statistics.fmean(wk.legs[s].funding_long for s in shorts) if shorts and tw else 0.0
        out.append(WeekResult(wk.day, len(wk.universe), len(longs) if tw else 0, price, fund, cost, turn, lp, sp, lf, sf))
    return out


def liquidation_capped(wk: Week, pick: Pick, leverage: float, *, mm: float = 0.025) -> Week:
    """The same week with picked legs that hit isolated liquidation at ``leverage`` replaced by a loss of their margin.

    A liquidated leg loses 1/k of its notional (price) and its funding is dropped (exploratory constraint variant)."""
    legs = dict(wk.legs)
    loss = 1.0 / leverage
    for side, names in ((+1, pick.longs), (-1, pick.shorts)):
        for s in names:
            lg = legs.get(s)
            if lg is not None and liquidated(lg, side, leverage, mm=mm):
                legs[s] = LegWeek(s, -loss * side, 0.0, lg.up, lg.down, lg.exit, lg.truncated, lg.n_settle, lg.hold_gaps)
    return Week(wk.day, wk.universe, legs)


def rank_pick(wk: Week, *, cap: int = 10, signal: Mapping[str, float] | None = None) -> Pick:
    """Registered main pick: n = side_width(N, cap) lowest-signal longs and highest-signal shorts."""
    sig = signal if signal is not None else {s: c.signal for s, c in wk.universe.items() if s in wk.legs}
    n = side_width(len(sig), cap=cap)
    lo, hi = pick_baskets(sig, n)
    return Pick(lo, hi)


def random_pick(wk: Week, rng: random.Random, *, cap: int = 10) -> Pick:
    """Permutation null: the same basket shape filled with random universe members."""
    pool = sorted(s for s in wk.universe if s in wk.legs)
    n = side_width(len(pool), cap=cap)
    if n == 0:
        return Pick([], [])
    sel = rng.sample(pool, 2 * n)
    return Pick(sel[:n], sel[n:])


def beta_scales(beta_long: float | None, beta_short: float | None) -> tuple[float, float]:
    """Side scales with equal beta exposure and unchanged gross: ls * bL = ss * bS, ls + ss = 2 (1, 1 if undefined)."""
    if beta_long is None or beta_short is None or beta_long <= 0 or beta_short <= 0:
        return 1.0, 1.0
    t = beta_long + beta_short
    return 2.0 * beta_short / t, 2.0 * beta_long / t


# ---------------------------------------------------------------- statistics


def ranks(xs: Sequence[float]) -> list[float]:
    """Average ranks (1-based) with ties."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return out


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    return pearson(ranks(xs), ranks(ys))


def fm_slope(signal: Sequence[float], returns: Sequence[float]) -> float:
    """Cross-sectional OLS slope of returns on the z-score of the signal's rank: return per 1 SD of rank."""
    rk = ranks(signal)
    mu, sd = statistics.fmean(rk), statistics.pstdev(rk)
    if sd == 0:
        return 0.0
    z = [(r - mu) / sd for r in rk]
    my = statistics.fmean(returns)
    return sum(zi * (y - my) for zi, y in zip(z, returns, strict=True)) / sum(zi * zi for zi in z)


def newey_west_se(xs: Sequence[float], lag: int) -> float:
    """Newey-West (Bartlett) standard error of the mean of a time series."""
    n = len(xs)
    if n < 2:
        return float("nan")
    mu = statistics.fmean(xs)
    e = [x - mu for x in xs]
    s = sum(v * v for v in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        g = sum(e[i] * e[i - k] for i in range(k, n)) / n
        s += 2.0 * (1.0 - k / (lag + 1.0)) * g
    return math.sqrt(max(s, 0.0) / n)


def mde_nw(xs: Sequence[float], lag: int = 4, *, z: float = 2.8) -> float:
    """Minimum detectable mean at 5 % two-sided / 80 % power: z * Newey-West SE."""
    return z * newey_west_se(xs, lag)


def block_bootstrap_ci(
    xs: Sequence[float], *, block: int = 4, n_boot: int = 10_000, seed: int = 7, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile CI of the mean by the moving-block bootstrap (blocks of consecutive observations)."""
    n = len(xs)
    rng = random.Random(seed)
    starts = n - block + 1
    means = []
    for _ in range(n_boot):
        acc: list[float] = []
        while len(acc) < n:
            i = rng.randrange(starts)
            acc.extend(xs[i : i + block])
        means.append(statistics.fmean(acc[:n]))
    means.sort()
    lo = means[int(math.floor(alpha / 2 * n_boot))]
    hi = means[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]
    return lo, hi


def ols_beta(ys: Sequence[float], xs: Sequence[float]) -> float:
    """Slope of ``ys`` on ``xs``."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx > 0 else 0.0


def trailing_beta(bars: Bars, btc: Bars, day: int, *, window: int = 60, min_days: int = 40) -> float | None:
    """OLS beta of daily close-to-close returns to BTC over the ``window`` days strictly before ``day``."""
    ys, xs = [], []
    for d in range(day - window, day):
        if d in bars.close and d - 1 in bars.close and d in btc.close and d - 1 in btc.close:
            ys.append(bars.close[d] / bars.close[d - 1] - 1.0)
            xs.append(btc.close[d] / btc.close[d - 1] - 1.0)
    return ols_beta(ys, xs) if len(ys) >= min_days else None


def sma_regime(btc: Bars, day: int, *, window: int = 200) -> int | None:
    """+1 when the last BTC close before ``day`` is above its ``window``-day mean (closes before ``day``), -1 below."""
    cl = [btc.close[d] for d in range(day - window, day) if d in btc.close]
    if len(cl) < window * 0.9 or (day - 1) not in btc.close:
        return None
    return 1 if btc.close[day - 1] > statistics.fmean(cl) else -1
