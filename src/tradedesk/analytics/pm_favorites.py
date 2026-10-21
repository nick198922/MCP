"""Pure analytics for the Polymarket favourites study (IDEA-20261021-polymarket-favorites-fresh-quotes).

Research support for scripts/verify_pm_favorites.py. No I/O; every time is a unix-seconds parameter. Prices and
probabilities are decimals (0.93 = 93 %), returns are fractions of capital (entry + fee).

Conventions (pre-registered in the idea card)
* ``q`` is the YES quote at ``t0`` (``/prices-history`` snapshot, see ``pm_threshold.quote_at``). In neg-risk events
  ``q`` is first normalised over every option open at ``t0`` (``q / sum q``, overround removed).
* The favourite side is YES if ``q >= 0.5`` else NO; its price ``p_f = max(q, 1 - q)`` and its outcome
  ``y_f = y`` (YES) or ``1 - y`` (NO).
* Freshness: the last print of the market (either outcome token, as a favourite-equivalent price) at or before ``t0``
  is at most ``stale_s`` old. The strict variant counts only prints on the favourite token itself.
* Clusters are events; statistics are equal-weight means of event means.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tradedesk.analytics.pm_calibration import devig_event, no_return
from tradedesk.analytics.pm_threshold import last_trade_before, quote_at

FAV_BASKET = (0.88, 0.97)
CONTROL_BASKET = (0.60, 0.80)
PAYOUT_BASKET = (0.97, 1.00)


def favourite(q: float) -> tuple[str, float]:
    """("yes", q) if q >= 0.5 else ("no", 1 - q)."""
    return ("yes", q) if q >= 0.5 else ("no", 1.0 - q)


def in_basket(p: float, band: tuple[float, float]) -> bool:
    """Closed interval [lo; hi]."""
    return band[0] <= p <= band[1]


def fav_outcome(side: str, outcome_yes: int) -> int:
    return int(outcome_yes) if side == "yes" else 1 - int(outcome_yes)


def normalised_quotes(open_quotes: Mapping[str, float | None]) -> dict[str, float] | None:
    """q / sum q over every option of a neg-risk event open at t0; None if any quote is missing or fewer than 2."""
    if len(open_quotes) < 2 or any(v is None for v in open_quotes.values()):
        return None
    return devig_event({k: float(v) for k, v in open_quotes.items() if v is not None})


def last_print(trades: Iterable[dict[str, Any]], t0: int, yes_token: str, side: str, fav_token: str | None = None) -> dict[str, Any] | None:
    """Last print with ``timestamp <= t0`` as a favourite-equivalent price.

    ``fav_token`` set -> only prints on that token count (strict variant). Returns {"t", "age", "px"} or None.
    """
    rows = list(trades)
    if fav_token is not None:
        rows = [r for r in rows if str(r.get("asset")) == fav_token]
    lt = last_trade_before(rows, t0, yes_token)
    if lt is None:
        return None
    return {"t": lt["t"], "age": lt["age"], "px": lt["yes_px"] if side == "yes" else lt["no_px"]}


def state_at(
    points: Sequence[dict[str, Any]],
    trades: Iterable[dict[str, Any]],
    t0: int,
    *,
    yes_token: str,
    no_token: str,
    q_override: float | None = None,
    stale_s: int = 7200,
) -> dict[str, Any] | None:
    """Everything the strategy may know at ``t0`` for one market (no data after ``t0`` is used).

    ``q_override`` replaces the raw quote (neg-risk normalised q). None when there is no quote at ``t0``.
    """
    qa = quote_at(points, t0)
    if qa is None:
        return None
    q = qa[0] if q_override is None else q_override
    side, p_f = favourite(q)
    trades = list(trades)
    lp = last_print(trades, t0, yes_token, side)
    lp_strict = last_print(trades, t0, yes_token, side, fav_token=yes_token if side == "yes" else no_token)
    return {
        "q_raw": qa[0],
        "q": q,
        "side": side,
        "p_f": p_f,
        "last_px": lp["px"] if lp else None,
        "fresh": lp is not None and lp["age"] <= stale_s,
        "fresh_strict": lp_strict is not None and lp_strict["age"] <= stale_s,
    }


def fav_return(
    p_f: float,
    y_f: int,
    *,
    half_spread: float = 0.01,
    last_fav_px: float | None = None,
    fee_rate: float = 0.0,
    fee_exp: float = 1.0,
) -> dict[str, float]:
    """Taker purchase of the favourite at t0 held to resolution, return on capital (entry + fee).

    entry = max(p_f + half_spread, last favourite-equivalent print), fee = ``fee_rate * (entry (1 - entry))^fee_exp``
    per share (upper bound of ``feeSchedule``). Same arithmetic as :func:`pm_calibration.no_return` on the
    complementary quote.
    """
    return no_return(1.0 - p_f, 1 - int(y_f), half_spread=half_spread, last_no_px=last_fav_px, fee_rate=fee_rate, fee_exp=fee_exp)


def awaiting_payout(anchor: str, T: int, closed: int) -> bool:
    """Descriptive flag: a non-sports market whose ``closedTime`` came before its scheduled end (resolved early, so at
    t0 its price may be a wait for the UMA payout rather than a probability). Uses ``closedTime``: never a filter."""
    return anchor == "end_date" and closed < T


def price_mde(groups: Iterable[Sequence[float]], *, k: float = 2.8, within: str = "full") -> dict[str, float]:
    """MDE of the equal-weight mean of event means of ``p_f - y_f`` from prices only (outcomes not used).

    Under calibration ``Var(y_i) = p_i (1 - p_i)``. Within an event the market terms are either perfectly correlated
    (``within="full"``, upper bound: sd of the event mean = mean sd_i) or independent (``"indep"``: sum var_i / k_e^2).
    SE = sqrt(sum_e V_e) / n_events; MDE = k * SE (alpha 5 % two-sided, power 80 % at k = 2.8).
    Returns {"n", "se", "sd_eff", "mde"} with ``sd_eff = sqrt(mean V_e)`` (so ``mde = k sd_eff / sqrt(n)``).
    """
    if within not in ("full", "indep"):
        raise ValueError("within must be 'full' or 'indep'")
    vs = []
    for ps in groups:
        sds = [math.sqrt(max(p * (1.0 - p), 0.0)) for p in ps]
        if not sds:
            continue
        if within == "full":
            vs.append((sum(sds) / len(sds)) ** 2)
        else:
            vs.append(sum(s * s for s in sds) / len(sds) ** 2)
    n = len(vs)
    if n == 0:
        return {"n": 0, "se": math.inf, "sd_eff": math.nan, "mde": math.inf}
    se = math.sqrt(sum(vs)) / n
    return {"n": n, "se": se, "sd_eff": math.sqrt(sum(vs) / n), "mde": k * se}


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> dict[str, float]:
    """Cochran's Q and I^2 for group means with standard errors (inverse-variance weights); groups with se <= 0 skipped."""
    pairs = [(m, s) for m, s in zip(means, ses, strict=True) if s and s > 0 and math.isfinite(s)]
    if len(pairs) < 2:
        return {"Q": math.nan, "df": 0, "I2": math.nan, "pooled": math.nan}
    w = [1.0 / (s * s) for _, s in pairs]
    pooled = sum(wi * m for wi, (m, _) in zip(w, pairs, strict=True)) / sum(w)
    Q = sum(wi * (m - pooled) ** 2 for wi, (m, _) in zip(w, pairs, strict=True))
    df = len(pairs) - 1
    i2 = max(0.0, (Q - df) / Q) if Q > 0 else 0.0
    return {"Q": Q, "df": df, "I2": i2, "pooled": pooled}


def worst_losing_streak(outcomes_in_time_order: Sequence[int]) -> int:
    """Longest run of consecutive zeros (lost favourites) in time order."""
    best = cur = 0
    for y in outcomes_in_time_order:
        cur = cur + 1 if int(y) == 0 else 0
        best = max(best, cur)
    return best
