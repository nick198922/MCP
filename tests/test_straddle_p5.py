"""Tests of `tradedesk.analytics.straddle_p5` (playbook P5', IDEA-20261114-options-playbook item 4.5)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.calendar_p10 import entry_cost, exec_ivs
from tradedesk.analytics.option_ideas import fee
from tradedesk.analytics.options import black76_price
from tradedesk.analytics.straddle_p5 import (
    YEAR_MS,
    dvol_change,
    hedge_pnl,
    iv_rank_at,
    overlap_clusters,
    plan_straddle,
    sell_early,
    settle_straddle,
    straddle_delta,
    two_sided_tail_gate,
    verdict,
)
from tradedesk.analytics.trend_spread import DAY_MS

T0 = 1_700_006_400_000 // DAY_MS * DAY_MS  # a UTC midnight


def _dvol(n: int, f=lambda i: 0.30 + 0.001 * i) -> list[tuple[int, float]]:
    return [(T0 + i * DAY_MS, f(i)) for i in range(n)]


def _snap(t_ms: int, spot: float, F: float, T_days: float, iv: float = 0.5, strikes=(90.0, 95.0, 100.0, 105.0, 110.0)) -> dict:
    e = t_ms + int(T_days * DAY_MS)
    ks = {k: {"mark_iv": iv, "buy_iv": iv + 0.01, "sell_iv": iv - 0.01} for k in strikes}
    return {"day": t_ms // DAY_MS * DAY_MS, "t_ms": t_ms, "spot": spot, "expiries": {e: {"F": F, "T": T_days / 365, "strikes": ks}}}, e


# ----------------------------------------------------------------------------- condition


def test_iv_rank_uses_last_closed_candle_and_ignores_the_future() -> None:
    n = 400
    t = T0 + 399 * DAY_MS + 9 * 3_600_000  # 09:00 on day 399: candle 398 is the last closed one
    v, why = iv_rank_at(_dvol(n), t)
    assert why is None and v is not None
    assert v["day"] and v["rank"] == pytest.approx(1.0)  # rising series: the known close is the window maximum
    assert v["n_days"] == 365
    # day 398 dips to the window minimum -> rank 0
    dip = _dvol(n, lambda i: 0.10 if i == 398 else 0.30 + 0.001 * i)
    assert iv_rank_at(dip, t)[0]["rank"] == pytest.approx(0.0)
    # changing the candle of the entry day and later ones does not change the rank
    fut = _dvol(n, lambda i: 9.0 if i >= 399 else 0.30 + 0.001 * i)
    assert iv_rank_at(fut, t)[0]["rank"] == pytest.approx(v["rank"])
    # midpoint: rank = (x - min) / (max - min)
    mid = _dvol(n, lambda i: 0.40 if i == 398 else (0.30 if i == 100 else (0.50 if i == 200 else 0.35)))
    assert iv_rank_at(mid, t)[0]["rank"] == pytest.approx(0.5)


def test_iv_rank_needs_330_closes() -> None:
    v, why = iv_rank_at(_dvol(300), T0 + 299 * DAY_MS + 9 * 3_600_000)
    assert v is None and "DVOL history" in why


def test_dvol_change() -> None:
    m = dict(_dvol(60))
    t = T0 + 11 * DAY_MS + 9 * 3_600_000  # known candle: day 10
    assert dvol_change(m, t, 14) == pytest.approx(0.014)
    assert dvol_change(m, T0 + 59 * DAY_MS, 14) is None


# ----------------------------------------------------------------------------- clusters


def test_overlap_clusters_chain_and_prefix_stability() -> None:
    d = DAY_MS
    spans = [(0, 90 * d), (7 * d, 97 * d), (100 * d, 190 * d), (150 * d, 240 * d), (240 * d, 330 * d)]
    assert overlap_clusters(spans) == [0, 0, 1, 1, 2]
    assert overlap_clusters(spans[:3]) == [0, 0, 1]  # later trades never change earlier clusters
    assert overlap_clusters([]) == []
    with pytest.raises(ValueError):
        overlap_clusters([(10, 20), (5, 30)])


# ----------------------------------------------------------------------------- construction and P&L


def test_plan_and_debit_match_black76_with_fees() -> None:
    t = T0 + 9 * 3_600_000
    snap, e = _snap(t, 100.0, 101.0, 75.0)
    plan, why = plan_straddle(snap, e)
    assert why == "" and plan["K"] == 100.0 and plan["iv"] == pytest.approx(0.5)
    assert [leg["arb"] for leg in plan["legs"]] == ["ok", "ok"]
    ivs, fb = exec_ivs(plan, "mark", 0.015)
    assert ivs == [pytest.approx(0.515)] * 2 and fb == 0
    T = 75 / 365
    c, p = black76_price(101.0, 100.0, T, 0.515, True), black76_price(101.0, 100.0, T, 0.515, False)
    assert entry_cost(plan, ivs) == pytest.approx(c + p + fee(100.0, c) + fee(100.0, p))
    # side model: the taker-buy print IV
    assert exec_ivs(plan, "side", 0.015)[0] == [pytest.approx(0.51)] * 2


def test_plan_rejects_far_strike_and_missing_expiry() -> None:
    snap, e = _snap(T0, 100.0, 103.0, 75.0, strikes=(90.0, 100.0, 110.0))
    assert plan_straddle(snap, e) == (None, "no strike near the forward")  # |100 - 103| > 1.5
    assert plan_straddle(snap, e + 1)[1] == "no expiry in window"


def test_plan_replaces_an_arbitrage_mark() -> None:
    snap, e = _snap(T0, 100.0, 100.0, 75.0)
    snap["expiries"][e]["strikes"][100.0]["mark_iv"] = 0.05  # far below the neighbours: convexity broken
    plan, why = plan_straddle(snap, e)
    assert why == "" and plan["legs"][0]["arb"] == "replaced" and plan["iv"] == pytest.approx(0.5)
    assert plan_straddle(snap, e, arb_rule="skip") == (None, "arbitrage in marks")


def test_settle_straddle_pays_intrinsic_less_delivery_fee() -> None:
    snap, e = _snap(T0, 100.0, 100.0, 75.0)
    plan, _ = plan_straddle(snap, e)
    res = settle_straddle(plan, 8.0, 110.0)
    assert res["pnl"] == pytest.approx(10.0 - min(0.00015 * 110.0, 0.125 * 10.0) - 8.0)
    assert res["r"] == pytest.approx(res["pnl"] / 8.0)
    assert settle_straddle(plan, 8.0, 100.0)["r"] == pytest.approx(-1.0)  # pinned at K: the whole premium is lost
    with pytest.raises(ValueError):
        settle_straddle(plan, 0.0, 100.0)


def test_sell_early_prices_both_legs_at_mark_minus_spread() -> None:
    snap, e = _snap(T0, 100.0, 100.0, 75.0)
    plan, _ = plan_straddle(snap, e)
    later = {"day": T0 + 30 * DAY_MS, "t_ms": T0 + 30 * DAY_MS, "spot": 100.0, "expiries": {e: {**snap["expiries"][e], "T": 45 / 365}}}
    res = sell_early(plan, 10.0, later, hs=0.015)
    c = black76_price(100.0, 100.0, 45 / 365, 0.485, True)
    p = black76_price(100.0, 100.0, 45 / 365, 0.485, False)
    assert res["pnl"] == pytest.approx(c + p - fee(100.0, c) - fee(100.0, p) - 10.0)
    assert res["exit"] == "early"


def test_straddle_delta_limits() -> None:
    assert straddle_delta(100.0, 100.0, 1e-9, 0.5) == pytest.approx(0.0, abs=1e-3)
    assert straddle_delta(400.0, 100.0, 0.2, 0.5) == pytest.approx(1.0, abs=1e-6)
    assert straddle_delta(25.0, 100.0, 0.2, 0.5) == pytest.approx(-1.0, abs=1e-6)
    d1 = 0.5 * 0.5 * math.sqrt(0.2)
    assert straddle_delta(100.0, 100.0, 0.2, 0.5) == pytest.approx(math.erf(d1 / math.sqrt(2)))
    assert straddle_delta(101.0, 100.0, 0.0, 0.5) == 1.0


def test_hedge_pnl_deep_itm_offsets_the_move() -> None:
    t = T0
    snap, e = _snap(t, 100.0, 100.0, 75.0, strikes=(10.0, 15.0, 20.0))
    plan = {"t_ms": t, "spot": 100.0, "K": 20.0, "iv": 0.5, "expiry": {"m": e}, "F": {"m": 100.0}}
    # deep ITM call: delta ~ +1 -> hedge ~ -1 BTC; spot 100 -> 120 -> 130 at delivery: hedge loses ~30
    path = [(t + 10 * DAY_MS, 120.0)]
    h = hedge_pnl(plan, path, 130.0, fee_side=0.0)
    assert h["gross"] == pytest.approx(-30.0, abs=1e-3) and h["rebalances"] == 2
    hf = hedge_pnl(plan, path, 130.0, fee_side=0.001)
    assert hf["fees"] == pytest.approx(0.001 * (100.0 + 130.0), rel=1e-3)
    # weekly rebalancing skips a point one day after the entry
    assert hedge_pnl(plan, [(t + DAY_MS, 110.0)], 110.0, fee_side=0.0, every_days=7)["rebalances"] == 1
    with pytest.raises(ValueError):
        hedge_pnl(plan, [(e + 1, 100.0)], 100.0, fee_side=0.0)
    assert YEAR_MS == 365 * DAY_MS


def test_hedge_pnl_prefix_does_not_see_later_points() -> None:
    t = T0
    snap, e = _snap(t, 100.0, 100.0, 75.0)
    plan = {"t_ms": t, "spot": 100.0, "K": 100.0, "iv": 0.5, "expiry": {"m": e}, "F": {"m": 100.0}}
    a = hedge_pnl(plan, [(t + DAY_MS, 101.0), (t + 2 * DAY_MS, 99.0)], 99.0, fee_side=0.0)
    b = hedge_pnl(plan, [(t + DAY_MS, 101.0), (t + 2 * DAY_MS, 99.0), (t + 3 * DAY_MS, 99.0)], 99.0, fee_side=0.0)
    assert a["gross"] == pytest.approx(b["gross"])  # a flat extra day adds nothing


# ----------------------------------------------------------------------------- gates and verdict


def test_two_sided_tail_gate() -> None:
    vals = [-1.0] * 5 + [1.0] * 5
    assert two_sided_tail_gate(vals, list(range(10)))["ok"]
    g = two_sided_tail_gate([1.0] * 10, list(range(10)))
    assert not g["ok"] and g["n_loss"] == 0 and g["n_win"] == 10
    assert not two_sided_tail_gate([-1.0] * 9 + [5.0], list(range(10)))["ok"]  # one win: the mean rests on it


def _v(**kw) -> str:
    base = {
        "mean": 0.3,
        "lo": 0.1,
        "hi": 0.5,
        "holm_p": 0.01,
        "is_mean": 0.3,
        "oos_mean": 0.3,
        "stress_cost_mean": 0.2,
        "edge": 0.10,
        "tail_ok": True,
        "coverage_ok": True,
    }
    return verdict(**{**base, **kw})


def test_verdict() -> None:
    assert _v() == "✓"
    assert _v(tail_ok=False) == "◯"
    assert _v(lo=-0.1) == "≈"
    assert _v(mean=-0.3, lo=-0.6, hi=-0.1) == "✗"
    assert _v(mean=-0.3, lo=-0.6, hi=-0.1, coverage_ok=False) == "◯"  # coverage gate overrides ✗ too (card 4.5)
    assert _v(mean=-0.3, lo=-0.6, hi=-0.1, tail_ok=False) == "✗"
    assert _v(mean=0.05, lo=-0.5, hi=0.6, oos_mean=-0.1) == "◯"
