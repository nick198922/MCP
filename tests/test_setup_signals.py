"""analytics/setup_signals.py: the live scanner equals the full-history detectors on the last closed bar."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_fvg_ob import fvg_zones, limit_long
from tradedesk.analytics.course_s14_ema import Bars, align_daily, atr, ema, find_signals, indicators, segment_pos

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


# ---------------------------------------------------------------- S12 (FVG retest with the daily trend; rejected by the backtest)

D1 = ss.TF_MS["1d"]


def _daily_loop(b: Bars) -> Bars:
    """Complete UTC days by a plain loop (independent of ``ss.daily_bars``)."""
    days: dict[int, list[int]] = {}
    for k in range(len(b)):
        days.setdefault(int(b.t[k]) // D1, []).append(k)
    full = [ks for d, ks in sorted(days.items()) if [int(b.t[k]) for k in ks] == [d * D1 + m * b.bar_ms for m in range(D1 // b.bar_ms)]]
    pick = lambda f: np.array([f(ks) for ks in full], dtype=float)  # noqa: E731
    return Bars(
        np.array([int(b.t[ks[0]]) for ks in full], dtype=np.int64),
        pick(lambda ks: b.o[ks[0]]),
        pick(lambda ks: max(b.h[k] for k in ks)),
        pick(lambda ks: min(b.low[k] for k in ks)),
        pick(lambda ks: b.c[ks[-1]]),
        D1,
    )


def _s12_reference(b: Bars) -> set[tuple[str, int, int]]:
    """(side, touch bar, gap bar) from the backtest's own pieces: the FVG zones with the daily-EMA21 trend flag, and the
    backtest's resting limit at the middle (``limit_long`` walked on the 4h bars) for the first test within 20 bars."""
    out = set()
    for side, bb in (("long", b), ("short", b.mirrored())):
        pos = segment_pos(bb.t)
        a = atr(bb, 14, pos)
        d = _daily_loop(bb)
        e = ema(d.c, 21, segment_pos(d.t))
        with np.errstate(invalid="ignore"):
            up = align_daily(bb, d, (d.c > e).astype(float)) > 0.5
        hits: dict[int, int] = {}
        for z in fvg_zones(bb, pos, a, up):
            if "trend" not in z.arms or pos[z.form] < 100:
                continue
            level = z.levels["mid"]
            stop, target = level - z.atr, level + 2 * z.atr
            tr = limit_long(bb, int(bb.t[z.form]) + bb.bar_ms, 20 * bb.bar_ms, 20 * bb.bar_ms, level, stop, target)
            if tr is None or not tr.filled:
                continue
            j = int(np.searchsorted(bb.t, tr.touch_ms))
            if bb.low[j] <= stop or bb.c[j] >= target:
                continue
            hits[j] = max(hits.get(j, -1), z.form)
        out |= {(side, j, i) for j, i in hits.items()}
    return out


def _s12_keys(b: Bars, sigs) -> set[tuple[str, int, int]]:
    idx = {int(t): k for k, t in enumerate(b.t)}
    return {(s.side, idx[s.signal_ts], idx[int(s.indicators["fvg_ts"])]) for s in sigs}


def _with_hole(b: Bars, at: int, bars: int) -> Bars:
    keep = np.r_[0:at, at + bars : len(b)]
    return Bars(b.t[keep], b.o[keep], b.h[keep], b.low[keep], b.c[keep], b.bar_ms)


def test_daily_bars_keep_complete_utc_days_only():
    b = _bars(200, 3)
    d = ss.daily_bars(b)
    ref = _daily_loop(b)
    assert len(d) > 25 and np.array_equal(d.t, ref.t)
    for x, y in ((d.o, ref.o), (d.h, ref.h), (d.low, ref.low), (d.c, ref.c)):
        assert np.allclose(x, y)
    assert int(b.t[0]) % D1 != 0 and d.t[0] > b.t[0]  # the partial first day is dropped
    holed = _with_hole(b, 60, 1)  # one missing 4h bar: that day is gone, the others stay
    assert len(ss.daily_bars(holed)) == len(d) - 1
    assert len(ss.daily_bars(b.slice(0, len(b) - 1))) == len(d) - (int(b.t[-1]) % D1 == D1 - H4)  # the running day


@pytest.mark.parametrize("seed", [7, 2, 5])
def test_s12_equals_the_backtest_pieces(seed):
    b = _bars(1000, seed)
    for bb in (b, _with_hole(b, 500, 20)):  # an 80 h hole restarts the segment
        want = _s12_reference(bb)
        got = _s12_keys(bb, ss.s12_all(bb, "4h"))
        assert got == want
        assert {w[0] for w in got} == {"long", "short"} and len(got) > 10


def test_s12_live_scan_on_each_prefix_equals_the_full_history():
    b = _bars(600, 7)
    full = _s12_keys(b, ss.s12_all(b, "4h"))
    live = set()
    for i in range(120, len(b)):
        sigs = ss.s12(b.slice(0, i + 1), "4h")
        assert all(s.setup == "S12" and s.signal_ts == int(b.t[i]) for s in sigs)
        live |= _s12_keys(b, sigs)
    assert live == {w for w in full if w[1] >= 120} and live


def _s12_rest_reference(b: Bars) -> set[tuple[str, int, float, float, float]]:
    """(side, gap bar, middle, stop, target) of every trend gap, from the backtest's pieces (long frame)."""
    out = set()
    for side, bb in (("long", b), ("short", b.mirrored())):
        pos = segment_pos(bb.t)
        a = atr(bb, 14, pos)
        d = _daily_loop(bb)
        e = ema(d.c, 21, segment_pos(d.t))
        with np.errstate(invalid="ignore"):
            up = align_daily(bb, d, (d.c > e).astype(float)) > 0.5
        for z in fvg_zones(bb, pos, a, up):
            if "trend" in z.arms and pos[z.form] >= 100:
                mid = z.levels["mid"]
                out.add((side, z.form, round(mid, 9), round(mid - z.atr, 9), round(mid + 2 * z.atr, 9)))
    return out


def test_s12_rest_is_every_trend_gap_with_a_limit_at_its_middle():
    b = _bars(1000, 2)
    sigs = ss.s12_rest_all(b, "4h")
    idx = {int(t): k for k, t in enumerate(b.t)}
    flip = lambda side, x: x if side == "long" else -x  # noqa: E731
    got = {
        (s.side, idx[s.signal_ts], round(flip(s.side, s.entry), 9), round(flip(s.side, s.stop), 9), round(flip(s.side, s.target), 9))
        for s in sigs
    }
    assert got == _s12_rest_reference(b) and {g[0] for g in got} == {"long", "short"}
    for s in sigs:
        i = idx[s.signal_ts]
        assert s.indicators["rest_until_ms"] == b.close_time(i) + 20 * H4 and s.indicators["fvg_ts"] == s.signal_ts
        assert s.entry == s.indicators["fvg_mid"] and (s.stop < s.entry < s.target if s.side == "long" else s.stop > s.entry > s.target)
        assert "Лімітка заздалегідь на середині FVG" in s.comment
        assert abs(s.target - s.entry) == pytest.approx(2 * abs(s.entry - s.stop))
    # the live scan (S12 = the resting form since v7) on each prefix finds the same signals as the full history
    live = []
    for i in range(120, len(b)):
        got_i = ss.scan(b.slice(0, i + 1), "4h", ("S12",))
        assert all(x.signal_ts == int(b.t[i]) for x in got_i)
        live += got_i
    assert {(x.side, x.signal_ts) for x in live} == {(x.side, x.signal_ts) for x in sigs if idx[x.signal_ts] >= 120} and live


def _other_future(b: Bars, t: int, rng: np.random.Generator) -> Bars:
    """``b`` up to bar t, then a different random walk (a strong move either way, so a peek would change the flags)."""
    c2 = b.c.copy()
    c2[t + 1 :] = b.c[t] * np.exp(np.cumsum(rng.normal(rng.choice([-0.02, 0.02]), 0.03, len(b) - t - 1)))
    o2 = np.concatenate([[b.o[0]], c2[:-1]])
    o2[: t + 1] = b.o[: t + 1]
    h2, l2 = b.h.copy(), b.low.copy()
    h2[t + 1 :] = np.maximum(o2, c2)[t + 1 :] * 1.004
    l2[t + 1 :] = np.minimum(o2, c2)[t + 1 :] * 0.996
    return Bars(b.t, o2, h2, l2, c2, b.bar_ms)


def test_s12_is_causal_a_different_future_after_each_signal_bar_changes_nothing_up_to_it():
    """Cut right at every signal bar t (and the bar before it) and give the history a different future: the signals
    up to t, levels and comment included, stay the same, and the live scan on bars 0..t sees the signal of bar t."""
    b = _bars(700, 5)
    full = ss.s12_all(b, "4h")
    rng = np.random.default_rng(99)
    cuts = sorted({k for s in full for k in (int(np.searchsorted(b.t, s.signal_ts)) - 1, int(np.searchsorted(b.t, s.signal_ts)))})
    assert len(cuts) > 20
    for t in cuts:
        upto = int(b.t[t])
        want = [s for s in full if s.signal_ts <= upto]
        assert [s for s in ss.s12_all(_other_future(b, t, rng), "4h") if s.signal_ts <= upto] == want, t
        assert ss.s12(b.slice(0, t + 1), "4h") == [s for s in want if s.signal_ts == upto], t


def test_s12_levels_comment_and_timeframe():
    b = _bars(1000, 7)
    sigs = ss.s12_all(b, "4h")
    idx = {int(t): k for k, t in enumerate(b.t)}
    for s in sigs:
        j = idx[s.signal_ts]
        sign = 1 if s.side == "long" else -1
        assert s.entry == pytest.approx(float(b.c[j]))
        assert sign * (s.entry - s.stop) > 0 and sign * (s.target - s.entry) > 0, s
        mid, a = s.indicators["fvg_mid"], s.indicators["atr14"]
        assert a > 0 and s.stop == pytest.approx(mid - sign * a) and s.target == pytest.approx(mid + sign * 2 * a)
        assert s.indicators["fvg_low"] < mid < s.indicators["fvg_high"]
        assert (b.low[j] <= mid) if s.side == "long" else (b.h[j] >= mid)  # the bar reached the middle
        assert s.comment.startswith("S12 (4h)") and "Вхід" in s.comment and "стоп" in s.comment and "ATR14" in s.comment
        assert "Бектест відкинув" in s.comment and "не доведена перевага" in s.comment
    assert ss.s12_all(b, "1d") == [] and ss.scan(b, "1d", ("S12",)) == []  # 4h only
    assert ss.s12_all(b.slice(0, 90), "4h") == []
    assert "S12" in ss.DETECTORS and "S12" not in {s.setup for s in ss.scan(b, "4h")}  # not in the default scan


# ---------------------------------------------------------------- S38 / S39 (trend-continuation shorts, short-setups-demo step 3)


def _s38_reference(b: Bars) -> list[tuple[int, float, float, float]]:
    """(entry bar, entry, stop, target) of the registered rule A for shorts, by a plain loop in real prices (no
    mirroring): a close below the 30-bar range low L, then the first bar of the next 6 whose high >= L - 0.25 ATR decides."""
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    out, resolved = [], -1
    for s in range(30, len(b)):
        if s <= resolved or pos[s] < 30 or not (np.isfinite(a[s - 1]) and a[s - 1] > 0):
            continue
        lo, hi = b.low[s - 30 : s].min(), b.h[s - 30 : s].max()
        if hi - lo > 6 * a[s - 1] or not b.c[s] < lo:
            continue
        resolved = s
        for j in range(s + 1, min(len(b), s + 7)):
            if pos[j] != pos[s] + (j - s):
                break
            resolved = j
            if not (np.isfinite(a[j]) and a[j] > 0) or b.h[j] < lo - 0.25 * a[j]:
                continue
            if b.c[j] < lo:
                stop = b.h[j] + 0.5 * a[j]
                out.append((j, float(b.c[j]), float(stop), float(b.c[j] - 2 * (stop - b.c[j]))))
            break
    return out


def _s39_reference(b: Bars) -> list[tuple[int, float, float, float]]:
    """(entry bar, entry, stop, target) of the registered rule B for shorts in real prices: a live S3 long (a sweep of
    the range low, stop 0.5 ATR under it) closed below its stop within 6 bars before the range middle was reached."""
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    out, used = [], set()
    for ev in range_events(b, pos, a, w=30, width_atr=6.0):
        r = ev.r
        if ev.kind != "sweep" or not (np.isfinite(a[r]) and a[r] > 0):
            continue
        s3_stop = ev.ld - 0.5 * a[r]
        for j in range(r + 1, min(len(b), r + 7)):
            if pos[j] != pos[r] + (j - r):
                break
            if b.c[j] < s3_stop:
                stop = b.h[r : j + 1].max() + 0.5 * a[j]
                if j not in used and stop > b.c[j]:
                    used.add(j)
                    out.append((j, float(b.c[j]), float(stop), float(b.c[j] - 2 * (stop - b.c[j]))))
                break
            if b.h[j] >= ev.mid:
                break
    return sorted(out)


def _rows(b: Bars, sigs) -> list[tuple[int, float, float, float]]:
    idx = {int(t): k for k, t in enumerate(b.t)}
    return sorted((idx[s.signal_ts], s.entry, s.stop, s.target) for s in sigs)


@pytest.mark.parametrize("seed", [1, 3, 7])
def test_s38_s39_equal_the_registered_rules_and_the_step1_detectors(seed):
    """The live adapters give the registered short rules (a plain loop in real prices) and the step-1 detectors of
    ``short_setups`` with their default ("base") parameters on the mirrored bars, as scripts/verify_short_setups.py
    calls them."""
    import inspect

    from tradedesk.analytics.short_setups import breakout_retest, failed_sweep

    b = _bars(1000, seed)
    for bb in (b, _with_hole(b, 500, 20)):  # a hole restarts the segment
        for fn, ref, det in ((ss.s38_all, _s38_reference, breakout_retest), (ss.s39_all, _s39_reference, failed_sweep)):
            sigs = fn(bb, "4h")
            got = _rows(bb, sigs)
            want = ref(bb)
            assert len(got) == len(want) > 3
            for g, w in zip(got, want, strict=True):
                assert g[0] == w[0] and g[1:] == pytest.approx(w[1:], rel=1e-12)
            m = bb.mirrored()
            pos = segment_pos(m.t)
            step1 = det(m, pos, atr(m, 14, pos))
            assert got == sorted((x.i, -x.entry, -x.stop, -x.target) for x in step1)
            assert {s.side for s in sigs} == {"short"}
    p = inspect.signature(breakout_retest).parameters  # the live constants are the registered defaults
    assert (p["w"].default, p["width_atr"].default, p["max_wait"].default) == (ss.RANGE_W, ss.RANGE_WIDTH_ATR, ss.SHORT_WAIT)
    assert (p["touch_atr"].default, p["stop_atr"].default, p["target_r"].default) == (
        ss.S38_TOUCH_ATR,
        ss.SHORT_STOP_ATR,
        ss.SHORT_TARGET_R,
    )
    q = inspect.signature(failed_sweep).parameters
    assert (q["max_wait"].default, q["stop_atr"].default, q["target_r"].default) == (ss.SHORT_WAIT, ss.SHORT_STOP_ATR, ss.SHORT_TARGET_R)


@pytest.mark.parametrize("setup", ["S38", "S39"])
def test_s38_s39_live_scan_on_each_prefix_equals_the_full_history(setup):
    b = _bars(600, 3)
    full_fn = {"S38": ss.s38_all, "S39": ss.s39_all}[setup]
    full = {(s.signal_ts, s.entry, s.stop, s.target) for s in full_fn(b, "4h") if s.signal_ts >= int(b.t[60])}
    live = set()
    for i in range(60, len(b)):
        sigs = ss.scan(b.slice(0, i + 1), "4h", (setup,))
        assert all(s.setup == setup and s.side == "short" and s.signal_ts == int(b.t[i]) for s in sigs)
        live |= {(s.signal_ts, s.entry, s.stop, s.target) for s in sigs}
    assert live == full and len(live) > 3


@pytest.mark.parametrize("setup", ["S38", "S39"])
def test_s38_s39_are_causal_a_different_future_after_each_signal_bar_changes_nothing_up_to_it(setup):
    """Cut at every signal bar t (and the bar before it), give the history a different future: the signals up to t,
    levels and comment included, stay the same, and the live scan on bars 0..t sees the signal of bar t."""
    full_fn, live_fn = {"S38": (ss.s38_all, ss.s38), "S39": (ss.s39_all, ss.s39)}[setup]
    b = _bars(700, 1)
    full = full_fn(b, "4h")
    rng = np.random.default_rng(42)
    cuts = sorted({k for s in full for k in (int(np.searchsorted(b.t, s.signal_ts)) - 1, int(np.searchsorted(b.t, s.signal_ts)))})
    assert len(cuts) > 6
    for t in cuts:
        upto = int(b.t[t])
        want = [s for s in full if s.signal_ts <= upto]
        assert [s for s in full_fn(_other_future(b, t, rng), "4h") if s.signal_ts <= upto] == want, t
        assert live_fn(b.slice(0, t + 1), "4h") == [s for s in want if s.signal_ts == upto], t


def test_s38_s39_levels_comment_and_timeframe():
    b = _bars(1000, 3)
    idx = {int(t): k for k, t in enumerate(b.t)}
    s38, s39 = ss.s38_all(b, "4h"), ss.s39_all(b, "4h")
    assert s38 and s39
    for s in s38 + s39:
        j = idx[s.signal_ts]
        assert s.side == "short" and s.entry == pytest.approx(float(b.c[j]))
        assert s.stop > s.entry > s.target and s.target == pytest.approx(s.entry - 2 * (s.stop - s.entry))
        assert s.indicators["atr14"] > 0 and s.indicators["range_low"] < s.indicators["range_high"]
        assert s.comment.startswith(f"{s.setup} (4h)") and "Вхід" in s.comment and "стоп" in s.comment and "ATR14" in s.comment
        assert "Бектест кроку 1" in s.comment and "плацебо" in s.comment and "не доведена перевага" in s.comment
    for s in s38:
        j, lo = idx[s.signal_ts], s.indicators["range_low"]
        assert b.c[j] < lo and b.h[j] >= lo - 0.25 * s.indicators["atr14"]  # a retest from below that closed under
        assert s.stop == pytest.approx(s.indicators["retest_high"] + 0.5 * s.indicators["atr14"])
        assert "−0.062R" in s.comment and "відкинуто" in s.comment  # the OOS sign is opposite: said plainly
    for s in s39:
        j = idx[s.signal_ts]
        assert b.c[j] < s.indicators["s3_stop"] < s.indicators["range_low"] < s.indicators["range_mid"]
        assert "+0.177R" in s.comment and "даних замало" in s.comment
    assert ss.s38_all(b, "1d") == [] and ss.s39_all(b, "1d") == [] and ss.scan(b, "1d", ("S38", "S39")) == []  # 4h only
    assert ss.s38_all(b.slice(0, 40), "4h") == [] and ss.s39_all(b.slice(0, 40), "4h") == []
    assert {"S38", "S39"} <= set(ss.DETECTORS) and not {"S38", "S39"} & {s.setup for s in ss.scan(b, "4h")}  # not default


# ---------------------------------------------------------------- S40 (the long side of detector B, owner 25.10, Q-20261025-3)


def _s40_reference(b: Bars) -> list[tuple[int, float, float, float]]:
    """(entry bar, entry, stop, target) of rule B for longs in real prices: a live S3 short (a sweep of the range high,
    stop 0.5 ATR over it) closed above its stop within 6 bars before the range middle was reached."""
    m = b.mirrored()
    return [(j, -e, -st, -tg) for j, e, st, tg in _s39_reference(m)]


@pytest.mark.parametrize("seed", [1, 3, 7])
def test_s40_is_the_mirror_of_s39_and_the_step1_long_detector(seed):
    from tradedesk.analytics.short_setups import failed_sweep

    b = _bars(1000, seed)
    for bb in (b, _with_hole(b, 500, 20)):
        sigs = ss.s40_all(bb, "4h")
        got = _rows(bb, sigs)
        want = sorted(_s40_reference(bb))
        assert len(got) == len(want) > 3
        for g, w in zip(got, want, strict=True):
            assert g[0] == w[0] and g[1:] == pytest.approx(w[1:], rel=1e-12)
        pos = segment_pos(bb.t)
        step1 = failed_sweep(bb, pos, atr(bb, 14, pos))  # the long form, as the backtest's B-long rows
        assert got == sorted((x.i, x.entry, x.stop, x.target) for x in step1)
        assert {s.side for s in sigs} == {"long"}


def test_s40_live_scan_on_each_prefix_equals_the_full_history_and_is_causal():
    b = _bars(700, 1)
    full = ss.s40_all(b, "4h")
    live = set()
    for i in range(60, len(b)):
        sigs = ss.scan(b.slice(0, i + 1), "4h", ("S40",))
        assert all(s.setup == "S40" and s.side == "long" and s.signal_ts == int(b.t[i]) for s in sigs)
        live |= {(s.signal_ts, s.entry, s.stop, s.target) for s in sigs}
    assert live == {(s.signal_ts, s.entry, s.stop, s.target) for s in full if s.signal_ts >= int(b.t[60])} and len(live) > 3
    rng = np.random.default_rng(42)
    for s in full:
        t = int(np.searchsorted(b.t, s.signal_ts))
        want = [x for x in full if x.signal_ts <= s.signal_ts]
        assert [x for x in ss.s40_all(_other_future(b, t, rng), "4h") if x.signal_ts <= s.signal_ts] == want, t


def test_s40_levels_comment_and_timeframe():
    b = _bars(1000, 3)
    idx = {int(t): k for k, t in enumerate(b.t)}
    s40 = ss.s40_all(b, "4h")
    assert s40
    for s in s40:
        j = idx[s.signal_ts]
        assert s.setup == "S40" and s.side == "long" and s.entry == pytest.approx(float(b.c[j]))
        assert s.stop < s.entry < s.target and s.target == pytest.approx(s.entry + 2 * (s.entry - s.stop))
        assert b.c[j] > s.indicators["s3_stop"] > s.indicators["range_high"] > s.indicators["range_mid"]
        assert s.comment.startswith("S40 (4h)") and "+0.056R" in s.comment and "плацебо" in s.comment
        assert "не доведена перевага" in s.comment and "Q-20261025-3" in s.comment
    assert ss.s40_all(b, "1d") == [] and ss.s40_all(b.slice(0, 40), "4h") == []
    assert "S40" in ss.DETECTORS and "S40" not in {s.setup for s in ss.scan(b, "4h")}  # not default
