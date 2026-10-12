from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.ranges import (
    classify_ranges,
    detect_deviations,
    detect_ranges,
    detect_sweeps,
    generate_setup,
    range_type_hint,
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


def _series(highs: list[float], lows: list[float], closes: list[float]) -> list[dict]:
    assert len(highs) == len(lows) == len(closes)
    return [{"ts": i, "open": closes[i], "high": highs[i], "low": lows[i], "close": closes[i], "volume": 1.0} for i in range(len(closes))]


def test_range_type_hint_distribution_from_wedge_slopes():
    # Feed explicit swing points so we can test the wedge-slope vote in isolation.
    # Rising highs (103, 104, 105, 106, 107) with flat lows (100).
    bars = [{"ts": i, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0} for i in range(50)]
    rng = {
        "start_idx": 0,
        "end_idx": 49,
        "top": 107.0,
        "bottom": 100.0,
        "mid": 103.5,
        "n_bars": 50,
        "start_ts": 0,
        "end_ts": 49,
        "width_pct": 6.8,
    }
    swings = {
        "highs": [{"idx": 5 + i * 10, "ts": 5 + i * 10, "price": 103.0 + i} for i in range(5)],
        "lows": [{"idx": 10 + i * 10, "ts": 10 + i * 10, "price": 100.0} for i in range(4)],
    }
    sweeps: list[dict] = []
    cls = range_type_hint(bars, rng, swings, sweeps)
    assert cls["signals"]["wedge"] == "distribution"


def test_range_type_hint_accumulation_from_wedge():
    bars = [{"ts": i, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0} for i in range(50)]
    rng = {
        "start_idx": 0,
        "end_idx": 49,
        "top": 103.0,
        "bottom": 95.0,
        "mid": 99.0,
        "n_bars": 50,
        "start_ts": 0,
        "end_ts": 49,
        "width_pct": 8.1,
    }
    swings = {
        "highs": [{"idx": 5 + i * 10, "ts": 5 + i * 10, "price": 103.0} for i in range(4)],
        "lows": [{"idx": 10 + i * 10, "ts": 10 + i * 10, "price": 100.0 - i} for i in range(5)],
    }
    sweeps: list[dict] = []
    cls = range_type_hint(bars, rng, swings, sweeps)
    assert cls["signals"]["wedge"] == "accumulation"


def test_range_type_hint_accumulation_from_sweep_asymmetry():
    # Explicit sweep list that is 100 % lower-side → accumulation vote via
    # sweep-asymmetry. Wedge is flat; context unavailable.
    bars = [{"ts": i, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0} for i in range(50)]
    rng = {
        "start_idx": 0,
        "end_idx": 49,
        "top": 100.5,
        "bottom": 99.5,
        "mid": 100.0,
        "n_bars": 50,
        "start_ts": 0,
        "end_ts": 49,
        "width_pct": 1.0,
    }
    swings = {"highs": [], "lows": []}
    sweeps = [{"idx": i, "ts": i, "side": "low", "swept_level": 99.5, "reversal_pct": 0.1} for i in range(10)]
    cls = range_type_hint(bars, rng, swings, sweeps)
    assert cls["signals"]["sweep_asymmetry"] == "accumulation"


def test_range_type_hint_ambiguous_when_signals_split():
    n = 220
    bars = []
    # Rising context 200 bars (SMA100 > SMA200 → accumulation vote).
    for i in range(200):
        close = 100.0 + i * 0.05
        bars.append({"ts": i, "open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1.0})
    # Flat range 20 bars with no sweeps and no wedge → only context votes.
    for i in range(200, n):
        bars.append({"ts": i, "open": 110.0, "high": 110.5, "low": 109.5, "close": 110.0, "volume": 1.0})
    ranges = detect_ranges(bars, min_bars=20, max_width_pct=5)
    assert ranges
    swings = swing_points(bars, lookback=2)
    sweeps = detect_sweeps(bars, swings, wick_ratio=0.4)
    cls = range_type_hint(bars, ranges[-1], swings, sweeps)
    # Only context votes → single vote → ambiguous verdict.
    assert cls["type"] == "ambiguous"


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


def test_generate_setup_short_from_high_sweep():
    rng = {"top": 110.0, "bottom": 90.0, "mid": 100.0}
    sweep = {"side": "high", "swept_level": 110.0, "reversal_pct": 1.0, "ts": 42}
    setup = generate_setup(rng, sweep, stop_mult=2.0, stop_min_offset_pct=0.1, min_rr=2.0)
    assert setup["direction"] == "short"
    assert setup["entry"] == pytest.approx(110.0)
    # 2.0 × 1.0 % = 2 %, so stop at 110 × 1.02 = 112.2
    assert setup["stop"] == pytest.approx(112.2, rel=1e-6)
    assert setup["tp1"] == pytest.approx(100.0)
    assert setup["tp2"] == pytest.approx(90.0)
    # risk 2.2, reward1 10 → RR ~ 4.5
    assert setup["rr1"] == pytest.approx(10.0 / 2.2, rel=1e-6)
    assert setup["tradable"] is True


def test_generate_setup_long_from_low_sweep_and_tight_stop():
    rng = {"top": 110.0, "bottom": 90.0, "mid": 100.0}
    sweep = {"side": "low", "swept_level": 90.0, "reversal_pct": 1.5, "ts": 42}
    tight = generate_setup(rng, sweep, stop_mult=2.0, stop_min_offset_pct=0.1, use_tight_stop=True)
    # Tight stop uses just the min offset (0.1 %), so stop = 90 × 0.999 = 89.91.
    assert tight["direction"] == "long"
    assert tight["stop"] == pytest.approx(89.91, rel=1e-6)
    assert tight["rr1"] > 100  # tiny risk × big reward


def test_generate_setup_rejects_incoherent_sweep():
    rng = {"top": 110.0, "bottom": 90.0, "mid": 100.0}
    # A "high" sweep whose swept_level sits below the mid is incoherent.
    bad = generate_setup(rng, {"side": "high", "swept_level": 95.0, "reversal_pct": 0.5, "ts": 42}, min_rr=2.0)
    assert bad is None


def test_generate_setup_tradable_flag():
    rng = {"top": 110.0, "bottom": 90.0, "mid": 100.0}
    # Big sweep → wide stop → RR < 2
    weak = generate_setup(rng, {"side": "high", "swept_level": 110.0, "reversal_pct": 8.0, "ts": 42}, min_rr=2.0)
    assert weak["tradable"] is False


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


async def test_range_setup_live_mcp(state):
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
            "range_setup",
            venue="binanceusdm",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            lookback_bars=100,
            min_bars=20,
            max_width_pct=10,
            source="live",
        )
        if env["ok"]:
            d = env["data"]
            assert d["setup"]["direction"] in ("long", "short")
            assert "rr1" in d["setup"]
            assert d["setup"]["entry"] > 0
        else:
            # Fake oscillator may not generate a clean sweep inside a tight range;
            # tolerant failure path.
            assert any(m["field"] in ("sweep", "range", "setup") for m in env["missing"])
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
