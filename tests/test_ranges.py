from __future__ import annotations

import json

from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.ranges import (
    classify_ranges,
    detect_deviations,
    detect_ranges,
    detect_sweeps,
    swing_points,
)
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _bar(ts: int, o: float, h: float, lo: float, c: float, v: float = 1.0) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": v}


# ---------- pure analytics ----------


def test_swing_points_detects_pivots():
    # Simple V shape: idx 2 is the low, idx 4 is the high.
    closes = [100, 99, 95, 98, 103, 101, 100]
    bars = [_bar(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
    sw = swing_points(bars, lookback=2)
    low_idxs = [p["idx"] for p in sw["lows"]]
    hi_idxs = [p["idx"] for p in sw["highs"]]
    assert 2 in low_idxs
    assert 4 in hi_idxs


def test_detect_ranges_merges_overlapping_windows():
    # 60 bars oscillating 95..105 → a single range spanning the whole series.
    bars = []
    for i in range(60):
        c = 100.0 + (3.0 if i % 2 == 0 else -3.0)
        bars.append(_bar(i, c, c + 1, c - 1, c))
    ranges = detect_ranges(bars, min_bars=20, max_width_pct=10)
    assert len(ranges) == 1
    assert ranges[0]["n_bars"] >= 55
    assert 95 <= ranges[0]["bottom"] <= 98
    assert 102 <= ranges[0]["top"] <= 105


def test_detect_ranges_rejects_trending_bars():
    bars = [_bar(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(60)]
    ranges = detect_ranges(bars, min_bars=20, max_width_pct=10)
    assert ranges == []  # width exceeds 10 % mid immediately


def test_detect_sweeps_catches_wick_above_prior_high():
    # Build a prior swing high at idx 2 (price 105), then at idx 7 a sweep candle
    # that pokes up to 108 with a long upper wick but closes at 103.
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 104, 100, 103),
        _bar(2, 103, 106, 102, 105),  # swing high
        _bar(3, 105, 105, 102, 103),
        _bar(4, 103, 103, 100, 101),
        _bar(5, 101, 102, 100, 101),
        _bar(6, 101, 103, 100, 102),
        _bar(7, 102, 108, 102, 103),  # sweep: body 102->103, wick to 108, close below prior high 106
    ]
    sw = swing_points(bars, lookback=1)
    sweeps = detect_sweeps(bars, sw, wick_ratio=0.4)
    highs = [x for x in sweeps if x["side"] == "high"]
    assert highs and highs[-1]["idx"] == 7
    assert highs[-1]["swept_level"] <= bars[7]["high"]


def test_detect_deviations_counts_fast_return():
    # Build a 20-bar range [95, 105] with a bar that closes at 108 then snaps back.
    bars = []
    for i in range(20):
        c = 100.0 + (2.0 if i % 2 else -2.0)
        bars.append(_bar(i, c, c + 1, c - 1, c))
    # Deviation: idx 20 closes at 107 (above top 105), idx 21 closes at 100 (back in range).
    bars.append(_bar(20, 102, 108, 101, 107))
    bars.append(_bar(21, 107, 108, 99, 100))
    ranges = detect_ranges(bars[:20], min_bars=20, max_width_pct=15)
    assert ranges
    rng = ranges[0]
    # Expand end_idx so the deviation sits inside the range window we evaluate.
    rng = {**rng, "end_idx": 21}
    devs = detect_deviations(bars, rng, max_bars_back=3)
    assert devs and devs[0]["side"] == "high" and devs[0]["bars_back_in_range"] == 1


def test_classify_ranges_end_to_end():
    bars = [_bar(i, c, c + 1, c - 1, c) for i, c in enumerate(100.0 + (3.0 if i % 2 == 0 else -3.0) for i in range(60))]
    out = classify_ranges(bars, min_bars=20, max_width_pct=10)
    assert out["n_bars"] == 60
    assert len(out["ranges"]) == 1
    assert len(out["deviations_per_range"]) == 1


# ---------- MCP tool ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_range_detect_live_source(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "range_detect",
            venue="binanceusdm",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            lookback_bars=100,
            min_bars=20,
            max_width_pct=10,
            source="live",
        )
        d = env["data"]
        assert d["n_bars_evaluated"] >= 20
        # FakeEx oscillates mildly inside ~5-6 % band → a range should emerge.
        assert d["n_ranges"] >= 1
        assert d["latest_range"]["bottom"] < d["latest_range"]["top"]
    finally:
        await rt.aclose()


async def test_range_detect_store_source_missing(state, tmp_path):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "r.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    await rt.store.init()
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "range_detect", venue="binanceusdm", symbol="BTC/USDT:USDT", source="store")
        assert env["ok"] is False
    finally:
        await rt.aclose()
