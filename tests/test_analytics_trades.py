"""Direct tests for analytics/trades.py (recent-trades flow summary).

Expected values are computed by hand on a synthetic ccxt-shaped list (input order matters for CVD):
  A ts=1000 buy  p=100 a=2   -> cost 200
  B ts=3000 sell p=110 a=1   -> cost 110
  C ts=2000 buy  p=90  a=3   -> cost 270
  D ts=None sell p=100 a=0.5 -> cost 50
  buy: base 5, notional 470, vwap 94; sell: base 1.5, notional 160, vwap 160/1.5
  total: base 6.5, notional 630, vwap 630/6.5, imbalance 310/630
  CVD in input order: +200, -110 -> 90, +270 -> 360, -50 -> 310 (D has no ts, so it moves the
  running total but adds no series point); first/last ts are min/max, not input order.
"""

from __future__ import annotations

import pytest

from tradedesk.analytics.trades import _f, summarise


def _t(ts, side, price, amount, cost=None) -> dict:
    r = {"timestamp": ts, "side": side, "price": price, "amount": amount}
    if cost is not None:
        r["cost"] = cost
    return r


def _sample() -> list[dict]:
    return [
        _t(1000, "buy", 100, 2),
        _t(3000, "sell", 110, 1),
        _t(2000, "buy", 90, 3),
        _t(None, "sell", 100, 0.5),
    ]


# ---- _f ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("1.5", 1.5), (2, 2.0), ("abc", None), ([1], None), ({}, None)],
)
def test_f_parses_or_returns_none(raw, expected):
    assert _f(raw) == expected


# ---- summarise: reference sample ----


def test_summarise_counts_and_sides():
    s = summarise(_sample())
    assert s["count"] == 4
    assert s["buy_count"] == 2
    assert s["sell_count"] == 2
    assert s["buy_base"] == pytest.approx(5.0)
    assert s["sell_base"] == pytest.approx(1.5)
    assert s["delta_base"] == pytest.approx(3.5)
    assert s["base_volume"] == pytest.approx(6.5)


def test_summarise_notionals_and_imbalance():
    s = summarise(_sample())
    assert s["buy_notional"] == pytest.approx(470.0)
    assert s["sell_notional"] == pytest.approx(160.0)
    assert s["delta_notional"] == pytest.approx(310.0)
    assert s["quote_volume"] == pytest.approx(630.0)
    assert s["imbalance"] == pytest.approx(310.0 / 630.0)


def test_summarise_vwaps():
    s = summarise(_sample())
    assert s["vwap"] == pytest.approx(630.0 / 6.5)
    assert s["buy_vwap"] == pytest.approx(94.0)
    assert s["sell_vwap"] == pytest.approx(160.0 / 1.5)


def test_summarise_time_span_uses_min_max_not_input_order():
    s = summarise(_sample())
    assert s["first_ts"] == 1000
    assert s["last_ts"] == 3000
    assert s["duration_ms"] == 2000


def test_summarise_cvd_follows_input_order_and_skips_points_without_ts():
    s = summarise(_sample())
    series = s["cvd_series"]
    assert [p[0] for p in series] == [1000, 3000, 2000]
    assert [p[1] for p in series] == pytest.approx([200.0, 90.0, 360.0])
    # The ts-less sell (D) still counts in the running total.
    assert s["cvd_end_notional"] == pytest.approx(310.0)
    assert s["cvd_end_notional"] == pytest.approx(s["delta_notional"])


def test_summarise_top_trades_by_notional():
    s = summarise(_sample(), top_n=2)
    top = s["top_trades"]
    assert [t["cost"] for t in top] == pytest.approx([270.0, 200.0])
    assert top[0] == {"ts": 2000, "side": "buy", "price": 90.0, "amount": 3.0, "cost": 270.0}
    assert set(top[1]) == {"ts", "side", "price", "amount", "cost"}


def test_summarise_top_n_default_and_zero():
    assert len(summarise(_sample())["top_trades"]) == 4  # default 5, only 4 rows
    assert summarise(_sample(), top_n=0)["top_trades"] == []


def test_summarise_top_trades_ties_keep_input_order():
    rows = [_t(1, "buy", 10, 1), _t(2, "sell", 5, 2), _t(3, "buy", 1, 10)]  # all cost 10
    top = summarise(rows, top_n=3)["top_trades"]
    assert [t["ts"] for t in top] == [1, 2, 3]


# ---- summarise: edges ----


def test_summarise_empty_input():
    assert summarise([]) == {"count": 0}


def test_summarise_all_rows_rejected():
    rows = [
        _t(1, "buy", None, 1),  # no price
        _t(2, "buy", 100, None),  # no amount
        _t(3, "buy", 100, 0),  # zero amount
        _t(4, "sell", -1, 1),  # negative price
        _t(5, "sell", 0, 1),  # zero price
        _t(6, "buy", "abc", 1),  # unparsable price
        _t(7, None, 100, 1),  # no side
        _t(8, "long", 100, 1),  # unknown side
    ]
    assert summarise(rows) == {"count": 0}


def test_summarise_accepts_generator_strings_and_upper_case_side():
    gen = (r for r in [_t(1000, "BUY", "100", "2"), _t(2000, "Sell", "50", "1")])
    s = summarise(gen)
    assert s["count"] == 2
    assert s["buy_notional"] == pytest.approx(200.0)
    assert s["sell_notional"] == pytest.approx(50.0)
    assert s["top_trades"][0]["side"] == "buy"


def test_summarise_prefers_reported_cost_over_price_times_amount():
    # Inverse/contract venues report cost != price*amount; the reported value wins.
    s = summarise([_t(1, "buy", 100, 2, cost=150), _t(2, "sell", 100, 1, cost="bad")])
    assert s["buy_notional"] == pytest.approx(150.0)
    assert s["sell_notional"] == pytest.approx(100.0)  # unparsable cost -> price*amount
    assert s["buy_vwap"] == pytest.approx(75.0)  # 150 / 2
    assert s["vwap"] == pytest.approx(250.0 / 3.0)


def test_summarise_one_sided_flow():
    s = summarise([_t(1, "buy", 100, 1), _t(2, "buy", 200, 1)])
    assert s["sell_count"] == 0
    assert s["sell_base"] == 0
    assert s["sell_vwap"] is None
    assert s["buy_vwap"] == pytest.approx(150.0)
    assert s["imbalance"] == pytest.approx(1.0)
    assert s["delta_notional"] == pytest.approx(300.0)


def test_summarise_zero_total_notional_gives_none_imbalance():
    s = summarise([_t(1, "buy", 100, 1, cost=0), _t(2, "sell", 100, 1, cost=0)])
    assert s["imbalance"] is None
    assert s["quote_volume"] == 0
    assert s["vwap"] == pytest.approx(0.0)  # tot_cost 0 / tot_amount 2


def test_summarise_timestamps_missing_or_non_numeric():
    s = summarise([_t(None, "buy", 100, 1), _t("1000", "sell", 100, 1)])
    assert s["first_ts"] is None
    assert s["last_ts"] is None
    assert s["duration_ms"] is None
    assert s["cvd_series"] == []
    assert s["cvd_end_notional"] == pytest.approx(0.0)


def test_summarise_float_timestamp_truncated_to_int():
    s = summarise([_t(1500.9, "buy", 100, 1)])
    assert s["first_ts"] == 1500
    assert isinstance(s["first_ts"], int)
    assert s["duration_ms"] == 0
    assert s["cvd_series"] == [[1500, 100.0]]
