"""Funding benchmark for criterion (v) of the forward paper carry (analytics/carry_bench.py, scripts/verify_paper_carry_bench.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from carry_synth import DAY, H8, T0, markets_a, markets_b
from tradedesk.analytics.carry import simulate_carry_portfolio, simulate_perp_spread_portfolio
from tradedesk.analytics.carry_bench import (
    EDGES_A,
    EDGES_B,
    band_check,
    bucket_of,
    build_bench,
    ladder_summary,
    merge_buckets,
    percentile,
    realized_funding_apr,
    trade_samples,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_realized_funding_apr_annualises_on_days_held():
    # 1 % of slot notional over 36.5 days = 10 % a year
    assert realized_funding_apr(0.01, 0, int(36.5 * DAY)) == pytest.approx(0.10)
    assert realized_funding_apr(-0.002, 0, 73 * DAY) == pytest.approx(-0.01)
    assert realized_funding_apr(0.01, 5, 5) is None


def test_percentile_is_linear_between_order_statistics():
    xs = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(xs, 0.05) == pytest.approx(1.2)
    assert percentile(xs, 0.95) == pytest.approx(4.8)
    assert percentile(xs, 0.5) == pytest.approx(3.0)
    assert percentile([7.0], 0.95) == 7.0
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_bucket_edges_are_left_closed():
    assert bucket_of(0.2, EDGES_A) == 0
    assert bucket_of(0.2999, EDGES_A) == 0
    assert bucket_of(0.3, EDGES_A) == 1
    assert bucket_of(1.0, EDGES_A) == 3
    assert bucket_of(50.0, EDGES_A) == 3
    assert bucket_of(0.1999, EDGES_A) is None
    assert bucket_of(3.0, EDGES_B) == 3
    assert bucket_of(0.1, EDGES_B) == 0


def test_thin_buckets_merge_into_lower_neighbour():
    # top bucket (3) merges into [0.5, 1) -> 8, still thin -> into [0.3, ...) -> 20
    assert merge_buckets(EDGES_A, [30, 12, 5, 3], 10) == [(0.2, 0.3), (0.3, None)]
    # only the lowest is thin: it merges upward
    assert merge_buckets(EDGES_A, [5, 20, 20, 20], 10) == [(0.2, 0.5), (0.5, 1.0), (1.0, None)]
    # everything thin collapses into one open-ended bucket
    assert merge_buckets(EDGES_B, [1, 1, 1, 1], 10) == [(0.1, None)]
    assert merge_buckets(EDGES_B, [10, 10, 10, 10], 10) == [(0.1, 0.3), (0.3, 1.0), (1.0, 3.0), (3.0, None)]


def test_build_bench_and_band_check_on_an_analytic_sample():
    # 20 trades in [0.2, 0.3) with realized APR 0..19; 3 trades at entry 2.0 merge down through empty buckets
    samples = [{"entry": 0.25, "realized_apr": float(k)} for k in range(20)]
    samples += [{"entry": 2.0, "realized_apr": 100.0} for _ in range(3)]
    b = build_bench(samples, EDGES_A, min_n=10)
    assert b["registered_counts"] == [20, 0, 0, 3]
    assert len(b["buckets"]) == 1
    only = b["buckets"][0]
    assert (only["entry_lo"], only["entry_hi"], only["n"]) == (0.2, None, 23)
    b2 = build_bench(samples[:20], EDGES_A, min_n=10)
    lo = b2["buckets"][0]
    assert lo["p05"] == pytest.approx(0.95)
    assert lo["p95"] == pytest.approx(18.05)
    assert band_check(b2, 0.25, 10.0)["in_band"] is True
    assert band_check(b2, 0.25, 19.0)["in_band"] is False
    assert band_check(b2, 0.15, 10.0)["bucket"] is None


def test_trade_samples_filter_on_days_held():
    trades = [
        {"symbol": "X", "open_ts": 0, "close_ts": 10 * DAY, "funding": 0.01, "entry_apr": 0.4, "open_at_end": True},
        {"symbol": "Y", "open_ts": 0, "close_ts": 2 * DAY, "funding": 0.01, "entry_apr": 0.4},
    ]
    s = trade_samples(trades, "entry_apr", min_days=7)
    assert [x["symbol"] for x in s] == ["X"]
    assert s[0]["realized_apr"] == pytest.approx(0.01 / (10 / 365))
    assert s[0]["open_at_end"] is True
    assert len(trade_samples(trades, "entry_apr")) == 2


def _perturb_after(funding: list[dict], cut: int) -> list[dict]:
    return [dict(e, rate=-3 * e["rate"] + 0.001) if e["ts_ms"] > cut else e for e in funding]


def test_bench_samples_before_t_do_not_depend_on_data_after_t():
    cut = T0 + 40 * DAY
    a = markets_a()
    a2 = {s: dict(d, funding=_perturb_after(d["funding"], cut)) for s, d in a.items()}
    kw = dict(slots=3, entry_apr=0.20)
    before = [x for x in trade_samples(simulate_carry_portfolio(a, **kw)["trades"], "entry_apr") if x["close_ts"] <= cut]
    after = [x for x in trade_samples(simulate_carry_portfolio(a2, **kw)["trades"], "entry_apr") if x["close_ts"] <= cut]
    assert before and before == after

    b = markets_b()
    b2 = {s: {"a": d["a"], "b": dict(d["b"], funding=_perturb_after(d["b"]["funding"], cut))} for s, d in b.items()}
    kwb = dict(slots=3, entry_apr=0.10, max_entry_gap=0.02, stop_move=0.5)
    sb = [x for x in trade_samples(simulate_perp_spread_portfolio(b, **kwb)["trades"], "entry_spread") if x["close_ts"] <= cut]
    sb2 = [x for x in trade_samples(simulate_perp_spread_portfolio(b2, **kwb)["trades"], "entry_spread") if x["close_ts"] <= cut]
    assert sb and sb == sb2


def test_ladder_summary_for_a_2x_leg():
    tiers = [
        {"limit_usd": 200_000, "mmr": 0.01, "max_leverage": 50},
        {"limit_usd": 400_000, "mmr": 0.02, "max_leverage": 25},
        {"limit_usd": 3_000_000, "mmr": 0.30, "max_leverage": 1.5},
        {"limit_usd": 1_000_000, "mmr": 0.25, "max_leverage": 2},
    ]
    s = ladder_summary(tiers, notional_usd=2_000, leverage=2)
    assert s == {"mmr": 0.01, "max_position_usd": 1_000_000, "fits": True, "n_tiers": 4}
    big = ladder_summary(tiers, notional_usd=2_000_000, leverage=2)
    assert big["mmr"] == 0.30 and big["fits"] is False
    assert ladder_summary([], notional_usd=1, leverage=2) == {"mmr": None, "max_position_usd": None, "fits": False, "n_tiers": 0}


def test_runner_gap_detector():
    import verify_paper_carry_bench as r

    f = [{"ts_ms": T0 + i * H8, "rate": 0.0001} for i in range(30)]
    assert r.funding_gap(f, T0 + 2 * H8, T0 + 20 * H8) is False
    holed = f[:10] + f[13:]  # three payments missing
    assert r.funding_gap(holed, T0 + 2 * H8, T0 + 20 * H8) is True
    # a gap outside the holding window does not count
    assert r.funding_gap(holed, T0 + 14 * H8, T0 + 25 * H8) is False


def test_runner_gap_detector_12h_floor():
    import verify_paper_carry_bench as r

    h = 3_600_000
    # 4h interval switches to 8h: median step 4h, so the registered rule (floor 0) flags the 8h steps as a gap
    switch = [{"ts_ms": T0 + i * 4 * h, "rate": 0.0001} for i in range(20)]
    switch += [{"ts_ms": T0 + 76 * h + i * 8 * h, "rate": 0.0001} for i in range(1, 6)]
    lo, hi = T0 + 40 * h, T0 + 110 * h
    assert r.funding_gap(switch, lo, hi) is True
    assert r.funding_gap(switch, lo, hi, floor_ms=r.GAP_FLOOR_MS) is False
    # a real 16 h hole (on the 4h grid) stays a gap under the 12 h floor
    holed = [e for e in switch if not (T0 + 48 * h < e["ts_ms"] < T0 + 64 * h)]
    assert r.funding_gap(holed, lo, hi, floor_ms=r.GAP_FLOOR_MS) is True
    # exactly 12 h is not above the floor
    twelve = [e for e in switch if not (T0 + 48 * h < e["ts_ms"] < T0 + 60 * h)]
    assert r.funding_gap(twelve, lo, hi, floor_ms=r.GAP_FLOOR_MS) is False


class _FakeBybit:
    """Bybit /v5/market/funding/history: rows with start <= t <= end, newest first, at most `limit` (like the venue)."""

    def __init__(self, rows_by_id: dict[str, list[tuple[int, float]]]) -> None:
        self.rows, self.calls = rows_by_id, []

    async def public_get(self, venue, method, params):
        from types import SimpleNamespace

        self.calls.append((venue, method, dict(params)))
        got = [(t, x) for t, x in self.rows.get(params["symbol"], []) if params["startTime"] <= t <= params["endTime"]]
        got.sort(reverse=True)
        lst = [{"fundingRateTimestamp": str(t), "fundingRate": str(x)} for t, x in got[: params["limit"]]]
        return SimpleNamespace(data={"result": {"list": lst}})

    async def markets(self, venue):
        from types import SimpleNamespace

        return SimpleNamespace(data={"X/USDT:USDT": {"id": "XUSDT"}, "Y/USDT:USDT": {"id": "YUSDT"}})


def test_fill_bybit_holes_walks_backwards_through_a_hole(tmp_path, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    import verify_carry_perp_perp as pp
    import verify_paper_carry_bench as r

    h, days = 3_600_000, 30
    full_x = [(T0 + i * h, 0.0001 * (i % 7)) for i in range(420)]  # 1h interval, 420 payments
    cached_x = full_x[:10] + full_x[410:]  # a 400 h hole, 400 payments missing (two full pages of 200)
    full_y = [(T0 + i * 8 * h, 0.0001) for i in range(10)] + [(T0 + 9 * 8 * h + 4 * h * k, 0.0002) for k in range(1, 5)]
    cached_y = full_y[:4] + full_y[7:]  # one 32 h hole the venue confirms empty (rows 4..6 not served below)
    served_y = full_y[:4] + full_y[7:]
    for sym, rows in (("X/USDT:USDT", cached_x), ("Y/USDT:USDT", cached_y)):
        pp.venue_funding_path(tmp_path, pp.B, sym, days).write_text(json.dumps([{"ts_ms": t, "rate": x} for t, x in rows]))
    fake = _FakeBybit({"XUSDT": full_x, "YUSDT": served_y})

    async def aclose():
        return None

    monkeypatch.setattr(r, "build_server", lambda: (None, SimpleNamespace(exchanges=fake, aclose=aclose)))
    out = asyncio.run(r.fill_bybit_holes(tmp_path, ["X/USDT:USDT", "Y/USDT:USDT"], days))

    got_x = json.loads(pp.venue_funding_path(tmp_path, pp.B, "X/USDT:USDT", days).read_text())
    assert [(e["ts_ms"], e["rate"]) for e in got_x] == full_x
    # backward walk: first page ends just before the hole's right edge, the next one before the oldest row found
    x_calls = [c[2] for c in fake.calls if c[2]["symbol"] == "XUSDT"]
    assert x_calls[0]["startTime"] == full_x[9][0] + 1 and x_calls[0]["endTime"] == full_x[410][0] - 1
    assert x_calls[1]["endTime"] == full_x[210][0] - 1
    assert len(x_calls) == 3  # the third page is empty and ends the walk
    rec = json.loads((tmp_path / "bybit_holes.json").read_text())
    assert rec["X/USDT:USDT"] == [[full_x[9][0], full_x[410][0], 400]]
    assert rec["Y/USDT:USDT"] == [[full_y[3][0], full_y[7][0], 0]]
    # the 8h -> 4h switch in Y is not a hole (<= 12 h): only one Y hole was queried
    assert out == {"holes_seen": 2, "rows_added": 400, "holes_confirmed_empty": 1, "holes_query_failed": 0}
