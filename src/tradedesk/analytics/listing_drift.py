"""New-perp listing drift: short a fresh USDT-M perp after its listing, measured against BTC / an old-perp basket.

Pure functions (no I/O). Time is passed in: UTC day indices (``ms // DAY_MS``) and epoch milliseconds.
Returns are decimals on notional (0.05 = 5 %). Funding rates are decimals per settlement (positive: longs pay shorts).
Idea card: docs/ideas/IDEA-20261015-new-perp-listing-drift.md.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

STABLE_OR_WRAPPED = frozenset(
    {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "USD1", "RLUSD", "PYUSD", "EUR", "AEUR"}
    | {"WBTC", "WBETH", "WETH", "STETH", "BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD"}
    | {"PAXG", "XAUT"}  # gold-backed tokens: TradFi exposure under a COIN label
)


def day_of(ms: int) -> int:
    return int(ms) // DAY_MS


def month_key(ms: int) -> int:
    """Calendar month of an epoch-ms timestamp as ``year * 12 + month - 1`` (UTC)."""
    d = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return d.year * 12 + d.month - 1


def symbol_eligible(symbol: str, contract_type: str | None, underlying_type: str | None) -> bool:
    """Linear USDT perp on a crypto coin: not dated, not TradFi/index, not a stable or wrapped token.

    ``contract_type`` / ``underlying_type`` come from today's exchange info (None for symbols no longer listed)."""
    if not symbol.endswith("USDT") or "_" in symbol or len(symbol) <= 4 or symbol.endswith("SETTLED"):
        return False
    if contract_type is not None and contract_type != "PERPETUAL":
        return False
    if underlying_type is not None and underlying_type != "COIN":
        return False
    return symbol[:-4] not in STABLE_OR_WRAPPED


def first_trade_ms(rows: Sequence[Sequence[float]]) -> int | None:
    """Open time of the first bar with positive base volume (rows ``[open_ms, o, h, l, c, base_vol, ...]``)."""
    for r in sorted(rows, key=lambda r: r[0]):
        if len(r) > 5 and r[5] > 0 and r[4] > 0:
            return int(r[0])
    return None


def entry_day(t0_ms: int, *, min_gap_ms: int = DAY_MS, delay_days: int = 0) -> int:
    """First UTC day whose 00:00 open is at least ``min_gap_ms`` after the first trade, plus ``delay_days``."""
    earliest = t0_ms + min_gap_ms
    d = -(-earliest // DAY_MS)  # ceil: day starts at d * DAY_MS >= earliest
    return int(d) + delay_days


@dataclass
class DailyBars:
    """Daily bars keyed by UTC day index; only days that traded (base volume > 0) are kept."""

    open: dict[int, float]
    high: dict[int, float]
    close: dict[int, float]
    qv: dict[int, float]

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[float]]) -> DailyBars:
        """Rows ``[open_ms, o, h, l, c, base_vol, quote_vol]`` (the archive layout)."""
        o: dict[int, float] = {}
        h: dict[int, float] = {}
        c: dict[int, float] = {}
        q: dict[int, float] = {}
        for r in rows:
            if len(r) < 6 or r[5] <= 0 or r[4] <= 0 or r[1] <= 0:
                continue
            d = day_of(int(r[0]))
            o[d], h[d], c[d] = float(r[1]), float(r[2]), float(r[4])
            q[d] = float(r[6]) if len(r) > 6 else float(r[5]) * float(r[4])
        return cls(o, h, c, q)

    @property
    def first(self) -> int | None:
        return min(self.close) if self.close else None

    @property
    def last(self) -> int | None:
        return max(self.close) if self.close else None

    def close_on_or_before(self, day: int, floor: int) -> int | None:
        for d in range(day, floor - 1, -1):
            if d in self.close:
                return d
        return None


@dataclass(frozen=True)
class ShortLeg:
    entry: int
    exit: int  # day whose close ends the trade (the liquidation day if liquidated)
    p_entry: float
    p_exit: float
    pnl: float  # short price P&L on notional, floored at -1 by the 1x liquidation rule
    liquidated: bool
    mae: float  # max(high) / p_entry - 1 over [entry; planned exit or last bar]
    truncated: bool  # planned exit day had no bar (delisted / gap), last earlier close used


def liq_threshold(leverage: float, mm: float = 0.025) -> float:
    """Adverse move (fraction of entry) that liquidates an isolated short at ``leverage``: 1/k - maintenance."""
    return 1.0 / leverage - mm


def short_leg(
    bars: DailyBars, entry: int, horizon: int, *, liq_move: float | None = liq_threshold(1.0), liq_loss: float = 1.0
) -> ShortLeg | None:
    """Short opened at the open of ``entry``, closed at the close of ``entry + horizon - 1``.

    If a daily high reaches ``p_entry * (1 + liq_move)`` the position is closed that day with P&L ``-liq_loss``
    (1x: -100 % of notional). Uses only bars in [entry; entry + horizon - 1]. None without an entry open."""
    if entry not in bars.open:
        return None
    p0 = bars.open[entry]
    planned = entry + horizon - 1
    mae = 0.0
    for d in range(entry, planned + 1):
        h = bars.high.get(d)
        if h is None:
            continue
        mae = max(mae, h / p0 - 1.0)
        if liq_move is not None and h >= p0 * (1.0 + liq_move):
            return ShortLeg(entry, d, p0, bars.close[d], -liq_loss, True, mae, False)
    ex = bars.close_on_or_before(planned, entry)
    if ex is None:
        return None
    p1 = bars.close[ex]
    return ShortLeg(entry, ex, p0, p1, -(p1 / p0 - 1.0), False, mae, ex < planned)


def bench_return(bars: DailyBars, entry: int, end_day: int) -> float | None:
    """Open of ``entry`` to close of ``end_day`` (last earlier close if that day is missing)."""
    if entry not in bars.open:
        return None
    ex = bars.close_on_or_before(end_day, entry)
    if ex is None:
        return None
    return bars.close[ex] / bars.open[entry] - 1.0


def funding_received(records: Sequence[Sequence[float]], closes: Mapping[int, float], p_entry: float, start_ms: int, end_ms: int) -> float:
    """Funding a fixed-quantity short receives over settlements with time in ``[start_ms; end_ms)``.

    ``records`` = ``[time_ms, interval_hours, rate]``. Each payment is ``rate * notional_i / notional_0`` with
    notional marked at the close of the settlement day (last earlier close if missing)."""
    tot = 0.0
    for r in records:
        t = int(r[0])
        if not (start_ms <= t < end_ms):
            continue
        d = day_of(t)
        px = None
        for k in range(d, d - 5, -1):
            if k in closes:
                px = closes[k]
                break
        tot += float(r[2]) * ((px / p_entry) if px else 1.0)
    return tot


def funding_gaps(records: Sequence[Sequence[float]], start_ms: int, end_ms: int, *, slack_ms: int = HOUR_MS) -> list[tuple[int, int]]:
    """Holes in a settlement series over ``[start_ms; end_ms)``.

    A hole is a stretch longer than the prevailing interval + ``slack_ms`` without a settlement: between the window
    start and the first settlement, between neighbours, and between the last settlement and the window end. The
    interval is the record's ``interval_hours`` (the median spacing when the column is 0 / absent)."""
    ts = sorted((int(r[0]), float(r[1]) if len(r) > 2 else 0.0) for r in records)
    inside = [(t, h) for t, h in ts if start_ms <= t < end_ms]
    spacings = [b[0] - a[0] for a, b in zip(ts, ts[1:], strict=False) if b[0] > a[0]]
    fallback = statistics.median(spacings) if spacings else 8 * HOUR_MS

    def iv(h: float) -> float:
        return h * HOUR_MS if h > 0 else fallback

    gaps = []
    if not inside:
        return [(start_ms, end_ms)]
    prev_t = start_ms
    prev_iv = iv(inside[0][1])
    for t, h in inside:
        if t - prev_t > prev_iv + slack_ms:
            gaps.append((prev_t, t))
        prev_t, prev_iv = t, iv(h)
    if end_ms - prev_t > prev_iv + slack_ms:
        gaps.append((prev_t, end_ms))
    return gaps


def placebo_day(symbol: str, t0_day: int, *, lo: int = 120, hi: int = 240) -> int:
    """Deterministic pseudo-random day of age ``lo..hi`` (inclusive) after the listing day."""
    h = int(hashlib.sha256(symbol.encode()).hexdigest(), 16)
    return t0_day + lo + h % (hi - lo + 1)


def basket_members(
    entry: int,
    first_days: Mapping[str, int],
    qv: Mapping[str, Mapping[int, float]],
    *,
    top_n: int = 30,
    min_age: int = 365,
    window: int = 30,
    min_days: int = 20,
    exclude: Iterable[str] = (),
) -> list[str]:
    """Top ``top_n`` symbols by quote volume over the ``window`` days strictly before ``entry`` among symbols listed
    at least ``min_age`` days before ``entry``. Uses no data from ``entry`` on. Ties break by name."""
    ex = set(exclude)
    ranked = []
    for s, f in first_days.items():
        if s in ex or entry - f < min_age:
            continue
        q = qv.get(s) or {}
        vals = [q[d] for d in range(entry - window, entry) if d in q]
        if len(vals) >= min_days:
            ranked.append((-sum(vals), s))
    ranked.sort()
    return [s for _, s in ranked[:top_n]]


def basket_return(members: Sequence[str], bars: Mapping[str, DailyBars], entry: int, end_day: int) -> float | None:
    """Equal-weight mean of member returns open(entry) -> close(end_day); members without an entry open are skipped."""
    rs = [r for s in members if (r := bench_return(bars[s], entry, end_day)) is not None]
    return statistics.fmean(rs) if rs else None


# ---------------------------------------------------------------- statistics


def _groups(values: Sequence[float], clusters: Sequence[object]) -> dict[object, list[float]]:
    g: dict[object, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        g.setdefault(c, []).append(float(v))
    return g


def icc_anova(values: Sequence[float], clusters: Sequence[object]) -> float:
    """One-way ANOVA intraclass correlation for unequal cluster sizes; negative estimates are returned as is."""
    g = _groups(values, clusters)
    n, k = len(values), len(g)
    if k < 2 or n <= k:
        return 0.0
    grand = statistics.fmean(values)
    ssb = sum(len(v) * (statistics.fmean(v) - grand) ** 2 for v in g.values())
    ssw = sum(sum((x - statistics.fmean(v)) ** 2 for x in v) for v in g.values())
    msb, msw = ssb / (k - 1), ssw / (n - k)
    k0 = (n - sum(len(v) ** 2 for v in g.values()) / n) / (k - 1)
    den = msb + (k0 - 1) * msw
    return (msb - msw) / den if den > 0 else 0.0


def design_effect(clusters: Sequence[object], icc: float) -> float:
    """1 + (sum m_i^2 / n - 1) * max(icc, 0): size-weighted mean cluster size."""
    sizes: dict[object, int] = {}
    for c in clusters:
        sizes[c] = sizes.get(c, 0) + 1
    n = len(clusters)
    if n == 0:
        return 1.0
    mbar = sum(m * m for m in sizes.values()) / n
    return 1.0 + (mbar - 1.0) * max(icc, 0.0)


def cr1_se(values: Sequence[float], clusters: Sequence[object]) -> float:
    """Cluster-robust (CR1) standard error of the mean."""
    g = _groups(values, clusters)
    n, k = len(values), len(g)
    if k < 2:
        return float("nan")
    mu = statistics.fmean(values)
    s = sum(sum(x - mu for x in v) ** 2 for v in g.values())
    return math.sqrt(s * k / (k - 1)) / n


def mde(sd: float, n: int, deff: float = 1.0, *, z: float = 2.8) -> float:
    """Minimum detectable effect at 5 % two-sided / 80 % power: z * sd * sqrt(deff / n)."""
    return z * sd * math.sqrt(deff / n) if n > 0 else float("inf")


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values (same order as the input)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    out = [0.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvalues[i]))
        out[i] = run
    return out


def one_sided_p(t: float) -> float:
    """P(Z > t) under the normal approximation."""
    return 0.5 * math.erfc(t / math.sqrt(2.0))


def market_age_days(t0_ms: int, evidence_ms: Iterable[int | None]) -> float | None:
    """Days between the earliest trading evidence elsewhere and the perp's first trade (None without evidence).

    Evidence after ``t0_ms`` counts as 0 (the token traded first on this perp)."""
    xs = [e for e in evidence_ms if e is not None]
    if not xs:
        return None
    return max(0.0, (t0_ms - min(xs)) / DAY_MS)


def is_new_token(t0_ms: int, evidence_ms: Iterable[int | None], *, max_age_days: float = 30.0) -> bool:
    """True when no venue shows trading earlier than ``max_age_days`` before the perp's first trade."""
    a = market_age_days(t0_ms, evidence_ms)
    return a is None or a <= max_age_days
