"""analytics/setup_signals.py: the live scanner equals the full-history detectors on the last closed bar."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_s14_ema import Bars, atr, find_signals, indicators, segment_pos

H4 = ss.TF_MS["4h"]


def _bars(n: int = 1000, seed: int = 7) -> Bars:
    """Regime-switching walk (trends and flat ranges) so that every detector fires a few times."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.004, 0.0, 0.0, 0.004], size=n // 60 + 1), 60)[:n]
    c = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.012, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    t = 1_700_000_000_000 // H4 * H4 + np.arange(n) * H4
    return Bars(t.astype(np.int64), o, h, low, c, H4)


def _full_history_events(b: Bars) -> set[tuple[str, str, int]]:
    out = set()
    for side, bb in (("long", b), ("short", b.mirrored())):
        ind = indicators(bb, ss.S14_PARAMS)
        out |= {("S14", side, s.idx) for s in find_signals(bb, ind, ss.S14_PARAMS, variant="sweep")}
        pos = segment_pos(bb.t)
        for ev in range_events(bb, pos, atr(bb, 14, pos), w=ss.RANGE_W, width_atr=ss.RANGE_WIDTH_ATR):
            out.add(("S16" if ev.kind == "dev" else "S3", side, ev.r))
    return out


@pytest.fixture(scope="module")
def live():
    """(bars, [(i, signal)]) of the live scan on every prefix; computed once for the module (the slow part)."""
    b = _bars()
    return b, [(i, s) for i in range(150, len(b)) for s in ss.scan(b.slice(0, i + 1), "4h")]


def test_live_scan_on_each_prefix_equals_the_full_history_detector(live):
    b, sigs = live
    want = _full_history_events(b)
    got = set()
    for i, s in sigs:
        assert s.signal_ts == int(b.t[i])
        got.add((s.setup, s.side, i))
    want = {w for w in want if w[2] >= 150}
    assert got == want
    assert {w[0] for w in got} == {"S14", "S16", "S3"} and {w[1] for w in got} == {"long", "short"}  # the sample covers all


def test_levels_and_comment_of_every_signal(live):
    b, sigs = live
    seen = 0
    for i, s in sigs:
        seen += 1
        sign = 1 if s.side == "long" else -1
        assert s.entry == pytest.approx(float(b.c[i]))
        assert sign * (s.entry - s.stop) > 0 and sign * (s.target - s.entry) > 0, s
        assert s.setup in s.comment and "Вхід" in s.comment and "стоп" in s.comment and "ATR14" in s.comment
        assert s.indicators["atr14"] > 0
    assert seen > 10


def test_closed_bars_drops_the_forming_bar():
    rows = [[0, 1, 2, 0.5, 1.5, 10], [H4, 1.5, 2, 1, 1.8, 10], [2 * H4, 1.8, 1.9, 1.7, 1.75, 1]]
    assert len(ss.closed_bars(rows, "4h", now_ms=2 * H4 + 60_000)) == 2  # the third bar is still forming
    assert len(ss.closed_bars(rows, "4h", now_ms=3 * H4)) == 3


def test_too_few_bars_gives_nothing():
    assert ss.scan(_bars(40), "4h") == []


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_s14_live_equals_the_detector_on_more_seeds(seed):
    """One signal per impulse: the live scan keeps the detector's memory of used impulses (critic 21.10, finding 5)."""
    b = _bars(700, seed)
    want = set()
    for side, bb in (("long", b), ("short", b.mirrored())):
        ind = indicators(bb, ss.S14_PARAMS)
        want |= {(side, s.idx) for s in find_signals(bb, ind, ss.S14_PARAMS, variant="sweep") if s.idx >= 150}
    got = {(s.side, i) for i in range(150, len(b)) for s in ss.s14(b.slice(0, i + 1), "4h")}
    assert got == want


def test_short_s14_comment_names_the_impulse_low():
    b = _bars(700, 1)  # seed 1 has a short S14 whose target is the impulse extreme
    for i in range(150, len(b)):
        for s in ss.s14(b.slice(0, i + 1), "4h"):
            if s.side == "short" and "імпульсу" in s.comment:
                assert "лоу імпульсу" in s.comment and "хай імпульсу" not in s.comment
                return
    raise AssertionError("seed 1 should give a short S14 with the impulse target")


def test_trend_and_score():
    up = _bars(300, 11)
    rising = Bars(up.t, up.o, up.h, up.low, np.linspace(100, 200, len(up)), up.bar_ms)
    falling = Bars(up.t, up.o, up.h, up.low, np.linspace(200, 100, len(up)), up.bar_ms)
    assert ss.trend_dir(rising) == 1 and ss.trend_dir(falling) == -1 and ss.trend_dir(up.slice(0, 30)) == 0
    sig = ss.LiveSignal("S14", "long", "4h", 0, 100.0, 95.0, 115.0, "c")
    full = ss.score(sig, 3.2, rising, rising, is_btc=False)
    assert full.points == 4 and len(full.reasons) == 4
    assert ss.score(sig, 1.6, falling, falling, is_btc=False).points == 0
    assert ss.score(sig, 2.5, rising, None, is_btc=True).points == 2  # BTC: no separate BTC-trend point
    assert ss.score(replace_side(sig), 2.5, falling, falling, is_btc=False).points == 3


def replace_side(sig):
    from dataclasses import replace

    return replace(sig, side="short", stop=105.0, target=85.0)
