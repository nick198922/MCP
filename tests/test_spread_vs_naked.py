"""Tests for analytics.spread_vs_naked (playbook P6 / P3 mechanism: debit spread vs naked option)."""

from __future__ import annotations

import pytest

from tradedesk.analytics import spread_vs_naked as sv
from tradedesk.analytics import trend_spread as ts
from tradedesk.analytics.option_ideas import fee
from tradedesk.analytics.options import black76_price

D0 = 1_700_006_400_000 - (1_700_006_400_000 % ts.DAY_MS)
EXP = D0 + 14 * ts.DAY_MS + 8 * ts.HOUR_MS
P = {
    "target": 14.0,
    "lo": 7.0,
    "hi": 24.0,
    "long_delta": 0.40,
    "short_delta": 0.15,
    "band": 0.015,
    "model": "mark",
    "hs": 0.015,
    "arb": "replace",
    "exit": "expiry",
    "tp": 0.70,
    "last_expiry": EXP + 30 * ts.DAY_MS,
}


def surf(day: int, spot: float = 100.0, iv: float = 0.5, expiry: int = EXP) -> dict:
    t = day + 9 * ts.HOUR_MS
    T = (expiry - t) / (365 * ts.DAY_MS)
    strikes = {float(k): {"mark_iv": iv, "sell_iv": iv - 0.02, "buy_iv": iv + 0.02, "n_put": 1} for k in range(40, 201)}
    return {"day": day, "t_ms": t, "spot": spot, "expiries": {expiry: {"F": spot, "T": T, "ratio": 1.0, "strikes": strikes}}}


def test_naked_debit_and_settle_analytic():
    plan, _ = ts.plan_spread(surf(D0), EXP, bearish=False)
    p = black76_price(100.0, plan["K_long"], plan["T"], 0.515, True)
    nd = sv.naked_debit(plan, 0.515)
    assert nd == pytest.approx(p + fee(100.0, p))
    assert sv.naked_debit(plan, 0.005) is None
    out = sv.naked_settle(plan, 4.0, 50.0)  # OTM call: lose the debit
    assert out["pnl"] == pytest.approx(-4.0) and out["r"] == pytest.approx(-1.0)
    K = plan["K_long"]
    out = sv.naked_settle(plan, 4.0, K + 20.0)  # ITM: intrinsic minus 0.015 % of the price (cap not binding)
    assert out["pnl"] == pytest.approx(20.0 - 0.00015 * (K + 20.0) - 4.0)
    put = dict(plan, is_call=False)
    assert sv.naked_settle(put, 4.0, K - 0.01)["pnl"] == pytest.approx(0.01 - 0.125 * 0.01 - 4.0)  # 12.5 % cap


def test_paired_difference_is_the_sold_leg_without_costs():
    """With zero half-spread the USD difference spread - naked equals the short leg's P&L (premium - payoff - fees)."""
    snap = surf(D0)
    b, why = sv.build_pair(snap, EXP, False, {**P, "hs": 0.0})
    assert why == ""
    plan = b["plan"]
    ps = black76_price(100.0, plan["K_short"], plan["T"], 0.5, True)
    for price in (80.0, plan["K_long"] + 3.0, plan["K_short"] + 10.0):
        res = sv.settle_pair({}, b, price, {**P, "hs": 0.0})
        payoff = max(price - plan["K_short"], 0.0)
        dfee = min(0.00015 * price, 0.125 * payoff) if payoff > 0 else 0.0
        assert res["pnl_spot"] * 100.0 == pytest.approx(ps - fee(100.0, ps) - payoff - dfee)
        assert res["r"] == pytest.approx(res["r_spread"] - res["r_naked"])
    assert b["debit_spread"] < b["debit_naked"]


def test_clean_marks_replaces_or_skips():
    snap = surf(D0)
    plan, _ = ts.plan_spread(snap, EXP, bearish=False)
    same, st = sv.clean_marks(snap, plan)
    assert same["mark_long"] == pytest.approx(0.5) and st == {"long": "ok", "short": "ok"}
    bad = surf(D0)
    bad["expiries"][EXP]["strikes"][plan["K_short"]]["mark_iv"] = 0.05  # far too cheap: convexity breaks, repaired
    fixed, st = sv.clean_marks(bad, plan)
    assert st["short"] == "replaced" and fixed["mark_short"] == pytest.approx(0.5)
    out, st = sv.clean_marks(bad, plan, rule="skip")
    assert out is None and st["short"] == "skip"
    assert sv.clean_marks(bad, plan, rule="none")[1] == {"long": "unchecked", "short": "unchecked"}
    with pytest.raises(ValueError):
        sv.clean_marks(bad, plan, rule="x")


def test_pairs_for_reasons_and_stage0_has_no_outcome():
    days = {D0: surf(D0), D0 + 7 * ts.DAY_MS: surf(D0 + 7 * ts.DAY_MS, expiry=EXP + 40 * ts.DAY_MS)}
    entries = [(D0, False), (D0, True), (D0 + 7 * ts.DAY_MS, False), (D0 + 14 * ts.DAY_MS, True)]
    built, why = sv.pairs_for(days, {EXP: 100.0}, entries, P, outcome=False)
    assert len(built) == 2 and "r" not in built[0]
    assert why == {"no expiry in window": 1, "no surface": 1}
    got, _ = sv.pairs_for(days, {}, entries[:1], P)
    assert got == []


def test_no_lookahead_later_data_does_not_change_earlier_pairs():
    e2 = EXP + 7 * ts.DAY_MS
    d2 = D0 + 7 * ts.DAY_MS
    days = {D0: surf(D0), D0 + 3 * ts.DAY_MS: surf(D0 + 3 * ts.DAY_MS, spot=104.0), d2: surf(d2, expiry=e2)}
    delivery = {EXP: 107.0, e2: 95.0}
    entries = [(D0, False), (D0, True), (d2, False)]
    for exit_ in ("expiry", "managed"):
        Q = {**P, "exit": exit_}
        base, _ = sv.pairs_for(days, delivery, entries, Q)
        first = [x for x in base if x["expiry"] == EXP]
        assert len(first) == 2
        later = dict(days)
        # d2 is before EXP, but its surface lists only e2, so it cannot price the first pairs
        later[d2] = surf(d2, spot=30.0, iv=1.5, expiry=e2)
        # data after the first expiry: a later surface and the second expiry's delivery price
        later[EXP + ts.DAY_MS] = surf(EXP + ts.DAY_MS, spot=10.0, expiry=e2)
        changed, _ = sv.pairs_for(later, {EXP: 107.0, e2: 300.0}, entries, Q)
        assert [x for x in changed if x["expiry"] == EXP] == first


def test_p6_labels():
    assert sv.p6_labels("✓", "✗", "◯") == {
        "capital": "spread loses less per capital",
        "usd": "sold leg does not earn after costs",
        "p3_side": "insufficient",
    }
    assert sv.p6_labels("≈", "◯", "✗")["capital"] == "insufficient"
