"""analytics/setup_signals.py: the live scanner equals the full-history detectors on the last closed bar."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_s14_ema import Bars, atr, find_signals, indicators, segment_pos

H4 = ss.TF_MS["4h"]


def _bars(n: int = 1500, seed: int = 7) -> Bars:
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
