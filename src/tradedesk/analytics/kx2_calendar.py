"""KX-2 (KillaXBT "time-based pivots"): fixed calendar days of the month as entry dates.

Registration: ``docs/ideas/IDEA-20261113-x-traders.md`` section 5.5. Short BTC from the 00:00 UTC open of the 14th, long
BTC from the 00:00 UTC open of the 1st (the month open), each held 14 or 3 days; one trade per month and test, so the
windows of one test never overlap. Pure functions: daily bars come in as arrays (open time in ms, open price), funding
as (time ms, rate) points; nothing here reads a clock or the network. Returns are simple fractions on notional
(0.01 = 1 %).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

DAY_MS = 86_400_000
FUNDING_PROXY_8H = 0.0001  # Binance default (interest component) per 8h, used where no funding history exists
PLACEBO_DAYS = 28  # days 1..28 exist in every month


def day_of_month(t_ms: np.ndarray) -> np.ndarray:
    """UTC day of month (1..31) of each open time."""
    d = np.asarray(t_ms, dtype="int64").astype("datetime64[ms]").astype("datetime64[D]")
    return (d - d.astype("datetime64[M]")).astype(int) + 1


def month_index(t_ms: np.ndarray) -> np.ndarray:
    """Months since 1970-01 (UTC) of each open time."""
    return np.asarray(t_ms, dtype="int64").astype("datetime64[ms]").astype("datetime64[M]").astype(int)


def window_return(t: np.ndarray, o: np.ndarray, t_entry: int, hold_days: int) -> float:
    """Open-to-open simple return from the daily bar opening at ``t_entry`` to the one ``hold_days`` later.

    NaN if either bar is missing (gaps are not bridged). Uses only the two bars, so data after the exit never matter."""
    i = int(np.searchsorted(t, t_entry))
    j = int(np.searchsorted(t, t_entry + hold_days * DAY_MS))
    if i >= t.size or j >= t.size or int(t[i]) != t_entry or int(t[j]) != t_entry + hold_days * DAY_MS:
        return float("nan")
    a, b = float(o[i]), float(o[j])
    return b / a - 1.0 if a > 0 and b > 0 else float("nan")


def funding_sum(f_t: np.ndarray, f_r: np.ndarray, t0: int, t1: int, proxy_before: int | None = None) -> float:
    """Sum of funding rates with t0 < time <= t1 (paid by longs when positive).

    If ``proxy_before`` is given and the window starts before it (no funding history), the window gets the
    0.01 % / 8h proxy for its full length instead."""
    if proxy_before is not None and t0 < proxy_before:
        return FUNDING_PROXY_8H * (t1 - t0) / (8 * 3_600_000)
    lo = int(np.searchsorted(f_t, t0, side="right"))
    hi = int(np.searchsorted(f_t, t1, side="right"))
    return float(np.sum(f_r[lo:hi]))


def funding_for_side(f_t: np.ndarray, f_r: np.ndarray, t0: int, t1: int, side: str, proxy_before: int) -> float:
    """Funding of one trade (positive = paid by longs) with no free credit for shorts before any perp history.

    Windows starting before ``proxy_before``: a long pays the 0.01 % / 8h proxy, a short gets 0 (there was no perp to
    collect funding from; borrowing BTC to short cost money). Later windows: actual funding, t0 < time <= t1."""
    if t0 < proxy_before:
        return FUNDING_PROXY_8H * (t1 - t0) / (8 * 3_600_000) if side == "long" else 0.0
    return funding_sum(f_t, f_r, t0, t1)


def round_to_hour(t_ms: np.ndarray) -> np.ndarray:
    """Funding stamps rounded to the nearest whole hour (Binance stamps carry a few ms of jitter, e.g. ...600009)."""
    t = np.asarray(t_ms, dtype="int64")
    return (t + 1_800_000) // 3_600_000 * 3_600_000


def net_target(gross: float, side: str, hold_days: int, fee_rt: float, short_credit_share: float) -> float:
    """Net-of-cost effect of interest for the MDE gate: the gross price move minus the round-trip fee and the expected
    funding at the 0.01 % / 8h proxy. A long pays it on every trade; a short collects it only on the share of trades
    that have a real perp funding history (``short_credit_share``, 0..1), matching ``funding_for_side``."""
    f = FUNDING_PROXY_8H * 3 * hold_days
    return gross - fee_rt - f if side == "long" else gross - fee_rt + short_credit_share * f


def net_return(price_ret: float, side: str, funding: float, fee_rt: float) -> float:
    """Return on notional after the round-trip fee and funding: long pays positive funding, short receives it."""
    s = 1.0 if side == "long" else -1.0
    return s * price_ret - fee_rt - s * funding


def entry_times(t: np.ndarray, dom: int, start_ms: int, end_ms: int, shift_days: int = 0) -> list[int]:
    """Open times of the entry bars: day ``dom`` of every month with start_ms <= day open < end_ms, plus ``shift_days``.

    Months where the day bar is missing are skipped (counted by the caller as gaps)."""
    t = np.asarray(t, dtype="int64")
    sel = t[(t >= start_ms) & (t < end_ms) & (day_of_month(t) == dom)]
    return [int(x) + shift_days * DAY_MS for x in sel]


def month_day_matrix(t: np.ndarray, o: np.ndarray, months: Sequence[int], hold_days: int, shift_days: int = 0) -> np.ndarray:
    """Price returns R[k, d-1] for month ``months[k]`` (months since 1970-01) and entry day d = 1..28 (+ shift)."""
    out = np.full((len(months), PLACEBO_DAYS), np.nan)
    for k, m in enumerate(months):
        base = np.datetime64(int(m), "M").astype("datetime64[D]").astype("datetime64[ms]").astype("int64")
        for d in range(PLACEBO_DAYS):
            out[k, d] = window_return(t, o, int(base) + (d + shift_days) * DAY_MS, hold_days)
    return out


def placebo_means(sided: np.ndarray, target_dom: int, n_sets: int, seed: int) -> np.ndarray:
    """Means of the same trade started on a random other day 1..28 in each month (one draw per month and set).

    ``sided`` is the month x day matrix of net returns already signed for the trade side. NaN cells are skipped."""
    rng = np.random.default_rng(seed)
    others = np.array([d for d in range(PLACEBO_DAYS) if d != target_dom - 1])
    n_m = sided.shape[0]
    pick = others[rng.integers(0, others.size, size=(n_sets, n_m))]
    vals = sided[np.arange(n_m)[None, :], pick]
    with np.errstate(invalid="ignore"):
        return np.nanmean(vals, axis=1)


def placebo_p(actual: float, plc: np.ndarray) -> float:
    """One-sided rank p: share of placebo means at or above the actual mean (with the +1 correction)."""
    plc = plc[np.isfinite(plc)]
    return (1.0 + float(np.sum(plc >= actual))) / (1.0 + plc.size)


def mean_ci(x: np.ndarray, n_boot: int, seed: int) -> dict[str, float]:
    """Mean, iid bootstrap 95 % percentile CI, normal SE and one-sided p (H0: mean <= 0) of independent samples."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 2:
        return {"n": float(n), "m": float("nan"), "lo": float("nan"), "hi": float("nan"), "se": float("nan"), "p": float("nan")}
    rng = np.random.default_rng(seed)
    boots = x[rng.integers(0, n, size=(n_boot, n))].mean(axis=1)
    m = float(x.mean())
    se = float(x.std(ddof=1) / math.sqrt(n))
    p = 0.5 * math.erfc((m / se) / math.sqrt(2.0)) if se > 0 else (0.0 if m > 0 else 1.0)
    return {"n": float(n), "m": m, "lo": float(np.quantile(boots, 0.025)), "hi": float(np.quantile(boots, 0.975)), "se": se, "p": p}


def z_quantile(q: float) -> float:
    """Standard normal quantile (bisection on erfc; enough for power arithmetic)."""
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 0.5 * math.erfc(-mid / math.sqrt(2.0)) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def mde80(sd: float, n: int, m_tests: int, alpha: float = 0.05, power: float = 0.8) -> float:
    """Minimum detectable mean (one-sided, Bonferroni-level alpha / m, i.e. Holm's first step) at the given power."""
    if n <= 0 or not math.isfinite(sd):
        return float("nan")
    return (z_quantile(1.0 - alpha / m_tests) + z_quantile(power)) * sd / math.sqrt(n)
