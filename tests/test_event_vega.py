"""Card IDEA-20261114-options-playbook, item 4.7: P9 at 3-5 days (event_vega)."""

from __future__ import annotations

import pytest

from tradedesk.analytics.event_vega import (
    control_anchor,
    exit_snapshot,
    expiry_after_gap,
    hold_outcome,
    iv_change,
    p9_labels,
    paired,
    stratified_diff,
)
from tradedesk.analytics.option_ideas import fee
from tradedesk.analytics.options import black76_price
from tradedesk.analytics.straddle_p5 import plan_straddle
from tradedesk.analytics.trend_spread import DAY_MS

H = 3_600_000
T0 = 1_700_006_400_000 // DAY_MS * DAY_MS  # a UTC midnight
STRIKES = (90.0, 95.0, 100.0, 105.0, 110.0)


def _exp(F: float, T_days: float, iv: float) -> dict:
    ks = {k: {"mark_iv": iv, "buy_iv": iv + 0.01, "sell_iv": iv - 0.01} for k in STRIKES}
    return {"F": F, "T": T_days / 365, "strikes": ks}


def _snap(day: int, spot: float, expiries: dict[int, tuple[float, float]]) -> dict:
    """Surface at 09:00 of ``day``; ``expiries``: expiry ms -> (forward, iv)."""
    t = day + 9 * H
    return {
        "day": day,
        "t_ms": t,
        "spot": spot,
        "expiries": {e: _exp(F, (e - t) / DAY_MS, iv) for e, (F, iv) in expiries.items()},
    }


def test_expiry_after_gap_picks_first_expiry_three_days_after_release() -> None:
    e1, e3, e4, e7 = (T0 + k * DAY_MS + 8 * H for k in (1, 3, 4, 7))
    snap = _snap(T0, 100.0, {e1: (100.0, 0.5), e3: (100.0, 0.5), e4: (100.0, 0.5), e7: (100.0, 0.5)})
    release = T0 + 12 * H + 30 * 60_000  # 12:30 UTC
    assert expiry_after_gap(snap, release) == (e4, "")  # e3 is 2.81 d after the release
    assert expiry_after_gap(snap, release, min_gap_days=2.0) == (e3, "")
    assert expiry_after_gap(snap, release, min_gap_days=5.0) == (e7, "")
    assert expiry_after_gap(snap, release, max_dte_days=3.5) == (None, "first expiry after the gap too far")
    assert expiry_after_gap(snap, release, min_gap_days=8.0)[0] is None
    # the bound is inclusive
    assert expiry_after_gap(snap, e4 - 3 * DAY_MS) == (e4, "")


def test_control_anchor_matches_a_release_time() -> None:
    assert control_anchor(T0) == T0 + 13 * H
    e4 = T0 + 4 * DAY_MS + 8 * H
    snap = _snap(T0, 100.0, {T0 + 3 * DAY_MS + 8 * H: (100.0, 0.5), e4: (100.0, 0.5)})
    assert expiry_after_gap(snap, control_anchor(T0))[0] == e4  # the same expiry a 12:30-19:00 release would pick


def test_hold_outcome_sells_at_mark_minus_spread_on_next_morning() -> None:
    e4 = T0 + 4 * DAY_MS + 8 * H
    entry = _snap(T0, 100.0, {e4: (100.0, 0.55)})
    plan, why = plan_straddle(entry, e4)
    assert why == ""
    ex = _snap(T0 + DAY_MS, 103.0, {e4: (103.0, 0.45)})
    res, why = hold_outcome(plan, 6.0, {T0: entry, T0 + DAY_MS: ex}, hs=0.015)
    assert why == "" and res["hold_days"] == 1 and res["exit"] == "early"
    T = (e4 - ex["t_ms"]) / DAY_MS / 365
    c = black76_price(103.0, 100.0, T, 0.435, True)
    p = black76_price(103.0, 100.0, T, 0.435, False)
    assert res["pnl"] == pytest.approx(c + p - fee(103.0, c) - fee(103.0, p) - 6.0)
    assert res["r"] == pytest.approx(res["pnl"] / 6.0)
    assert iv_change(plan, ex) == pytest.approx(-0.10)


def test_hold_outcome_ignores_surfaces_after_the_exit_day() -> None:
    e4 = T0 + 4 * DAY_MS + 8 * H
    entry = _snap(T0, 100.0, {e4: (100.0, 0.55)})
    plan, _ = plan_straddle(entry, e4)
    ex = _snap(T0 + DAY_MS, 101.0, {e4: (101.0, 0.50)})
    base, _ = hold_outcome(plan, 6.0, {T0: entry, T0 + DAY_MS: ex})
    later = {T0: entry, T0 + DAY_MS: ex, T0 + 2 * DAY_MS: _snap(T0 + 2 * DAY_MS, 150.0, {e4: (150.0, 2.0)})}
    assert hold_outcome(plan, 6.0, later)[0]["pnl"] == pytest.approx(base["pnl"])
    # the exit-day surface does matter, and a two-day hold reads the later one
    ex2 = _snap(T0 + DAY_MS, 101.0, {e4: (101.0, 0.60)})
    assert hold_outcome(plan, 6.0, {T0: entry, T0 + DAY_MS: ex2})[0]["pnl"] > base["pnl"]
    assert hold_outcome(plan, 6.0, later, hold_days=2)[0]["pnl"] > base["pnl"]


def test_hold_outcome_reasons() -> None:
    e1 = T0 + DAY_MS + 8 * H
    entry = _snap(T0, 100.0, {e1: (100.0, 0.55)})
    plan, _ = plan_straddle(entry, e1)
    assert hold_outcome(plan, 6.0, {T0: entry}) == (None, "no exit surface")
    ex = _snap(T0 + DAY_MS, 100.0, {T0 + 5 * DAY_MS: (100.0, 0.5)})
    assert hold_outcome(plan, 6.0, {T0 + DAY_MS: ex}) == (None, "exit at or after expiry")  # 09:00 > 08:00 expiry
    e4 = T0 + 4 * DAY_MS + 8 * H
    plan4, _ = plan_straddle(_snap(T0, 100.0, {e4: (100.0, 0.55)}), e4)
    assert hold_outcome(plan4, 6.0, {T0 + DAY_MS: ex}) == (None, "no exit mark")
    with pytest.raises(ValueError):
        exit_snapshot({}, T0, 0)
    assert iv_change(plan4, None) is None and iv_change(plan4, ex) is None


def test_paired_matches_days() -> None:
    a = [{"day": 3, "r": 0.1}, {"day": 1, "r": -0.2}, {"day": 2, "r": 0.0}]
    b = [{"day": 1, "r": -0.5}, {"day": 3, "r": 0.3}]
    out = paired(a, b)
    assert [t["day"] for t in out] == [1, 3]
    assert [t["r"] for t in out] == [pytest.approx(0.3), pytest.approx(-0.2)]
    assert out[0]["a"] == -0.2 and out[0]["b"] == -0.5


def test_stratified_diff_removes_the_mix() -> None:
    # stratum 0: a = 1, b = 1; stratum 1: a = 3, b = 3 -> no difference within strata, only the mix differs
    a = [{"s": 0, "r": 1.0}] * 1 + [{"s": 1, "r": 3.0}] * 3
    b = [{"s": 0, "r": 1.0}] * 3 + [{"s": 1, "r": 3.0}] * 1 + [{"s": 2, "r": 9.0}]
    out = stratified_diff(a, b, lambda t: t["s"])
    assert out["diff"] == pytest.approx(0.0) and out["n_a"] == 4 and out["dropped"] == 0
    assert (sum(t["r"] for t in a) / 4) - (sum(t["r"] for t in b) / 5) != pytest.approx(0.0)  # the raw difference is not 0
    # a within-stratum shift of +0.5 in stratum 1 only, weighted by a's counts 1 : 3
    a2 = [{"s": 0, "r": 1.0}] + [{"s": 1, "r": 3.5}] * 3 + [{"s": 5, "r": 0.0}]
    out2 = stratified_diff(a2, b, lambda t: t["s"])
    assert out2["diff"] == pytest.approx(0.375) and out2["dropped"] == 1


def test_p9_labels() -> None:
    assert p9_labels("✗", "✓", "✗") == {"H1": "loses", "H2": "better than daily", "H3": "crush costs"}
    assert p9_labels("✓", "✗", "✓") == {"H1": "new card", "H2": "not better", "H3": "release pays the buyer"}
    for v in ("≈", "◯"):
        assert set(p9_labels(v, v, v).values()) == {"insufficient"}
