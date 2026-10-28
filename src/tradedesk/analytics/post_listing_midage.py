"""Post-listing mid-age decay: short a young USDT-M perp from age 30 to age 90 days against a mature-alt basket.

Pure functions (no I/O). Time is passed in as UTC day indices (``ms // DAY_MS``); age 0 is the day of the perp's
first traded daily bar. Returns are decimals on notional (0.05 = 5 %). Reuses the bar, leg and basket helpers of
``listing_drift``. Idea card: docs/ideas/IDEA-20261025-post-listing-midage-decay-short.md.

Step 0 (power gate) deliberately exposes no mean of the outcome: :func:`power_gate` returns n, clusters, SD,
ICC, design effect, n_eff and the MDE only.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tradedesk.analytics.listing_drift import (
    DailyBars,
    basket_return,
    cr1_se,
    design_effect,
    icc_anova,
    liq_threshold,
    mde,
    short_leg,
)

ENTRY_AGE = 30
HOLD_DAYS = 60
PLACEBO_AGE = 150
MIN_TURNOVER = 5_000_000.0
GATE_N = 150
GATE_MDE = 0.06


def window_days(first_day: int, *, entry_age: int = ENTRY_AGE, hold: int = HOLD_DAYS) -> tuple[int, int]:
    """(entry day, planned exit day): open of age ``entry_age`` to close of age ``entry_age + hold - 1``."""
    e = first_day + entry_age
    return e, e + hold - 1


def median_turnover(qv: Mapping[int, float], entry: int, *, window: int = 30, min_days: int = 20) -> float | None:
    """Median daily quote volume (USD) over the ``window`` days strictly before ``entry``; None if too few days."""
    vals = [qv[d] for d in range(entry - window, entry) if d in qv]
    if len(vals) < min_days:
        return None
    return statistics.median(vals)


@dataclass(frozen=True)
class RelativeLeg:
    entry: int
    exit: int
    short_pnl: float  # short price P&L on notional (floored by the liquidation rule)
    basket_ret: float  # long basket return open(entry) -> close(exit)
    rel: float  # short_pnl + basket_ret: hedged price P&L on the short's notional, before funding and costs
    liquidated: bool
    truncated: bool
    mae: float


def relative_short(
    coin: DailyBars,
    members: Sequence[str],
    bars: Mapping[str, DailyBars],
    entry: int,
    hold: int,
    *,
    leverage: float = 1.0,
) -> RelativeLeg | None:
    """Short ``coin`` from open(entry) for ``hold`` days, long the equal-weight basket of ``members`` on the same
    notional until the same exit day (the liquidation day if the short is liquidated).

    Liquidation: an adverse move of ``liq_threshold(leverage)`` closes the short with a loss of ``1 / leverage``
    of notional (all the margin). Uses only bars in [entry; entry + hold - 1]."""
    leg = short_leg(coin, entry, hold, liq_move=liq_threshold(leverage), liq_loss=1.0 / leverage)
    if leg is None:
        return None
    rb = basket_return(members, bars, entry, leg.exit)
    if rb is None:
        return None
    return RelativeLeg(entry, leg.exit, leg.pnl, rb, leg.pnl + rb, leg.liquidated, leg.truncated, leg.mae)


@dataclass(frozen=True)
class Gate:
    n: int
    clusters: int
    sd: float
    icc: float
    deff: float
    n_eff: float
    mde_icc: float  # z * sd / sqrt(n_eff)
    mde_cr1: float  # z * cluster-robust SE of the mean
    n_min: int
    mde_max: float

    @property
    def passed(self) -> bool:
        """n >= n_min and both MDE estimates <= mde_max (conservative when they disagree)."""
        return self.n >= self.n_min and self.mde_icc <= self.mde_max and self.mde_cr1 <= self.mde_max


def power_gate(
    values: Sequence[float], clusters: Sequence[object], *, n_min: int = GATE_N, mde_max: float = GATE_MDE, z: float = 2.8
) -> Gate:
    """Power gate without the mean: SD, ANOVA ICC over ``clusters``, design effect, n_eff = n / deff, MDE."""
    n = len(values)
    if n < 2:
        return Gate(n, len(set(clusters)), float("nan"), 0.0, 1.0, float(n), math.inf, math.inf, n_min, mde_max)
    sd = statistics.stdev(values)
    icc = icc_anova(values, clusters)
    deff = design_effect(clusters, icc)
    se = cr1_se(values, clusters)
    return Gate(
        n=n,
        clusters=len(set(clusters)),
        sd=sd,
        icc=icc,
        deff=deff,
        n_eff=n / deff,
        mde_icc=mde(sd, n, deff, z=z),
        mde_cr1=z * se if math.isfinite(se) else math.inf,
        n_min=n_min,
        mde_max=mde_max,
    )
