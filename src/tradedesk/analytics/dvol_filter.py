"""DVOL-percentile filter for a weekly short option position (IDEA-20261031-structural-edges-depth, item 8e).

Pure functions, no I/O. Units: P&L as a fraction of entry spot per 1 BTC of notional (0.01 = 1 % of spot);
percentiles in [0, 1]; times in epoch milliseconds (UTC).

The filter trades week ``t`` only when the DVOL percentile known at entry is ``>= threshold``. The gain over
the unfiltered book on week ``t`` is ``pnl_t * 1[selected] - pnl_t = -pnl_t * 1[not selected]``; its mean over
all calendar weeks is the change of the book's average weekly P&L caused by the filter.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

from tradedesk.analytics.put_skew import DAY_MS, dvol_known_at_entry, trailing_percentile


def entry_percentiles(dvol_candles: Sequence[tuple[int, float]], *, window_days: int = 365, min_obs: int = 180) -> dict[int, float]:
    """Entry day (00:00 UTC ms) -> percentile of the DVOL known at that day's 09:00 entry.

    The value known on entry day ``d`` is the close of the daily candle stamped ``d - 1 day``
    (``dvol_known_at_entry``). Its percentile is the mid-rank among values known on strictly earlier entry
    days within ``window_days`` (``t - s <= window``), and only if at least ``min_obs`` such values exist.
    """
    known = dvol_known_at_entry(dvol_candles)
    return trailing_percentile(sorted(known.items()), window_ms=window_days * DAY_MS, min_obs=min_obs)


def filter_rows(pnl_by_day: Mapping[int, float], pct_by_day: Mapping[int, float], threshold: float) -> list[dict[str, Any]]:
    """Time-ordered rows for days present in both maps: pnl, pct, selected, filtered pnl and gain over unfiltered."""
    out = []
    for d in sorted(set(pnl_by_day) & set(pct_by_day)):
        pnl, pct = float(pnl_by_day[d]), float(pct_by_day[d])
        sel = pct >= threshold
        filt = pnl if sel else 0.0
        out.append({"day": d, "pnl": pnl, "pct": pct, "selected": sel, "filtered": filt, "gain": filt - pnl})
    return out


def block_bootstrap_mean(values: Sequence[float], *, block: int = 4, n_boot: int = 10_000, seed: int = 20261031) -> dict[str, Any]:
    """Moving-block bootstrap of the mean: 95 % percentile CI and two-sided p = 2 * min(P(<=0), P(>=0))."""
    n = len(values)
    if n < 2 * block:
        return {"n": n, "mean": (sum(values) / n) if n else float("nan"), "ci95": None, "p": None}
    rng = random.Random(seed)
    means = []
    last = n - block + 1
    for _ in range(n_boot):
        s, c = 0.0, 0
        while c < n:
            i = rng.randrange(0, last)
            for v in values[i : i + block]:
                if c >= n:
                    break
                s += v
                c += 1
        means.append(s / n)
    means.sort()
    le = sum(1 for m in means if m <= 0) / n_boot
    ge = sum(1 for m in means if m >= 0) / n_boot
    return {
        "n": n,
        "mean": sum(values) / n,
        "ci95": (means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]),
        "p": min(1.0, 2.0 * min(le, ge)),
    }


def placebo_gains(pnls: Sequence[float], k: int, *, n_draws: int = 10_000, seed: int = 20261031) -> list[float]:
    """Gain of selecting ``k`` random weeks (without replacement) instead of all: -sum(pnl of unselected) / n."""
    n = len(pnls)
    if not 0 <= k <= n or n == 0:
        raise ValueError("need 0 <= k <= n and n > 0")
    rng = random.Random(seed)
    total = sum(pnls)
    idx = list(range(n))
    out = []
    for _ in range(n_draws):
        chosen = rng.sample(idx, k)
        kept = sum(pnls[i] for i in chosen)
        out.append(-(total - kept) / n)
    return out


def placebo_p(observed: float, draws: Sequence[float]) -> float:
    """One-sided placebo p: (1 + #draws >= observed) / (1 + n_draws)."""
    return (1 + sum(1 for x in draws if x >= observed)) / (1 + len(draws))


def quantile(values: Sequence[float], q: float) -> float:
    """Lower empirical quantile (index floor(q * (n - 1)) of the sorted values)."""
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def cvar(values: Sequence[float], q: float = 0.05) -> float:
    """Mean of the worst ceil(q * n) values (expected shortfall, same sign as the values)."""
    s = sorted(values)
    k = max(1, math.ceil(q * len(s)))
    return sum(s[:k]) / k


def mean_without_top(values: Sequence[float], k: int) -> float:
    """Mean over all values after removing the ``k`` largest (denominator n - k)."""
    s = sorted(values, reverse=True)[k:]
    return sum(s) / len(s) if s else float("nan")


def ruin_gate(
    worst: float, mean: float, ci_upper: float | None, *, worst_limit: float = -0.08, mean_limit: float = 0.005
) -> dict[str, bool]:
    """Ruin criterion (д) of 2026-10-15-weekly-straddle-vrp.

    ``strict``: worst week < -8 % of spot and the upper 95 % CI bound of the mean < +0.5 % (how the report applied it);
    ``mechanical``: worst week < -8 % and the point mean < +0.5 % (the baseline script's form).
    """
    bad = worst < worst_limit
    return {
        "strict": bool(bad and ci_upper is not None and ci_upper < mean_limit),
        "mechanical": bool(bad and mean < mean_limit),
    }


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative sum (positive number, same units as the values)."""
    peak = cum = dd = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd
