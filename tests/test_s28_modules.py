"""S28 modules (item 8 / 7b): mirrored detector, analytic expectations, rank correlation, D1 hedge, no-lookahead."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import s28_modules as m
from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.stop_management import Fees, StopRule, risk_unit, simulate_stop_rule

H = HOUR_MS
B4 = 4 * H
T0 = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp() * 1000)
RULE_B = StopRule("B", be_frac=0.5)


def random_bars(n: int, step: int, seed: int = 1, vol: float = 0.02) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, vol / 2, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, vol / 2, n))
    return Bars.from_rows([[T0 + i * step, o[i], h[i], lo[i], c[i]] for i in range(n)], step)


def head(b: Bars, k: int) -> Bars:
    return Bars(b.t[:k], b.o[:k], b.h[:k], b.low[:k], b.c[:k], b.bar_ms)


def bars_1h(rows: list[tuple[float, float, float, float]]) -> Bars:
    return Bars.from_rows([[T0 + i * H, *r] for i, r in enumerate(rows)], H)


# ---------------------------------------------------------------- H9 mirror


@pytest.mark.parametrize("seed", [1, 2])
def test_mirror_twice_is_identity(seed: int) -> None:
    b = random_bars(3000, B4, seed)
    want = [(s.signal_ts, s.entry, s.stop, s.target) for s in ss.s28_all(b, "4h")]
    got = [(s.signal_ts, s.entry, s.stop, s.target) for s in m.mirror_signals(b.mirrored(), "4h")]  # mirrored twice
    assert len(want) > 0
    assert got == want


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_mirror_signals_are_bearish_order_blocks(seed: int) -> None:
    b = random_bars(3000, B4, seed)
    sigs = m.mirror_signals(b, "4h")
    assert len(sigs) > 0
    for s in sigs:
        i = int(np.searchsorted(b.t, int(s.indicators["ob_ts"])))
        e = i + 1
        level = -s.entry  # real price: the bottom of the bullish candle's body
        assert b.c[i] > b.o[i] and b.c[e] < b.o[e] and b.c[e] < b.o[i]  # bullish i engulfed by bearish e
        assert level == pytest.approx(b.o[i])
        assert -s.stop > level and -s.target < level  # a short: stop above, target below
        assert -s.target == pytest.approx(level - 2 * (-s.stop - level))
        j = int(np.searchsorted(b.t, s.signal_ts))
        assert b.c[j] < level  # the close of j is below the level (mirror of "above")


# ---------------------------------------------------------------- features


def test_fvg_size_and_distance_and_structural_stop() -> None:
    # bars e-1 .. e+2; middle-e FVG: low[e+1] 105 > high[e-1] 101 -> 4
    b = Bars.from_rows([[T0, 100, 101, 99, 100], [T0 + B4, 100, 106, 100, 105], [T0 + 2 * B4, 105, 107, 105, 106]], B4)
    assert m.fvg_size(b, 1) == pytest.approx(4.0)
    # no middle-e gap, middle e+1 gap: low[3] - high[1]
    b2 = Bars.from_rows(
        [[T0, 100, 101, 99, 100], [T0 + B4, 100, 104, 100, 103], [T0 + 2 * B4, 103, 108, 100.5, 107], [T0 + 3 * B4, 107, 109, 105, 108]], B4
    )
    assert m.fvg_size(b2, 1) == pytest.approx(1.0)
    b3 = Bars.from_rows([[T0, 100, 101, 99, 100], [T0 + B4, 100, 102, 100, 101], [T0 + 2 * B4, 101, 102, 100, 101]], B4)
    assert math.isnan(m.fvg_size(b3, 1))
    assert m.limit_distance(110.0, 100.0, 4.0) == pytest.approx(2.5)
    assert math.isnan(m.limit_distance(110.0, 100.0, 0.0))
    assert m.structural_stop(95.0, 97.0, 10.0) == pytest.approx(94.0)


@pytest.mark.parametrize("seed", [1, 4])
def test_features_use_bars_up_to_signal_only(seed: int) -> None:
    b = random_bars(2500, B4, seed)
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    sigs = ss.s28_all(b, "4h")
    assert len(sigs) > 0
    for s in sigs[:40]:
        j = int(np.searchsorted(b.t, s.signal_ts))
        e = int(np.searchsorted(b.t, int(s.indicators["ob_ts"]))) + 1
        cut = head(b, j + 1)
        ac = atr(cut, 14, segment_pos(cut.t))
        assert m.fvg_size(b, e) == m.fvg_size(cut, e)
        assert np.isfinite(m.fvg_size(b, e))  # the detector's FVG flag
        assert m.limit_distance(float(b.c[j]), s.entry, float(a[j])) == pytest.approx(
            m.limit_distance(float(cut.c[j]), s.entry, float(ac[j]))
        )


# ---------------------------------------------------------------- H6


def touch_path() -> Bars:
    # level 100: bar 1 touches (low 99), bar 2 closes 100.5 (< high[1] 101), bar 3 closes 101.5 > 101 -> m = 3
    return bars_1h(
        [
            (102, 103, 101, 102),
            (101.5, 101, 99, 100),  # touch (high 101 for the test; o within)
            (100, 100.8, 98.5, 100.5),
            (100.5, 102, 100, 101.5),
            (101.5, 103, 101, 102.5),
            (102.5, 110, 102, 109),
            (109, 110, 108, 109.5),
        ]
    )


def test_confirm_entry_analytic() -> None:
    p = touch_path()
    c = m.confirm_entry(p, 1, 100.0, 2.0)
    assert c is not None and (c.k, c.m) == (1, 3)
    assert c.entry == pytest.approx(101.5)
    assert c.stop == pytest.approx(98.5 - 0.5)  # min low of bars 1..3 minus 0.25 * 2
    assert c.target == pytest.approx(101.5 + 2 * (101.5 - 98.0))
    assert m.confirm_entry(p, 1, 100.0, 2.0, max_wait=1) is None
    d = m.confirm_entry(p, 1, 100.0, 2.0, delay=1)  # placebo: close of k + 1 without the condition
    assert d is not None and d.m == 2 and d.entry == pytest.approx(100.5) and d.stop == pytest.approx(98.0)


def test_confirm_trade_matches_simulation_and_is_causal() -> None:
    p = touch_path()
    t0, t1 = T0, T0 + 3 * H
    o, c, k = m.confirm_trade(p, t0, t1, 100.0, 2.0, 7 * 24 * H, RULE_B, Fees())
    assert k == 1 and c is not None and o is not None
    want = simulate_stop_rule(p, int(p.t[c.m]) + H, c.entry, c.stop, c.target, 7 * 24 * H, RULE_B, fees=Fees())
    assert o.r_net == pytest.approx(want.r_net)
    # bars after m do not change the entry decision
    p2 = Bars(p.t, p.o, p.h.copy(), p.low.copy(), p.c.copy(), p.bar_ms)
    p2.h[4:] *= 0.9
    p2.low[4:] *= 0.9
    p2.c[4:] *= 0.9
    _, c2, _ = m.confirm_trade(p2, t0, t1, 100.0, 2.0, 7 * 24 * H, RULE_B, Fees())
    assert c2 == c
    # the guard rejects -> no trade; no touch -> nothing
    o3, c3, _ = m.confirm_trade(p, t0, t1, 100.0, 2.0, 7 * 24 * H, RULE_B, Fees(), lambda *_: False)
    assert o3 is None and c3 == c
    assert m.confirm_trade(p, t0, t1, 90.0, 2.0, 7 * 24 * H, RULE_B, Fees()) == (None, None, -1)


# ---------------------------------------------------------------- statistics, D1


def test_spearman_analytic() -> None:
    assert m.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert m.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert m.spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7]) == pytest.approx(8 / math.sqrt(95))
    assert list(m.ranks([3.0, 1.0, 3.0, 2.0])) == [3.5, 1.0, 3.5, 2.0]
    assert math.isnan(m.spearman([1, 1, 1], [1, 2, 3]))


def test_spearman_ci_and_diff() -> None:
    rng = np.random.default_rng(5)
    x = rng.normal(size=400)
    y = x + rng.normal(size=400)
    cl = np.arange(400) // 4
    r = m.spearman_ci(x, y, cl, n_boot=500, seed=1)
    assert r["m"] == pytest.approx(m.spearman(x, y))
    assert r["lo"] < r["m"] < r["hi"] and r["p"] < 0.01
    d = m.spearman_diff_ci(x, y, cl, x, -y, cl, n_boot=500, seed=1)
    assert d["d"] == pytest.approx(2 * m.spearman(x, y))
    assert d["lo"] > 0
    assert m.one_sided(0.1, 0.04) == pytest.approx(0.02) and m.one_sided(-0.1, 0.04) == pytest.approx(0.98)


def test_hedged_r_and_basket() -> None:
    f = Fees()
    assert m.hedged_r(1.0, 0.05, 100.0, 90.0, "long") == pytest.approx(1.0 - 5.0 / risk_unit(100.0, 90.0, f))
    assert m.hedged_r(1.0, 0.05, 100.0, 110.0, "short") == pytest.approx(1.0 + 5.0 / risk_unit(-100.0, -110.0, f))
    t = np.array([T0, T0 + H, T0 + 2 * H], dtype=np.int64)
    closes = {"A": (t, np.array([100.0, 110.0, 121.0])), "B": (t, np.array([50.0, 50.0, 45.0])), "C": (t[:1], np.array([1.0]))}
    # close ending at T0 + H is bar 0, at T0 + 3H is bar 2
    r, n = m.basket_return(closes, ["A", "B", "C", "D"], T0 + H, T0 + 3 * H)
    assert n == 2 and r == pytest.approx((0.21 - 0.10) / 2)
    r0, n0 = m.basket_return(closes, ["C"], T0 + H, T0 + 3 * H)
    assert math.isnan(r0) and n0 == 0
