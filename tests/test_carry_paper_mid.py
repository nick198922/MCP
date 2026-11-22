"""Item 383a: carry paper price P&L re-priced at the book mid (analytic expectations)."""

import copy

import pytest

from tradedesk.analytics import carry_paper_mid as m


def _b(event: str, a_model: float, b_model: float, a_mid: float, b_mid: float, *, tid: str = "B:X:1") -> list[dict]:
    return [
        {
            "trade_id": tid,
            "event": event,
            "venue": "binanceusdm",
            "model_price": a_model,
            "mid": a_mid,
            "vwap": a_mid,
            "contract_multiplier": 1,
        },
        {"trade_id": tid, "event": event, "venue": "bybit", "model_price": b_model, "mid": b_mid, "vwap": b_mid, "contract_multiplier": 1},
    ]


def _a(event: str, s_model: float, p_model: float, s_mid: float, p_mid: float, mult: int = 1) -> list[dict]:
    return [
        {"event": event, "leg": "spot", "venue": "binance", "model_price": s_model, "mid": s_mid / mult, "contract_multiplier": mult},
        {"event": event, "leg": "perp", "venue": "binanceusdm", "model_price": p_model, "mid": p_mid, "contract_multiplier": 1},
    ]


def test_b_delta_and_split() -> None:
    o, c = _b("open", 100, 101, 100, 100.5), _b("close", 102, 101.5, 102, 101.5)
    r = m.trade_delta("B", o, c, 1)
    assert r["p_model"] == pytest.approx(0.015)  # ((102-100) - (101.5-101)) / 100
    assert r["p_mid"] == pytest.approx(0.010)  # ((102-100) - (101.5-100.5)) / 100
    assert r["delta"] == pytest.approx(-0.005)
    assert r["delta_open"] == pytest.approx(-0.005)
    assert r["delta_close"] == pytest.approx(0.0)
    assert m.trade_delta("B", o, c, -1)["delta"] == pytest.approx(0.005)
    assert m.trade_delta("B", o, c, None)["delta"] is None


def test_a_delta_with_multiplier() -> None:
    o, c = _a("open", 10, 10.1, 10, 10.2, mult=1000), _a("close", 11, 11.05, 11, 11.05, mult=1000)
    r = m.trade_delta("A", o, c, None)
    assert r["p_model"] == pytest.approx(0.005)  # (1 - 0.95) / 10
    assert r["p_mid"] == pytest.approx(0.015)  # (1 - 0.85) / 10, spot mid x 1000
    assert r["delta"] == pytest.approx(0.010)
    assert r["p_vwap"] is None  # no vwap recorded -> not priced


def test_missing_book_is_none() -> None:
    o, c = _b("open", 100, 101, 100, 100.5), _b("close", 102, 101.5, 102, 101.5)
    c[1]["mid"] = None
    assert m.trade_delta("B", o, c, 1)["delta"] is None
    assert m.trade_delta("B", o, c, 1)["p_model"] == pytest.approx(0.015)
    assert m.price_pnl("B", o, [], 1, "mid", "mid") is None


def test_open_gap_and_funding_scale() -> None:
    o = _b("open", 100, 101, 98, 100.5)
    assert m.open_gap_delta("B", o) == pytest.approx((100.5 / 98) / (101 / 100) - 1)
    assert m.funding_scale("B", o) == pytest.approx(100 / 98)
    assert m.funding_scale("A", _a("open", 10, 10.1, 8, 10.2)) == pytest.approx(10 / 8)


def test_select_trades_ignores_data_after_t() -> None:
    t = 1_000_000
    pos = [
        {"trade_id": "B:X:1", "symbol": "X", "status": "closed", "opened_ts": 100, "closed_ts": 500, "trade": {"price_pnl": 0.015}},
        {"trade_id": "B:Y:2", "symbol": "Y", "status": "closed", "opened_ts": 600, "closed_ts": t + 10, "trade": {"price_pnl": 0.0}},
    ]
    rows = [{"cycle_ts": 100, "payload": x} for x in _b("open", 100, 101, 100, 100.5)]
    rows += [{"cycle_ts": 500, "payload": x} for x in _b("close", 102, 101.5, 102, 101.5)]
    rows += [{"cycle_ts": 600, "payload": x} for x in _b("open", 1, 1, 1, 1, tid="B:Y:2")]
    later = [{"cycle_ts": t + 10, "payload": x} for x in _b("close", 2, 2, 2, 2, tid="B:Y:2")]
    base = m.select_trades(pos, rows + later, t)
    changed = copy.deepcopy(later)
    for r in changed:
        r["payload"]["mid"] = 999.0
    assert m.select_trades(pos, rows + changed, t) == base
    assert [x["closed"] for x in base] == [True, False]
    assert base[1]["close"] == [] and base[1]["trade"] is None
    assert m.select_trades(pos, rows, 50) == []


@pytest.mark.parametrize(
    ("n", "mean", "lo", "hi", "want"),
    [
        (15, -0.003, -0.005, -0.001, "✓"),
        (15, -0.0005, -0.0008, -0.0002, "≈"),
        (15, 0.0001, -0.0009, 0.0009, "✗"),
        (15, 0.0001, -0.002, 0.002, "◯"),
        (9, -0.003, -0.005, -0.001, "◯"),
    ],
)
def test_verdict(n: int, mean: float, lo: float, hi: float, want: str) -> None:
    assert m.verdict(n, mean, lo, hi) == want
