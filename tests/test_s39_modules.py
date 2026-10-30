"""S39 modules (item 8 / 7b): the failed-long detector against ``s39_all``, analytic expectations, no-lookahead."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import s39_modules as m
from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, resample, segment_pos
from tradedesk.analytics.stop_management import Fees, StopRule, risk_unit, simulate_stop_rule

H = HOUR_MS
B4 = 4 * H
T0 = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp() * 1000)  # a Monday 00:00 UTC
F = Fees()
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


def s3_parents(b: Bars) -> list[m.Parent]:
    out = []
    for s in ss.range_all(b, "4h"):
        if s.setup == "S3" and s.side == "long":
            out.append(m.Parent(int(np.searchsorted(b.t, s.signal_ts)), s.stop, s.target))
    return out


def fails(b: Bars) -> list[m.Fail]:
    pos = segment_pos(b.t)
    return m.failed_long(b, pos, atr(b, 14, pos), s3_parents(b))


# ---------------------------------------------------------------- detector


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_failed_long_with_s3_parents_is_s39_all(seed: int) -> None:
    b = random_bars(3000, B4, seed)
    want = [(s.signal_ts, s.entry, s.stop, s.target) for s in ss.s39_all(b, "4h")]
    got = [(int(b.t[f.j]), f.entry, f.stop, f.target) for f in fails(b)]
    assert len(want) > 0
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        assert g[0] == w[0]
        assert g[1:] == pytest.approx(w[1:], rel=1e-12)


def test_failed_long_is_causal() -> None:
    b = random_bars(3000, B4, 4)
    full = fails(b)
    assert full
    for f in full[:: max(1, len(full) // 8)]:
        cut = fails(head(b, f.j + 1))
        assert any(c.j == f.j and c.entry == f.entry and c.stop == f.stop for c in cut)


def test_failed_long_analytic() -> None:
    # parent at bar 1 (stop 95, target 110); bar 2 high 104; bar 3 closes 94 < 95: short at 94, stop max(h 1..3) + 0.5 ATR
    rows = [[T0, 100, 101, 99, 100], [T0 + B4, 100, 102, 96, 99], [T0 + 2 * B4, 99, 104, 98, 103], [T0 + 3 * B4, 103, 103, 93, 94]]
    b = Bars.from_rows(rows, B4)
    pos = segment_pos(b.t)
    a = np.full(4, 2.0)
    (f,) = m.failed_long(b, pos, a, [m.Parent(1, 95.0, 110.0, "S14")])
    assert (f.j, f.entry, f.stop, f.target, f.tag) == (3, 94.0, 105.0, 94.0 - 2 * 11.0, "S14")
    # the target touched first (bar 2 high 104 >= 104): no failure; a used bar gives none either
    assert m.failed_long(b, pos, a, [m.Parent(1, 95.0, 104.0)]) == []
    assert m.failed_long(b, pos, a, [m.Parent(1, 95.0, 110.0)], used={3}) == []


# ---------------------------------------------------------------- H3 / H4


def test_structural_stop_and_level() -> None:
    assert m.structural_stop(90.0, 110.0, 95.0, 2.0) == 96.0
    assert m.structural_stop(90.0, 95.5, 95.0, 2.0) == 95.5  # never further than the base
    assert m.thesis_level(95.0, 2.0) == 95.5


def test_thesis_exit_without_level_is_the_base() -> None:
    p = random_bars(3000, H, 5, vol=0.01)
    for k in range(5):
        t_in = int(p.t[100 + 400 * k])
        e = float(p.o[100 + 400 * k])
        st, tg = e * 0.96, e * 1.08
        base = simulate_stop_rule(p, t_in, e, st, tg, 7 * 24 * H, RULE_B, fees=F)
        mine = m.thesis_exit(p, t_in, e, st, tg, 7 * 24 * H, RULE_B, -math.inf, B4, F)
        assert base is not None and mine is not None
        assert (mine.exit, mine.exit_kind, mine.exit_ms) == (base.exit, base.exit_kind, base.exit_ms)
        assert mine.r_net == pytest.approx(base.r_net, abs=1e-12)


def test_thesis_exit_at_the_first_4h_close_below_the_level() -> None:
    # long space: entry 100, stop 90, target 120, level 97. 1h bars: the 4h bar closes at 96 at hour 4 -> exit there;
    # the hour-2 close 96 is not a 4h close and does not count
    closes = [99, 96, 98, 96, 95]
    rows = [[T0 + i * H, 100 if i == 0 else closes[i - 1], 101, 94.5, c] for i, c in enumerate(closes)]
    p = Bars.from_rows(rows, H)
    o = m.thesis_exit(p, T0, 100.0, 90.0, 120.0, 7 * 24 * H, RULE_B, 97.0, B4, F)
    assert o is not None and o.exit_kind == "thesis" and o.exit == 96.0 and o.exit_ms == T0 + 4 * H
    ru = risk_unit(100.0, 90.0, F)
    assert o.r_net == pytest.approx((96.0 - 100.0) / ru - (F.maker * 100 + F.taker_exit * 96) / ru)
    # bars after the exit do not matter
    rows2 = rows + [[T0 + 5 * H, 95, 200, 1, 150]]
    o2 = m.thesis_exit(Bars.from_rows(rows2, H), T0, 100.0, 90.0, 120.0, 7 * 24 * H, RULE_B, 97.0, B4, F)
    assert o2 is not None and (o2.exit, o2.exit_kind, o2.r_net) == (o.exit, o.exit_kind, o.r_net)


def test_thesis_exit_is_causal() -> None:
    # Cut the path right after the exit bar: a decision that peeked at later bars would change (or turn into
    # `data_end`). Only real exits are checked, and the exit kind must match exactly (no `data_end` escape hatch).
    p = random_bars(2000, H, 6, vol=0.01)
    kinds = []
    for i in range(50, 1700, 37):
        t_in, e = int(p.t[i]), float(p.o[i])
        o = m.thesis_exit(p, t_in, e, e * 0.95, e * 1.1, 7 * 24 * H, RULE_B, e * 0.985, B4, F)
        assert o is not None
        if o.exit_kind in ("time", "data_end"):
            continue
        kinds.append(o.exit_kind)
        k = int(np.searchsorted(p.t, o.exit_ms))
        o2 = m.thesis_exit(head(p, k), t_in, e, e * 0.95, e * 1.1, 7 * 24 * H, RULE_B, e * 0.985, B4, F)
        assert o2 is not None
        assert (o2.exit_kind, o2.exit, o2.exit_ms) == (o.exit_kind, o.exit, o.exit_ms)
        assert o2.r_net == pytest.approx(o.r_net, abs=1e-12)
    assert "thesis" in kinds and len(kinds) >= 10


# ---------------------------------------------------------------- H7


def test_non_htf_edge_and_no_lookahead() -> None:
    p = random_bars(24 * 70, H, 7, vol=0.005)
    t_sw = int(p.t[24 * 50 + 8])  # the sweep bar opens on day 50, 08:00
    day = (t_sw // (24 * H)) * 24 * H
    prev_lo = float(p.low[(p.t >= day - 24 * H) & (p.t < day)].min())
    assert m.non_htf_edge(p.t, p.low, t_sw, prev_lo, 1.0) is False  # the edge is the previous day's low
    assert m.non_htf_edge(p.t, p.low, t_sw, -1e9, 1.0) is True
    assert m.non_htf_edge(p.t, p.low, t_sw, prev_lo, math.nan) is None
    # bars from the start of the sweep day on (incl. the sweep and the failure) do not change the levels
    k = int(np.searchsorted(p.t, day))
    low2 = p.low.copy()
    low2[k:] = 1.0
    for edge in (prev_lo, prev_lo * 0.99, prev_lo * 1.03):
        assert m.non_htf_edge(p.t, low2, t_sw, edge, 0.5) == m.non_htf_edge(p.t, p.low, t_sw, edge, 0.5)


def test_permuted_and_price_return() -> None:
    v = np.arange(50.0)
    a, b = m.permuted(v), m.permuted(v)
    assert np.array_equal(a, b) and sorted(a) == list(v) and not np.array_equal(a, v)
    assert m.price_return(1.0, 100.0, 90.0, F) == pytest.approx(risk_unit(100.0, 90.0, F) / 100.0)


def test_resample_2h_aligns() -> None:
    p = random_bars(48, H, 8)
    b2 = resample(p, 2)
    assert b2.bar_ms == 2 * H and len(b2) == 24
