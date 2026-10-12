from __future__ import annotations

import random

import pytest

from tradedesk.analytics.sfp_backtest import (
    ltf_entry,
    range_time_share,
    simulate_bracket,
    summarise_r,
    walk_forward_sfp,
)


def _bar(o, h, lo, c, ts=0):
    return {"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}


def test_bracket_long_hits_target():
    bars = [_bar(100, 101, 99.5, 100.5), _bar(100.5, 104, 100, 103.5)]
    res = simulate_bracket(bars, 0, "long", 100.0, 99.0, 103.0, 10)
    assert res["outcome"] == "target"
    assert res["r"] == pytest.approx(3.0)
    assert res["bars_held"] == 2


def test_bracket_both_in_same_bar_counts_as_stop():
    bars = [_bar(100, 104, 98, 100)]
    res = simulate_bracket(bars, 0, "long", 100.0, 99.0, 103.0, 10)
    assert res["outcome"] == "stop"
    assert res["both_in_bar"] is True
    assert res["r"] == pytest.approx(-1.0)


def test_bracket_short_gap_through_stop_fills_at_open():
    bars = [_bar(103, 104, 102.5, 103.5)]
    res = simulate_bracket(bars, 0, "short", 100.0, 102.0, 95.0, 10)
    assert res["outcome"] == "stop"
    assert res["exit_price"] == pytest.approx(103.0)
    assert res["r"] == pytest.approx(-1.5)


def test_bracket_timeout_exits_at_close():
    bars = [_bar(100, 100.5, 99.5, 100.2), _bar(100.2, 100.8, 99.8, 100.6)]
    res = simulate_bracket(bars, 0, "long", 100.0, 99.0, 103.0, 2)
    assert res["outcome"] == "timeout"
    assert res["r"] == pytest.approx(0.6)
    assert res["mfe_pct"] == pytest.approx(0.8)


def test_bracket_no_bars_after_entry():
    assert simulate_bracket([_bar(1, 1, 1, 1)], 1, "long", 1.0, 0.9, 1.2, 5) is None


def _random_bars(n, seed=7):
    rnd = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        o = p
        p *= 1 + rnd.gauss(0, 0.01)
        h = max(o, p) * (1 + abs(rnd.gauss(0, 0.006)))
        lo = min(o, p) * (1 - abs(rnd.gauss(0, 0.006)))
        out.append(_bar(o, h, lo, p, ts=i))
    return out


def test_walk_forward_signals_do_not_depend_on_future_bars():
    bars = _random_bars(700)
    full = walk_forward_sfp(bars, window=200, max_hold_bars=30, one_position=False)
    cut = 500
    altered = bars[:cut] + [_bar(1.0, 1.0, 1.0, 1.0, ts=i) for i in range(cut, 700)]
    trunc = walk_forward_sfp(altered, window=200, max_hold_bars=30, one_position=False)
    key = lambda s: (s["idx"], s["side"], round(s["overshoot_pct"], 9), s["classification"])  # noqa: E731
    early_full = [key(s) for s in full if s["idx"] < cut - 1]
    early_trunc = [key(s) for s in trunc if s["idx"] < cut - 1]
    assert early_full, "fixture should produce signals"
    assert early_full == early_trunc


def test_walk_forward_one_position_skips_overlaps():
    bars = _random_bars(700)
    sigs = walk_forward_sfp(bars, window=200, max_hold_bars=30, one_position=True)
    for a, b in zip(sigs, sigs[1:], strict=False):
        assert b["idx"] > a["variants"]["m2_2x"]["exit_idx"]


def test_summarise_r_basic():
    s = summarise_r([2.0, -1.0, -1.0, 2.0], ["target", "stop", "stop", "target"])
    assert s["n"] == 4
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["avg_r"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)
    assert s["small_sample"] is True


def test_range_time_share_flat_series_is_fully_ranged():
    bars = [_bar(100, 100.5, 99.5, 100, ts=i) for i in range(60)]
    assert range_time_share(bars, min_bars=20, max_width_pct=5) == pytest.approx(1.0)


# Short after a high sweep of level 100: stop 101, target 95.
def test_ltf_entry_htf_close_takes_first_open():
    bars = [_bar(99.5, 99.8, 99.0, 99.2)]
    e = ltf_entry(bars, 0, "short", 100.0, 101.0, 95.0, "htf_close")
    assert e["entry_price"] == pytest.approx(99.5)
    assert e["exit_from"] == 0


def test_ltf_entry_retest_fills_at_level():
    bars = [_bar(99.5, 99.7, 99.0, 99.2), _bar(99.2, 100.2, 99.1, 99.6)]
    e = ltf_entry(bars, 0, "short", 100.0, 101.0, 95.0, "retest")
    assert e["entry_price"] == pytest.approx(100.0)
    assert e["exit_from"] == 2
    assert e["stopped_in_fill_bar"] is False


def test_ltf_entry_retest_fill_and_stop_same_bar_is_flagged():
    bars = [_bar(99.5, 101.5, 99.0, 99.2)]
    e = ltf_entry(bars, 0, "short", 100.0, 101.0, 95.0, "retest")
    assert e["stopped_in_fill_bar"] is True


def test_ltf_entry_retest_cancelled_by_target_first():
    bars = [_bar(99.0, 99.2, 94.5, 95.0), _bar(95.0, 100.5, 94.8, 100.2)]
    assert ltf_entry(bars, 0, "short", 100.0, 101.0, 95.0, "retest") == {"skip": "target_before_fill"}


def test_ltf_entry_retest_no_fill():
    bars = [_bar(99.0, 99.5, 98.0, 98.5)] * 3
    assert ltf_entry(bars, 0, "short", 100.0, 101.0, 95.0, "retest", wait_bars=3) == {"skip": "no_fill"}


def test_ltf_entry_confirm_on_close_below_previous_low():
    bars = [_bar(99.6, 99.9, 99.4, 99.5), _bar(99.5, 99.8, 99.45, 99.7), _bar(99.7, 99.75, 99.1, 99.3)]
    e = ltf_entry(bars, 1, "short", 100.0, 101.0, 95.0, "confirm")
    assert e["entry_price"] == pytest.approx(99.3)
    assert e["exit_from"] == 3


def test_ltf_entry_confirm_cancelled_by_stop():
    bars = [_bar(99.6, 99.9, 99.4, 99.5), _bar(99.5, 101.2, 99.45, 100.8)]
    assert ltf_entry(bars, 1, "short", 100.0, 101.0, 95.0, "confirm") == {"skip": "stop_before_confirm"}


def test_ltf_entry_long_retest_mirror():
    bars = [_bar(100.5, 100.8, 99.8, 100.4)]
    e = ltf_entry(bars, 0, "long", 100.0, 99.0, 105.0, "retest")
    assert e["entry_price"] == pytest.approx(100.0)
