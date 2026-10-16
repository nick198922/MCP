"""Analytic references for analytics/markup.py and the BingX forex symbol mapping."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics import markup as mk
from tradedesk.tools.markup import px, resolve_symbol

H = 3_600_000


def bar(i: int, o: float, h: float, lo: float, c: float, v: float = 1.0, t0: int = 0) -> dict:
    return {"ts": t0 + i * H, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def ms(y: int, m: int, d: int, hh: int = 0) -> int:
    return int(datetime(y, m, d, hh, tzinfo=UTC).timestamp() * 1000)


def test_ema_seeded_with_sma():
    assert mk.ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]
    assert mk.last_ema([7.0] * 50, 21) == pytest.approx(7.0)
    assert mk.last_ema([1.0, 2.0], 3) is None


def test_atr_and_rsi():
    bars = [bar(i, 10, 11, 9, 10) for i in range(30)]
    assert mk.atr(bars) == pytest.approx(2.0)
    assert mk.rsi([float(i) for i in range(30)]) == 100.0
    assert mk.rsi([10.0, 11.0] * 20) == pytest.approx(50.0, abs=3.0)


def test_anchor_start_periods():
    t = ms(2026, 10, 16, 13)  # Friday
    assert mk.anchor_start(t, "week") == datetime(2026, 10, 12, tzinfo=UTC)
    assert mk.anchor_start(t, "month") == datetime(2026, 10, 1, tzinfo=UTC)
    assert mk.anchor_start(t, "quarter") == datetime(2026, 10, 1, tzinfo=UTC)
    assert mk.anchor_start(t, "year") == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        mk.anchor_start(t, "decade")


def test_anchored_vwap_and_band():
    q = ms(2026, 10, 1)
    bars = [
        {"ts": q - H, "open": 1, "high": 1000, "low": 1000, "close": 1000, "volume": 99},  # previous quarter: excluded
        {"ts": q, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
        {"ts": q + H, "open": 20, "high": 20, "low": 20, "close": 20, "volume": 3},
    ]
    v = mk.anchored_vwap(bars, "quarter")
    assert v["bars"] == 2 and v["vwap"] == pytest.approx(17.5)
    assert v["sd"] == pytest.approx(math.sqrt((7.5**2 + 3 * 2.5**2) / 4))
    assert v["upper_1sd"] == pytest.approx(17.5 + v["sd"])


def test_volume_profile_finds_the_heavy_shelf():
    bars = [bar(i, 100.5, 101, 100, 100.5, v=50) for i in range(10)] + [bar(10, 105, 110, 100, 108, v=1)]
    vp = mk.volume_profile(bars, bins=20)
    assert 100 <= vp["poc"] <= 101
    assert vp["val"] == pytest.approx(100) and vp["vah"] <= 102
    assert vp["range_low"] == 100 and vp["range_high"] == 110


def test_fractals_three_candles():
    bars = [bar(0, 1, 2, 0.5, 1), bar(1, 1, 3, 0.2, 2), bar(2, 2, 2.5, 1, 2), bar(3, 2, 2.6, 0.9, 2)]
    f = mk.fractals(bars)
    assert [s["idx"] for s in f["highs"]] == [1]
    assert [s["idx"] for s in f["lows"]] == [1]


def test_structure_close_is_bos_wick_is_sweep():
    bars = [
        bar(0, 9, 9.5, 8.5, 9),
        bar(1, 9, 10, 8.8, 9.5),  # swing high 10
        bar(2, 9.5, 9.6, 8.6, 9),
        bar(3, 9, 9.4, 8.7, 9.2),
        bar(4, 9.2, 10.4, 9, 9.6),  # wick above 10, close back below: sweep
        bar(5, 9.6, 12, 9.5, 11.5),  # swing high 12
        bar(6, 11.5, 11.8, 10.8, 11),
        bar(7, 11, 11.5, 10.6, 11.2),
        bar(8, 11.2, 12.6, 11, 12.4),  # close above 12: BOS up
    ]
    st = mk.structure(bars, mk.fractals(bars))
    kinds = [(e["type"], e["level"]) for e in st["events"] if e["type"] in ("sweep_high", "bos_up")]
    assert kinds == [("sweep_high", 10.0), ("bos_up", 12.0)]
    assert st["trend"] == "up" and st["last_bos"]["level"] == 12.0


def test_fvg_states():
    bars = [
        bar(0, 9, 10, 8, 9.8),
        bar(1, 9.8, 12, 9.8, 11.8),
        bar(2, 11.8, 12.5, 11, 12.2),  # bull gap [10, 11], mid 10.5
        bar(3, 12.2, 12.4, 10.4, 11.0),  # reaches below the mid: balanced
    ]
    g = mk.fvgs(bars)
    assert len(g) == 1 and g[0]["type"] == "bull" and (g[0]["bottom"], g[0]["top"], g[0]["mid"]) == (10, 11, 10.5)
    assert g[0]["state"] == "balanced"
    assert mk.fvgs([*bars, bar(4, 11, 11.2, 9.9, 10)])[0]["state"] == "filled"


def test_order_block_flags_and_state():
    bars = [
        bar(0, 9.6, 9.8, 9.3, 9.5),
        bar(1, 10, 10.2, 8.8, 9),  # bearish candle: the order block
        bar(2, 9, 10.6, 9, 10.5),  # bullish engulf (close above 10)
        bar(3, 10.5, 11.5, 10.4, 11.4),  # FVG from bar 1 to bar 3 (low 10.4 > high 10.2)
        bar(4, 11.4, 11.6, 11, 11.5),
    ]
    st = mk.structure(bars, mk.fractals(bars))
    obs = mk.order_blocks(bars, mk.fvgs(bars), st["events"], atr_value=1.0)
    ob = next(o for o in obs if o["idx"] == 1)
    assert ob["type"] == "bull" and (ob["zone_low"], ob["zone_high"]) == (8.8, 10.2)
    assert ob["impulsive"] and ob["fvg_after"] and ob["state"] == "untested"
    broken = mk.order_blocks([*bars, bar(5, 11.5, 11.5, 8.5, 8.6)], [], [], None)
    assert next(o for o in broken if o["idx"] == 1)["state"] == "broken"


def test_equal_levels_pool():
    swings = [{"idx": 1, "price": 100.0}, {"idx": 5, "price": 100.05}, {"idx": 9, "price": 103.0}]
    pools = mk.equal_levels(swings, tol=0.1)
    assert pools == [{"low": 100.0, "high": 100.05, "touches": 2, "last_idx": 5}]


def test_dealing_range_ote_levels():
    bars = [bar(i, 10, 10.5, 9.5, 10) for i in range(6)]
    bars[1]["low"] = 8.0
    bars[5]["high"] = 14.0
    bars[5]["close"] = 12.5
    dr = mk.dealing_range(bars, {"type": "bos_up", "idx": 4, "swing_idx": 0, "level": 10.5})
    assert (dr["low"], dr["high"]) == (8.0, 14.0)
    assert dr["levels"]["0.5"] == pytest.approx(11.0)
    assert dr["levels"]["0.618"] == pytest.approx(14 - 0.618 * 6)
    assert dr["price_zone"] == "premium" and dr["in_ote"] is False


def test_asia_session_and_hunt_window():
    day = ms(2026, 10, 16)  # CEST: Frankfurt opens 06:00 UTC; BST: London 07:00 UTC, hunt until 10:00 UTC
    bars = [{"ts": day + h * H, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1} for h in range(6)]
    bars.append({"ts": day + 6 * H, "open": 100, "high": 101.5, "low": 99.5, "close": 100.2, "volume": 1})
    s = mk.asia_session(bars, bars[-1]["ts"])
    assert (s["asia_high"], s["asia_low"]) == (101, 99)
    assert s["frankfurt_open_ms"] == day + 6 * H and s["london_open_ms"] == day + 7 * H and s["hunt_until_ms"] == day + 10 * H
    assert s["swept_high"] is True and s["swept_low"] is False


def test_bingx_forex_symbol_and_rounding():
    assert resolve_symbol("bingx", "EURUSD") == "NCFXEUR2USD/USDT:USDT"
    assert resolve_symbol("bingx", "gbp/usd") == "NCFXGBP2USD/USDT:USDT"
    assert resolve_symbol("bingx", "BTC/USDT:USDT") == "BTC/USDT:USDT"
    assert resolve_symbol("binanceusdm", "EURUSD") == "EURUSD"
    assert px(1.0876543) == 1.08765 and px(115234.56) == 115235.0


def test_one_event_per_bar_and_kind():
    bars = [
        bar(0, 9, 9.5, 8.5, 9),
        bar(1, 9, 11, 8.8, 9.5),  # swing high 11
        bar(2, 9.5, 9.6, 8.6, 9),
        bar(3, 9, 9.4, 8.7, 9.2),
        bar(4, 9.2, 10, 8.9, 9.6),  # lower swing high 10
        bar(5, 9.6, 9.7, 9.1, 9.5),
        bar(6, 9.5, 9.6, 9.0, 9.4),
        bar(7, 9.4, 11.6, 9.3, 10.6),  # takes both: closes above 10 (BOS) but below 11 (sweep)
    ]
    ev = [e for e in mk.structure(bars, mk.fractals(bars))["events"] if e["idx"] == 7]
    assert [(e["type"], e["level"]) for e in ev] == [("bos_up", 10.0), ("sweep_high", 11.0)]


def test_swing_structure_course_rules():
    rows = [
        (10, 10.5, 9.5, 10),
        (10, 11, 9.8, 10.8),  # swing high 11
        (10.8, 10.6, 9.6, 9.9),
        (9.9, 10.2, 9.0, 9.5),  # swing low 9.0
        (9.5, 10.4, 9.2, 10.3),
        (10.3, 11.4, 10.2, 11.3),  # close above 11: uptrend, protected HL 9.0
        (11.3, 12, 11, 11.8),  # new high without a pullback: HH extends to 12, no event
        (11.8, 11.6, 10.8, 11),
        (11, 11.2, 10.5, 10.7),  # pullback swing low 10.5
        (10.7, 11.5, 10.6, 11.4),
        (11.4, 12.5, 11.3, 12.4),  # close above HH 12 after the pullback: BOS, HL moves to 10.5
        (12.4, 12.3, 11.0, 11.2),
        (11.2, 11.5, 10.4, 10.6),  # wick below HL 10.5, close above: sweep
        (10.6, 10.8, 10.0, 10.2),  # close below HL: change of character
    ]
    bars = [bar(i, *r) for i, r in enumerate(rows)]
    ss = mk.swing_structure(bars, mk.fractals(bars))
    assert [(e["type"], e["level"]) for e in ss["events"]] == [
        ("bos_up", 11.0),
        ("bos_up", 12.0),
        ("sweep_low", 10.5),
        ("choch_down", 10.5),
    ]
    assert ss["trend"] == "down" and ss["last_break"]["type"] == "choch_down"
    assert (ss["protected"]["label"], ss["protected"]["price"], ss["protected"]["idx"]) == ("LH", 12.5, 10)
    assert (ss["extreme"]["label"], ss["extreme"]["price"]) == ("LL", 10.0)
    dr = mk.swing_dealing_range(bars, ss)
    assert (dr["low"], dr["high"], dr["direction"]) == (10.0, 12.5, "down") and dr["price_zone"] == "discount"
    up = mk.swing_structure(bars[:11], mk.fractals(bars[:11]))
    assert (up["trend"], up["protected"]["label"], up["protected"]["price"], up["extreme"]["price"]) == ("up", "HL", 10.5, 12.5)
