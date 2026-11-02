"""Execution quality of the demo trades (analytics/demo_execution.py): analytic expectations."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics import demo_execution as de
from tradedesk.analytics.stop_management import MAKER, SLIPPAGE, TAKER

H = 3_600_000


def row(**kw):
    base = {
        "link_id": "td-S3-l-4h-X-1",
        "symbol": "X/USDT:USDT",
        "setup": "S3",
        "timeframe": "4h",
        "side": "long",
        "signal_ts": 10 * H,
        "entry": 100.0,
        "stop": 95.0,
        "target": 110.0,
        "qty": 2.0,
        "risk_usd": 10.0,  # risk unit 5 per base unit
        "status": "closed",
        "fill": 99.0,
        "opened_ts": 15 * H,  # signal bar closes at 14h: one hour later
        "exit": 110.0,
        "closed_ts": 40 * H,
        "exit_reason": "target",
        "entry_kind": "maker",
        "fees_usd": 2 * MAKER * (99.0 + 110.0),
        "be_ts": None,
        "be_stop": None,
    }
    base.update(kw)
    base["pnl_usd"] = kw.get(
        "pnl_usd", (1 if base["side"] == "long" else -1) * base["qty"] * (base["exit"] - base["fill"]) - base["fees_usd"]
    )
    return base


def test_long_target_trade():
    m = de.trade_metrics(row(), now_ms=100 * H)
    assert m["exec_edge_r"] == pytest.approx(0.2)  # filled 1 below the planned 100, risk unit 5
    assert m["latency_min"] == pytest.approx(60)
    assert m["hours_to_close"] == pytest.approx(25)
    assert m["model_fee_r"] == pytest.approx(2 * MAKER * 209 / 10)
    assert m["fee_gap_r"] == pytest.approx(0)
    assert m["stop_gap_r"] is None and m["target_slip_r"] == pytest.approx(0)
    assert m["pnl_gap_r"] == pytest.approx(0) and not m["pnl_flag"]
    assert m["gap_r"] == pytest.approx(0.2)


def test_short_stop_after_break_even_with_slippage_and_taker_fees():
    # short: planned 100, filled 100.5 (better by 0.5 / 5 = +0.1R); break-even stop 99.8, exited at 100.3 (0.5 worse)
    fees = 2 * (TAKER * 100.5 + TAKER * 100.3)
    m = de.trade_metrics(
        row(
            side="short",
            stop=105.0,
            target=90.0,
            fill=100.5,
            exit=100.3,
            exit_reason="breakeven",
            be_ts=20 * H,
            be_stop=99.8,
            fees_usd=fees,
        ),
        now_ms=100 * H,
    )
    assert m["exec_edge_r"] == pytest.approx(0.1)
    assert m["stop_level"] == pytest.approx(99.8)
    assert m["stop_slip_r"] == pytest.approx(-0.1)
    assert m["model_slip_r"] == pytest.approx(-SLIPPAGE * 99.8 / 5)
    assert m["stop_gap_r"] == pytest.approx(-0.1 + SLIPPAGE * 99.8 / 5)
    # model: maker entry, taker exit; actual: taker both sides
    assert m["fee_gap_r"] == pytest.approx(2 * (MAKER - TAKER) * 100.5 / 10)
    assert m["gap_r"] == pytest.approx(m["exec_edge_r"] + m["fee_gap_r"] + m["stop_gap_r"])


def test_stop_level_without_break_even_and_pnl_flag():
    m = de.trade_metrics(row(exit=94.0, exit_reason="stop", pnl_usd=+5.0), now_ms=100 * H)
    assert m["stop_level"] == 95.0
    assert m["stop_slip_r"] == pytest.approx(-0.2)
    expected = (5.0 - (2 * (94.0 - 99.0) - row()["fees_usd"])) / 10
    assert m["pnl_gap_r"] == pytest.approx(expected) and m["pnl_flag"]


def test_open_or_later_trades_are_ignored_and_earlier_results_do_not_move():
    early = row(link_id="a", closed_ts=40 * H)
    late = row(link_id="b", closed_ts=200 * H, fill=98.0)
    now = 100 * H
    assert de.trade_metrics(row(status="open"), now) is None
    assert de.trade_metrics(late, now) is None
    base = de.metrics([early, late], now)
    changed = de.metrics([early, {**late, "fill": 50.0, "exit": 1.0, "fees_usd": 99.0}], now)
    assert base == changed and [m["link_id"] for m in base] == ["a"]


def test_stress_gap_adds_slippage_on_market_sides_only():
    target = de.trade_metrics(row(), 100 * H)
    assert de.stress_gap(target, 0.001) == pytest.approx(target["gap_r"])  # maker entry, maker target: untouched
    stop = de.trade_metrics(row(exit=95.0, exit_reason="stop", entry_kind="taker"), 100 * H)
    assert de.stress_gap(stop, 0.001) == pytest.approx(stop["gap_r"] - 0.001 * 95.0 / 5 - 0.001 * 99.0 / 5)


def test_missing_numbers_give_none():
    assert de.trade_metrics({**row(), "fill": None}, 100 * H) is None
    m = de.trade_metrics({**row(), "fees_usd": None}, 100 * H)
    assert m["fee_gap_r"] is None and m["pnl_gap_r"] is None and m["gap_r"] == pytest.approx(0.2)


def test_t975_and_describe():
    assert de.t975(1) == pytest.approx(12.706)
    assert de.t975(30) == pytest.approx(2.042)
    assert de.t975(1000) == pytest.approx(1.962, abs=1e-3)
    d = de.describe([1.0, -1.0, 1.0, -1.0], [0, 0, 1, 1], n_boot=200)
    assert d["mean"] == 0 and d["median"] == 0
    assert d["sd"] == pytest.approx(math.sqrt(4 / 3))
    assert d["t"][1] == pytest.approx(3.182 * math.sqrt(4 / 3) / 2)
    assert d["n_needed"] == math.ceil((1.959964 * math.sqrt(4 / 3) / 0.05) ** 2)
    assert d["n_days"] == 2


@pytest.mark.parametrize(
    ("mean", "ci", "want"),
    [
        (0.01, (-0.03, 0.04), "✓"),
        (-0.10, (-0.15, -0.06), "✗-"),
        (0.10, (0.06, 0.14), "✗+"),
        (-0.03, (-0.06, -0.01), "≈"),
        # CI inside ±0.05 and without 0 fits both ✓ and ≈: ✓ has priority (systematic but immaterial shift)
        (-0.009, (-0.014, -0.006), "✓"),
        (0.0, (-0.2, 0.2), "◯"),
        (None, None, "◯"),
    ],
)
def test_verdict(mean, ci, want):
    assert de.verdict(mean, ci) == want


def _sig(ts, lid, side, entry, stop, plan, rr, setup="S3"):
    return {
        "ts": ts,
        "link_id": lid,
        "setup": setup,
        "payload": {"signal": {"side": side, "entry": entry, "stop": stop}, "proposal": {"entry": plan, "rr": rr}},
    }


def test_decision_edges_groups_signs_and_time():
    rows = [
        _sig(1_000, "a", "long", 100.0, 90.0, 98.0, 2.0),  # long, plan 2 below the signal entry: +0.2R
        _sig(2_000, "b", "short", 100.0, 110.0, 101.0, 1.2),  # short, plan 1 above: +0.1R; refused by R:R
        _sig(3_000, "c", "long", 100.0, 90.0, 103.0, 1.8),  # long, plan 3 above: -0.3R; refused otherwise
        _sig(3_500, "a", "long", 100.0, 90.0, 50.0, 9.0),  # repeat of a link: ignored
        _sig(3_600, "d", "long", 100.0, 90.0, 95.0, 3.0, setup="S12"),  # skipped setup
        _sig(3_700, "e", "long", 100.0, 100.0, 95.0, 3.0),  # zero risk: dropped
        {"ts": 3_800, "link_id": "f", "setup": "S3", "payload": {}},  # no numbers: dropped
    ]
    out = de.decision_edges(rows, {"a"}, now_ms=10_000)
    got = {x["link_id"]: (x["group"], round(x["edge_r"], 9)) for x in out}
    assert got == {"a": ("taken", 0.2), "b": ("refused_rr", 0.1), "c": ("refused_other", -0.3)}
    # rows after now are not seen; earlier results do not move when later data changes
    early = de.decision_edges(rows, {"a"}, now_ms=2_500)
    later = rows[:2] + [_sig(2_600, "z", "short", 1.0, 2.0, 0.5, 0.1)] + rows[2:]
    assert de.decision_edges(later, {"a"}, now_ms=2_500) == early
    assert [x["link_id"] for x in early] == ["a", "b"]
