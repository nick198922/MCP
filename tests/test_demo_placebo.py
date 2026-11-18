"""analytics/demo_placebo.py: placebo of the same candle for demo trades (analytic expectations, causality)."""

from __future__ import annotations

import pytest

from tradedesk.analytics import demo_placebo as dp
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.s38_modules import draw_coins
from tradedesk.analytics.stop_management import MAKER, SLIPPAGE, TAKER, StopRule

H = 3_600_000
TF = 4 * H
T0 = 1_790_000_000_000 // TF * TF
RULE = StopRule("B", be_frac=0.5)
HOLD = 7 * 24 * H


def flat_4h(n: int = 30, start: int = T0) -> Bars:
    """Bars with true range 2 (h 101, l 99, o = c = 100): Wilder ATR = 2 once seeded."""
    return Bars.from_rows([[start + i * TF, 100.0, 101.0, 99.0, 100.0] for i in range(n)], TF)


def path_1h(first: list[float], n: int, start: int) -> Bars:
    rows = [[start, *first]] + [[start + i * H, 100.0, 100.5, 99.5, 100.0] for i in range(1, n)]
    return Bars.from_rows(rows, H)


def test_levels_and_mult() -> None:
    assert dp.placebo_levels("long", 100.0, 1.5, 2.0) == (100.0, 97.0, 106.0)
    assert dp.placebo_levels("short", 100.0, 1.5, 2.0) == (100.0, 103.0, 94.0)
    with pytest.raises(ValueError):
        dp.placebo_levels("flat", 100.0, 1.0, 1.0)
    assert dp.stop_atr_mult(100.0, 103.0, 2.0) == pytest.approx(1.5)
    assert dp.stop_atr_mult(100.0, 100.0, 2.0) is None
    assert dp.stop_atr_mult(100.0, 98.0, 0.0) is None


def test_atr_at_constant_range_and_causal() -> None:
    b = flat_4h(40)
    sig = int(b.t[29])
    assert dp.atr_at(b, sig) == pytest.approx(2.0)
    assert dp.atr_at(b, sig + 1) is None  # no bar opens there
    assert dp.atr_at(flat_4h(10), T0 + 9 * TF) is None  # not seeded
    # bars after the signal bar change: the ATR at the signal bar does not
    rows = [[int(b.t[i]), 100.0, 101.0, 99.0, 100.0] if i <= 29 else [int(b.t[i]), 50.0, 400.0, 1.0, 9.0] for i in range(40)]
    assert dp.atr_at(Bars.from_rows(rows, TF), sig) == pytest.approx(2.0)


def test_long_target_analytic() -> None:
    b = flat_4h(30)
    sig = int(b.t[-1])
    entry_ms = sig + TF
    p = path_1h([100.0, 105.0, 99.5, 104.5], 200, entry_ms)
    o = dp.placebo_outcome("long", b, p, sig, 1.0, HOLD, RULE, now_ms=entry_ms + 300 * H)
    ru = 2.0 + TAKER * (100.0 + 98.0)
    assert o.status == "ok" and o.exit_kind == "target"
    assert o.r == pytest.approx(4.0 / ru - (MAKER * 100.0 + MAKER * 104.0) / ru)


def test_short_stop_analytic_and_causal() -> None:
    b = flat_4h(30)
    sig = int(b.t[-1])
    entry_ms = sig + TF
    p = path_1h([100.0, 103.0, 99.5, 101.0], 200, entry_ms)
    now = entry_ms + 300 * H
    o = dp.placebo_outcome("short", b, p, sig, 1.0, HOLD, RULE, now_ms=now)
    ru = 2.0 + TAKER * (100.0 + 102.0)
    assert o.status == "ok" and o.exit_kind == "stop"
    assert o.r == pytest.approx(-2.0 / ru - (MAKER * 100.0 + (TAKER + SLIPPAGE) * 102.0) / ru)
    # 1h bars after the exit and 4h bars after the signal change: the R does not
    rows = [[int(p.t[i]), float(p.o[i]), float(p.h[i]), float(p.low[i]), float(p.c[i])] for i in range(len(p))]
    for r in rows[1:]:
        r[1:] = [10.0, 10.5, 9.5, 10.0]
    b2 = Bars.from_rows([[int(x), 100.0, 101.0, 99.0, 100.0] for x in b.t] + [[sig + TF, 1.0, 900.0, 0.5, 2.0]], TF)
    o2 = dp.placebo_outcome("short", b2, Bars.from_rows(rows, H), sig, 1.0, HOLD, RULE, now_ms=now)
    assert o2.r == pytest.approx(o.r)


def test_open_gap_and_missing() -> None:
    b = flat_4h(30)
    sig = int(b.t[-1])
    entry_ms = sig + TF
    p = path_1h([100.0, 100.5, 99.5, 100.0], 24, entry_ms)  # a day of quiet path
    # horizon ahead of now: marked to market
    o = dp.placebo_outcome("long", b, p, sig, 1.0, HOLD, RULE, now_ms=entry_ms + 24 * H)
    assert o.status == "open" and o.r is not None and abs(o.r) < 0.1
    # horizon passed but the path ends: a hole / delisting -> missing
    g = dp.placebo_outcome("long", b, p, sig, 1.0, HOLD, RULE, now_ms=entry_ms + HOLD + H)
    assert g.status == "gap" and g.r is None
    assert dp.placebo_outcome("long", b, p, sig + H, 1.0, HOLD, RULE, now_ms=entry_ms + HOLD).status == "no_bar"
    assert dp.placebo_outcome("long", flat_4h(10), p, T0 + 9 * TF, 1.0, HOLD, RULE, now_ms=entry_ms).status == "no_atr"
    assert dp.placebo_outcome("long", b, Bars.from_rows([], H), sig, 1.0, HOLD, RULE, now_ms=entry_ms + HOLD).status == "no_path"


def test_trade_delta() -> None:
    assert dp.trade_delta(1.0, [0.5, None, -0.5]) == (1.0, 2)
    assert dp.trade_delta(None, [0.5]) == (None, 1)
    assert dp.trade_delta(0.2, [None, float("nan")]) == (None, 0)


def test_summarize_and_verdict() -> None:
    s = dp.summarize([0.5] * 40, list(range(40)), n_boot=500, seed=1)
    assert s["n"] == 40 and s["mean"] == pytest.approx(0.5) and s["se"] == pytest.approx(0.0, abs=1e-12)
    assert s["ci"] == pytest.approx([0.5, 0.5]) and s["p_gt0"] < 0.01
    assert dp.verdict(s) == "✓"
    assert dp.verdict(s, p_holm=0.2) == "◯"
    assert dp.verdict({**s, "n": 29}) == "descriptive"
    assert dp.verdict({"n": 30, "ci": [-0.4, -0.1], "mde80": 0.2}) == "✗"
    assert dp.verdict({"n": 30, "ci": [-0.1, 0.2], "mde80": 0.25}) == "≈"
    assert dp.verdict({"n": 30, "ci": [-0.1, 0.4], "mde80": 0.29}) == "◯"
    assert dp.verdict({"n": 30, "ci": [0.1, 0.4], "mde80": 0.5}) == "◯"  # gate not passed
    one = dp.summarize([1.0, 2.0], ["d", "d"])
    assert one["ci"] is None and one["mean"] == pytest.approx(1.5) and dp.verdict({**one, "n": 30}) == "◯"


def test_draw_excludes_signal_coin_and_prefix_stable() -> None:
    pool = [f"C{i}" for i in range(100)]
    k3, k10 = draw_coins("link-1", pool, 3), draw_coins("link-1", pool, 10)
    assert k10[:3] == k3 and len(set(k10)) == 10
    assert "C5" not in draw_coins("x", [c for c in pool if c != "C5"], 10)
