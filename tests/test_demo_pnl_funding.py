"""Funding split of the demo P&L gap (analytics/demo_pnl_funding.py): analytic expectations, no network."""

from __future__ import annotations

import pytest

from tradedesk.analytics import demo_pnl_funding as f

H = 3_600_000


def _row(**kw):
    base = {
        "link_id": "a", "setup": "S12", "symbol": "X/USDT:USDT", "status": "closed", "side": "short", "fill": 1.0,
        "entry": 1.0, "exit": 0.9, "qty": 1000.0, "fees_usd": 1.0, "risk_usd": 100.0,
    }  # fmt: skip
    base.update(kw)
    return base


def test_funding_paid_signs_and_window():
    ev = [(1 * H, -0.01, 1.0), (2 * H, -0.01, 0.5), (5 * H, 0.02, 1.0)]
    # short pays a negative rate: -1 * -0.01 * 1000 * 1.0 = +10, then +5 at 0.5; the event at 5h is after the close
    assert f.funding_paid("short", 1000.0, ev, 0, 3 * H) == pytest.approx(15.0)
    # long receives the same events: -15
    assert f.funding_paid("long", 1000.0, ev, 0, 3 * H) == pytest.approx(-15.0)
    # the event exactly at the open or the close is not counted
    assert f.funding_paid("short", 1000.0, ev, 1 * H, 2 * H) == 0.0


def test_funding_before_close_does_not_change_with_later_events():
    early = [(1 * H, 0.001, 2.0), (2 * H, -0.002, 2.0)]
    a = f.funding_paid("long", 10.0, early, 0, 3 * H)
    b = f.funding_paid("long", 10.0, early + [(4 * H, 0.5, 9.0), (7 * H, -0.3, 1.0)], 0, 3 * H)
    assert a == b == pytest.approx(10.0 * (0.001 * 2.0 - 0.002 * 2.0))


def test_close_time_and_price_at():
    bars = [[0, 1.0, 1.1, 0.9, 1.05], [60_000, 1.05, 1.3, 1.0, 1.2], [120_000, 1.2, 1.25, 1.15, 1.2]]
    assert f.close_time(1.28, bars, 0, 180_000) == 60_000
    assert f.close_time(1.28, bars, 120_000, 180_000) is None
    assert f.close_time(0.95, bars, 0, 180_000) == 0
    assert f.price_at(60_000, bars) == 1.05  # the open of the bar starting there
    assert f.price_at(90_000, bars) == 1.2  # the close of the latest earlier bar
    assert f.price_at(-1, bars) is None


def test_close_window_last_cycle_time_exit_and_fallbacks():
    m = 60_000
    closed, window = 10 * H, 65 * m
    # 1m bars of the last cycle: the exit 1.0 is held at 9:10 and again at 9:40
    m1 = [[closed - 50 * m, 1.1, 1.2, 0.95, 1.0], [closed - 20 * m, 1.0, 1.05, 0.98, 1.0]]
    h1 = [[2 * H, 1.5, 1.6, 0.99, 1.5], [9 * H, 1.1, 1.2, 0.95, 1.0]]
    # the earliest 1m bar in the window holding the exit; funding counts up to that bar's end
    assert f.close_window(1.0, m1, h1, 0, closed, window) == (closed - 50 * m, closed - 49 * m, None)
    # a time exit cannot close before its exit order: the minute of exit_ts is the lower bound
    assert f.close_window(1.0, m1, h1, 0, closed, window, exit_ts=closed - 30 * m + 5)[0] == closed - 20 * m
    # exit not in the last cycle: the earliest 1h bar since the opening hour holding it, to that bar's end
    c, end, note = f.close_window(1.6, m1, h1, H + 5, closed, window)
    assert (c, end) == (2 * H, 3 * H) and note.startswith("close not in the last cycle")
    # no bar holds the exit: closed_ts itself
    assert f.close_window(9.0, m1, h1, 0, closed, window) == (None, closed, "close bar not found: closed_ts used")


def test_priced_events_reports_dropped():
    bars = [[2 * H, 1.0, 1.1, 0.9, 1.05]]
    ev, dropped = f.priced_events([(H, 0.01), (2 * H, -0.02), (3 * H, 0.03)], bars)
    assert ev == [(2 * H, -0.02, 1.0), (3 * H, 0.03, 1.05)]
    assert dropped == [H]  # no bar at or before 1h: left out, not priced


def test_decompose_funding_explains_gap():
    # price P&L: -(0.9 - 1.0) * 1000 - 1 = +99; the venue booked +99 - 30 funding paid = 69
    p = f.decompose(_row(pnl_usd=69.0), paid=30.0)
    assert p["price"] == pytest.approx(99.0)
    assert p["gap"] == pytest.approx(-30.0)
    assert p["residual"] == pytest.approx(0.0)
    assert p["explained"] is True
    assert p["funding_r"] == pytest.approx(-0.30)
    assert p["gap_r"] == pytest.approx(-0.30)
    # a residual beyond max(0.5 % of 1000, 0.5) = 5 USD is not explained
    q = f.decompose(_row(pnl_usd=60.0), paid=30.0)
    assert q["residual"] == pytest.approx(-9.0) and q["explained"] is False
    # without a funding number the split is left open
    r = f.decompose(_row(pnl_usd=69.0), paid=None)
    assert r["residual"] is None and r["explained"] is None and r["funding_r"] is None
    assert f.decompose(_row(pnl_usd=69.0, exit=None), paid=0.0) is None


def test_variant_rows():
    rows = [_row(link_id="a", pnl_usd=69.0, r_multiple=0.69), _row(link_id="b", pnl_usd=10.0, r_multiple=0.1)]
    j = f.variant_rows(rows, {"a"}, "journal")
    assert [r["r_multiple"] for r in j] == [0.69, 0.1]
    p = f.variant_rows(rows, {"a"}, "price_flagged")
    assert p[0]["r_multiple"] == pytest.approx(0.99) and p[0]["pnl_usd"] == pytest.approx(99.0)
    assert p[1]["r_multiple"] == 0.1  # unflagged unchanged
    e = f.variant_rows(rows, {"a"}, "excl_flagged")
    assert [r["link_id"] for r in e] == ["b"]
    assert rows[0]["r_multiple"] == 0.69  # inputs are not mutated
    with pytest.raises(ValueError):
        f.variant_rows(rows, set(), "other")


def test_mean_funding_r():
    parts = [{"setup": "S12", "funding_r": -0.2}, {"setup": "S12", "funding_r": 0.1}, {"setup": "S3", "funding_r": None}]
    out = f.mean_funding_r(parts)
    assert out["S12"][0] == 2 and out["S12"][1] == pytest.approx(-0.05)
    assert "S3" not in out
