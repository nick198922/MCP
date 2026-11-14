"""Tests for analytics.calendar_p10 (playbook rule P10: front inversion -> calendar / diagonal)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.calendar_p10 import (
    arb_check,
    batch_raw_p,
    clean_iv,
    entry_cost,
    event_times_ms,
    exec_ivs,
    inversion_at,
    month_expiry,
    plan_calendar,
    plan_diagonal,
    settle_at_front,
    variance_ok,
    verdict,
)
from tradedesk.analytics.option_ideas import fee
from tradedesk.analytics.options import black76_price

DAY = 86_400_000
HOUR = 3_600_000
SAT = int(datetime(2026, 8, 8, tzinfo=UTC).timestamp() * 1000)  # a Saturday
T0 = SAT + 9 * HOUR


def ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def expiry(F: float, dte: float, ivs: dict[float, float]) -> dict:
    return {"F": F, "T": dte / 365, "strikes": {k: {"mark_iv": v, "sell_iv": None, "buy_iv": None} for k, v in ivs.items()}}


def flat(F: float, dte: float, iv: float, ks: list[float]) -> dict:
    return expiry(F, dte, dict.fromkeys(ks, iv))


KS = [float(k) for k in range(90_000, 110_001, 1_000)]


def snap(exps: dict[float, dict], t_ms: int = T0, spot: float = 100_000.0) -> dict:
    """Surface whose expiries are keyed by DTE from ``t_ms`` (expiry ms = t + dte days)."""
    return {"day": t_ms - 9 * HOUR, "t_ms": t_ms, "spot": spot, "expiries": {int(t_ms + d * DAY): x for d, x in exps.items()}}


def term(front_iv: float = 0.60, month_iv: float = 0.55) -> dict:
    return snap(
        {
            0.96: flat(100_000, 0.96, front_iv, KS),
            5.96: flat(100_000, 5.96, front_iv, KS),
            19.96: flat(100_000, 19.96, month_iv, KS),
            26.96: flat(100_000, 26.96, month_iv, KS),
            47.96: flat(100_000, 47.96, month_iv, KS),
        }
    )


# ----------------------------------------------------------------------------- condition


def test_event_times_et_to_utc_with_dst_and_filters() -> None:
    rows = [
        {"date": "2021-01-13", "kind": "cpi", "time_et": "08:30", "status": "actual"},
        {"date": "2021-06-16", "kind": "fomc", "time_et": "14:00", "status": "actual"},
        {"date": "2021-01-08", "kind": "nfp", "time_et": "08:30", "status": "actual"},
        {"date": "2021-03-17", "kind": "fomc", "time_et": "14:00", "status": "cancelled"},
    ]
    assert event_times_ms(rows) == [ms(2021, 1, 13, 13, 30), ms(2021, 6, 16, 18, 0)]


def test_month_expiry_nearest_30_in_band() -> None:
    s = term()
    assert month_expiry(s) == int(T0 + 26.96 * DAY)
    s2 = snap({5.96: flat(1e5, 5.96, 0.6, KS), 19.96: flat(1e5, 19.96, 0.5, KS), 47.96: flat(1e5, 47.96, 0.5, KS)})
    assert month_expiry(s2) is None


def test_inversion_met_front_after_event() -> None:
    inv = inversion_at(term(0.60, 0.55), [T0 + int(4.4 * DAY)])
    assert inv["state"] == "measured" and inv["met"] is True
    assert inv["front"] == int(T0 + 5.96 * DAY) and inv["month"] == int(T0 + 26.96 * DAY)
    assert inv["diff"] == pytest.approx(0.05)


def test_inversion_below_threshold_and_states() -> None:
    assert inversion_at(term(0.56, 0.55), [T0 + int(4.4 * DAY)])["met"] is False  # +1 point < 1.5
    no_front = inversion_at(term(), [T0 + int(6.5 * DAY)])  # nothing between 6.5 and 8 days
    assert no_front["state"] == "no_front" and no_front["met"] is False
    assert inversion_at(term(), [T0 + 9 * DAY])["state"] == "no_event"
    s = snap({5.96: flat(1e5, 5.96, 0.6, KS), 47.96: flat(1e5, 47.96, 0.5, KS)})
    nm = inversion_at(s, [T0 + 4 * DAY])
    assert nm["state"] == "no_month" and nm["met"] is None


def test_inversion_ignores_past_and_far_events() -> None:
    """No look-ahead / no stale data: events before t or >= 8 days ahead never change the condition."""
    base = inversion_at(term(), [T0 + int(4.4 * DAY)])
    more = inversion_at(term(), [T0 - HOUR, T0 + int(4.4 * DAY), T0 + 8 * DAY, T0 + 30 * DAY])
    assert more == base


# ----------------------------------------------------------------------------- static arbitrage


def p12_like() -> dict:
    """Mark at 22500 too low (the 2022-06-18 case of the P12 review): C(22500) < C(23000)."""
    return expiry(20_500, 13, {21_500: 1.10, 22_000: 1.034, 22_500: 0.834, 23_000: 0.968, 23_500: 0.95})


def test_arb_check_clean_on_flat_smile() -> None:
    x = flat(100_000, 10, 0.5, KS)
    c = arb_check(x, 100_000, 0.5)
    assert c["ok"] and c["sides"] == 2
    assert arb_check(x, 90_000, 0.5)["sides"] == 1


def test_arb_check_flags_monotonicity_and_replaces_by_interpolation() -> None:
    x = p12_like()
    c = arb_check(x, 22_500, 0.834)
    assert not c["ok"] and any(v.startswith("monotone") for v in c["violations"])
    w = math.log(22_500 / 22_000) / math.log(23_000 / 22_000)
    iv, st = clean_iv(x, 22_500)
    assert st == "replaced" and iv == pytest.approx(1.034 + w * (0.968 - 1.034))
    assert clean_iv(x, 22_500, rule="skip") == (None, "skip")
    iv_raw, st_raw = clean_iv(x, 22_500, rule="skip", at_exit=True)
    assert st_raw == "raw" and iv_raw == pytest.approx(0.834)


def test_arb_check_vertical_wider_than_gap() -> None:
    # A call priced above its lower neighbour by more than the strike gap is impossible; build it with a huge IV below.
    x = expiry(100_000, 10, {99_000: 0.01, 100_000: 3.0, 101_000: 0.5})
    c = arb_check(x, 100_000, 3.0)
    assert not c["ok"]


def test_variance_ok() -> None:
    assert variance_ok(0.60, 6 / 365, 0.55, 27 / 365)
    assert not variance_ok(1.20, 6 / 365, 0.50, 7 / 365)


# ----------------------------------------------------------------------------- construction and P&L


def test_calendar_debit_and_settlement_analytic() -> None:
    s = term(0.5, 0.5)
    inv = inversion_at(s, [T0 + int(4.4 * DAY)], threshold=0.0)
    plan, why = plan_calendar(s, inv)
    assert why == "" and plan["K"] == 100_000
    ivs, fb = exec_ivs(plan, "mark", hs=0.0)
    assert fb == 0 and ivs == [0.5] * 4
    debit = entry_cost(plan, ivs)
    c_m, c_f = (black76_price(1e5, 1e5, d / 365, 0.5, True) for d in (26.96, 5.96))
    exp_debit = 2 * c_m - 2 * c_f + 2 * fee(1e5, c_m) + 2 * fee(1e5, c_f)
    assert debit == pytest.approx(exp_debit)
    # exit at the front expiry: month leg 21 days left, flat 0.5, hs 0; delivery at the strike -> no front intrinsic
    front_ms = plan["expiry"]["f"]
    ex = {"day": front_ms - 8 * HOUR, "t_ms": front_ms + HOUR, "spot": 1e5, "expiries": {plan["expiry"]["m"]: flat(1e5, 21, 0.5, KS)}}
    res, why = settle_at_front(plan, debit, 100_000.0, ex, hs=0.0)
    c_x = black76_price(1e5, 1e5, 21 / 365, 0.5, True)
    assert why == "" and res["pnl"] == pytest.approx(-debit + 2 * c_x - 2 * fee(1e5, c_x))
    assert res["r"] == pytest.approx(res["pnl"] / debit)
    # delivery 105000: the sold call is 5000 ITM, delivery fee min(0.015 % x 105000, 12.5 % x 5000)
    res2, _ = settle_at_front(plan, debit, 105_000.0, ex, hs=0.0)
    assert res2["pnl"] - res["pnl"] == pytest.approx(-5000 - 0.00015 * 105_000)


def test_settle_needs_exit_surface_after_front() -> None:
    s = term(0.5, 0.5)
    plan, _ = plan_calendar(s, inversion_at(s, [T0 + int(4.4 * DAY)], threshold=0.0))
    assert settle_at_front(plan, 100.0, 1e5, None) == (None, "no exit surface")
    early = {"day": SAT, "t_ms": T0 + DAY, "spot": 1e5, "expiries": {}}
    with pytest.raises(ValueError):
        settle_at_front(plan, 100.0, 1e5, early)


def test_calendar_needs_common_fresh_strike_and_variance() -> None:
    s = term(0.5, 0.5)
    m = int(T0 + 26.96 * DAY)
    s["expiries"][m] = flat(100_000, 26.96, 0.5, [80_000.0, 120_000.0])
    inv = inversion_at(s, [T0 + int(4.4 * DAY)], threshold=0.0)
    assert plan_calendar(s, inv) == (None, "no common fresh strike")
    assert plan_calendar(s, inv, month_interp=True)[0] is not None
    s2 = term(2.5, 0.5)  # front total variance above the month's
    inv2 = inversion_at(s2, [T0 + int(4.4 * DAY)])
    assert plan_calendar(s2, inv2) == (None, "calendar variance")


def test_diagonal_strikes_not_closer_than_bought() -> None:
    s = term(0.6, 0.5)
    plan, why = plan_diagonal(s, inversion_at(s, [T0 + int(4.4 * DAY)]), qty=0.5)
    assert why == ""
    K = plan["K"]
    assert K["mc"] > 1e5 > K["mp"] and K["fc"] >= K["mc"] and K["fp"] <= K["mp"]
    assert [leg["qty"] for leg in plan["legs"]] == [1.0, 1.0, -0.5, -0.5]
    assert entry_cost(plan, exec_ivs(plan, "mark")[0]) > 0


def test_exec_ivs_side_model_fallback() -> None:
    s = term(0.5, 0.5)
    plan, _ = plan_calendar(s, inversion_at(s, [T0 + int(4.4 * DAY)], threshold=0.0))
    plan["legs"][0]["buy"] = 0.52
    ivs, fb = exec_ivs(plan, "side", hs=0.015)
    assert ivs == pytest.approx([0.52, 0.515, 0.485, 0.485]) and fb == 3


def test_plan_does_not_depend_on_later_surfaces() -> None:
    """The entry plan reads only the entry surface; later surfaces enter only through the exit surface."""
    s = term(0.6, 0.55)
    inv = inversion_at(s, [T0 + int(4.4 * DAY)])
    p1, _ = plan_calendar(s, inv)
    later = term(0.9, 0.9)  # a different, later surface never passed to the plan
    assert later is not s
    p2, _ = plan_calendar(s, inv)
    assert p1 == p2


# ----------------------------------------------------------------------------- batch and verdict


def test_batch_raw_p_degenerate_as_one() -> None:
    raw = {"P1-H1": 0.0, "P3-H1": 0.042, "P10-H1": 0.2}
    assert batch_raw_p(raw, ["P1-H1"]) == {"P1-H1": 1.0, "P3-H1": 0.042, "P10-H1": 0.2}
    with pytest.raises(ValueError):
        batch_raw_p(raw, ["P9-H1"])


def V(**kw: float | bool) -> str:
    base = {
        "mean": 0.1,
        "lo": 0.02,
        "hi": 0.2,
        "holm_p": 0.01,
        "is_mean": 0.1,
        "oos_mean": 0.1,
        "stress_cost_mean": 0.05,
        "edge": 0.10,
        "tail_ok": True,
        "coverage_ok": True,
    }
    return verdict(**{**base, **kw})


def test_verdict_rules() -> None:
    assert V() == "✓"
    assert V(holm_p=0.2) == "≈"  # lo > 0 but Holm fails: ≈ by the registered text
    assert V(stress_cost_mean=-0.01) == "≈"
    assert V(lo=-0.05) == "≈"
    assert V(lo=-0.05, oos_mean=-0.01) == "◯"
    assert V(mean=-0.05, lo=-0.2, hi=0.05) == "✗"
    assert V(mean=-0.05, lo=-0.2, hi=0.15) == "◯"
    assert V(mean=-0.1, lo=-0.2, hi=-0.01) == "✗"
    assert V(tail_ok=False) == "◯"
    assert V(coverage_ok=False, lo=-0.05) == "◯"
    assert V(coverage_ok=False, mean=-0.1, lo=-0.2, hi=-0.01) == "✗"
