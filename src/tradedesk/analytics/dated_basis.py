"""Dated-future basis as a forecast of perp funding: forward rate vs realized funding over the same period.

Units follow the project convention: basis, funding and returns are decimal fractions of notional
(0.10 = 10 %); annualised figures are simple (x 365 / days). Times are ms since epoch, UTC; Deribit and
Binance quarterly futures expire at 08:00 UTC on the last Friday of March / June / September / December.

Everything here is pure: callers pass the prints, funding records and the reference time, and every
function reads only data at or before the time it is asked about (entry uses prints <= t0; realized
funding uses records in (t0, expiry]).
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


# ----------------------------------------------------------------------------- calendar


def last_friday(year: int, month: int) -> datetime:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1, 8, tzinfo=UTC)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 4) % 7)


def quarterly_expiries(start_ms: int, end_ms: int) -> list[int]:
    """Quarterly expiries (last Friday of Mar/Jun/Sep/Dec, 08:00 UTC) within [start_ms, end_ms]."""
    y0 = datetime.fromtimestamp(start_ms / 1000, UTC).year
    y1 = datetime.fromtimestamp(end_ms / 1000, UTC).year
    out = []
    for y in range(y0, y1 + 1):
        for m in (3, 6, 9, 12):
            t = int(last_friday(y, m).timestamp() * 1000)
            if start_ms <= t <= end_ms:
                out.append(t)
    return out


def deribit_name(currency: str, expiry_ms: int) -> str:
    d = datetime.fromtimestamp(expiry_ms / 1000, UTC)
    return f"{currency.upper()}-{d.day}{MONTHS[d.month - 1]}{d:%y}"


def binance_usdm_name(base: str, expiry_ms: int) -> str:
    return f"{base.upper()}USDT_{datetime.fromtimestamp(expiry_ms / 1000, UTC):%y%m%d}"


# ----------------------------------------------------------------------------- prices


def ratio_from_prints(prints: list[dict[str, Any]], t_ref_ms: int, *, window_ms: int = HOUR_MS, max_n: int = 50) -> dict[str, float] | None:
    """Median ``price / index_price`` over the freshest ``max_n`` prints in [t_ref - window, t_ref].

    Price and index come from the same print, so the ratio is synchronous. Prints after ``t_ref`` are
    ignored. Returns ``{"ratio", "index", "n", "age_ms"}`` (``index`` of the latest print) or None.
    """
    ok = [p for p in prints if p.get("price") and p.get("index_price") and t_ref_ms - window_ms <= int(p["timestamp"]) <= t_ref_ms]
    if not ok:
        return None
    ok.sort(key=lambda p: int(p["timestamp"]))
    ok = ok[-max_n:]
    r = statistics.median(float(p["price"]) / float(p["index_price"]) for p in ok)
    return {"ratio": r, "index": float(ok[-1]["index_price"]), "n": len(ok), "age_ms": t_ref_ms - int(ok[-1]["timestamp"])}


def bar_close_at(bars: list[list[float]], t_ref_ms: int, bar_ms: int = HOUR_MS) -> float | None:
    """Close of the bar that ends exactly at ``t_ref`` (open = t_ref - bar_ms); bars are [open_ms, o, h, l, c, ...]."""
    for b in bars:
        if int(b[0]) == t_ref_ms - bar_ms:
            return float(b[4])
    return None


def annualize(x: float, days: float) -> float:
    return x * 365.0 / days


def basis_ann(ratio: float, days: float, *, log: bool = False) -> float:
    """Annualised basis of a future at ``ratio = F / index`` with ``days`` to expiry (simple, or log)."""
    return annualize(math.log(ratio) if log else ratio - 1.0, days)


# ----------------------------------------------------------------------------- funding


class FundingSeries:
    """Funding records ``(ts_ms, rate[, index])`` with prefix sums; ``rate`` is the payment as a fraction of notional
    accrued at ``ts`` (longs pay when positive). Venues with any interval (1h Deribit, 8h/4h Binance) are handled
    because sums run over real event times."""

    def __init__(self, records: list[tuple[int, float] | tuple[int, float, float]]) -> None:
        rec = sorted(records, key=lambda r: r[0])
        self.ts = [int(r[0]) for r in rec]
        self.rate = [float(r[1]) for r in rec]
        self.index = [float(r[2]) if len(r) > 2 and r[2] else float("nan") for r in rec]
        self._p = [0.0]
        for v in self.rate:
            self._p.append(self._p[-1] + v)

    def _span(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        return bisect.bisect_right(self.ts, start_ms), bisect.bisect_right(self.ts, end_ms)

    def total(self, start_ms: int, end_ms: int) -> float:
        """Sum of rates for records with ts in (start_ms, end_ms]."""
        a, b = self._span(start_ms, end_ms)
        return self._p[b] - self._p[a]

    def count(self, start_ms: int, end_ms: int) -> int:
        a, b = self._span(start_ms, end_ms)
        return b - a

    def weighted(self, start_ms: int, end_ms: int, base: float) -> float:
        """Sum of ``rate * index / base`` over (start, end]: USD payments per unit of entry notional for a fixed coin quantity."""
        a, b = self._span(start_ms, end_ms)
        return sum(self.rate[i] * self.index[i] / base for i in range(a, b))

    def first_ts(self) -> int | None:
        return self.ts[0] if self.ts else None


def coverage(fs: FundingSeries, start_ms: int, end_ms: int, interval_ms: int) -> float:
    """Records in (start, end] as a share of the expected count at a fixed ``interval_ms``."""
    expected = (end_ms - start_ms) / interval_ms
    return fs.count(start_ms, end_ms) / expected if expected > 0 else 0.0


# ----------------------------------------------------------------------------- one contract


def contract_record(
    *,
    expiry_ms: int,
    days: float,
    fut_ratio: float,
    funding: FundingSeries,
    index0: float | None = None,
    perp_ratio0: float | None = None,
    perp_ratio_exit: float | None = None,
    index_exit: float | None = None,
    delivery: float | None = None,
    log: bool = False,
    cal_cost: float = 0.00175,
    cc_cost: float = 0.00275,
    perp_cc_cost: float = 0.003,
) -> dict[str, Any]:
    """H1 difference and the P&L of three books for one contract entered ``days`` before ``expiry_ms``.

    * ``d`` = annualised basis at entry - annualised realized funding over (t0, expiry].
    * ``calendar``: short dated + long perp (1 coin each), held to expiry, per unit of entry index, after
      ``cal_cost``: ``(F0 - D) + (P_E - P0) - sum(rate * I_h)``, all / I0.
    * ``dated_cc``: long spot at I0 + short dated, delivered at D: ``(F0 - I0)/I0 - cc_cost`` (on capital = I0).
    * ``perp_cc``: long spot + short perp over the same dates: funding received + premium at entry - premium at exit.
    P&L fields are per period; ``*_ann`` fields are x 365 / days. Fields needing optional inputs are None without them.
    """
    t0 = int(expiry_ms - days * DAY_MS)
    b = basis_ann(fut_ratio, days, log=log)
    f_sum = funding.total(t0, expiry_ms)
    out: dict[str, Any] = {
        "expiry": expiry_ms,
        "t0": t0,
        "days": days,
        "basis": fut_ratio - 1.0,
        "basis_ann": b,
        "funding": f_sum,
        "funding_ann": annualize(f_sum, days),
        "d": b - annualize(f_sum, days),
        "calendar": None,
        "dated_cc": fut_ratio - 1.0 - cc_cost,
        "perp_cc": None,
    }
    if index0 and perp_ratio0 and perp_ratio_exit and index_exit and delivery:
        f0, p0 = fut_ratio * index0, perp_ratio0 * index0
        pe = perp_ratio_exit * index_exit
        paid = funding.weighted(t0, expiry_ms, index0)  # USD per unit of I0
        out["calendar"] = ((f0 - delivery) + (pe - p0)) / index0 - paid - cal_cost
        out["perp_cc"] = paid + (p0 - index0) / index0 - (pe - index_exit) / index0 - perp_cc_cost
        out["convergence_noise"] = (index_exit - delivery) / index0
        # calendar = (R0 - 1) - f_sum  [= d over the period]  - perp_premium0 + exit_gap - (weighted - f_sum) - cost
        out["perp_premium0"] = perp_ratio0 - 1.0
        out["exit_gap"] = (pe - delivery) / index0
        out["funding_weighted"] = paid
    for k in ("calendar", "dated_cc", "perp_cc"):
        out[k + "_ann"] = annualize(out[k], days) if out[k] is not None else None
    return out


# ----------------------------------------------------------------------------- statistics


def t_quantile(p: float, df: int) -> float:
    """Student-t quantile via the Cornish-Fisher expansion around the normal (error < 1e-3 for df >= 5)."""
    z = statistics.NormalDist().inv_cdf(p)
    v = float(df)
    return (
        z + (z**3 + z) / (4 * v) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * v**2) + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * v**3)
    )


def mean_ci(xs: list[float], level: float = 0.95) -> dict[str, Any]:
    """Mean, sample SD and a two-sided t confidence interval."""
    n = len(xs)
    if n == 0:
        return {"n": 0}
    m = statistics.fmean(xs)
    if n < 2:
        return {"n": n, "mean": m, "sd": None, "ci": None, "median": m, "share_pos": float(m > 0)}
    sd = statistics.stdev(xs)
    h = t_quantile(0.5 + level / 2, n - 1) * sd / math.sqrt(n)
    return {
        "n": n,
        "mean": m,
        "sd": sd,
        "ci": (m - h, m + h),
        "median": statistics.median(xs),
        "share_pos": sum(1 for x in xs if x > 0) / n,
    }


def mde(sd: float, n: int, *, alpha: float = 0.05, power: float = 0.80) -> float:
    """Minimum detectable mean (two-sided one-sample t-test) for SD ``sd`` and ``n`` observations."""
    if n < 3:
        return float("inf")
    return (t_quantile(1 - alpha / 2, n - 1) + t_quantile(power, n - 1)) * sd / math.sqrt(n)


def bootstrap_ci(xs: list[float], *, reps: int = 10_000, seed: int = 7, level: float = 0.95) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean (i.i.d. resampling; for non-overlapping per-contract values)."""
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    n = len(xs)
    ms = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(reps))
    lo = ms[int((1 - level) / 2 * reps)]
    hi = ms[min(reps - 1, int((1 + level) / 2 * reps))]
    return lo, hi


def block_bootstrap_diff(
    days: list[int], xs: list[float], groups: list[int], *, block_days: int, reps: int = 2000, seed: int = 7
) -> dict[str, Any]:
    """Mean of group 1 minus group 0 on daily points with a moving-block bootstrap (blocks of calendar days).

    ``days`` are day indices (ts // DAY_MS), ``groups`` 0/1/-1 (-1 = middle, ignored). Blocks of ``block_days``
    consecutive calendar days are resampled with replacement, which keeps the serial dependence of
    overlapping holding periods inside a block.
    """
    pts = sorted(zip(days, xs, groups, strict=True))
    if not pts:
        return {"diff": None, "ci": None}

    def diff(sample: list[tuple[int, float, int]]) -> float | None:
        a = [x for _, x, g in sample if g == 1]
        b = [x for _, x, g in sample if g == 0]
        return statistics.fmean(a) - statistics.fmean(b) if a and b else None

    d0 = diff(pts)
    first, last = pts[0][0], pts[-1][0]
    blocks: dict[int, list[tuple[int, float, int]]] = {}
    for p in pts:
        blocks.setdefault((p[0] - first) // block_days, []).append(p)
    starts = list(blocks)
    n_blocks = (last - first) // block_days + 1
    rng = random.Random(seed)
    out = []
    for _ in range(reps):
        sample: list[tuple[int, float, int]] = []
        for _ in range(n_blocks):
            sample.extend(blocks[rng.choice(starts)])
        v = diff(sample)
        if v is not None:
            out.append(v)
    out.sort()
    ci = (out[int(0.025 * len(out))], out[min(len(out) - 1, int(0.975 * len(out)))]) if out else None
    return {"diff": d0, "ci": ci, "n": len(pts), "n_blocks": n_blocks}


def tercile_groups(values: list[float]) -> list[int]:
    """1 for the top tercile, 0 for the bottom, -1 for the middle (thresholds from the whole sample)."""
    s = sorted(values)
    n = len(s)
    if n < 3:
        return [-1] * n
    lo, hi = s[n // 3], s[(2 * n) // 3]
    return [1 if v >= hi else 0 if v < lo else -1 for v in values]
