"""Direct tests of analytics functions that feed the forward reports and had no direct test (coverage audit item 67,
docs/changes/2026-11-04-analytics-coverage-audit.md): the carry A / B accounting of the paper engine
(analytics/carry_rules.py), the weekly closed-trade stats (analytics/carry_report.py) and the collector-health alarm
filter (analytics/collector_health.py). Expected values are worked out by hand in the comments (analytical references)."""

from __future__ import annotations

import pytest

from tradedesk.analytics.carry_report import closed_stats
from tradedesk.analytics.carry_rules import carry_mark, carry_step, carry_trade, spread_mark, spread_step, spread_trade
from tradedesk.analytics.collector_health import alarms

T0, T1, T2 = 1_000, 2_000, 3_000


def _carry_pos(**kw) -> dict:
    """An open strategy-A position: spot entry 100, perp entry 101, entry side cost 0.002 (fraction of notional)."""
    p = {
        "symbol": "X", "open_ts": T0, "entry_apr": 0.3, "spot_e": 100.0, "perp_e": 101.0, "spot_last": 100.0,
        "perp_last": 101.0, "funding": 0.0, "fees": 0.002, "last": T0, "last_bar": T0, "max_runup": 0.0,
        "max_premium_rise": 0.0,
    }  # fmt: skip
    return p | kw


def _spread_pos(d: int = 1, **kw) -> dict:
    """An open strategy-B position: leg A entry 100, leg B entry 50, entry side cost 0.001."""
    p = {
        "symbol": "Y", "open_ts": T0, "dir": d, "entry_spread": 0.3, "a_e": 100.0, "b_e": 50.0, "a_last": 100.0,
        "b_last": 50.0, "funding": 0.0, "fees": 0.001, "last": T0, "last_bar": T0, "max_up": 0.0, "max_down": 0.0,
        "max_gap": 0.0,
    }  # fmt: skip
    return p | kw


# ------------------------------------------------------------------------------------------------ A: carry_mark


def test_carry_mark_accrues_short_perp_funding_and_extremes():
    p = _carry_pos()
    # 0.0001 x 102 / 100 + 0.0002 x 101 / 100 (no price at ts -> the last perp price, BEFORE this call's update)
    carry_mark(p, T1, payments=[(T1 - 10, 0.0001, 102.0), (T1, 0.0002, None)], perp_high=111.1, spot=110.0, perp=112.0)
    assert p["funding"] == pytest.approx(0.000102 + 0.000202)
    assert p["max_runup"] == pytest.approx(111.1 / 101.0 - 1.0)  # 0.1
    assert p["max_premium_rise"] == pytest.approx(112.0 / 110.0 - 101.0 / 100.0)  # premium 1.01 -> 1.01818
    assert (p["last"], p["last_bar"], p["spot_last"], p["perp_last"]) == (T1, T1, 110.0, 112.0)


def test_carry_mark_without_bar_or_prices_keeps_them_and_uses_last_perp_price():
    p = _carry_pos(perp_last=112.0, spot_last=110.0, max_runup=0.1)
    carry_mark(p, T2, payments=[(T2, -0.0005, None)], perp_high=None, spot=None, perp=None)
    assert p["funding"] == pytest.approx(-0.0005 * 112.0 / 100.0)  # negative funding: the short perp pays
    assert (p["last"], p["last_bar"]) == (T2, T0)  # no bar -> last_bar stays
    assert (p["spot_last"], p["perp_last"], p["max_runup"]) == (110.0, 112.0, 0.1)


# ----------------------------------------------------------------------------------------------- A: carry_trade


def test_carry_trade_pnl_is_funding_plus_basis_minus_both_sides_costs():
    p = _carry_pos(spot_last=110.0, perp_last=112.0, funding=0.000304)
    tr = carry_trade(p, T2, 0.002, at_end=True)
    # basis: spot +10, short perp -11 -> -1 / 100; exit cost scales with the spot move: 0.002 x 110 / 100
    assert tr["basis"] == pytest.approx(-0.01)
    assert tr["fees"] == pytest.approx(0.002 + 0.0022)
    assert tr["pnl"] == pytest.approx(0.000304 - 0.01 - 0.0042)
    assert (tr["symbol"], tr["open_ts"], tr["close_ts"], tr["open_at_end"], tr["entry_apr"]) == ("X", T0, T2, True, 0.3)


# ------------------------------------------------------------------------------------------------ A: carry_step


def test_carry_step_closes_holds_and_fills_free_slots_by_apr():
    open_pos = {"AAA": _carry_pos(symbol="AAA"), "BBB": _carry_pos(symbol="BBB")}
    marks = {
        "AAA": {"payments": [], "perp_high": None, "spot": 100.0, "perp": 101.0, "apr": 0.04},  # <= exit 0.05: close
        "BBB": {"payments": [], "perp_high": None, "spot": 100.0, "perp": 101.0, "apr": None},  # unknown: hold
    }
    entries = {
        "AAA": (0.04, 100.0, 101.0),  # just closed, below entry
        "CCC": (0.3, 1.0, 1.01),
        "DDD": (0.5, 2.0, 2.02),
        "EEE": (0.9, None, 1.0),  # no spot price: never ranked
        "FFF": (0.3, 3.0, 3.03),  # ties with CCC: symbol descending
    }
    entry_calls: list[str] = []

    def entry_obs(s):
        entry_calls.append(s)
        return entries[s]

    out = carry_step(
        open_pos, T1, ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], lambda s, p: marks[s], entry_obs,
        slots=2, entry_apr=0.2, exit_apr=0.05, side_cost=0.002,
    )  # fmt: skip
    assert "BBB" not in entry_calls  # a held symbol is not offered again
    assert out["ranked"] == [(0.5, "DDD"), (0.3, "FFF"), (0.3, "CCC")]
    assert (out["free_slots"], out["opened"]) == (1, ["DDD"])  # 2 slots - BBB held
    assert [c["symbol"] for c in out["closed"]] == ["AAA"]
    assert sorted(open_pos) == ["BBB", "DDD"]
    d = open_pos["DDD"]
    assert (d["entry_apr"], d["spot_e"], d["perp_e"], d["fees"], d["open_ts"]) == (0.5, 2.0, 2.02, 0.002, T1)
    assert [(x["symbol"], x["action"], x["reason"]) for x in out["decisions"]] == [
        ("AAA", "close", "apr<=exit"),
        ("BBB", "hold", "apr_unavailable"),
        ("DDD", "open", "apr>=entry"),
    ]


# ----------------------------------------------------------------------------------------------- B: spread_mark


def test_spread_mark_signs_funding_by_direction_and_tracks_extremes():
    p = _spread_pos(d=1)  # long A / short B
    spread_mark(
        p, T1, pay_a=[(T1, 0.0001, 102.0)], pay_b=[(T1, 0.0003, None)], ext_a=(110.0, 95.0), ext_b=(52.0, 40.0),
        pa=104.0, pb=50.0,
    )  # fmt: skip
    # long A pays 0.0001 x 102 / 100; short B receives 0.0003 x 50 (no price -> last B) / 100 (slot priced at leg A)
    assert p["funding"] == pytest.approx(-0.000102 + 0.00015)
    assert p["max_up"] == pytest.approx(0.1)  # A: 110 / 100 - 1 beats B: 52 / 50 - 1
    assert p["max_down"] == pytest.approx(0.2)  # B: 1 - 40 / 50 beats A: 1 - 95 / 100
    assert p["max_gap"] == pytest.approx(104.0 / 50.0 - 100.0 / 50.0)  # 0.08
    assert (p["last"], p["last_bar"], p["a_last"], p["b_last"]) == (T1, T1, 104.0, 50.0)


def test_spread_mark_short_a_flips_funding_sign():
    p = _spread_pos(d=-1)
    spread_mark(p, T1, pay_a=[(T1, 0.0001, 100.0)], pay_b=[(T1, 0.0003, 50.0)], ext_a=None, ext_b=None, pa=None, pb=None)
    assert p["funding"] == pytest.approx(0.0001 - 0.00015)  # short A receives, long B pays
    assert (p["max_up"], p["max_down"], p["max_gap"], p["a_last"]) == (0.0, 0.0, 0.0, 100.0)


# ---------------------------------------------------------------------------------------------- B: spread_trade


@pytest.mark.parametrize(
    ("d", "stopped", "price_pnl", "fees"),
    [
        (1, False, 0.04, 0.001 + 0.00104),  # exit cost 0.001 x 104 / 100
        (1, True, 0.04, 0.001 + 0.00104 + 0.003),  # + stop penalty
        (-1, False, -0.04, 0.001 + 0.00104),
    ],
)
def test_spread_trade_price_pnl_fees_and_stop_penalty(d, stopped, price_pnl, fees):
    p = _spread_pos(d=d, a_last=104.0, b_last=50.0, funding=0.000048)
    tr = spread_trade(p, T2, 0.001, 0.003, at_end=False, stopped=stopped)
    assert tr["price_pnl"] == pytest.approx(price_pnl)  # dir x ((104 - 100) - (50 - 50)) / 100
    assert tr["fees"] == pytest.approx(fees)
    assert tr["pnl"] == pytest.approx(0.000048 + price_pnl - fees)
    assert (tr["stopped"], tr["open_at_end"], tr["close_ts"], tr["dir"]) == (stopped, False, T2, d)


# ----------------------------------------------------------------------------------------------- B: spread_step


def test_spread_step_stop_exit_hold_and_entry_filters():
    open_pos = {
        "AAA": _spread_pos(1, symbol="AAA"),
        "BBB": _spread_pos(-1, symbol="BBB"),
        "CCC": _spread_pos(1, symbol="CCC"),
    }
    flat = {"pay_a": [], "pay_b": [], "ext_b": None, "pa": None, "pb": None}
    marks = {
        "AAA": flat | {"ext_a": (120.0, 100.0), "ra": 0.1, "rb": 0.5},  # max_up 0.2 >= stop 0.15
        "BBB": flat | {"ext_a": None, "ra": 0.10, "rb": 0.12},  # held spread -1 x 0.02 <= exit 0
        "CCC": flat | {"ext_a": None, "ra": None, "rb": 0.3},
    }
    entries = {
        "AAA": (0.1, 0.1, 1.0, 1.0),  # no spread
        "BBB": (0.5, 0.1, 10.0, 10.5),  # spread 0.4, but prices 4.8 % apart > max gap 2 %
        "DDD": (0.0, 0.3, 5.0, 5.01),  # 0.3, B higher -> +1
        "EEE": (0.2, -0.05, 3.0, 3.0),  # 0.25, A higher -> -1
        "FFF": (0.1, 0.25, 1.0, 1.0),  # 0.15 < entry 0.2
    }
    out = spread_step(
        open_pos, T1, ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], lambda s, p: marks[s], lambda s: entries[s],
        slots=3, entry_apr=0.2, exit_apr=0.0, side_cost=0.001, max_entry_gap=0.02, stop_move=0.15, stop_penalty=0.003,
    )  # fmt: skip
    closed = {c["symbol"]: c for c in out["closed"]}
    assert closed["AAA"]["stopped"] and not closed["BBB"]["stopped"]
    assert closed["AAA"]["fees"] == pytest.approx(0.001 + 0.001 + 0.003)
    assert [(s, d) for _, s, d in out["ranked"]] == [("DDD", 1), ("EEE", -1)]
    assert [x for x, _, _ in out["ranked"]] == pytest.approx([0.3, 0.25])
    assert (out["free_slots"], out["opened"]) == (2, ["DDD", "EEE"])
    assert sorted(open_pos) == ["CCC", "DDD", "EEE"]
    assert (open_pos["EEE"]["dir"], open_pos["EEE"]["a_e"], open_pos["EEE"]["fees"]) == (-1, 3.0, 0.001)
    dec = [(x["symbol"], x["action"], x["reason"]) for x in out["decisions"]]
    assert dec == [
        ("AAA", "close", "stop"),
        ("BBB", "close", "spread<=exit"),
        ("CCC", "hold", "apr_unavailable"),
        ("DDD", "open", "spread>=entry"),
        ("EEE", "open", "spread>=entry"),
    ]
    assert out["decisions"][1]["spread"] == pytest.approx(-0.02)


def test_spread_step_without_stop_never_stops():
    open_pos = {"AAA": _spread_pos(1, symbol="AAA")}
    mark = {"pay_a": [], "pay_b": [], "ext_a": (300.0, 1.0), "ext_b": None, "pa": None, "pb": None, "ra": 0.0, "rb": 0.3}
    out = spread_step(
        open_pos, T1, ["AAA"], lambda s, p: mark, lambda s: (None, None, None, None),
        slots=1, entry_apr=0.2, exit_apr=0.0, side_cost=0.001, max_entry_gap=None, stop_move=None, stop_penalty=0.003,
    )  # fmt: skip
    assert out["closed"] == [] and list(open_pos) == ["AAA"] and out["free_slots"] == 0
    assert out["decisions"][0]["reason"] == "spread>exit" and out["decisions"][0]["spread"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------------------- report: closed_stats


def _tr(ret: float, *, stopped: bool = False) -> dict:
    return {
        "return_on_notional": ret, "return_on_capital": ret / 2, "stopped": stopped, "days": 4.0,
        "funding_usd": 3.0, "price_usd": -1.0, "costs_usd": 0.5, "pnl_usd": 1.5,
    }  # fmt: skip


def test_closed_stats_even_count_median_and_shares():
    s = closed_stats([_tr(0.02), _tr(-0.01, stopped=True), _tr(0.05), _tr(0.0)])
    assert s["n"] == 4
    assert s["median_return_on_notional"] == pytest.approx(0.01)  # mean of the middle two of [-0.01, 0, 0.02, 0.05]
    assert s["mean_return_on_notional"] == pytest.approx(0.015)
    assert s["mean_return_on_capital"] == pytest.approx(0.0075)
    assert (s["share_losing"], s["share_stopped"]) == (0.25, 0.25)  # a zero return is not a loss
    assert s["mean_days"] == pytest.approx(4.0)
    assert (s["funding_usd"], s["price_or_basis_usd"], s["costs_usd"], s["pnl_usd"]) == pytest.approx((12.0, -4.0, 2.0, 6.0))


def test_closed_stats_odd_count_and_empty():
    assert closed_stats([_tr(0.03), _tr(-0.02), _tr(0.01)])["median_return_on_notional"] == pytest.approx(0.01)
    assert closed_stats([]) == {"n": 0}


# --------------------------------------------------------------------------------------- collector_health: alarms


def test_alarms_names_low_stale_never_error_in_input_order():
    rows = [{"label": s, "status": s} for s in ("ok", "error", "new", "low", "stale", "never")] + [{"label": "x"}]
    assert [r["label"] for r in alarms(rows)] == ["error", "low", "stale", "never"]
    assert alarms([]) == []
