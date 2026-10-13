from __future__ import annotations

import math

import ccxt.async_support as ccxt
import pytest

from conftest import NOW, FakeEx, make_transports
from test_ranges import _call
from tradedesk.analytics.vrp import (
    DAY_MS,
    align_daily,
    percentile_rank,
    realized_vol,
    short_straddle_hedged,
    stats,
    vrp_records,
)
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _rows(closes, iv=0.5, start_day=100):
    return [{"day": start_day + i, "iv": iv, "close": c} for i, c in enumerate(closes)]


def _zigzag(n, r, s0=100.0):
    out, s = [s0], s0
    for i in range(n):
        s *= math.exp(r if i % 2 == 0 else -r)
        out.append(s)
    return out


def test_realized_vol_zero_mean_annualised():
    closes = _zigzag(30, 0.02)
    assert realized_vol(closes) == pytest.approx(0.02 * math.sqrt(365))
    assert realized_vol([100.0]) is None


def test_align_daily_joins_on_utc_day():
    iv = [(5 * DAY_MS + 1000, 0.4), (6 * DAY_MS, 0.5)]
    px = [(5 * DAY_MS, 100.0), (7 * DAY_MS, 110.0)]
    rows = align_daily(iv, px)
    assert rows == [{"day": 5, "iv": 0.4, "close": 100.0}]


def test_vrp_records_windows_and_gap():
    closes = _zigzag(80, 0.01)
    recs = vrp_records(_rows(closes, iv=0.3), horizon_days=30, trailing_days=30)
    assert recs and recs[0]["day"] == 130
    rv = 0.01 * math.sqrt(365)
    assert recs[0]["rv_forward"] == pytest.approx(rv)
    assert recs[0]["vrp_vol"] == pytest.approx(0.3 - rv)
    assert recs[0]["spread"] == pytest.approx(0.3 - rv)
    rows = _rows(closes, iv=0.3)
    for r in rows[50:]:
        r["day"] += 1  # a missing day breaks every window that spans it
    assert all(not (r["day"] - 30 <= 149 < r["day"] + 30) for r in vrp_records(rows))


def test_hedged_short_straddle_wins_when_realized_below_implied():
    rows = _rows(_zigzag(40, 0.005), iv=0.8)  # realized ~10 % vs implied 80 %
    res = short_straddle_hedged(rows, 0, days=30, entry_cost_vol=0, option_fee=0, hedge_fee=0)
    assert res["pnl"] > 0


def test_hedged_short_straddle_loses_when_realized_above_implied():
    rows = _rows(_zigzag(40, 0.06), iv=0.3)  # realized ~115 % vs implied 30 %
    res = short_straddle_hedged(rows, 0, days=30, entry_cost_vol=0, option_fee=0, hedge_fee=0)
    assert res["pnl"] < 0


def test_short_straddle_costs_and_gap():
    rows = _rows([100.0] * 40, iv=0.5)
    free = short_straddle_hedged(rows, 0, days=30, entry_cost_vol=0, option_fee=0, hedge_fee=0)
    paid = short_straddle_hedged(rows, 0, days=30, entry_cost_vol=0.01, option_fee=0.0003, hedge_fee=0.0005)
    assert free["payoff"] == 0 and free["hedge_pnl"] == 0
    assert free["pnl"] == pytest.approx(free["premium"])
    assert paid["pnl"] < free["pnl"]
    del rows[10]  # a missing day: the 30-row window now spans 31 calendar days
    assert short_straddle_hedged(rows, 0, days=30) is None


def test_stats_and_percentile():
    s = stats([0.02, -0.01, 0.03, -0.04], periods_per_year=12)
    assert s["n"] == 4
    assert s["mean"] == pytest.approx(0.0)
    assert s["share_positive"] == pytest.approx(0.5)
    assert s["max_drawdown"] == pytest.approx(0.04)
    assert percentile_rank([1, 2, 3, 4], 3) == pytest.approx(0.625)


def test_vrp_summary_snapshot_premium_and_straddle():
    from tradedesk.analytics.vrp import vrp_summary

    rows = _rows(_zigzag(200, 0.01), iv=0.4)  # realized ~19 % vs implied 40 %
    s = vrp_summary(rows, entry_cost_vol=0.0, option_fee=0.0, hedge_fee=0.0)
    cur = s["current"]
    assert cur["iv"] == pytest.approx(0.4)
    assert cur["rv_30d"] == pytest.approx(0.01 * math.sqrt(365))
    assert cur["spread_vs_rv30"] == pytest.approx(0.4 - 0.01 * math.sqrt(365))
    assert s["premium"]["share_iv_above_realized"] == pytest.approx(1.0)
    assert s["hedged_short_straddle"]["all_starts"]["mean"] > 0
    assert s["hedged_short_straddle"]["last_365d_mean"] > 0
    assert vrp_summary([]) == {}


def test_vrp_summary_short_history_has_snapshot_only():
    from tradedesk.analytics.vrp import vrp_summary

    s = vrp_summary(_rows(_zigzag(20, 0.01), iv=0.4))
    assert s["current"]["rv_7d"] is not None
    assert s["current"]["rv_30d"] is None
    assert "premium" not in s and "hedged_short_straddle" not in s


class _NoOhlcv(FakeEx):
    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100):
        raise ccxt.ExchangeNotAvailable("ohlcv down")


async def test_deribit_vrp_price_provider_failure(state):
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(state), exchange_factory=_NoOhlcv)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "deribit_vrp", currency="BTC", history_days=200)
        assert env["ok"] is False
        assert any(m["field"] == "ohlcv:binance" for m in env["missing"])
    finally:
        await rt.aclose()


def test_trailing_percentiles_use_past_only():
    from tradedesk.analytics.vrp import trailing_percentiles

    rows = [{"day": i, "iv": 0.3 + 0.001 * i, "close": 100.0} for i in range(400)]
    pct = trailing_percentiles(rows, window=365)
    assert min(pct) == 365
    assert pct[399] == pytest.approx(1.0)  # rising series: every new day is above the whole past year
    rows[399]["iv"] = 0.0
    assert trailing_percentiles(rows, window=365)[399] == 0.0


def test_vrp_summary_by_iv_level_buckets():
    from tradedesk.analytics.vrp import vrp_summary

    closes = _zigzag(500, 0.01)
    rows = [{"day": 100 + i, "iv": 0.3 + 0.1 * math.sin(i / 20), "close": c} for i, c in enumerate(closes)]
    s = vrp_summary(rows, entry_cost_vol=0, option_fee=0, hedge_fee=0)
    levels = s["by_iv_level"]
    assert len(levels) == 5 and sum(x["n_starts"] for x in levels) > 0
    assert 0.0 <= s["current"]["iv_percentile_365d"] <= 1.0
    with_data = [x for x in levels if x["n_starts"]]
    assert with_data[-1]["mean_pnl"] > with_data[0]["mean_pnl"]  # selling at higher IV earns more on the same path
