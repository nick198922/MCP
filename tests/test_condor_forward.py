"""Tests of analytics/condor_forward.py (item 8j registration of IDEA-20261031-structural-edges-depth)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from tradedesk.analytics.condor_forward import (
    condor_cost,
    cost_verdict,
    friday_entry,
    leg_cost,
    mde,
    power,
    ratio_ci,
    ratio_of_sums,
    thursday_exit,
    weeks_for_mde,
)
from tradedesk.analytics.options import black76_greeks
from tradedesk.analytics.put_skew import inverse_price

FRI = date(2026, 11, 6)
F = 100_000.0
TAU = 6.96
EXP = int(datetime(2026, 11, 13, 8, tzinfo=UTC).timestamp())
TS = int(datetime(2026, 11, 6, 9, 0, 5, tzinfo=UTC).timestamp())


def row(role: str, K: float, opt: str, *, iv: float = 0.5, half: float = 0.0002, **kw):
    T = TAU / 365.0
    mark = inverse_price(F, K, T, iv, opt == "C")
    r = {
        "currency": "BTC",
        "day": FRI.isoformat(),
        "role": role,
        "ts": TS,
        "expiry_ts": EXP,
        "tau_days": TAU,
        "spot": F,
        "forward": F,
        "opt": opt,
        "strike": K,
        "dist": 0.0,
        "src": "ticker",
        "bid": mark - half,
        "ask": mark + half,
        "mark": mark,
        "mark_iv": iv,
    }
    r.update(kw)
    return r


def condor(**kw):
    return [row("lp", 85_000, "P", **kw), row("sp10", 92_000, "P", **kw), row("sc10", 108_000, "C", **kw), row("lc", 115_000, "C", **kw)]


def test_model_cost_is_vega_times_shift():
    r = row("sp10", 92_000, "P")
    T = TAU / 365.0
    vega_coin = black76_greeks(F, 92_000, T, 0.5, False)["vega"] / F  # USD per 1 vol point / F
    actual, model = leg_cost(r, -1, "open", hs=0.0001)
    assert actual == pytest.approx(0.0002, abs=1e-12)  # sold at the bid: mark - bid
    assert model == pytest.approx(vega_coin * 0.01, rel=1e-3)  # 0.0001 = 0.01 vol points


def test_close_swaps_the_sides():
    r = row("sp10", 92_000, "P", bid=None)
    assert leg_cost(r, -1, "open") is None  # selling needs the bid
    a, _ = leg_cost(r, -1, "close")  # buying back uses the ask
    assert a == pytest.approx(0.0002, abs=1e-12)
    assert leg_cost(row("lp", 85_000, "P", src="summary"), 1) is None


def test_condor_cost_sums_four_legs():
    legs, why = friday_entry(condor(), FRI, "BTC")
    assert why == "ok"
    c = condor_cost(legs)
    assert c["actual"] == pytest.approx(4 * 0.0002, abs=1e-12)
    assert c["model"] > 0 and c["ratio"] == pytest.approx(c["actual"] / c["model"])


def test_reading_rule_cutoff_band_and_order():
    late = int(datetime(2026, 11, 6, 10, 0, 1, tzinfo=UTC).timestamp())
    rows = condor()
    rows[1] = {**rows[1], "ts": late}
    assert friday_entry(rows, FRI, "BTC") == (None, "no row: sp10")
    rows = condor()
    rows[3] = {**rows[3], "dist": 0.016}
    assert friday_entry(rows, FRI, "BTC")[1] == "outside +-1.5 %: lc"
    rows = condor()
    rows[2] = {**rows[2], "strike": 99_000.0}  # short call below the forward
    assert friday_entry(rows, FRI, "BTC")[1] == "strike order"
    assert friday_entry(condor(), date(2026, 11, 7), "BTC")[1] == "not a Friday"
    assert friday_entry(condor(), FRI, "ETH")[0] is None


def test_rows_after_the_cutoff_do_not_change_the_entry():
    base, _ = friday_entry(condor(), FRI, "BTC")
    later = [{**r, "day": "2026-11-07", "role": "hold_" + r["role"], "ts": TS + 86_400, "bid": 0.5} for r in condor()] + [
        {**r, "ts": TS + 7_200, "bid": 0.9} for r in condor()
    ]  # same Friday, 11:00 UTC: after the cutoff
    after, why = friday_entry(condor() + later, FRI, "BTC")
    assert why == "ok" and after == base
    assert condor_cost(after) == condor_cost(base)


def test_entry_ignores_another_expiry_and_keeps_the_earliest_row():
    rows = condor()
    rows[0] = {**rows[0], "expiry_ts": EXP + 7 * 86_400}  # lp of the 2-week expiry
    assert friday_entry(rows, FRI, "BTC") == (None, "no row: lp")
    assert friday_entry(condor() + [rows[0]], FRI, "BTC")[0] == friday_entry(condor(), FRI, "BTC")[0]
    early = {**condor()[1], "ts": TS - 600, "bid": 0.001}  # sp10 seen twice by 10:00 UTC
    for order in ([early, *condor()], [*condor(), early]):
        legs, why = friday_entry(order, FRI, "BTC")
        assert why == "ok" and legs["sp10"]["ts"] == TS - 600  # independent of the iteration order


def test_thursday_exit_takes_the_entry_instruments_by_the_cutoff():
    ent = [{**r, "instrument": f"BTC-{r['strike']:.0f}-{r['opt']}"} for r in condor()]
    legs, _ = friday_entry(ent, FRI, "BTC")
    thu_ts = int(datetime(2026, 11, 12, 9, 0, 5, tzinfo=UTC).timestamp())
    hold = [{**r, "role": "hold_" + r["role"], "day": "2026-11-12", "ts": thu_ts, "tau_days": 0.96} for r in ent]
    ex, why = thursday_exit(hold, legs, FRI, "BTC")
    assert why == "ok" and ex["sp10"]["tau_days"] == 0.96
    other = [{**hold[0], "instrument": "BTC-80000-P"}] + hold[1:]
    assert thursday_exit(other, legs, FRI, "BTC")[1] == "no hold row: lp"
    late = [{**hold[3], "ts": thu_ts + 3_600}] + hold[:3]
    assert thursday_exit(late, legs, FRI, "BTC")[1] == "no hold row: lc"
    dup = [{**hold[1], "ts": thu_ts - 600, "tau_days": 0.97}]
    for order in (dup + hold, hold + dup):
        assert thursday_exit(order, legs, FRI, "BTC")[0]["sp10"]["tau_days"] == 0.97  # earliest ts wins
    assert condor_cost(ex, "close")["actual"] == pytest.approx(4 * 0.0002, abs=1e-12)


def test_ratio_ci_and_verdict():
    assert ratio_of_sums([1, 2, 3], [2, 2, 2]) == pytest.approx(1.0)
    assert math.isnan(ratio_of_sums([1], [0]))
    lo, hi = ratio_ci([0.5] * 8, [1.0] * 8)
    assert lo == hi == pytest.approx(0.5)
    assert cost_verdict(0.4, 0.9, 8) == "conservative"
    assert cost_verdict(1.1, 1.6, 8) == "optimistic"
    assert cost_verdict(0.8, 1.2, 8) == "consistent"
    assert cost_verdict(0.1, 0.2, 5) == "◯"


def test_mde_arithmetic():
    assert mde(1.0, 49) == pytest.approx(0.4)
    assert weeks_for_mde(1.0, 0.28) == 100  # (2.8 / 0.28)^2 exactly
    assert power(1.0, 49, 0.4) == pytest.approx(0.80, abs=0.003)  # MDE is the 80 %-power effect (2.8 = 1.96 + 0.84)
    assert power(1.0, 49, 1.959964 / 7) == pytest.approx(0.5)
    n = weeks_for_mde(0.42, 0.13)
    assert mde(0.42, n) <= 0.13 < mde(0.42, n - 1)
