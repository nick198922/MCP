"""P12 (condor with 25-delta shorts): tail gate, Clopper-Pearson bound, stress mean, clusters, verdict rule."""

from __future__ import annotations

import pytest

from tradedesk.analytics import condor_25d as c25
from tradedesk.analytics import condor_dvol as cd
from tradedesk.analytics.option_condor import exec_legs

DAY = c25.DAY_MS
WEEK = c25.WEEK_MS
HOUR = 3_600_000
D0 = 1_700_006_400_000 - (1_700_006_400_000 % DAY)
EXP = D0 + 14 * DAY + 8 * HOUR


def surf(day: int, spot: float = 100.0, iv: float = 0.5) -> dict:
    t = day + 9 * HOUR
    T = (EXP - t) / (365 * DAY)
    strikes = {float(k): {"mark_iv": iv, "sell_iv": iv - 0.02, "buy_iv": iv + 0.02} for k in range(40, 201)}
    return {"day": day, "t_ms": t, "spot": spot, "expiries": {EXP: {"F": spot, "T": T, "ratio": 1.0, "strikes": strikes}}}


def test_25d_plan_is_closer_than_15d_and_credit_larger():
    p25 = cd.plan_p1(surf(D0), EXP, short_delta=0.25)[0]
    p15 = cd.plan_p1(surf(D0), EXP, short_delta=0.15)[0]
    assert p25 is not None and p15 is not None
    assert abs(p25["delta"]["sp"] + 0.25) < 0.03 and abs(p25["delta"]["sc"] - 0.25) < 0.03
    assert p15["K"]["sp"] < p25["K"]["sp"] < 100.0 < p25["K"]["sc"] < p15["K"]["sc"]
    c25_ = cd.entry_credit(p25, exec_legs(p25, "mark", 0.015)[0])
    c15_ = cd.entry_credit(p15, exec_legs(p15, "mark", 0.015)[0])
    assert c25_ is not None and c15_ is not None and c25_ > c15_ > 0


def test_block_clusters_and_no_lookahead():
    days = [D0 + i * WEEK for i in range(10)]
    cl = c25.block_clusters(days, start_ms=D0, weeks=4)
    assert [cl[d] for d in days] == [0, 0, 0, 0, 1, 1, 1, 1, 2, 2]
    # adding later entries never changes the cluster of earlier ones
    more = c25.block_clusters([*days, *(D0 + i * WEEK for i in range(10, 30))], start_ms=D0, weeks=4)
    assert all(more[d] == cl[d] for d in days)
    with pytest.raises(ValueError):
        c25.block_clusters(days, start_ms=D0, weeks=0)


def test_p_no_loss_resample_analytic():
    assert c25.p_no_loss_resample(1, 2) == pytest.approx(0.25)
    assert c25.p_no_loss_resample(0, 5) == pytest.approx(1.0)
    assert c25.p_no_loss_resample(5, 5) == pytest.approx(0.0)
    assert c25.p_no_loss_resample(3, 60) == pytest.approx((57 / 60) ** 60)


def test_tail_gate():
    # 16 winners in 5 clusters: no loss at all -> not ok (the P1' case)
    g = c25.tail_gate([0.08] * 16, [i % 5 for i in range(16)])
    assert g["n_loss"] == 0 and g["p_no_loss_resample"] == 1.0 and not g["ok"]
    # 5 losses but all in one of 20 clusters: degenerate bootstrap tail
    vals = [-0.5] * 5 + [0.1] * 95
    cls = [0] * 5 + [1 + i % 19 for i in range(95)]
    g = c25.tail_gate(vals, cls)
    assert g["n_loss"] == 5 and g["n_loss_clusters"] == 1 and g["p_no_loss_resample"] == pytest.approx(0.95**20)
    assert not g["ok"]
    # 6 losses spread over 6 of 20 clusters: ok; R == 0 counts as a loss
    cls = list(range(6)) + [6 + i % 14 for i in range(94)]
    vals = [-0.5] * 5 + [0.0] + [0.1] * 94
    g = c25.tail_gate(vals, cls)
    assert g["n_loss"] == 6 and g["ok"]
    # 4 losses: fewer than the registered minimum of 5
    assert not c25.tail_gate([-1.0] * 4 + [0.1] * 96, list(range(100)))["ok"]


def test_clopper_pearson_upper_analytic():
    for n in (5, 8, 16, 100):
        assert c25.clopper_pearson_upper(0, n) == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-9)
    # k = 1, n = 10: the bound solves P(X <= 1) = 0.05 -> 0.3942 (standard table value)
    assert c25.clopper_pearson_upper(1, 10) == pytest.approx(0.3942, abs=5e-4)
    assert c25._binom_cdf(1, 10, c25.clopper_pearson_upper(1, 10)) == pytest.approx(0.05, abs=1e-9)
    assert c25.clopper_pearson_upper(10, 10) == 1.0
    assert c25.clopper_pearson_upper(3, 0) == 1.0
    with pytest.raises(ValueError):
        c25.clopper_pearson_upper(4, 3)


def test_stress_and_parametric_tail():
    assert c25.stress_mean(0.1, -0.8, 0.2) == pytest.approx(0.08 - 0.16)
    vals = [0.1] * 8 + [-0.6, -1.0]
    t = c25.parametric_tail(vals, k_eff=1, n_eff=5)
    assert t["q_obs"] == pytest.approx(0.2) and t["loss_from"] == "sample"
    assert t["win_mean"] == pytest.approx(0.1) and t["loss_mean"] == pytest.approx(-0.8)
    assert 0.2 < t["q_up"] < 1.0
    assert t["stress_mean"] == pytest.approx((1 - t["q_up"]) * 0.1 - 0.8 * t["q_up"])
    # fewer than 5 own losses with a reference given: the loss size comes from the reference
    t = c25.parametric_tail(vals, k_eff=1, n_eff=5, loss_ref=[-0.2, -0.4, 0.3])
    assert t["loss_from"] == "reference" and t["loss_mean"] == pytest.approx(-0.3)
    # a loss-free sample borrows the loss size from the reference sample
    t = c25.parametric_tail([0.08] * 16, k_eff=0, n_eff=8, loss_ref=[0.2, -0.5, -1.0])
    assert t["loss_from"] == "reference" and t["loss_mean"] == pytest.approx(-0.75)
    q_up = 1 - 0.05 ** (1 / 8)
    assert t["stress_mean"] == pytest.approx((1 - q_up) * 0.08 - 0.75 * q_up)


def test_verdict_rule_and_tail():
    base = {"mean": 0.05, "lo": 0.01, "hi": 0.09, "holm_p": 0.01, "is_mean": 0.04, "oos_mean": 0.06, "stress_cost_mean": 0.01, "edge": 0.03}
    assert c25.verdict(**base, tail_ok=True) == "✓"
    assert c25.verdict(**base, tail_ok=False) == "◯"  # the P1' lesson: no measured tail, no ✓
    assert c25.verdict(**{**base, "holm_p": 0.2}, tail_ok=True) == "≈"
    assert c25.verdict(**{**base, "holm_p": 0.2}, tail_ok=False) == "◯"
    assert c25.verdict(**{**base, "stress_cost_mean": -0.01}, tail_ok=True) == "≈"
    neg = {**base, "mean": -0.02, "lo": -0.06, "hi": 0.02}
    assert c25.verdict(**neg, tail_ok=True) == "✗"  # mean <= 0 and hi < edge
    assert c25.verdict(**neg, tail_ok=False) == "✗"  # losses are visible: the tail rule does not lift a ✗
    assert c25.verdict(**{**neg, "hi": 0.05}, tail_ok=True) == "◯"
    assert c25.verdict(**{**neg, "hi": -0.001}, tail_ok=True) == "✗"
