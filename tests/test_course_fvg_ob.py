import numpy as np
import pytest

from tradedesk.analytics import markup
from tradedesk.analytics.course_fvg_ob import (
    HOUR_MS,
    MAKER,
    MARKET_SIDE,
    Bars,
    bos_up,
    breaker_zones,
    fvg_zones,
    limit_long,
    ob_zones,
    untouched,
)
from tradedesk.analytics.course_s14_ema import atr as atr_series
from tradedesk.analytics.course_s18_trend import pos_from_seg, segments

T0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC


def walk(n: int, seed: int, bar_ms: int = 4 * HOUR_MS) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.concatenate([[100.0], c[:-1]]) + rng.normal(0, 0.3, n)
    h = np.maximum(o, c) + rng.exponential(0.5, n)
    lo = np.minimum(o, c) - rng.exponential(0.5, n)
    t = T0 + np.arange(n, dtype=np.int64) * bar_ms
    return Bars(t, o, h, lo, c, bar_ms)


def as_rows(b: Bars) -> list[dict]:
    return [{"ts": int(b.t[i]), "open": b.o[i], "high": b.h[i], "low": b.low[i], "close": b.c[i]} for i in range(len(b))]


def hourly(o, h, lo, c, t0=T0) -> Bars:
    t = t0 + np.arange(len(c), dtype=np.int64) * HOUR_MS
    return Bars(t, np.asarray(o, float), np.asarray(h, float), np.asarray(lo, float), np.asarray(c, float), HOUR_MS)


# ---------------------------------------------------------------- detection matches markup


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_fvgs_match_markup_both_sides(seed):
    b = walk(300, seed)
    pos = pos_from_seg(segments(b.t))
    a = np.full(len(b), 1.0)
    ref = markup.fvgs(as_rows(b))
    bull = [(z.form - 1, z.levels["full"], z.levels["near"]) for z in fvg_zones(b, pos, a)]
    assert bull == [(g["idx"], g["bottom"], g["top"]) for g in ref if g["type"] == "bull"]
    m = b.mirrored()
    bear = [(z.form - 1, -z.levels["near"], -z.levels["full"]) for z in fvg_zones(m, pos, a)]
    assert bear == [(g["idx"], g["bottom"], g["top"]) for g in ref if g["type"] == "bear"]
    assert bull and bear


@pytest.mark.parametrize("seed", [5, 6, 7, 8, 9])
def test_order_blocks_and_flags_match_markup(seed):
    b = walk(400, seed)
    pos = pos_from_seg(segments(b.t))
    a = np.full(len(b), 1.2)
    rows = as_rows(b)
    st = markup.structure(rows, markup.fractals(rows))
    ref = [x for x in markup.order_blocks(rows, markup.fvgs(rows), st["events"], 1.2) if x["type"] == "bull"]
    mine = ob_zones(b, pos, a, bos_up(b, pos))
    assert [z.meta["i"] for z in mine] == [x["idx"] for x in ref]
    for z, x in zip(mine, ref, strict=True):
        assert ("imp" in z.arms) == x["impulsive"]
        assert ("fvg" in z.arms) == x["fvg_after"]
        assert ("bos" in z.arms) == x["bos_after"]
        assert ("valid" in z.arms) == (x["score"] == 3)
        assert z.levels["body"] == x["body_high"] and z.levels["wick"] == x["zone_high"]
    assert any("valid" in z.arms for z in mine)


def test_bos_up_matches_markup_events():
    b = walk(500, 11)
    pos = pos_from_seg(segments(b.t))
    rows = as_rows(b)
    ev = markup.structure(rows, markup.fractals(rows))["events"]
    assert list(np.flatnonzero(bos_up(b, pos))) == sorted({e["idx"] for e in ev if e["type"] == "bos_up"})


def test_breakers_match_markup():
    seen = {"bimp": 0, "fob": 0, "valid": 0, "n": 0}
    for seed in range(21, 61):
        b = walk(55, seed)  # shorter than the 60-bar break window: every markup break is inside it
        pos = pos_from_seg(segments(b.t))
        a = np.full(len(b), 1.0)
        rows = as_rows(b)
        ref = [x for x in markup.order_blocks(rows, markup.fvgs(rows), [], 1.0) if x["type"] == "bear" and x["state"] == "broken"]
        mine = breaker_zones(b, pos, a)
        assert [z.meta["i"] for z in mine] == [x["idx"] for x in ref]
        for z, x in zip(mine, ref, strict=True):
            assert ("bimp" in z.arms) == x["break_impulsive"]
            assert ("fob" in z.arms) == x["fvg_on_break"]
            assert z.levels["body"] == x["body_high"] and z.far == x["zone_low"]
            seen["n"] += 1
            for k in ("bimp", "fob", "valid"):
                seen[k] += k in z.arms
    assert all(v > 0 for v in seen.values()), seen


def test_ob_arm_is_latest_flag_bar():
    # i bearish, e engulfs with a 3-unit body (>= ATR 1), FVG with middle e (low[e+1] > high[i]), BOS at e+1
    o = [10, 11, 12, 11.0, 10.5, 13.6, 14.2]
    c = [11, 12, 11, 10.0, 13.5, 14.0, 15.0]
    h = [11.2, 12.1, 12.5, 11.1, 13.6, 14.3, 15.2]
    lo = [9.9, 10.9, 10.8, 9.8, 10.4, 13.0, 14.1]
    b = Bars(T0 + np.arange(7, dtype=np.int64) * 4 * HOUR_MS, *(np.asarray(x, float) for x in (o, h, lo, c)), 4 * HOUR_MS)
    pos = pos_from_seg(segments(b.t))
    zs = ob_zones(b, pos, np.full(7, 1.0), bos_up(b, pos))
    z = next(z for z in zs if z.meta["i"] == 3)
    assert z.form == 4 and z.arms["imp"] == 4 and z.arms["fvg"] == 5
    assert z.arms["bos"] == 4  # bar 4 closes above the fractal high 12.5 of bar 2 (active from bar 4)
    assert z.arms["valid"] == 5
    assert z.levels == {"wick": 11.1, "body": 11.0, "bmid": 10.5}
    assert z.far == 9.8


# ---------------------------------------------------------------- no lookahead


@pytest.mark.parametrize("seed", [31, 32, 33])
def test_zones_before_t_do_not_change_when_later_bars_change(seed):
    b = walk(600, seed)
    t = 350
    rng = np.random.default_rng(seed + 100)
    c2 = b.c.copy()
    c2[t + 1 :] = c2[t + 1 :] + rng.normal(0, 3, c2.size - t - 1)
    o2, h2, l2 = b.o.copy(), b.h.copy(), b.low.copy()
    o2[t + 1 :] += rng.normal(0, 3, c2.size - t - 1)
    h2[t + 1 :] = np.maximum(o2[t + 1 :], c2[t + 1 :]) + 0.2
    l2[t + 1 :] = np.minimum(o2[t + 1 :], c2[t + 1 :]) - 0.2
    b2 = Bars(b.t, o2, h2, l2, c2, b.bar_ms)
    pos = pos_from_seg(segments(b.t))
    a1, a2 = atr_series(b, 14, pos), atr_series(b2, 14, pos)
    assert np.array_equal(a1[: t + 1], a2[: t + 1], equal_nan=True)
    bo1, bo2 = bos_up(b, pos), bos_up(b2, pos)
    assert np.array_equal(bo1[: t + 1], bo2[: t + 1])
    htf = np.arange(len(b)) % 3 == 0

    res = []
    for bb, a_, bo_ in ((b, a1, bo1), (b2, a2, bo2)):
        zs = fvg_zones(bb, pos, a_, htf) + ob_zones(bb, pos, a_, bo_) + breaker_zones(bb, pos, a_)
        res.append(
            {
                (z.kind, z.form, z.meta.get("i"), name): (arm, z.levels, z.far, z.atr, untouched(bb, z.form, arm, min(z.levels.values())))
                for z in zs
                for name, arm in z.arms.items()
                if arm <= t
            }
        )
    assert res[0] == res[1]
    assert len(res[0]) > 50


# ---------------------------------------------------------------- the limit trade


def test_untouched():
    b = hourly([5, 5, 5, 5], [6, 6, 6, 6], [4, 4.5, 3.9, 4], [5, 5, 5, 5])
    assert untouched(b, 0, 1, 4.2)
    assert not untouched(b, 0, 2, 4.0)
    assert untouched(b, 2, 2, 100.0)


def test_limit_fill_then_target_with_maker_costs():
    # start at the close of bar -1 (= T0); bar0 no touch, bar1 fills at 100, bar2 quiet, bar3 hits 104
    b = hourly([103, 102, 101, 102], [104, 102.5, 102.5, 104.5], [101, 99.5, 100.5, 101.5], [102, 101, 102, 104])
    tr = limit_long(b, T0, 3 * HOUR_MS, 10 * HOUR_MS, 100.0, 98.0, 104.0, react_px=2.0)
    assert tr.filled and tr.fill == 100.0 and tr.touch_ms == T0 + HOUR_MS
    assert tr.exit_kind == "target" and tr.exit == 104.0
    assert tr.r_gross == pytest.approx(2.0)
    assert tr.cost_r == pytest.approx((MAKER * 100 + MAKER * 104) / 2.0)
    assert tr.react == 1  # +2 (102) at bar2 high 102.5, never down to 98
    assert tr.mfe_r == pytest.approx(2.0)


def test_limit_fill_at_open_below_level_and_stop_on_fill_bar():
    b = hourly([103, 99, 99], [103.5, 99.5, 100], [102, 97.5, 98.5], [102, 98, 99])
    tr = limit_long(b, T0, 3 * HOUR_MS, 10 * HOUR_MS, 100.0, 98.0, 104.0, react_px=2.0)
    assert tr.fill == 99.0  # opened below the limit: filled at the open
    assert tr.exit_kind == "stop" and tr.exit == 98.0
    assert tr.r_gross == pytest.approx(-0.5)
    assert tr.cost_r == pytest.approx((MAKER * 99 + MARKET_SIDE * 98) / 2.0)
    assert tr.react == 0


def test_limit_stop_wins_a_bar_touching_both_and_gap_stop():
    b = hourly([101, 100.5, 101], [101.5, 105, 101], [100, 97, 101], [100.5, 101, 101])
    tr = limit_long(b, T0, 2 * HOUR_MS, 10 * HOUR_MS, 100.0, 98.0, 104.0)
    assert tr.exit_kind == "stop" and tr.r_gross == pytest.approx(-1.0)
    b = hourly([101, 100.5, 96], [101.5, 101, 97], [100, 99.5, 95], [100.5, 100.5, 96])
    tr = limit_long(b, T0, 2 * HOUR_MS, 10 * HOUR_MS, 100.0, 98.0, 104.0)
    assert tr.exit_kind == "stop_gap" and tr.exit == 96.0 and tr.r_gross == pytest.approx(-2.0)


def test_limit_not_filled_time_exit_and_penetration():
    b = hourly([101, 101, 101], [102, 102, 102], [100.0, 100.5, 100.5], [101, 101, 101])
    assert limit_long(b, T0, 3 * HOUR_MS, 2 * HOUR_MS, 100.0, 98.0, 104.0, pen=0.1).filled is False
    tr = limit_long(b, T0, 3 * HOUR_MS, 2 * HOUR_MS, 100.0, 98.0, 104.0)
    assert tr.filled and tr.exit_kind == "time" and tr.exit == 101.0
    assert tr.r_gross == pytest.approx(0.5)
    assert tr.react == -1


def test_limit_data_end_on_hole_and_no_path():
    t = T0 + np.array([0, 1, 60], dtype=np.int64) * HOUR_MS  # a 59-hour hole after bar 1
    b = Bars(
        t, np.array([101, 100.5, 120.0]), np.array([101.5, 101, 121]), np.array([100.5, 99.8, 119]), np.array([101, 100.8, 120]), HOUR_MS
    )
    tr = limit_long(b, T0, 3 * HOUR_MS, 10 * HOUR_MS, 100.0, 98.0, 104.0)
    assert tr.filled and tr.exit_kind == "data_end" and tr.exit == 100.8
    assert limit_long(b, T0 + 200 * HOUR_MS, HOUR_MS, HOUR_MS, 100.0, 98.0, 104.0) is None
    with pytest.raises(ValueError):
        limit_long(b, T0, HOUR_MS, HOUR_MS, 100.0, 101.0, 104.0)


def test_short_is_long_on_mirrored_prices():
    b = hourly([97, 98, 99, 98], [99, 100.5, 99.5, 98.5], [96, 97.5, 97.5, 95.5], [98, 99, 98, 96])
    tr = limit_long(b.mirrored(), T0, 3 * HOUR_MS, 10 * HOUR_MS, -100.0, -102.0, -96.0)
    assert tr.filled and tr.exit_kind == "target" and tr.r_gross == pytest.approx(2.0)
    assert tr.cost_r == pytest.approx((MAKER * 100 + MAKER * 96) / 2.0)
