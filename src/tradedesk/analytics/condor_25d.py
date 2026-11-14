"""Playbook rule P12 on history: iron condor with 25-delta short legs (IDEA-20261114-options-playbook, item 4.3).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). The construction and the P&L are those of
`condor_dvol` (``plan_p1(short_delta=0.25)``, ``entry_credit``, ``settle``, ``managed``: Bybit fees, marks shifted by an
assumed half-spread, R = P&L / max loss). This module adds what the P1' lesson requires of a short-premium test:

* calendar-block clusters for an unconditional weekly series (overlapping ~13-day condors);
* a tail gate: a sample with too few losing trades, or whose losses sit in so few clusters that a cluster-bootstrap
  resample often contains none, cannot carry a positive verdict (its bootstrap does not see the tail);
* a one-sided Clopper-Pearson upper bound on the loss frequency and a parametric stress mean
  ``(1 - q) * mean_win + q * mean_loss`` that replaces the observed loss frequency by that bound;
* the registered verdict rule.

Units: R is a decimal fraction of the max loss (0.10 = 10 % of capital at risk); probabilities are decimals.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from .trend_spread import DAY_MS

WEEK_MS = 7 * DAY_MS


def block_clusters(days: Sequence[int], *, start_ms: int, weeks: int = 4) -> dict[int, int]:
    """Entry day -> index of its calendar block of ``weeks`` weeks counted from ``start_ms``.

    The block of a day depends only on that day and ``start_ms``, so adding later entries never changes earlier ones.
    """
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    return {d: (d - start_ms) // (weeks * WEEK_MS) for d in days}


def p_no_loss_resample(n_loss_clusters: int, n_clusters: int) -> float:
    """Probability that a cluster-bootstrap resample (``n_clusters`` draws with replacement) holds no losing cluster."""
    if n_clusters <= 0:
        return 1.0
    return (1.0 - n_loss_clusters / n_clusters) ** n_clusters


def tail_gate(values: Sequence[float], clusters: Sequence[Any], *, min_losses: int = 5, max_p_no_loss: float = 0.05) -> dict[str, Any]:
    """Is the loss tail observed well enough for a positive verdict?

    A loss is R <= 0. ``ok`` needs at least ``min_losses`` losing trades and a probability <= ``max_p_no_loss`` that a
    cluster-bootstrap resample contains no losing cluster (else the bootstrap tail is degenerate).
    """
    loss_cl = {c for v, c in zip(values, clusters, strict=True) if v <= 0}
    n_loss = sum(1 for v in values if v <= 0)
    k = len(set(clusters))
    p0 = p_no_loss_resample(len(loss_cl), k)
    return {
        "n": len(values),
        "n_loss": n_loss,
        "n_loss_clusters": len(loss_cl),
        "n_clusters": k,
        "p_no_loss_resample": p0,
        "ok": n_loss >= min_losses and p0 <= max_p_no_loss,
    }


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    return min(
        1.0, sum(math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq) for i in range(k + 1))
    )


def clopper_pearson_upper(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided (1 - alpha) Clopper-Pearson upper bound of a binomial proportion with k successes in n trials."""
    if n <= 0:
        return 1.0
    if not 0 <= k <= n:
        raise ValueError("need 0 <= k <= n")
    if k == n:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def stress_mean(win_mean: float, loss_mean: float, q: float) -> float:
    """Mean R if a fraction ``q`` of trades lose ``loss_mean`` (negative) and the rest win ``win_mean``."""
    return (1.0 - q) * win_mean + q * loss_mean


def parametric_tail(
    values: Sequence[float],
    *,
    k_eff: int,
    n_eff: int,
    loss_ref: Sequence[float] | None = None,
    min_losses: int = 5,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Stress the loss frequency to its Clopper-Pearson upper bound on ``k_eff`` losses in ``n_eff`` independent trades.

    Loss size: the mean of the sample's losses (R <= 0) when there are at least ``min_losses`` of them (or no
    ``loss_ref``); else the mean of ``loss_ref`` (a reference sample's losing R values, e.g. the unconditional condor).
    Returns the observed and stressed means.
    """
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    own = len(losses) >= min_losses or not loss_ref
    ref = losses if own else [v for v in (loss_ref or []) if v <= 0]
    win_mean = sum(wins) / len(wins) if wins else 0.0
    loss_mean = sum(ref) / len(ref) if ref else float("nan")
    q_up = clopper_pearson_upper(k_eff, n_eff, alpha)
    return {
        "q_obs": len(losses) / len(values) if values else float("nan"),
        "q_up": q_up,
        "win_mean": win_mean,
        "loss_mean": loss_mean,
        "loss_from": "sample" if own else "reference",
        "stress_mean": stress_mean(win_mean, loss_mean, q_up),
    }


def verdict(
    *,
    mean: float,
    lo: float,
    hi: float,
    holm_p: float,
    is_mean: float,
    oos_mean: float,
    stress_cost_mean: float,
    edge: float,
    tail_ok: bool,
) -> str:
    """Registered verdict (card 4.3): ✓ / ≈ / ✗ / ◯ for one hypothesis.

    ✓: lo > 0, Holm p < 0.05, OOS mean > 0, mean under the cost stress > 0; ≈: mean, IS and OOS means > 0 but the CI
    holds 0; ✗: hi < 0, or mean <= 0 and hi < ``edge``; ◯ otherwise. Tail rule: a ✓ or ≈ whose tail gate fails is ◯.
    """
    if lo > 0 and holm_p < 0.05 and oos_mean > 0 and stress_cost_mean > 0:
        v = "✓"
    elif hi < 0 or (mean <= 0 and hi < edge):
        v = "✗"
    elif mean > 0 and is_mean > 0 and oos_mean > 0:
        v = "≈"
    else:
        v = "◯"
    if v in ("✓", "≈") and not tail_ok:
        return "◯"
    return v
