"""Paper trades of the assistant's option recommendations (IDEA-20261018-assistant, "Журнал").

Pure functions, no I/O. A recommendation is a structure of option legs on one Deribit expiry. It is booked as a
paper trade at the mark +- 1 vol pt (`option_replay.build_legs`), sized from the owner's deposit and risk
percentage, and settled at the expiry delivery price.

Units: prices and P&L in USD for 1 BTC of underlying per leg unit (``qty = +-1``); vols decimals; Black-76 with
r = 0 on the entry forward; T in years = days / 365. Deribit option fees: 0.03 % of the underlying per leg
(capped at 12.5 % of the option price) on entry, 0.015 % (same cap, of the payoff) on delivery of an in-the-money leg.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from .options import black76_price

Leg = tuple[float, bool, float, float]  # (strike USD, is_call, qty, exec_iv)

OPTION_FEE = 0.0003
DELIVERY_FEE = 0.00015
FEE_CAP = 0.125
STRESS_UP = 0.30  # undefined-risk structures: loss is measured at forward * (1 + STRESS_UP)


def leg_price(forward: float, leg: Leg, t_years: float) -> float:
    """Black-76 price of one unit of the leg in USD (positive, regardless of the sign of qty)."""
    k, is_call, _, iv = leg
    return black76_price(forward, k, t_years, iv, is_call)


def payoff(legs: Sequence[Leg], s: float) -> float:
    """Expiry payoff in USD (signed: sold legs subtract) when the underlying settles at ``s``."""
    return sum(q * (max(s - k, 0.0) if c else max(k - s, 0.0)) for k, c, q, _ in legs)


def entry_book(legs: Sequence[Leg], forward: float, spot: float, t_years: float) -> dict[str, Any]:
    """Premium (debit > 0), entry fees and per-leg entry prices for 1 unit of the structure."""
    prices = [leg_price(forward, lg, t_years) for lg in legs]
    premium = sum(lg[2] * p for lg, p in zip(legs, prices, strict=True))
    fees = sum(min(OPTION_FEE * spot, FEE_CAP * p) * abs(lg[2]) for lg, p in zip(legs, prices, strict=True))
    return {"leg_prices": prices, "premium": premium, "entry_fees": fees}


def delivery_fees(legs: Sequence[Leg], s: float) -> float:
    """Deribit delivery fee per unit of the structure when it settles at ``s`` (in-the-money legs only)."""
    total = 0.0
    for k, c, q, _ in legs:
        p = max(s - k, 0.0) if c else max(k - s, 0.0)
        if p > 0:
            total += min(DELIVERY_FEE * s, FEE_CAP * p) * abs(q)
    return total


def max_loss(legs: Sequence[Leg], premium: float, entry_fees: float, forward: float) -> tuple[float, str]:
    """Worst loss per unit (>= 0) and its basis, entry and delivery fees included.

    The payoff is piecewise linear, so for a structure whose upside is bounded the worst loss sits at 0 or at a
    strike ("max_loss"). A net short-call structure has unbounded loss; it is measured at forward * 1.3 as the
    larger of that and every strike point ("stress_up_30pct"), which is a stress, not a bound.
    """
    pts = [0.0, *sorted({lg[0] for lg in legs})]
    net_calls = sum(q for _, c, q, _ in legs if c)
    basis = "max_loss"
    if net_calls < -1e-12:
        pts.append(forward * (1.0 + STRESS_UP))
        basis = "stress_up_30pct"
    worst = max(-(payoff(legs, s) - premium - entry_fees - delivery_fees(legs, s)) for s in pts)
    return max(worst, 0.0), basis


def size_for_risk(risk_usd: float, loss_per_btc: float, lot: float = 0.1) -> float:
    """Largest size in BTC (multiple of ``lot``) whose worst loss fits ``risk_usd``; 0.0 when even one lot does not."""
    if loss_per_btc <= 0 or lot <= 0:
        return 0.0
    return math.floor(risk_usd / loss_per_btc / lot + 1e-9) * lot


def open_paper(
    legs: Sequence[Leg],
    *,
    forward: float,
    spot: float,
    t_years: float,
    deposit_usd: float,
    risk_pct: float,
    size_btc: float | None = None,
    lot: float = 0.1,
) -> dict[str, Any]:
    """Entry record of a paper trade. ``risk_pct`` is in percent (1.0 = 1 % of the deposit).

    Size = the largest lot multiple whose worst loss fits ``deposit * risk_pct / 100`` unless ``size_btc`` is
    given. When one lot already exceeds the risk budget the size is one lot and ``within_risk`` is False.
    """
    book = entry_book(legs, forward, spot, t_years)
    loss, basis = max_loss(legs, book["premium"], book["entry_fees"], forward)
    risk_usd = deposit_usd * risk_pct / 100.0
    max_size = size_for_risk(risk_usd, loss, lot)
    size = size_btc if size_btc is not None else (max_size if max_size > 0 else lot)
    return {
        **book,
        "max_loss_per_btc": loss,
        "risk_basis": basis,
        "risk_budget_usd": risk_usd,
        "max_size_btc": max_size,
        "size_btc": size,
        "risk_usd": loss * size,
        "within_risk": loss * size <= risk_usd + 1e-9,
    }


def settle_paper(trade: dict[str, Any], delivery_price: float) -> dict[str, Any]:
    """Settle an opened paper trade at the delivery price.

    ``trade`` needs ``legs`` (list of ``[strike, is_call, qty, exec_iv, entry_price]``), ``premium_per_btc``,
    ``entry_fees_per_btc``, ``size_btc``, ``risk_usd`` and ``spot``; optional ``method_q05_pct`` (the method's 5 %
    quantile of the P&L as a fraction of the entry forward, exactly as `option_replay.method_summary` returns it). Returns the USD P&L for the trade size, the P&L per 1 BTC of
    structure as a fraction of the entry spot (``pnl_pct_spot``, the main metric, same as in the 2026-10-18 study),
    ``r_worst_case`` = P&L / the trade's worst-loss risk in USD (NOT the study's R) and ``r_study`` = P&L per unit /
    the study's risk: the premium for a debit, |q05| * spot for a credit (spot stands in for the forward, as in the study; None when it is a credit and q05 unknown).
    """
    legs: list[Leg] = [(float(k), bool(c), float(q), float(iv)) for k, c, q, iv, *_ in trade["legs"]]
    gross = payoff(legs, delivery_price)
    dfees = delivery_fees(legs, delivery_price)
    per_btc = gross - trade["premium_per_btc"] - trade["entry_fees_per_btc"] - dfees
    size = float(trade["size_btc"])
    pnl = per_btc * size
    risk = float(trade["risk_usd"])
    premium = float(trade["premium_per_btc"])
    q05 = trade.get("method_q05_pct")
    if premium > 0:
        study_risk: float | None = premium
    elif q05 is not None:
        study_risk = abs(float(q05)) * float(trade["spot"])
    else:
        study_risk = None
    return {
        "payoff_per_btc": gross,
        "delivery_fees_per_btc": dfees,
        "pnl_per_btc": per_btc,
        "pnl_usd": pnl,
        "pnl_pct_spot": per_btc / float(trade["spot"]),
        "r_worst_case": pnl / risk if risk > 0 else None,
        "r_study": per_btc / study_risk if study_risk and study_risk > 0 else None,
    }


def comparable_groups(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The groups `assistant_paper_status` reports apart. 'method' = the collector's chain trades (``trigger`` None or
    'chain'), the only ones comparable with the H1 walk-forward; 'method_triggered' = extra method trades on a regime
    change or an event (they overlap the chain); 'manual' = picked by a person or a model, not a test of the method."""
    meth = [x for x in rows if x.get("source") == "method"]
    return {
        "method": [x for x in meth if x.get("trigger") in (None, "chain")],
        "method_triggered": [x for x in meth if x.get("trigger") not in (None, "chain")],
        "manual": [x for x in rows if x.get("source") == "manual"],
    }


# --------------------------------------------------------------------------- paper journal vs the historical simulation
#
# Rules fixed on 2026-11-25, before any comparable trade had settled (chain trades settled: 0). They are not tuned to
# the triggered or manual rows, which never enter the statistics. Revised the same day after the critic's review
# (docs/reviews/2026-11-25-assistant-paper-vs-sim.md), still with 0 comparable trades settled.
#
# 1. Comparable rows: source 'method' with trigger NULL or 'chain', status 'settled'. Triggered method trades, manual
#    trades and rows of any other source ('other') are counted (settled / open), never mixed in.
# 2. Metric: P&L per 1 BTC of structure as a fraction of the entry spot (`pnl_pct_spot`, decimal), as in the study.
# 3. (a) The method's own simulation at booking: `method_mean_pct` / `method_q05_pct` are fractions of the entry
#    FORWARD (option_replay.method_summary over every past window of the same length in the same RV30 bucket); they
#    are put on the spot scale as value * forward / spot. Per trade: actual - expected, and whether actual fell below
#    the booked 5 % quantile (nominal share 5 %). The simulation charges its own fee, 0.03 % of F per leg on entry AND
#    exit with no cap (rec_method.FEE_LEG), the paper Deribit's (0.03 % of spot capped at 12.5 % of the price on entry,
#    0.015 % on delivery of in-the-money legs only), so `expected` is low by ~0.06 % of spot for a two-leg structure.
#    The quantile check needs a quantile the trade can fall below. With a bounded loss (risk_basis 'max_loss') the
#    trade's P&L never goes under -max_loss_per_btc / spot (for bought options: premium + entry fees). The simulated
#    q05 is net of the whole premium and of the simulation's larger fees, so when more than 5 % of the past windows end
#    worthless (bought strangles) q05 sits BELOW that floor and "actual < q05" is impossible (live 25.11: method rows
#    1, 2, 4, 5 have q05 0.06 pt of spot under their floor). Such a trade gets `q05_at_floor` True, `below_q05` None,
#    and is counted in `n_q05_at_floor`, not in the share. A 'stress_up_30pct' row has no floor and stays testable; a
#    row whose floor cannot be read is counted in `missing_fields['max_loss']`.
#    (b) The study's walk-forward H1 (WALK_FORWARD_H1, numbers from the report itself): mean pnl % of spot, win share;
#    the study's R (P&L / premium for a debit, / |q05| * spot for a credit) is `r_study`, the same formula. It is NOT
#    the same experiment; each difference can move the mean:
#    - chain calendar: the study decided only on the expiry of the 40-day straddle, whatever the pick (put_skew.chain
#      on recs[d]["expiry"] in scripts/verify_assistant_recs.py), so after a 'no trade' or a 14-day pick it skipped
#      ~40 days; the paper chain re-enters as soon as its own trade has expired and re-decides daily while the method
#      picks nothing (rec_method.decide). The paper has more 14-day trades per unit of time and entry days conditioned
#      on a pick, so the structure mix differs (a 14-day 25-delta strangle costs ~3.4 % of spot, a 40-day straddle
#      ~9.6 % of F, report caveats). `by_horizon` shows ~14-day and ~40-day trades apart, cut at the method's two
#      expiry bands (held <= 21 days: the [7; 21] band; >= 28 days: [28; 56]), no new threshold;
#    - interval: the reference CI is a block bootstrap (blocks of 3), the paper's a Student t; on the study's own 27
#      trades the t upper bound is +2.35 % against the bootstrap +1.51 %. `mean_minus_reference` is a point
#      difference with NO interval of its own;
#    - period and regimes: 2021-04 .. 2026-10 against 2026-10 on;
#    - premium: Black-76 on the forward here, inverse marks at spot in the study (they differ by the basis, ~1 %).
#    Fees are NOT a difference here: the study's H1 used the same Deribit fees as the paper (vrp_marks.hedged_book).
# 4. A row lacking a needed field, or with a value beyond a sane bound (|P&L| > 10 x spot, |R| > 1000, prices
#    > 1e9 USD, timestamps outside year 1970..9999), is counted under `missing_fields` and left out of that statistic.
# 5. Below COMPARE_MIN_N rows in a statistic: status 'too_few' and only counts and the per-trade raw numbers; no
#    means, shares, intervals or verdicts. COMPARE_MIN_N = 28 is just more than the study's n = 27; from it on the
#    numbers are DESCRIPTIVE, not a conclusion. What 28 trades can detect (80 % power, two-sided 5 %: z = 1.96 + 0.84
#    = 2.80), with the SD taken from the report: its t-interval upper bound +2.35 % around the mean -0.59 % on n = 27
#    gives SD = (2.35 + 0.59) * sqrt(27) / 2.056 = 7.4 % of spot (a priori 11 %); its R interval [-0.63; +0.22] gives
#    SD = 0.425 * sqrt(27) / 2.056 = 1.07 R. Then:
#    - one mean against a fixed value (0, or the booked expectation, paired): 2.80 * SD / sqrt(28) = 3.9 % (SD 7.4)
#      to 5.8 % (SD 11) of spot, 0.57 R;
#    - the paper against H1, both samples uncertain: 2.80 * SD * sqrt(1/28 + 1/27) = 5.6 % to 8.3 % of spot, 0.81 R;
#    - the study's open question, an edge of about +0.2 R: n = (2.80 * 1.07 / 0.2)^2 = 225 chain trades, about 18
#      years at the study's mix (11 x 14 + 16 x 40 days over 27 trades = 29.4 days a trade): the journal will not
#      settle it in any reasonable time;
#    - the share below q05: with p = 5 % and 28 testable trades, up to 3 below is noise (P(X <= 3) = 0.95); 4 or more
#      shows a true share of 20 % with 84 % power, of 10 % with 31 %.
#    So at 28 only a gross gap shows (paper vs its simulation, or vs H1). Option P&L is heavily skewed (median -1 R in
#    the study), so a t-interval on fewer trades is not trusted either.
# 6. From COMPARE_MIN_N on: means, the share below q05 against the expected 5 % * n, and a Student-t 95 % interval on
#    the mean ONLY when no two comparable trades' holding periods (opened .. expiry) overlap and every holding period
#    is known; otherwise the interval is withheld with the reason. Even then trades share volatility regimes (serial
#    dependence) and the method's expectations come from the same overlapping history: descriptive, never an edge.
#    The study found the method's expectation uninformative (H3, Spearman +0.02) and the method picks its best-scoring
#    structure, so its expectation is biased upward by selection: a negative actual - expected is the expected shape,
#    and (a) checks the pipeline (booking, settlement, units), not the method.

COMPARE_MIN_N = 28
Q05_NOMINAL = 0.05
SHORT_MAX_DAYS = 21.0  # collector.assistant_paper.SHORT_DTE_BAND upper edge: a chain trade held <= 21 d is a ~14-day pick
LONG_MIN_DAYS = 28.0  # tools.assistant_paper.DTE_BAND lower edge: held >= 28 d is a ~40-day pick
LIM_PCT, LIM_R, LIM_USD, LIM_TS = 10.0, 1000.0, 1e9, 253_402_300_799.0  # sane bounds (rule 4)
WALK_FORWARD_H1: dict[str, Any] = {
    # docs/verification/2026-10-18-assistant-recommendations.md, H1: the method's picks on the non-overlapping decision
    # chain, 2021-04 .. 2026-10, real Deribit marks, unhedged; P&L % of entry spot per 1 BTC per leg; block bootstrap CI
    "n": 27,
    "mean_pnl_pct_spot": -0.0059,
    "ci95_mean_pnl_pct_spot": (-0.0243, 0.0151),
    "win_share": 0.30,
    "mean_r_study": -0.12,
    "ci95_mean_r_study": (-0.63, 0.22),
    "median_r_study": -1.0,
}


def _fin(x: Any, lim: float = math.inf) -> float | None:
    """A finite number with |x| <= ``lim``, else None (bool and text are not numbers)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) and abs(v) <= lim else None


def _ts(x: Any) -> float | None:
    v = _fin(x, LIM_TS)
    return v if v is not None and v >= 0 else None


def _horizon(opened: float | None, expiry: float | None) -> str | None:
    """'short_14d' / 'long_40d' by the holding period against the method's two expiry bands, else None."""
    if opened is None or expiry is None or expiry <= opened:
        return None
    days = (expiry - opened) / 86400
    return "short_14d" if days <= SHORT_MAX_DAYS else "long_40d" if days >= LONG_MIN_DAYS else None


def _t975(df: int) -> float:
    # smart_money.t975, inlined so the panel's stdlib copy can mirror it literally
    table = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131)
    table += (2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)
    return math.inf if df < 1 else table[df - 1] if df <= 30 else 1.96


def _mean_ci(xs: list[float], independent: bool) -> tuple[float, float, list[float] | None]:
    """Mean, sample SD and the t 95 % interval of the mean (None unless ``independent``)."""
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    h = _t975(n - 1) * sd / math.sqrt(n)
    return m, sd, ([m - h, m + h] if independent and n > 1 else None)


def _horizon_stats(ts: list[dict[str, Any]], min_n: int, independent: bool) -> dict[str, Any]:
    """Counts of one horizon's trades; from ``min_n`` on also its means (rule 5 holds per horizon)."""
    acts = [t["actual_pct_spot"] for t in ts]
    diffs = [t["actual_minus_expected"] for t in ts if t["actual_minus_expected"] is not None]
    rs = [t["r_study"] for t in ts if t["r_study"] is not None]
    out: dict[str, Any] = {
        "n": len(acts),
        "n_paired": len(diffs),
        "n_r_study": len(rs),
        "status": "too_few" if len(acts) < min_n else "descriptive",
    }
    if len(acts) >= min_n:
        m, _, ci = _mean_ci(acts, independent)
        out.update(mean_pnl_pct_spot=m, ci95_mean_pnl_pct_spot=ci, win_share=sum(1 for v in acts if v > 0) / len(acts))
    if len(diffs) >= min_n:
        out["mean_diff"] = sum(diffs) / len(diffs)
    if len(rs) >= min_n:
        out["mean_r_study"] = sum(rs) / len(rs)
    return out


def paper_vs_simulation(rows: Sequence[dict[str, Any]], *, min_n: int = COMPARE_MIN_N) -> dict[str, Any]:
    """Compare the settled comparable paper trades with (a) the method's own simulation at booking and (b) the
    2026-10-18 walk-forward H1, by the rules above. ``rows`` = all `rec_paper` rows (any status, any source).

    Units: every P&L number is a decimal fraction of the entry spot per 1 BTC of structure; R values are the study's R.
    """
    groups = comparable_groups(rows)
    parts = {
        "method_triggered": groups["method_triggered"],
        "manual": groups["manual"],
        "other": [x for x in rows if x.get("source") not in ("method", "manual")],
    }
    excluded = {
        k: {
            "settled": sum(1 for x in v if x.get("status") == "settled"),
            "open": sum(1 for x in v if x.get("status") == "open"),
        }
        for k, v in parts.items()
    }
    comp = [x for x in groups["method"] if x.get("status") == "settled"]
    missing = dict.fromkeys(
        ("pnl_pct_spot", "spot_forward", "method_mean_pct", "method_q05_pct", "max_loss", "r_study", "holding_period"), 0
    )
    trades: list[dict[str, Any]] = []
    spans: list[tuple[float, float]] = []
    for x in comp:
        actual = _fin(x.get("pnl_pct_spot"), LIM_PCT)
        if actual is None:
            missing["pnl_pct_spot"] += 1
            continue
        spot, fwd = _fin(x.get("spot"), LIM_USD), _fin(x.get("forward"), LIM_USD)
        conv = fwd / spot if spot is not None and fwd is not None and spot > 0 and fwd > 0 else None
        mean, q05 = _fin(x.get("method_mean_pct"), LIM_PCT), _fin(x.get("method_q05_pct"), LIM_PCT)
        r = _fin(x.get("r_study"), LIM_R)
        opened, expiry = _ts(x.get("opened_ts")), _ts(x.get("expiry_ts"))
        for key, v in (("spot_forward", conv), ("method_mean_pct", mean), ("method_q05_pct", q05), ("r_study", r)):
            if v is None:
                missing[key] += 1
        if opened is None or expiry is None or expiry <= opened:
            missing["holding_period"] += 1
        else:
            spans.append((opened, expiry))
        expected = mean * conv if mean is not None and conv is not None else None
        q05_spot = q05 * conv if q05 is not None and conv is not None else None
        # rule 3(a): the lowest P&L the trade can reach; a q05 at or under it cannot be undercut (not testable)
        floor: float | None = None
        at_floor: bool | None = None
        if q05_spot is not None and spot is not None:
            ml = _fin(x.get("max_loss_per_btc"), LIM_USD)
            if x.get("risk_basis") == "stress_up_30pct":
                at_floor = False  # unbounded loss: no floor
            elif x.get("risk_basis") == "max_loss" and ml is not None and ml >= 0:
                floor = -ml / spot
                at_floor = q05_spot <= floor
            else:
                missing["max_loss"] += 1
        trades.append(
            {
                "id": x.get("id"),
                "opened_ts": opened,
                "expiry_ts": expiry,
                "horizon": _horizon(opened, expiry),
                "structure": x.get("structure"),
                "actual_pct_spot": actual,
                "expected_pct_spot": expected,
                "actual_minus_expected": None if expected is None else actual - expected,
                "q05_pct_spot": q05_spot,
                "loss_floor_pct_spot": floor,
                "q05_at_floor": at_floor,
                "below_q05": None if q05_spot is None or at_floor is not False else actual < q05_spot,
                "r_study": r,
            }
        )
    overlaps = sum(1 for i, a in enumerate(spans) for b in spans[i + 1 :] if a[0] < b[1] and b[0] < a[1])
    independent = overlaps == 0 and missing["holding_period"] == 0
    withheld = (
        None
        if independent
        else f"{overlaps} overlapping holding period pair(s)"
        if overlaps
        else "a holding period is unknown, overlap cannot be ruled out"
    )

    paired = [t["actual_minus_expected"] for t in trades if t["actual_minus_expected"] is not None]
    with_q = [t["below_q05"] for t in trades if t["below_q05"] is not None]
    sim: dict[str, Any] = {
        "n": len(paired),
        "n_with_q05": len(with_q),
        "n_below_q05": sum(1 for b in with_q if b),
        "n_q05_at_floor": sum(1 for t in trades if t["q05_at_floor"]),
        "nominal_below_q05": Q05_NOMINAL,
        "status": "too_few" if len(paired) < min_n else "descriptive",
    }
    if len(paired) >= min_n:
        m, sd, ci = _mean_ci(paired, independent)
        sim.update(
            mean_actual=sum(t["actual_pct_spot"] for t in trades if t["actual_minus_expected"] is not None) / len(paired),
            mean_expected=sum(t["expected_pct_spot"] for t in trades if t["actual_minus_expected"] is not None) / len(paired),
            mean_diff=m,
            sd_diff=sd,
            ci95_mean_diff=ci,
        )
    if len(with_q) >= min_n:
        sim.update(share_below_q05=sim["n_below_q05"] / len(with_q), expected_below_q05=Q05_NOMINAL * len(with_q))

    acts = [t["actual_pct_spot"] for t in trades]
    rs = [t["r_study"] for t in trades if t["r_study"] is not None]
    wf: dict[str, Any] = {
        "reference": {k: list(v) if isinstance(v, tuple) else v for k, v in WALK_FORWARD_H1.items()},
        "n": len(acts),
        "n_r_study": len(rs),
        "status": "too_few" if len(acts) < min_n else "descriptive",
    }
    if len(acts) >= min_n:
        m, sd, ci = _mean_ci(acts, independent)
        wf.update(
            mean_pnl_pct_spot=m,
            sd_pnl_pct_spot=sd,
            ci95_mean_pnl_pct_spot=ci,
            win_share=sum(1 for v in acts if v > 0) / len(acts),
            mean_minus_reference=m - WALK_FORWARD_H1["mean_pnl_pct_spot"],
        )
    if len(rs) >= min_n:
        m, _, ci = _mean_ci(rs, independent)
        srt = sorted(rs)
        mid = len(srt) // 2
        wf.update(mean_r_study=m, ci95_mean_r_study=ci, median_r_study=srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2)
    by_horizon: dict[str, Any] = {
        h: _horizon_stats([t for t in trades if t["horizon"] == h], min_n, independent) for h in ("short_14d", "long_40d")
    }
    by_horizon["n_unclassified"] = sum(1 for t in trades if t["horizon"] is None)
    return {
        "min_n": min_n,
        "comparable": {"settled": len(comp), "open": sum(1 for x in groups["method"] if x.get("status") == "open")},
        "excluded": excluded,
        "missing_fields": missing,
        "overlapping_pairs": overlaps,
        "ci_withheld": withheld,
        "vs_method_sim": sim,
        "vs_walk_forward": wf,
        "by_horizon": by_horizon,
        "trades": trades,
    }


def summarize(settled: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate settled paper trades: n, win share, mean % of spot (main), mean R (two definitions), total USD."""
    n = len(settled)
    if n == 0:
        return {"n": 0}
    pct = [float(t["pnl_pct_spot"]) for t in settled]
    rw = [float(t["r_worst_case"]) for t in settled if t.get("r_worst_case") is not None]
    rs = [float(t["r_study"]) for t in settled if t.get("r_study") is not None]
    return {
        "n": n,
        "win_share": sum(1 for t in settled if float(t["pnl_usd"]) > 0) / n,
        "mean_pnl_pct_spot": sum(pct) / n,
        "mean_r_worst_case": sum(rw) / len(rw) if rw else None,
        "mean_r_study": sum(rs) / len(rs) if rs else None,
        "n_r_study": len(rs),
        "sum_pnl_usd": sum(float(t["pnl_usd"]) for t in settled),
    }
