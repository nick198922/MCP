import json
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import Bars, atr, ema
from tradedesk.analytics.course_s18_trend import pos_from_seg, resample_partial, segments
from tradedesk.analytics.deviation_weekly_trend import (
    DAY_MS,
    HOUR_MS,
    MARKET_SIDE,
    Params,
    Setup,
    classify,
    deviation_setups,
    last_daily_before,
    placebo_setups,
    target_price,
    trade_setups,
)

T0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
B4 = 4 * HOUR_MS
FIX = Path(__file__).parent / "fixtures" / "binance_vision_ethusdt_dwt_2026-10.json"


def bars(o, h, lo, c, bar_ms: int = B4, t0: int = T0) -> Bars:
    o, h, lo, c = (np.asarray(x, float) for x in (o, h, lo, c))
    return Bars(t0 + np.arange(o.size, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def pos_of(b: Bars) -> np.ndarray:
    return pos_from_seg(segments(b.t))


# ---------------------------------------------------------------- band and target


def test_classify_band():
    # band = [min(100, 90) - 0.5 * 10, max(100, 90) + 0.25 * 10] = [85, 102.5]; C needs ld > 102.5 + 2.5 = 105
    assert classify(95, 110, 100, 90, 10) == "T"
    assert classify(85, 110, 100, 90, 10) == "T"  # edges are inside
    assert classify(102.5, 110, 100, 90, 10) == "T"
    assert classify(95, 102, 100, 90, 10) == "band_high"  # the band must lie below the range low
    assert classify(105.1, 110, 100, 90, 10) == "C"
    assert classify(104, 110, 100, 90, 10) == "gap"
    assert classify(84.9, 110, 100, 90, 10) == "deep"
    assert classify(95, 110, np.nan, 90, 10) == "na"
    assert classify(95, 110, 100, 90, 0) == "na"


def test_target_price():
    # entry 100, stop 90: 1.5R = 115
    assert target_price(100, 90, (112, 130)) == (130, 1)
    assert target_price(100, 90, (120, 116)) == (116, 1)
    assert target_price(100, 90, (115, 140)) == (115, 0)
    assert target_price(100, 90, (np.nan, 110)) == (120, -1)  # fallback 2R
    with pytest.raises(ValueError):
        target_price(100, 100, (120,))


# ---------------------------------------------------------------- daily context


def test_last_daily_before_uses_completed_day_only():
    d = bars([1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4], DAY_MS)
    vals = np.array([10.0, 20.0, 30.0, 40.0])
    b = Bars(np.array([T0, T0 + DAY_MS, T0 + DAY_MS + 8 * HOUR_MS, T0 + 2 * DAY_MS]), *(np.ones(4) for _ in range(4)), B4)
    out = last_daily_before(b, d, vals)
    assert np.isnan(out[0])  # no day closed before the first open
    assert out[1] == 10 and out[2] == 10  # day 0 closes at the open of day 1; day 1 is still forming at 08:00
    assert out[3] == 20
    vals2 = vals.copy()
    vals2[1] = -999.0  # the day containing bar 2 changes: bar 2 must not see it
    assert last_daily_before(b, d, vals2)[2] == 10


def test_last_daily_before_stale_is_nan():
    d = bars([1], [1], [1], [1], DAY_MS)
    b = Bars(np.array([T0 + 4 * DAY_MS]), np.ones(1), np.ones(1), np.ones(1), np.ones(1), B4)
    assert np.isnan(last_daily_before(b, d, np.array([5.0]))[0])
    assert last_daily_before(b, d, np.array([5.0]), max_age_ms=4 * DAY_MS)[0] == 5.0


# ---------------------------------------------------------------- detector on a hand-made range


def flat_range(n: int = 80) -> tuple[list[float], list[float], list[float], list[float]]:
    """Bars oscillating inside [100, 110]: every bar's range is 10, so ATR = 10 and width = 1 ATR."""
    o = [104.0 if i % 2 == 0 else 106.0 for i in range(n)]
    c = [106.0 if i % 2 == 0 else 104.0 for i in range(n)]
    return o, [110.0] * n, [100.0] * n, c


def ctx(b: Bars, e100: float, e200: float, atr_d: float):
    pos = pos_of(b)
    a4 = atr(b, 14, pos)
    n = len(b)
    return pos, a4, np.full(n, np.nan), np.full(n, e100), np.full(n, e200), np.full(n, atr_d)


def test_sweep_into_band_is_T_with_registered_stop_and_target():
    o, h, lo, c = flat_range()
    o += [104.0]
    h += [104.0]
    lo += [95.0]  # sweep under lr = 100 into the band [96 - 2, 98 + 1] = [94, 99]
    c += [101.0]  # back in the range, below the middle 105
    b = bars(o, h, lo, c)
    pos, a4, e4, d100, d200, dat = ctx(b, 98.0, 96.0, 4.0)
    st = [x for x in deviation_setups(b, pos, a4, e4, d100, d200, dat) if x.s == len(b) - 1]
    assert len(st) == 1
    x = st[0]
    assert (x.kind, x.group, x.lr, x.ld, x.r) == ("sweep", "T", 100.0, 95.0, len(b) - 1)
    assert x.stop == pytest.approx(95.0 - 0.5 * a4[-1])
    risk = 100 - x.stop  # ~ 10: the middle (5 above the entry) is closer than 1.5R, no 4h EMA -> 2R
    assert risk > 5 / 1.5
    assert x.target == pytest.approx(100 + 2 * risk) and x.target_kind == "fixed"
    assert x.e100 == 98.0 and x.atr_d == 4.0


def test_sweep_target_fallback_and_C_group():
    o, h, lo, c = flat_range()
    o += [104.0]
    h += [104.0]
    lo += [99.0]
    c += [101.0]
    b = bars(o, h, lo, c)
    pos, a4, e4, d100, d200, dat = ctx(b, 90.0, 88.0, 4.0)  # band [86, 91]; 99 > 91 + 1 -> C
    x = [s for s in deviation_setups(b, pos, a4, e4, d100, d200, dat) if s.s == len(b) - 1][0]
    assert x.group == "C"
    risk = 100 - x.stop
    assert x.target == pytest.approx(100 + 2 * risk) and x.target_kind == "fixed"  # mid 105 < 1.5R


def test_deviation_three_closes_only_in_primary():
    o, h, lo, c = flat_range()
    o += [101.0, 98.0, 98.0, 98.0]
    h += [101.0, 99.0, 99.0, 103.0]
    lo += [96.0, 97.0, 97.0, 97.5]
    c += [98.0, 98.0, 98.0, 102.0]  # three closes under 100, back at bar +3
    b = bars(o, h, lo, c)
    pos, a4, e4, d100, d200, dat = ctx(b, 98.0, 96.0, 4.0)
    s0 = len(b) - 4
    main = [x for x in deviation_setups(b, pos, a4, e4, d100, d200, dat) if x.s == s0]
    assert len(main) == 1 and main[0].kind == "dev" and main[0].r == s0 + 3 and main[0].ld == 96.0 and main[0].group == "T"
    card = [x for x in deviation_setups(b, pos, a4, e4, d100, d200, dat, Params(width_atr=8.0, kmax=2)) if x.s == s0]
    assert card == []


def test_placebo_touch_without_breach():
    o, h, lo, c = flat_range(80)
    # 12 bars high in the range (closes above the middle 105, lows above the touch zone), then a touch of 100
    n0 = len(o)
    for _ in range(12):
        o.append(107.0)
        h.append(109.0)
        lo.append(106.0)
        c.append(108.0)
    o.append(107.0)
    h.append(107.5)
    lo.append(100.5)  # within 0.25 ATR of lr = 100, no breach
    c.append(101.0)
    b = bars(o, h, lo, c)
    pos = pos_of(b)
    a4 = atr(b, 14, pos)
    ps = [x for x in placebo_setups(b, pos, a4, np.full(len(b), np.nan), 2.0) if x.s > n0]
    assert [x.s for x in ps] == [len(b) - 1]
    x = ps[0]
    assert x.group == "P" and x.lr == 100.0 and x.stop == pytest.approx(100 - 2.0 * a4[-1])


# ---------------------------------------------------------------- causality on a random walk


def walk_1h(n: int, seed: int) -> Bars:
    rng = np.random.default_rng(seed)
    lc = np.log(100) + np.cumsum(rng.normal(0, 0.008, n))
    c = np.exp(lc)
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(rng.exponential(0.003, n))
    lo = np.minimum(o, c) * np.exp(-rng.exponential(0.003, n))
    return bars(o, h, lo, c, HOUR_MS)


def setups_of(h1: Bars, m_t: float = 2.0) -> tuple[list[Setup], np.ndarray]:
    seg = segments(h1.t)
    b4, s4 = resample_partial(h1, seg, 4, 4)
    d, sd = resample_partial(h1, seg, 24, 24)
    p4, dp = pos_from_seg(s4), pos_from_seg(sd)
    a4 = atr(b4, 14, p4)
    e4 = ema(b4.c, 100, p4)
    d100 = last_daily_before(b4, d, ema(d.c, 100, dp))
    d200 = last_daily_before(b4, d, ema(d.c, 200, dp))
    dat = last_daily_before(b4, d, atr(d, 14, dp))
    loose = Params(placebo_above=3, placebo_touch_atr=1.0)  # more placebo events on a short random walk
    out = deviation_setups(b4, p4, a4, e4, d100, d200, dat) + placebo_setups(b4, p4, a4, e4, m_t, loose)
    return out, b4.t + b4.bar_ms


@pytest.mark.parametrize("seed", [3, 6])
def test_setups_before_t_do_not_depend_on_later_bars(seed):
    h1 = walk_1h(24 * 330, seed)
    cut = 24 * 300  # a day boundary
    other = walk_1h(len(h1), seed + 100)
    k = h1.c[cut - 1] / other.o[cut]
    cat = lambda x, y: np.concatenate([x[:cut], y[cut:] * k])  # noqa: E731
    h2 = Bars(h1.t, cat(h1.o, other.o), cat(h1.h, other.h), cat(h1.low, other.low), cat(h1.c, other.c), HOUR_MS)
    a, close_a = setups_of(h1)
    b, close_b = setups_of(h2)
    cut_ms = int(h1.t[cut])
    before_a = [x for x in a if close_a[x.r] <= cut_ms]
    before_b = [x for x in b if close_b[x.r] <= cut_ms]
    assert [repr(x) for x in before_a] == [repr(x) for x in before_b]  # repr: NaN fields compare equal
    assert any(x.group in ("T", "C", "deep", "gap", "band_high") for x in before_a)  # the daily band is defined
    assert any(x.kind == "placebo" for x in before_a)
    assert len(a) != len(before_a)  # there were later setups that changed


# ---------------------------------------------------------------- trades


def test_trade_setups_one_at_a_time_and_costs():
    # 1h path: flat at 101 for 10 bars, dips to 100 (fill), then rallies to the target
    n = 60
    c = np.full(n, 101.0)
    lo = np.full(n, 100.8)
    h = np.full(n, 101.2)
    lo[2] = 99.5
    h[20] = 112.0
    path = bars(c, h, lo, c, HOUR_MS)
    close = path.t + HOUR_MS  # decision "bars" here are just indices into this close array
    st = Setup("sweep", 0, 0, 100.0, 110.0, 105.0, 95.0, "T", 95.0, 110.0, "fixed", 1.0, 1.0, 1.0, 1.0)
    st2 = Setup("sweep", 5, 5, 100.0, 110.0, 105.0, 95.0, "T", 95.0, 110.0, "fixed", 1.0, 1.0, 1.0, 1.0)
    st3 = Setup("sweep", 30, 30, 100.0, 110.0, 105.0, 95.0, "T", 95.0, 110.0, "fixed", 1.0, 1.0, 1.0, 1.0)
    out = trade_setups(path, [st3, st2, st], close)
    assert [x[0].r for x in out] == [0, 5, 30]
    tr0, sk0 = out[0][1], out[0][2]
    assert tr0.filled and not sk0 and tr0.exit_kind == "target"
    assert tr0.r_gross == pytest.approx((110 - 100) / 5)
    assert tr0.cost_r == pytest.approx(MARKET_SIDE * (100 + 110) / 5)  # taker + slippage on both legs
    assert out[1][2] is True and out[1][1] is None  # decided while the first trade was open
    assert out[2][2] is False  # after the exit: a new order (here it never fills: lows stay above 100)
    assert out[2][1] is not None and not out[2][1].filled


# ---------------------------------------------------------------- the author's live example


def test_eth_2026_10_15_is_a_T_event():
    fx = json.loads(FIX.read_text())
    d = Bars.from_rows(fx["daily"], DAY_MS)
    b4 = Bars.from_rows(fx["h4"], B4)
    dp, p4 = pos_from_seg(segments(d.t)), pos_from_seg(segments(b4.t))
    e100, e200 = ema(d.c, 100, dp), ema(d.c, 200, dp)
    a4 = atr(b4, 14, p4)
    args = (
        b4,
        p4,
        a4,
        ema(b4.c, 100, p4),
        last_daily_before(b4, d, e100),
        last_daily_before(b4, d, e200),
        last_daily_before(b4, d, atr(d, 14, dp)),
    )
    s_ms = 1_760_486_400_000 + 365 * DAY_MS  # 2026-10-15 00:00 UTC
    hits = [x for x in deviation_setups(*args) if int(b4.t[x.s]) == s_ms]
    assert len(hits) == 1
    x = hits[0]
    assert x.kind == "dev" and x.group == "T"
    assert x.ld == pytest.approx(2313.39) and x.lr == pytest.approx(2401.37)
    assert int(b4.t[x.r]) == s_ms + 12 * HOUR_MS  # back in the range at the 12:00 bar's close
    assert x.e200 == pytest.approx(2311, abs=10) and x.e100 == pytest.approx(2357, abs=10)  # the author's 2311 / 2357
    # the card's literal detector (8 ATR, 1-2 closes) misses the author's own example
    assert [y for y in deviation_setups(*args, Params(width_atr=8.0, kmax=2)) if int(b4.t[y.s]) == s_ms] == []
