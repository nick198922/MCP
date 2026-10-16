"""Hourly replay of a trading call (analytics/trade_replay.py)."""

import pytest

from tradedesk.analytics.trade_replay import Bar, replay

H = 3_600_000
T0 = 1_000 * H


def bars(*ohlc, t0=T0):
    return [Bar(t0 + i * H, *x) for i, x in enumerate(ohlc)]


def test_long_limit_fills_then_hits_target():
    b = bars((105, 106, 101, 102), (102, 103, 99.5, 100), (100, 111, 100, 110))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=95, target=110, cost_frac=0.0)
    assert r.status == "target" and r.fill == 100 and r.fill_t == T0 + H
    assert r.r_gross == pytest.approx(2.0) and r.r_net == pytest.approx(2.0)
    assert r.bars_to_exit == 1 and r.mae_r == pytest.approx(-0.1)


def test_gap_through_limit_fills_at_open_and_costs_are_in_r():
    b = bars((98, 99, 97, 98), (98, 111, 98, 110))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=96, target=110, cost_frac=0.002)
    assert r.fill == 98 and r.status == "target"
    # the size is planned on |100 - 96| = 4, so the better fill earns 12 / 4 R
    assert r.r_gross == pytest.approx(12 / 4) and r.r_net == pytest.approx(3 - 0.002 * 98 / 4)


def test_gap_fill_then_stop_loses_less_than_1r():
    b = bars((99, 99.5, 98.5, 99), (99, 99.2, 97, 97.5))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=98, target=None, cost_frac=0.0)
    assert r.fill == 99 and r.status == "stop" and r.r_gross == pytest.approx(-0.5)


def test_bars_before_start_are_ignored_and_market_entry_uses_first_open():
    b = bars((50, 200, 1, 100), (101, 102, 100, 101), (101, 103, 101, 102), (102, 102, 102, 102))
    r = replay(b, side="long", start_ms=T0 + H, entry=None, stop=99, target=None, hold_ms=2 * H, cost_frac=0.0)
    assert r.fill == 101 and r.status == "time" and r.exit == 102 and r.r_gross == pytest.approx(0.5)


def test_stop_on_fill_bar_counts_and_target_does_not():
    b = bars((101, 112, 94, 100))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=95, target=110, cost_frac=0.0)
    assert r.status == "stop" and r.exit == 95 and r.r_gross == pytest.approx(-1.0)


def test_bar_with_both_stop_and_target_is_a_stop_and_paper_sees_the_target():
    b = bars((100, 101, 99.5, 100), (100, 111, 94, 105))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=95, target=110, cost_frac=0.0)
    assert r.status == "stop" and r.r_gross == pytest.approx(-1.0)
    assert r.paper_status == "target" and r.target_after_stop is True and r.paper_mae_r == pytest.approx(-1.2)


def test_stop_gap_fills_at_open():
    b = bars((100, 101, 99.5, 100), (90, 92, 89, 91))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=95, target=None, cost_frac=0.0)
    assert r.status == "stop" and r.exit == 90 and r.r_gross == pytest.approx(-2.0)


def test_short_mirror():
    b = bars((98, 100.5, 97, 99), (99, 99.5, 89, 90))
    r = replay(b, side="short", start_ms=T0, entry=100, stop=105, target=90, cost_frac=0.0)
    assert r.status == "target" and r.fill == 100 and r.r_gross == pytest.approx(2.0)


def test_no_fill_and_waiting():
    b = bars(*[(105, 106, 104, 105)] * 4)
    assert replay(b, side="long", start_ms=T0, entry=100, stop=95, target=110, fill_ms=3 * H).status == "no_fill"
    assert replay(b, side="long", start_ms=T0, entry=100, stop=95, target=110, fill_ms=5 * H).status == "open"
    assert replay([], side="long", start_ms=T0, entry=100, stop=95, target=110).status == "no_data"


def test_time_exit_and_paper_without_stop():
    b = bars((100, 100.5, 99.9, 100), (100, 101, 90, 92), (92, 93, 91, 92.5), (92, 92, 92, 92))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=None, target=110, hold_ms=3 * H, cost_frac=0.0)
    assert r.status == "time" and r.r_gross is None and r.ret == pytest.approx(-0.075)
    assert r.paper_status == "time" and r.target_after_stop is None


def test_stop_on_wrong_side_is_dropped():
    b = bars((100, 101, 99, 100), (100, 102, 99, 101), (101, 101, 101, 101))
    r = replay(b, side="long", start_ms=T0, entry=None, stop=101, target=None, hold_ms=2 * H, cost_frac=0.0)
    assert r.status == "time" and r.r_gross is None


def test_bad_side():
    with pytest.raises(ValueError):
        replay([], side="buy", start_ms=0, entry=1, stop=None, target=None)


def test_minute_bars_of_the_call_hour_then_hourly_bars():
    m = 60_000
    start = T0 + 30 * m  # the call at hh:30
    minutes = [Bar(start + i * m, 100, 100.2, 99.9, 100) for i in range(30)]
    minutes[5] = Bar(start + 5 * m, 100, 100.1, 99.4, 99.6)  # the limit at 99.5 fills in the call's own hour
    hours = bars((99.6, 100, 99.6, 99.8), (99.8, 104.5, 99.7, 104), t0=T0 + H)
    r = replay(minutes + hours, side="long", start_ms=start, entry=99.5, stop=98.5, target=104.5, cost_frac=0.0)
    assert r.fill_t == start + 5 * m and r.fill == 99.5 and r.status == "target" and r.r_gross == pytest.approx(5.0)


def test_breakeven_after_one_r():
    b = bars((100, 100.5, 99.8, 100), (100, 105.5, 100, 105), (105, 105.2, 99.5, 100))
    r = replay(b, side="long", start_ms=T0, entry=100, stop=95, target=115, cost_frac=0.0, be_at_r=1.0)
    assert r.status == "breakeven" and r.exit == 100 and r.r_gross == pytest.approx(0.0)
    plain = replay(b, side="long", start_ms=T0, entry=100, stop=95, target=115, cost_frac=0.0, hold_ms=2 * H)
    assert plain.status == "time"


def test_script_plan_and_cancel_window():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import calls_hourly as ch

    start = ch._ms("2026-10-06T07:49")
    row = {"side": "long", "entry_low": "2690", "entry_high": "2701", "stop": "2680", "tp1": "2739", "cancel_utc": "2026-10-06T09:05"}
    first = Bar(start, 2707, 2708, 2705, 2706)
    entry, stop, target = ch.plan(row, [first], start)
    assert (entry, stop, target) == (2701, 2680, 2739)  # a long zone enters at its top edge
    assert ch.fill_window(row, start) == 76 * 60_000
    assert ch.fill_window({**row, "cancel_utc": ""}, start) is None
    short = {**row, "side": "short", "stop": "HIGH"}
    before = [Bar(start - 2 * H, 2700, 2720, 2690, 2710)]
    entry, stop, _ = ch.plan(short, before + [first], start)
    assert entry == 2690 and stop == pytest.approx(2720 * 1.001)
