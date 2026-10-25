"""Pure analytics for the Polymarket long-dated tail-NO study (IDEA-20261025-pm-longdated-tail-no).

Runner: scripts/verify_pm_longdated_tail_no.py. No I/O; every time is a unix-seconds parameter; prices and
probabilities are decimals (0.10 = 10 %).

Design conditions of the critic's triage (binding):
* ``t0 = T_sched - h`` with ``T_sched`` the scheduled ``endDate`` (never the actual close). A market that was still
  open at ``t0`` stays in the sample whatever happened after ``t0`` (early YES resolutions included).
* ``q`` is the last trade print (YES-equivalent) at or before ``t0`` and at most ``max_age`` old, never a quote of an
  empty book.
* Step 0 (gates) never reads outcomes: the minimum detectable effect comes from outcomes simulated under the null
  (``y ~ Bernoulli(q)``, one winner per negative-risk event), with an intra-cluster correlation sensitivity.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

from tradedesk.analytics.pm_threshold import last_trade_before

DAY_S = 86_400
TAIL = (0.03, 0.30)
TAIL_BUCKETS: tuple[tuple[float, float], ...] = ((0.03, 0.08), (0.08, 0.15), (0.15, 0.30))
EXCLUDED_CATEGORIES = frozenset({"sports", "esports"})


def t0_sched(T_sched: int, days: int) -> int:
    """Entry time ``days`` before the scheduled end (``T_sched`` = Gamma ``endDate``)."""
    return int(T_sched) - int(days) * DAY_S


def long_lived(m: dict[str, Any], min_days: int = 60) -> bool:
    """Scheduled life ``T_sched - start`` of at least ``min_days`` days (both known at listing)."""
    return int(m["T"]) - int(m["start"]) >= min_days * DAY_S


def eligible_long(m: dict[str, Any], t0: int, *, listed_lead_days: int = 7) -> bool:
    """Known at ``t0``: accepting orders since ``t0 - listed_lead_days`` and still open at ``t0`` (``closed > t0``).

    The actual close time only decides whether the market was open at ``t0``; it never moves ``t0``.
    """
    return int(m["start"]) <= t0 - listed_lead_days * DAY_S and int(m["closed"]) > t0


def in_tail(q: float | None, lo: float = TAIL[0], hi: float = TAIL[1]) -> bool:
    return q is not None and lo <= q <= hi


def tail_bucket(q: float) -> str | None:
    """Label of the pre-registered q bucket ([a, b), last bucket closed) or None outside the tail."""
    for i, (a, b) in enumerate(TAIL_BUCKETS):
        if a <= q < b or (i == len(TAIL_BUCKETS) - 1 and q == b):
            return f"{a:.2f}-{b:.2f}"
    return None


def fresh_trade_q(trades: Iterable[dict[str, Any]], t0: int, yes_token: str, *, max_age_days: float = 7.0) -> dict[str, Any] | None:
    """Last print at or before ``t0`` (YES-equivalent price) if it is at most ``max_age_days`` old, else None.

    Prints after ``t0`` are ignored, so the result cannot change when later trades change.
    """
    lt = last_trade_before(trades, t0, yes_token)
    if lt is None or lt["age"] > max_age_days * DAY_S:
        return None
    return lt


def month_key(ts: int) -> str:
    """UTC "YYYY-MM" of a unix time (cluster = month of the scheduled end)."""
    d = np.datetime64(int(ts), "s").astype("datetime64[M]")
    return str(d)


# ----------------------------------------------------------------------------- step 0: power without outcomes


def simulate_null_outcomes(event_qs: dict[str, float], neg_risk: bool, rng: np.random.Generator) -> dict[str, int]:
    """Outcomes drawn under the null "prices are calibrated".

    Binary / independent markets: ``y_i ~ Bernoulli(q_i)``. Negative-risk event: at most one winner, option ``i`` wins
    with probability ``q_i / max(1, sum q)`` (no winner among the passed options with the remaining mass).
    """
    keys = sorted(event_qs)
    q = np.asarray([min(max(event_qs[k], 0.0), 1.0) for k in keys], dtype=float)
    if not neg_risk:
        y = (rng.random(len(q)) < q).astype(int)
        return dict(zip(keys, y.tolist(), strict=True))
    p = q / max(1.0, float(q.sum()))
    u = float(rng.random())
    c = np.cumsum(p)
    win = int(np.searchsorted(c, u, side="right"))
    return {k: int(i == win) for i, k in enumerate(keys)}


def null_power(
    events: Sequence[dict[str, Any]],
    *,
    n_sim: int = 400,
    seed: int = 7,
) -> dict[str, Any]:
    """Sampling SD of the event-level tail gap estimator under the null, plain and with month clusters resampled.

    ``events``: dicts with ``event_id``, ``month`` (cluster), ``neg_risk``, ``qs`` (market -> q of every option used for
    the winner draw) and ``tail`` (markets that enter the gap). The estimator is the equal-weight mean over events of the
    mean over tail markets of ``q - y``. Returns the per-event SD of the gap under the null, the SD of the estimator over
    simulations (``se_sim``), the cluster-bootstrap SE averaged over simulations (``se_cluster``) and
    ``n_eff = sd_event**2 / se**2`` for both. Outcomes are simulated, never read.
    """
    rng = np.random.default_rng(seed)
    evs = [e for e in events if e["tail"]]
    n = len(evs)
    if n < 2:
        return {"n_events": n, "sd_event": None, "se_sim": None, "se_cluster": None, "n_eff_sim": None, "n_eff_cluster": None}
    months = sorted({e["month"] for e in evs})
    mi = {m: i for i, m in enumerate(months)}
    cl = np.asarray([mi[e["month"]] for e in evs])
    k = len(months)
    ests = np.empty(n_sim)
    sds = np.empty(n_sim)
    se_cl = np.empty(n_sim)
    for s in range(n_sim):
        g = np.empty(n)
        for j, e in enumerate(evs):
            y = simulate_null_outcomes(e["qs"], bool(e["neg_risk"]), rng)
            g[j] = float(np.mean([e["qs"][m] - y[m] for m in e["tail"]]))
        ests[s] = g.mean()
        sds[s] = g.std(ddof=1)
        S = np.bincount(cl, weights=g, minlength=k)
        N = np.bincount(cl, minlength=k).astype(float)
        idx = rng.integers(0, k, size=(200, k))
        se_cl[s] = float((S[idx].sum(axis=1) / N[idx].sum(axis=1)).std(ddof=1)) if k > 1 else float("nan")
    sd_event = float(sds.mean())
    se_sim = float(ests.std(ddof=1))
    se_cluster = float(np.nanmean(se_cl))
    return {
        "n_events": n,
        "n_clusters": k,
        "sd_event": sd_event,
        "se_sim": se_sim,
        "se_cluster": se_cluster,
        "n_eff_sim": sd_event**2 / se_sim**2 if se_sim > 0 else None,
        "n_eff_cluster": sd_event**2 / se_cluster**2 if se_cluster > 0 else None,
    }


def design_effect(cluster_sizes: Sequence[int], rho: float) -> float:
    """Kish design effect ``1 + (m_w - 1) * rho`` with the size-weighted mean cluster size ``m_w = sum m^2 / sum m``."""
    m = np.asarray([x for x in cluster_sizes if x > 0], dtype=float)
    if m.size == 0:
        return 1.0
    mw = float((m**2).sum() / m.sum())
    return 1.0 + (mw - 1.0) * rho


def mde_from_se(se: float, k: float = 2.8) -> float:
    """Minimum detectable effect (alpha 5 % two-sided, power 80 %) from a standard error."""
    return k * se


def cluster_counts(rows: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Distinct units per cluster from (cluster, unit) pairs."""
    acc: dict[str, set[str]] = defaultdict(set)
    for c, u in rows:
        acc[c].add(u)
    return {c: len(v) for c, v in sorted(acc.items())}


# ----------------------------------------------------------------------------- step 1 helpers


def hold_days(t0: int, closed: int) -> float:
    """Actual holding period in days from entry ``t0`` to the actual close (early NO resolutions shorten it)."""
    return max(0.0, (int(closed) - int(t0)) / DAY_S)
