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


def test_range_all_equals_the_live_range_scan_on_each_prefix(live):
    """The passport's full-history S3 / S16 form gives on bar k exactly what the live scan gives on bars[: k + 1]."""
    b, sigs = live
    want = {(s.setup, s.side, s.signal_ts, s.entry, s.stop, s.target, s.comment) for _, s in sigs if s.setup in ("S3", "S16")}
    got = {(s.setup, s.side, s.signal_ts, s.entry, s.stop, s.target, s.comment) for s in ss.range_all(b, "4h", lo=150)}
    assert got == want and {w[0] for w in got} == {"S3", "S16"}
    head = ss.range_all(b.slice(0, 600), "4h", lo=150)  # bars after 600 do not change the earlier signals
    assert {(s.setup, s.side, s.signal_ts) for s in head} == {w[:3] for w in got if w[2] <= int(b.t[599])}


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


def _key(s: ss.LiveSignal) -> tuple:
    return (s.setup, s.side, s.timeframe, s.signal_ts, s.entry, s.stop, s.target, s.comment, tuple(sorted(s.indicators.items())))


@pytest.mark.parametrize("seed", [3, 5, 10])  # seeds with S14 signals on both sides (S14 is rare: 0..6 in 700 bars)
def test_s14_all_equals_the_live_s14_on_each_prefix(seed):
    """The passport's full-history S14 gives on bar k exactly what the live s14 gives on bars[: k + 1] (levels, comment
    and indicators included), from the first bar the live scan can see; both sides are covered."""
    b = _bars(700, seed)
    live = [s for i in range(90, len(b)) for s in ss.s14(b.slice(0, i + 1), "4h")]
    full = ss.s14_all(b, "4h")
    assert sorted(map(_key, full)) == sorted(map(_key, live))
    assert len(full) == len(set(map(_key, full))) and {s.side for s in full} == {"long", "short"}
    assert all(s.setup == "S14" and s.signal_ts >= int(b.t[ss.S14_PARAMS.warmup + 1]) for s in full)
    k = 400  # lo keeps the bars >= lo and the earlier bars still feed the used-impulse memory
    assert sorted(map(_key, ss.s14_all(b, "4h", lo=k))) == sorted(_key(s) for s in full if s.signal_ts >= int(b.t[k]))


@pytest.mark.parametrize("seed", [3, 5, 10])
def test_s14_all_is_causal_a_different_future_after_each_signal_bar_changes_nothing_up_to_it(seed):
    """Cut at every S14 signal bar t (and the bar before it), give the history a different future: the signals up to
    t stay the same, and the live s14 on bars 0..t sees the signal of bar t."""
    b = _bars(700, seed)
    full = ss.s14_all(b, "4h")
    rng = np.random.default_rng(42)
    cuts = sorted({k for s in full for k in (int(np.searchsorted(b.t, s.signal_ts)) - 1, int(np.searchsorted(b.t, s.signal_ts)))})
    assert len(cuts) >= 8
    for t in cuts:
        upto = int(b.t[t])
        want = sorted(_key(s) for s in full if s.signal_ts <= upto)
        assert sorted(_key(s) for s in ss.s14_all(_other_future(b, t, rng), "4h") if s.signal_ts <= upto) == want, t
        assert sorted(map(_key, ss.s14(b.slice(0, t + 1), "4h"))) == [w for w in want if w[3] == upto], t


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


# ---------------------------------------------------------------- S28 (valid order block, long only; rejected by the backtest)


def _ob_bars(*, fractal: bool = True, e_open: float = 100.0, fvg_low: float = 101.5) -> Bars:
    """An analytic order block. Bars 0..110 alternate a bullish (o 100, c 100.5) and a bearish (o 100.5, c 100) candle,
    high/low 0.25 beyond the body: every true range is 1.0, so the Wilder ATR is exactly 1, no candle engulfs the one
    before it (100.5 is not > 100.5) and no high is a strict fractal. Bar 109 (bearish) is lifted by 0.25: a fractal
    high at 101 (true range still 1). Then: bar 111 bearish (o 100.5, c 100, h 100.75); bar 112 bullish, o ``e_open``,
    c 101.9, h 102.15, l 99.75 (engulfs 111; body 1.9 >= ATR 1; true range 2.4, so ATR (13 + 2.4) / 14 = 1.1; BOS: the
    close is above the fractal 101); bar 113 o 101.9, c 102.4, h 102.6, l ``fvg_low`` (101.5 > high of 111 = 100.75: a
    bullish FVG with the middle at 112; true range 1.1)."""
    o = [100.0 if k % 2 == 0 else 100.5 for k in range(111)]
    c = [100.5 if k % 2 == 0 else 100.0 for k in range(111)]
    h = [max(x, y) + 0.25 for x, y in zip(o, c, strict=True)]
    lo = [min(x, y) - 0.25 for x, y in zip(o, c, strict=True)]
    if fractal:
        o[109], c[109], h[109], lo[109] = 100.75, 100.25, 101.0, 100.0
    o += [100.5, e_open, 101.9]
    c += [100.0, 101.9, 102.4]
    h += [100.75, 102.15, 102.6]
    lo += [99.75, 99.75, fvg_low]
    t = 1_700_000_000_000 // H4 * H4 + np.arange(len(o)) * H4
    return Bars(t.astype(np.int64), np.array(o), np.array(h), np.array(lo), np.array(c), H4)


def test_s28_on_an_analytic_order_block():
    b = _ob_bars()
    assert atr(b, 14, segment_pos(b.t))[111] == 1.0  # the base is exact
    sigs = ss.s28_all(b, "4h")
    assert len(sigs) == 1
    s = sigs[0]
    # hand-computed: level = open of the bearish candle 111 = 100.5; ATR14 of the engulfing candle 112 = 1.1;
    # stop 100.5 - 1.1 = 99.4; target 100.5 + 2 * 1.1 = 102.7; the flags complete at the close of bar 113 (the FVG)
    assert (s.setup, s.side, s.timeframe, s.signal_ts) == ("S28", "long", "4h", int(b.t[113]))
    assert s.entry == 100.5 and s.stop == pytest.approx(99.4, abs=1e-12) and s.target == pytest.approx(102.7, abs=1e-12)
    assert s.indicators["atr14"] == pytest.approx(1.1, abs=1e-12) and s.rr == pytest.approx(2.0)
    ind = s.indicators
    assert (ind["ob_low"], ind["ob_high"], ind["ob_body_low"], ind["ob_body_high"]) == (99.75, 100.75, 100.0, 100.5)
    assert ind["ob_ts"] == b.t[111] and ind["bos_ts"] == b.t[112] and ind["rest_until_ms"] == b.close_time(113) + 20 * H4
    assert ss.s28(b, "4h") == sigs and ss.scan(b, "4h", ("S28",)) == sigs
    assert s.comment.startswith("S28 (4h)") and "Лімітка заздалегідь на верху тіла OB 100.5" in s.comment
    assert "стоп 99.4" in s.comment and "ціль 102.7" in s.comment and "на свічці поглинання" in s.comment
    assert "Бектест відкинув" in s.comment and "−0.044R" in s.comment and "Доведеної переваги немає" in s.comment
    # the FVG is not known before the close of bar 113: nothing on the bars up to 112
    assert ss.s28_all(b.slice(0, 113), "4h") == [] and ss.s28(b.slice(0, 113), "4h") == []
    # each flag is required: no fractal high (no BOS), a body of 0.94 < 1 ATR (no impulse), no gap over bar 111's high
    assert ss.s28_all(_ob_bars(fractal=False), "4h") == []
    assert ss.s28_all(_ob_bars(e_open=100.96), "4h") == []
    assert ss.s28_all(_ob_bars(fvg_low=100.7), "4h") == []
    # other timeframes, short history, and the default scan: no S28
    assert ss.s28_all(b, "1d") == [] and ss.s28_all(b.slice(0, 101), "4h") == []
    assert ss.scan(b, "4h") == ss.scan(b, "4h", ("S14", "S16", "S3")) and "S28" not in {x.setup for x in ss.scan(b, "4h")}


def _s28_reference(b: Bars) -> list[tuple[int, float, float, float]]:
    """(arm bar, level, stop, target) as the backtest's primary row B builds its events (long side): the valid OB
    zones, ``ok[arm]`` (warm-up and ATR), first test (level untouched up to the arm), the close not inside the zone."""
    from tradedesk.analytics.course_fvg_ob import bos_up, ob_zones, untouched

    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    out = []
    for z in ob_zones(b, pos, a, bos_up(b, pos)):
        arm = z.arms.get("valid")
        if arm is None or pos[arm] < 100 or not np.isfinite(a[arm]):
            continue
        level = z.levels["body"]
        if not untouched(b, z.form, arm, level) or b.c[arm] < level:
            continue
        out.append((arm, level, level - z.atr, level + 2 * z.atr))
    return sorted(out)


@pytest.mark.parametrize("seed", [1, 5, 7])
def test_s28_equals_the_backtest_pieces(seed):
    b = _bars(1000, seed)
    for bb in (b, _with_hole(b, 500, 20)):  # a hole restarts the segment
        sigs = ss.s28_all(bb, "4h")
        got = _rows(bb, sigs)
        want = _s28_reference(bb)
        assert len(got) == len(want) > 3
        for g, w in zip(got, want, strict=True):
            assert g[0] == w[0] and g[1:] == pytest.approx(w[1:], rel=1e-12)
        assert {s.side for s in sigs} == {"long"} and all(s.rr == pytest.approx(2.0) for s in sigs)


def test_s28_never_looks_ahead():
    """The signal at bar i is the same when the bars after i are removed (the live scan on bars 0..i) or replaced by a
    different future, and the live scan on every prefix finds exactly the full-history signals."""
    b = _bars(700, 1)
    full = ss.s28_all(b, "4h")
    assert len(full) > 3
    rng = np.random.default_rng(7)
    for s in full:
        t = int(np.searchsorted(b.t, s.signal_ts))
        for cut in (t - 1, t):
            upto = int(b.t[cut])
            want = [x for x in full if x.signal_ts <= upto]
            assert ss.s28_all(b.slice(0, cut + 1), "4h") == want, cut
            assert [x for x in ss.s28_all(_other_future(b, cut, rng), "4h") if x.signal_ts <= upto] == want, cut
    live = []
    for i in range(100, len(b)):
        got = ss.scan(b.slice(0, i + 1), "4h", ("S28",))
        assert all(x.setup == "S28" and x.side == "long" and x.signal_ts == int(b.t[i]) for x in got)
        live += got
    assert live == [x for x in full if x.signal_ts >= int(b.t[100])] and live


def test_s28_is_opt_in_only():
    import inspect

    assert inspect.signature(ss.scan).parameters["setups"].default == ("S14", "S16", "S3")
    b = _bars(1000, 1)
    assert ss.s28_all(b, "4h") and "S28" in ss.DETECTORS
    assert "S28" not in {s.setup for s in ss.scan(b, "4h")}
    assert ss.scan(b, "4h", ("S12", "S38", "S39", "S40")) == [
        s for s in ss.scan(b, "4h", ("S12", "S28", "S38", "S39", "S40")) if s.setup != "S28"
    ]


# ---------------------------------------------------------------- S17 (retest of an impulsively broken POB; rejected)

D1_MS = ss.TF_MS["1d"]
H1 = 3_600_000


def _reflect(b: Bars, m: float = 200.0) -> Bars:
    """Prices m - x with positive values: every long pattern becomes the same short pattern."""
    return Bars(b.t, m - b.o, m - b.low, m - b.h, m - b.c, b.bar_ms)


def _pob_bars(*, low_q: float = 89.0, o_k: float = 95.0, bar_ms: int = H4) -> Bars:
    """An analytic POB. Bars 0..109 alternate o 100 / c 100.5 and o 100.5 / c 100 with wicks of 0.25: every true range
    is 1, so the Wilder ATR is exactly 1. Bar 110 (the POB): o 100, c 101, h 101.25, l 99.75 (true range 1.5). Bar 111
    (the impulse): o 101, c low_q + 1, h 101.1, l ``low_q`` (a drop of 101 - 89 = 12 >= 10 % of 101 and >= 3 ATR).
    Bar 112: o low_q + 1, c 95, h 95.5, l low_q + 0.5. Bar 113 (the break): o ``o_k``, c 102 > 101.25, h 102.5,
    l o_k - 0.2 (impulsive: it opened at 95 <= the POB's low 99.75). Then five quiet bars near 102."""
    o = [100.0 if k % 2 == 0 else 100.5 for k in range(110)]
    c = [100.5 if k % 2 == 0 else 100.0 for k in range(110)]
    h = [max(x, y) + 0.25 for x, y in zip(o, c, strict=True)]
    lo = [min(x, y) - 0.25 for x, y in zip(o, c, strict=True)]
    o += [100.0, 101.0, low_q + 1, o_k]
    c += [101.0, low_q + 1, 95.0, 102.0]
    h += [101.25, 101.1, 95.5, 102.5]
    lo += [99.75, low_q, low_q + 0.5, o_k - 0.2]
    for k in range(5):
        o.append(102.0 if k % 2 == 0 else 102.3)
        c.append(102.3 if k % 2 == 0 else 102.0)
        h.append(102.5)
        lo.append(101.8)
    t = 1_700_000_000_000 // D1_MS * D1_MS + np.arange(len(o)) * bar_ms
    return Bars(t.astype(np.int64), np.array(o), np.array(h), np.array(lo), np.array(c), bar_ms)


def test_s17_on_an_analytic_pob():
    b = _pob_bars()
    a = 1.0
    for tr in (1.5, 12.1, 6.0, 7.7):  # true ranges of bars 110..113 (by hand, previous closes 100, 101, 90, 95)
        a = (13 * a + tr) / 14
    assert atr(b, 14, segment_pos(b.t))[113] == pytest.approx(a, abs=1e-12)
    # level = high of the POB 101.25; stop min(99.75, 101.25 - 0.5 a) - 0.1 a = 99.75 - 0.1 a (a = 2.5224 > 0.5 needed)
    stop = 99.75 - 0.1 * a
    target = 101.25 + 2 * (101.25 - stop)
    sigs = ss.s17_all(b, "4h")
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.setup, s.side, s.timeframe, s.signal_ts) == ("S17", "long", "4h", int(b.t[113]))
    assert s.entry == 101.25 and s.stop == pytest.approx(stop, abs=1e-12) and s.target == pytest.approx(target, abs=1e-12)
    assert s.rr == pytest.approx(2.0)
    ind = s.indicators
    assert (ind["pob_low"], ind["pob_high"], ind["pob_ts"], ind["impulse_ts"]) == (99.75, 101.25, b.t[110], b.t[111])
    assert ind["impulse_move"] == pytest.approx(12 / 101) and ind["atr14"] == pytest.approx(a, abs=1e-12)
    assert ind["rest_until_ms"] == b.close_time(113) + 90 * D1_MS
    assert s.comment.startswith("S17 (4h)") and "Лімітка заздалегідь на хаї POB 101.25" in s.comment
    assert "падінням на 11.9%" in s.comment and "відкрилась по той бік POB" in s.comment and "живе 90 діб" in s.comment
    assert "Бектест відкинув" in s.comment and "+0.036R" in s.comment and "Доведеної переваги немає" in s.comment
    assert ss.s17(b.slice(0, 114), "4h") == sigs and ss.scan(b.slice(0, 114), "4h", ("S17",)) == sigs
    assert ss.s17(b, "4h") == []  # decided on bar 113, not on the last bar
    # the break is not known before the close of bar 113
    assert ss.s17_all(b.slice(0, 113), "4h") == []
    # each rule is required: a drop of 10 < 10.1 (10 % of 101); a break that opened inside the POB with a body of 1 < ATR
    assert ss.s17_all(_pob_bars(low_q=91.0), "4h") == []
    assert ss.s17_all(_pob_bars(o_k=101.0), "4h") == []
    # the short is the same POB on reflected prices: a limit at its low, the stop above it
    r = ss.s17_all(_reflect(b), "4h")
    assert [(x.side, x.signal_ts) for x in r] == [("short", int(b.t[113]))]
    assert r[0].entry == pytest.approx(98.75) and r[0].stop == pytest.approx(200 - stop) and r[0].target == pytest.approx(200 - target)
    assert "Лімітка заздалегідь на лоу POB 98.75" in r[0].comment and "ростом" in r[0].comment
    # 1d: the same rules, the 1D numbers of the backtest; other timeframes and a short history give nothing
    d = ss.s17_all(_pob_bars(bar_ms=D1_MS), "1d")
    assert len(d) == 1 and "−0.007R" in d[0].comment and d[0].comment.startswith("S17 (1d)")
    assert ss.s17_all(_pob_bars(bar_ms=H1), "1h") == [] and ss.s17_all(b.slice(0, 101), "4h") == []


def _h1_walk(n: int, seed: int) -> Bars:
    """1h bars: a regime-switching walk with rare 12 % one-hour shocks (so that POBs and their breaks happen)."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.0008, 0.0, 0.0, 0.0008], size=n // 240 + 1), 240)[:n]
    r = drift + rng.normal(0, 0.005, n)
    hit = rng.random(n) < 0.004
    r[hit] += rng.choice([-0.12, 0.12], size=int(hit.sum()))
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.002, n)))
    t = 1_600_000_000_000 // D1_MS * D1_MS + np.arange(n) * H1
    return Bars(t.astype(np.int64), o, h, low, c, H1)


def _s17_backtest_rows(h1: Bars, hrs: int) -> tuple[Bars, list[tuple[int, int, float, float, float]]]:
    """The bars of scripts/verify_course_step5_s17.py (1h -> ``resample_partial`` inside 1h segments) and its events
    of the main variant ``Var(tf)``, the loop of ``run_symbol`` copied (without the universe and date filters):
    (side, break bar, level, stop, target) in real prices."""
    from tradedesk.analytics.course_pob import pob_events, pob_stop
    from tradedesk.analytics.course_s18_trend import pos_from_seg, resample_partial, segments

    bt, segt = resample_partial(h1, segments(h1.t), hrs, hrs)
    post = pos_from_seg(segt)
    at = atr(bt, 14, post)
    ok = (post >= 100) & np.isfinite(at)
    out = []
    for side in (1, -1):
        B = bt if side == 1 else bt.mirrored()
        for e in pob_events(B, post, at, drop=0.10, look=5, atr_mult=3.0, break_bars=180 * 24 // hrs, imp_atr=1.0, warmup=100):
            if e.k < 0 or not e.impulsive:
                continue
            arm, level = e.k, e.levels["wick"]
            if arm >= len(B) or not ok[arm] or post[e.k] < 100:
                continue
            A = float(at[e.k])
            stop = pob_stop(level, e.low, A, "pob")
            if (float(B.c[arm]) - level) / A < 0:
                continue
            out.append((side, arm, side * level, side * stop, side * (level + 2.0 * (level - stop))))
    return bt, sorted(out)


@pytest.mark.parametrize(("tf", "hrs", "seed"), [("4h", 4, 1), ("4h", 4, 2), ("1d", 24, 3)])
def test_s17_equals_the_backtest_events(tf, hrs, seed):
    """On the backtest's own bars (1h resampled, with a 3-day hole that restarts the segment) the live detector gives
    exactly the backtest's main-variant events: same break bars, sides, levels, stops and targets."""
    n_h = 1000 * hrs
    h1 = _h1_walk(n_h, seed)
    h1 = _with_hole(h1, n_h // 2, 72)
    bt, want = _s17_backtest_rows(h1, hrs)
    sigs = ss.s17_all(bt, tf)
    idx = {int(t): k for k, t in enumerate(bt.t)}
    got = sorted((1 if s.side == "long" else -1, idx[s.signal_ts], s.entry, s.stop, s.target) for s in sigs)
    assert len(got) == len(want) > 3 and {g[0] for g in got} == {1, -1}
    for g, w in zip(got, want, strict=True):
        assert g[:2] == w[:2] and g[2:] == pytest.approx(w[2:], rel=1e-12)
    for s in sigs:
        k = idx[s.signal_ts]
        sign = 1 if s.side == "long" else -1
        assert s.rr == pytest.approx(2.0) and sign * (s.entry - s.stop) > 0 and sign * (float(bt.c[k]) - s.entry) > 0
        assert s.entry == (s.indicators["pob_high"] if s.side == "long" else s.indicators["pob_low"])
        assert s.indicators["rest_until_ms"] == bt.close_time(k) + 90 * D1_MS and s.indicators["impulse_move"] >= 0.10


def test_s17_never_looks_ahead():
    """The signals up to bar t are the same when the bars after t are removed or replaced by a different future, and
    the live scan on every prefix finds exactly the full-history signals."""
    b, _ = _s17_backtest_rows(_h1_walk(4 * 700, 4), 4)
    full = ss.s17_all(b, "4h")
    assert len(full) > 3
    rng = np.random.default_rng(11)
    for s in full:
        t = int(np.searchsorted(b.t, s.signal_ts))
        for cut in (t - 1, t):
            upto = int(b.t[cut])
            want = [x for x in full if x.signal_ts <= upto]
            assert ss.s17_all(b.slice(0, cut + 1), "4h") == want, cut
            assert [x for x in ss.s17_all(_other_future(b, cut, rng), "4h") if x.signal_ts <= upto] == want, cut
    live = []
    for i in range(100, len(b)):
        got = ss.scan(b.slice(0, i + 1), "4h", ("S17",))
        assert all(x.setup == "S17" and x.signal_ts == int(b.t[i]) for x in got)
        live += got
    assert live == [x for x in full if x.signal_ts >= int(b.t[100])] and live


# ---------------------------------------------------------------- S23 (SFP retest, a QM proxy without BOS; negative)


def _sfp_window(*, o: float = 100.2, h: float = 100.5, low: float = 99.0, c: float = 100.4) -> Bars:
    """An analytic sweep. Bars 0..298 alternate o 100 / c 104 and o 104 / c 100, all with high 104.25 and low 99.75:
    one range of width 5.25 / 101.625 = 5.2 %, every inner bar a swing low at 99.75 and no candle with a long wick.
    Bar 299 (the default): o 100.2, c 100.4, h 100.5, l 99.0 — it pierces the swing low 99.75 by 0.75 with a lower wick
    of 1.2 of its range 1.5 (0.8 >= 0.5) and closes back above it."""
    o_ = [100.0 if k % 2 == 0 else 104.0 for k in range(299)] + [o]
    c_ = [104.0 if k % 2 == 0 else 100.0 for k in range(299)] + [c]
    h_ = [104.25] * 299 + [h]
    l_ = [99.75] * 299 + [low]
    t = 1_700_000_000_000 // H4 * H4 + np.arange(300) * H4
    return Bars(t.astype(np.int64), np.array(o_), np.array(h_), np.array(l_), np.array(c_), H4)


def test_s23_on_an_analytic_sweep():
    b = _sfp_window()
    sigs = ss.s23_all(b, "4h")
    assert len(sigs) == 1
    s = sigs[0]
    # by hand: the range is 99.0 .. 104.25 (the sweep low included), middle 101.625; the M2 stop is the level minus twice
    # the overshoot, 99.75 - 2 * 0.75 = 98.25 (lower than the wick - 0.1 % = 98.901); the limit rests at 99.75
    assert (s.setup, s.side, s.timeframe, s.signal_ts) == ("S23", "long", "4h", int(b.t[299]))
    assert s.entry == 99.75 and s.stop == pytest.approx(98.25, abs=1e-9) and s.target == pytest.approx(101.625, abs=1e-12)
    assert s.rr == pytest.approx(1.875 / 1.5)
    ind = s.indicators
    assert (ind["swept_level"], ind["sweep_extreme"], ind["range_low"], ind["range_high"]) == (99.75, 99.0, 99.0, 104.25)
    assert ind["range_mid"] == s.target and ind["rest_until_ms"] == b.close_time(299) + 4 * H1
    assert ind["max_hold_ms"] == 10 * D1_MS
    assert s.comment.startswith(
        "S23 (4h): SFP-ретест (проксі QM, без BOS) — свічка зняла свінг-лоу 99.75 до 99 і закрилась назад вище, на 100.4"
    )
    assert "BOS і FVG автора не моделюються" in s.comment and "утримання до 10 діб від входу" in s.comment
    assert "Лімітка на знятому рівні 99.75" in s.comment and "16 свічок 15m (4 год)" in s.comment
    assert "скасувати, якщо ціна раніше дійде до цілі" in s.comment and "(300 св., ширина 5.2 % ≤ 15 %)" in s.comment
    assert "значуще гірший за нуль" in s.comment and "−0.142R" in s.comment and "Доведеної переваги немає" in s.comment
    assert ss.s23(b, "4h") == sigs and ss.scan(b, "4h", ("S23",)) == sigs
    # each rule is required: a close below the swing low (no sweep), a short wick (0.3 of 1.5 < 0.5), a close above the
    # range middle (the backtest takes no trade), one bar too few for the 300-bar window, a timeframe other than 4h
    assert ss.s23_all(_sfp_window(c=99.6, o=99.7), "4h") == []
    assert ss.s23_all(_sfp_window(o=99.3), "4h") == []
    assert ss.s23_all(_sfp_window(o=101.9, c=102.0, h=102.1), "4h") == []
    assert ss.s23_all(b.slice(1, 300), "4h") == [] and ss.s23_all(b, "1d") == []
    # the short: the same sweep of the swing high on reflected prices
    r = ss.s23_all(_reflect(b), "4h")
    assert [(x.side, x.signal_ts) for x in r] == [("short", int(b.t[299]))]
    assert r[0].entry == pytest.approx(100.25) and r[0].stop == pytest.approx(101.75) and r[0].target == pytest.approx(98.375)
    assert "зняла свінг-хай 100.25 до 101 і закрилась назад нижче" in r[0].comment


def _sfp_bars(n: int = 700, seed: int = 1) -> Bars:
    """4h bars with quiet ranges and long wicks (exponential), so that sweeps inside ranges happen."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-0.003, 0.0, 0.0, 0.003], size=n // 60 + 1), 60)[:n]
    c = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.006, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + rng.exponential(0.01, n))
    low = np.minimum(o, c) * (1 - rng.exponential(0.01, n))
    t = 1_700_000_000_000 // H4 * H4 + np.arange(n) * H4
    return Bars(t.astype(np.int64), o, h, low, c, H4)


@pytest.mark.parametrize("seed", [1, 2])
def test_s23_equals_the_walk_forward_backtest(seed):
    """``walk_forward_sfp`` with the 4h parameters of scripts/verify_sfp_ltf.py: without its one-position skip it finds
    exactly the live detector's sweeps (the backtest cannot evaluate the last bar: no bars after it); with the skip
    (the backtest's sample) it is a subset. Level, stop and target are the backtest's swept level, M2 stop and TP1."""
    from tradedesk.analytics.sfp_backtest import walk_forward_sfp

    b = _sfp_bars(700, seed)
    rows = ss._ohlc_rows(b, 0, len(b))
    params = {"window": 300, "max_hold_bars": 60, "max_width_pct": 15.0, "fee_pct": 0.1, "sweep_wick_ratio": 0.5}
    every = walk_forward_sfp(rows, **params, one_position=False)
    taken = walk_forward_sfp(rows, **params)
    ref = {(x["idx"], x["direction"]): x for x in every}
    sigs = ss.s23_all(b, "4h")
    idx = {int(t): k for k, t in enumerate(b.t)}
    got = {(idx[s.signal_ts], s.side): s for s in sigs if idx[s.signal_ts] < len(b) - 1}
    assert set(got) == set(ref) and len(got) > 10 and {k[1] for k in got} == {"long", "short"}
    for key, s in got.items():
        v = ref[key]["variants"]["m2_2x"]
        assert (s.entry, s.stop, s.target) == (ref[key]["swept_level"], v["stop"], v["tp1"])
    assert {(x["idx"], x["direction"]) for x in taken} < set(ref)


def test_s23_never_looks_ahead():
    """The signal on bar t is the same when the bars after t are removed (the live scan on bars 0..t) or replaced by a
    different future, and the live scan on every prefix finds exactly the full-history signals."""
    b = _sfp_bars(650, 2)
    full = ss.s23_all(b, "4h")
    assert len(full) > 10
    rng = np.random.default_rng(5)
    for s in full:
        t = int(np.searchsorted(b.t, s.signal_ts))
        for cut in (t - 1, t):
            upto = int(b.t[cut])
            want = [x for x in full if upto - 5 * H4 <= x.signal_ts <= upto]
            assert ss.s23_all(b.slice(0, cut + 1), "4h", lo=cut - 5) == want, cut
            assert [x for x in ss.s23_all(_other_future(b, cut, rng), "4h", lo=cut - 5) if x.signal_ts <= upto] == want, cut
    live = []
    for i in range(299, len(b)):
        got = ss.scan(b.slice(0, i + 1), "4h", ("S23",))
        assert all(x.setup == "S23" and x.signal_ts == int(b.t[i]) for x in got)
        live += got
    assert live == full


def test_s17_s23_are_opt_in_only():
    import inspect

    assert inspect.signature(ss.scan).parameters["setups"].default == ("S14", "S16", "S3")
    b17, _ = _s17_backtest_rows(_h1_walk(4 * 700, 4), 4)
    b23 = _sfp_bars(400, 1)
    for b in (b17, b23):
        assert "S17" in ss.DETECTORS and "S23" in ss.DETECTORS
        assert not {"S17", "S23"} & {s.setup for s in ss.scan(b, "4h")}
        others = ("S12", "S28", "S38", "S39", "S40")
        assert ss.scan(b, "4h", others) == [s for s in ss.scan(b, "4h", (*others, "S17", "S23")) if s.setup not in {"S17", "S23"}]
    assert ss.s17_all(b17, "4h") and ss.s23_all(b23, "4h")
    # the opt-in scan on a prefix ending at a signal bar returns that signal
    s = ss.s17_all(b17, "4h")[0]
    k = int(np.searchsorted(b17.t, s.signal_ts))
    assert s in ss.scan(b17.slice(0, k + 1), "4h", ("S17",))
