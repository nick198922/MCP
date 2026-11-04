"""Direct tests of pure analytics functions that feed forward numbers and had no direct test (second round of the
coverage audit, item 70 of the standing-improvements card; list in docs/changes/2026-11-04-analytics-coverage-audit.md):
open-position P&L and entry ranking of carry A / B (analytics/carry_rules.py), the delisted-perp settlement proxy
(analytics/carry_delisting.py), rank scores and the capital curve of the carry modules (analytics/carry_modules.py),
the Polymarket <-> Deribit paper rule (analytics/pm_gap_paper.py) and outcome-price normalisation
(analytics/polymarket.py). Expected values are worked out by hand in the comments (analytical references)."""

from __future__ import annotations

import math
import random

import pytest

from tradedesk.analytics.carry import DAY_MS, _Market
from tradedesk.analytics.carry_delisting import settle_price
from tradedesk.analytics.carry_modules import apply_capital, rank_score
from tradedesk.analytics.carry_rules import carry_open, carry_pnl_now, carry_rank, spread_open, spread_pnl_now, spread_rank
from tradedesk.analytics.pm_gap_paper import YEAR_S, EntryRule, entry_cost, value_after, win_probs
from tradedesk.analytics.polymarket import devig_multi

H_MS = 3_600_000
STEP = 8 * H_MS


def _n(x: float) -> float:
    """Standard normal CDF, written out independently of analytics.options.ncdf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ------------------------------------------------------------------------------------- carry_rules: open P&L


def test_carry_pnl_now_funding_plus_basis_minus_entry_fees_only():
    p = carry_open("X", 0, 0.3, 100.0, 101.0, 0.002)
    p.update(funding=0.003, spot_last=110.0, perp_last=112.0)
    # basis = (spot move - perp move) / spot entry = ((110 - 100) - (112 - 101)) / 100 = -0.01
    # pnl = 0.003 - 0.01 - 0.002 = -0.009 (no exit cost: the equity-curve convention)
    assert carry_pnl_now(p) == pytest.approx(-0.009)


def test_carry_pnl_now_fresh_position_is_minus_entry_cost():
    # just opened: no funding, prices at entry -> basis 0, pnl = -side cost
    assert carry_pnl_now(carry_open("X", 0, 0.3, 100.0, 101.0, 0.002)) == pytest.approx(-0.002)


@pytest.mark.parametrize(("d", "expected"), [(1, 0.031), (-1, -0.029)])
def test_spread_pnl_now_sign_follows_direction(d, expected):
    p = spread_open("Y", 0, d, 0.3, 100.0, 50.0, 0.001)
    p.update(funding=0.002, a_last=104.0, b_last=51.0)
    # price pnl = dir x ((104 - 100) - (51 - 50)) / 100 = dir x 0.03
    # d=+1: 0.002 + 0.03 - 0.001 = 0.031;  d=-1: 0.002 - 0.03 - 0.001 = -0.029
    assert spread_pnl_now(p) == pytest.approx(expected)


def test_spread_pnl_now_fresh_position_is_minus_entry_cost():
    assert spread_pnl_now(spread_open("Y", 0, -1, 0.3, 100.0, 50.0, 0.001)) == pytest.approx(-0.001)


# ------------------------------------------------------------------------------------- carry_rules: ranking


def test_carry_rank_gate_prices_and_tie_order():
    cands = [
        ("AAA", 0.30, 1.0, 1.0),
        ("BBB", 0.25, 1.0, 1.0),
        ("CCC", 0.30, 1.0, 1.0),  # ties AAA on APR -> symbol descending puts CCC first
        ("DDD", 0.19, 1.0, 1.0),  # below the 0.20 gate
        ("EEE", None, 1.0, 1.0),  # no APR
        ("FFF", 0.50, None, 1.0),  # no spot price
        ("GGG", 0.50, 1.0, 0.0),  # zero perp price counts as missing
        ("HHH", 0.20, 1.0, 1.0),  # exactly at the gate: kept (>=)
    ]
    assert carry_rank(cands, 0.20) == [(0.30, "CCC"), (0.30, "AAA"), (0.25, "BBB"), (0.20, "HHH")]
    assert carry_rank([], 0.20) == []


def test_spread_rank_gate_gap_direction_and_order():
    # dyadic rates so |rb - ra| is exact in floats
    cands = [
        ("A", 0.125, 0.5, 100.0, 101.0),  # spread 0.375, B higher -> +1; gap |100/101 - 1| = 0.0099
        ("B", 0.75, 0.25, 10.0, 10.0),  # spread 0.5, A higher -> -1; gap 0
        ("C", 0.0, 0.125, 1.0, 1.0),  # spread 0.125 < 0.25 gate
        ("D", 0.125, 0.625, 100.0, 110.0),  # spread 0.5 but gap |100/110 - 1| = 0.0909
        ("E", None, 0.5, 1.0, 1.0),  # no APR
        ("F", 0.125, 0.5, None, 1.0),  # no price
        ("G", 0.25, 0.5, 1.0, 1.0),  # spread exactly 0.25: kept
    ]
    assert spread_rank(cands, 0.25, 0.02) == [(0.5, "B", -1), (0.375, "A", 1), (0.25, "G", 1)]
    # no gap limit: D is kept and ties B at 0.5 -> symbol descending puts D first
    assert spread_rank(cands, 0.25, None) == [(0.5, "D", 1), (0.5, "B", -1), (0.375, "A", 1), (0.25, "G", 1)]
    assert spread_rank([], 0.25, None) == []


# ------------------------------------------------------------------------------- carry_delisting.settle_price


def _market(funding=(), spot=(), perp=(), bar_ms=4 * H_MS) -> _Market:
    return _Market({"funding": list(funding), "spot": list(spot), "perp": list(perp)}, bar_ms)


def _bar(ts: int, close: float) -> dict:
    return {"ts": ts, "close": close, "high": close}


def test_settle_price_picks_last_close_at_or_before_t_end():
    # 4h bars opening at 0 and 4h close at 4h and 8h
    m = _market(spot=[_bar(0, 10.0), _bar(4 * H_MS, 11.0)], perp=[_bar(0, 20.0), _bar(4 * H_MS, 21.0)])
    assert settle_price(m, 8 * H_MS, "perp_last") == 21.0  # close time 8h is inclusive
    assert settle_price(m, 8 * H_MS - 1, "perp_last") == 20.0  # one ms earlier: only the 4h close is known
    assert settle_price(m, 8 * H_MS, "spot") == 11.0
    assert settle_price(m, 100 * H_MS, "spot") == 11.0  # after the data ends: the last close stays
    assert settle_price(m, 4 * H_MS - 1, "perp_last") is None  # before the first close
    assert settle_price(_market(), 8 * H_MS, "spot") is None  # no bars at all


def test_settle_price_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown settle"):
        settle_price(_market(perp=[_bar(0, 20.0)]), 8 * H_MS, "mark")


# ------------------------------------------------------------------------------------- carry_modules.rank_score


def _funding(rates: list[float]) -> list[dict]:
    """Payments every 8 h starting at ts 0."""
    return [{"ts_ms": i * STEP, "rate": r} for i, r in enumerate(rates)]


def test_rank_score_apr7_and_min7_30():
    m = _market(funding=_funding([0.0001] * 31))  # 31 payments: ts 0 .. 240 h = 10 days
    t = 30 * STEP
    assert rank_score("apr7", m, t, 0.42) == 0.42  # the base rank is the gate APR itself
    # APR30 over the available history: W = min(30 d, 10 d) = 10 d >= 7 d; payments in (0, 240 h] = 30 x 0.0001
    # -> 0.003 x 365 / 10 = 0.1095 (= 0.0001 x 3 per day x 365)
    assert rank_score("min7_30", m, t, 0.20) == pytest.approx(0.1095)
    assert rank_score("min7_30", m, t, 0.05) == pytest.approx(0.05)  # min(APR7, APR30) takes the lower one
    # at 6 days of history W = 6 d < 7 d -> no score
    assert rank_score("min7_30", m, 18 * STEP, 0.20) is None
    assert rank_score("min7_30", _market(), t, 0.20) is None  # no funding history


def test_rank_score_median30():
    m = _market(funding=_funding([0.0, 0.0001, 0.0003, 0.0002]))
    # per-payment annualised rates of payments 1..3: rate x 365 d / 8 h = rate x 1095
    # -> 0.1095, 0.3285, 0.219; median = 0.219
    assert rank_score("median30", m, 3 * STEP, 0.3) == pytest.approx(0.219)
    # only payments 1..2 known at t = 16 h -> 2 rates < min_n = 3 -> None
    assert rank_score("median30", m, 2 * STEP, 0.3) is None


def test_rank_score_random_and_errors():
    m = _market(funding=_funding([0.0001]))
    assert rank_score("random", m, 0, 0.3, random.Random(7)) == random.Random(7).random()
    with pytest.raises(ValueError, match="needs rng"):
        rank_score("random", m, 0, 0.3)
    with pytest.raises(ValueError, match="unknown rank"):
        rank_score("apr30", m, 0, 0.3)


# ----------------------------------------------------------------------------------- carry_modules.apply_capital


def test_apply_capital_constant_and_callable_factor():
    eq = [(0, 0.01), (DAY_MS, -0.02)]
    assert apply_capital(eq, 2.0) == [(0, 0.005), (DAY_MS, -0.01)]
    # factor(ts) = 1 + ts / day -> 1 at day 0, 2 at day 1
    assert apply_capital(eq, lambda t: 1.0 + t / DAY_MS) == [(0, 0.01), (DAY_MS, -0.01)]
    assert apply_capital([], 2.0) == []


# --------------------------------------------------------------------------------------- pm_gap_paper.entry_cost


def test_entry_cost_adds_taker_fee_upper_bound():
    # 0.4 + 0.02 x (0.4 x 0.6) ** 1 = 0.4 + 0.0048
    assert entry_cost(0.4, 0.02, 1.0) == pytest.approx(0.4048)
    # exponent 2: 0.02 x 0.24 ** 2 = 0.001152
    assert entry_cost(0.4, 0.02, 2.0) == pytest.approx(0.401152)
    assert entry_cost(0.4, 0.0, 1.0) == 0.4  # fee-free market
    assert entry_cost(0.4, -0.01, 1.0) == 0.4  # a non-positive rate means no fee


# ---------------------------------------------------------------------------------------- pm_gap_paper.win_probs


def _pm_row(**kw) -> dict:
    """A threshold-market snapshot: forward = strike = 100, IV 0.5, a quarter of a year to the end, model P(YES) 0.45."""
    r = {"model_prob": 0.45, "kind": "above", "spot": 100.0, "strike": 100.0, "model_iv": 0.5, "ts": 0, "end_ts": YEAR_S // 4}
    return r | kw


def test_win_probs_risk_neutral_only():
    w = win_probs(_pm_row(), EntryRule(vol_ks=(1.0,)))
    assert w == {"yes": 0.45, "no": pytest.approx(0.55)}


def test_win_probs_takes_less_favourable_reading_of_each_side():
    # k = 0.92: p_k = p + N(d2(0.92 sigma)) - N(d2(sigma)); at F = K, d2 = -sigma sqrt(tau) / 2
    # sigma sqrt(0.25) = 0.25 -> d2 = -0.125; 0.92 x 0.25 = 0.23 -> d2 = -0.115
    p_k = 0.45 + _n(-0.115) - _n(-0.125)  # ~0.45396: a lower vol raises P(above) at the money
    w = win_probs(_pm_row(), EntryRule())  # vol_ks = (1.0, 0.92)
    assert w["yes"] == pytest.approx(0.45)  # YES: the lower of 0.45 and p_k
    assert w["no"] == pytest.approx(1.0 - p_k)  # NO: 1 - the higher one


def test_win_probs_none_when_a_reading_is_missing():
    assert win_probs(_pm_row(model_prob=None), EntryRule()) is None
    assert win_probs(_pm_row(model_iv=None), EntryRule()) is None  # the k = 0.92 reading needs the IV
    assert win_probs(_pm_row(), EntryRule(vol_ks=())) is None  # no reading at all


# -------------------------------------------------------------------------------------- pm_gap_paper.value_after


def _snap(ts: int, **book) -> dict:
    return {"ts": ts, **book}


def test_value_after_nearest_snapshot_within_tolerance():
    target = 10_000
    rows = [
        _snap(target - 600, yes_bid=0.38, yes_ask=0.42),  # mid 0.40, 600 s early
        _snap(target + 600, yes_bid=0.48, yes_ask=0.52),  # mid 0.50, 600 s late: tie -> the earlier one wins
        _snap(target + 4_000, yes_bid=0.0, yes_ask=0.02),  # outside +-3600 s
    ]
    assert value_after(rows, "yes", target) == pytest.approx(0.40)
    rows.append(_snap(target + 100, yes_bid=0.58, yes_ask=0.62))  # nearer: mid 0.60
    assert value_after(rows, "yes", target) == pytest.approx(0.60)


def test_value_after_mid_fallback_tolerance_edge_and_empty():
    target = 10_000
    # the NO book is missing -> NO mid = 1 - YES mid = 1 - 0.30; exactly at the tolerance edge (inclusive)
    rows = [_snap(target + 3_600, yes_bid=0.28, yes_ask=0.32)]
    assert value_after(rows, "no", target) == pytest.approx(0.70)
    # a nearer snapshot without any book is skipped, not taken as None
    rows.append(_snap(target, yes_bid=None, yes_ask=0.5))
    assert value_after(rows, "no", target) == pytest.approx(0.70)
    assert value_after(rows, "no", target, tol_s=60) is None
    assert value_after([], "yes", target) is None


# ---------------------------------------------------------------------------------------- polymarket.devig_multi


def test_devig_multi_normalises_to_one():
    # 0.55 + 0.50 = 1.05 -> 0.55 / 1.05 = 0.523810, 0.50 / 1.05 = 0.476190
    out = devig_multi([0.55, 0.50])
    assert out == pytest.approx([0.55 / 1.05, 0.50 / 1.05])
    assert sum(out) == pytest.approx(1.0)
    # three outcomes summing to 1.04: 0.52 / 1.04 = 0.5, 0.26 / 1.04 = 0.25 each
    assert devig_multi([0.52, 0.26, 0.26]) == pytest.approx([0.5, 0.25, 0.25])
    assert devig_multi([0.0, 1.0]) == [0.0, 1.0]  # already a distribution


@pytest.mark.parametrize("prices", [[], [0.0, 0.0], [-0.1, 0.05]])
def test_devig_multi_none_for_non_positive_sum(prices):
    assert devig_multi(prices) is None
